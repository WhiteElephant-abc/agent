import os, json, logging, asyncio
from typing import Dict, Any, Optional, List, Set
from fastapi import FastAPI
from pydantic import BaseModel
import httpx

# --- 配置 (从环境变量读取) ---
GITHUB_API = "https://api.github.com"
GITHUB_GRAPHQL = "https://api.github.com/graphql"

BOT_TOKEN = os.getenv("BOT_TOKEN")          
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")    
CONTROL_REPO = os.getenv("CONTROL_REPO")
# 预处理白名单：转小写，去空格
ALLOWED_USERS = [u.strip().lower() for u in os.getenv("ALLOWED_USERS", "").split(",") if u.strip()]
PROCESSED_LOG = "/data/processed_notifications.log"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("BotWatcher")

# 通用 Header
bot_headers = {"Authorization": f"token {BOT_TOKEN}", "Accept": "application/vnd.github.v3+json"}
user_rest_headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
user_graphql_headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

app = FastAPI()
processed_cache: Set[str] = set()
state = {"etag": None, "poll_interval": 60}

def load_processed_log():
    if os.path.exists(PROCESSED_LOG):
        with open(PROCESSED_LOG, "r") as f:
            for line in f: processed_cache.add(line.strip())
    logger.info(f"Loaded {len(processed_cache)} processed notification IDs.")

def mark_processed_disk(note_id: str):
    with open(PROCESSED_LOG, "a") as f: f.write(f"{note_id}\n")

class TaskContext(BaseModel):
    repo: str
    event_type: str
    event_id: str
    trigger_user: Optional[str] = None
    issue_number: Optional[int] = None
    commit_sha: Optional[str] = None
    issue_body: Optional[str] = None
    clone_url: Optional[str] = None
    head_ref: Optional[str] = None
    base_ref: Optional[str] = None
    latest_comment_url: Optional[str] = None

async def fetch_discussion_by_node(client: httpx.AsyncClient, node_id: str):
    query = "query($id:ID!){node(id:$id){...on Discussion{body number author{login}}}}"
    resp = await client.post(GITHUB_GRAPHQL, json={"query": query, "variables": {"id": node_id}}, headers=user_graphql_headers)
    return resp.json().get("data", {}).get("node") if resp.status_code == 200 else None

async def handle_note(client: httpx.AsyncClient, note: Dict):
    repo_full = note["repository"]["full_name"]
    subject = note["subject"]
    note_id = note["id"]
    context = TaskContext(repo=repo_full, event_type=subject["type"].lower(), event_id=note_id, latest_comment_url=subject.get("latest_comment_url"))
    
    task_description = "New Task"
    logger.info(f"==> Processing Notification {note_id} ({subject['type']})")

    try:
        # 1. Discussion
        if subject["type"] == "Discussion":
            thread_resp = await client.get(note["url"], headers=user_rest_headers)
            node_id = thread_resp.json().get("subject", {}).get("node_id")
            if node_id:
                data = await fetch_discussion_by_node(client, node_id)
                if data:
                    context.issue_body, context.trigger_user, context.issue_number = data["body"][:3000], data["author"]["login"], data["number"]
                    task_description = data["body"]
                    if context.latest_comment_url:
                        lc = await client.get(context.latest_comment_url, headers=user_rest_headers)
                        if lc.status_code == 200:
                            lc_data = lc.json()
                            context.trigger_user = lc_data.get("author", {}).get("login") or context.trigger_user
                            task_description = lc_data.get("body")
                    context.clone_url = note["repository"]["html_url"] + ".git"

        # 2. Issue / PR
        elif subject["type"] in ["Issue", "PullRequest"]:
            detail_resp = await client.get(subject["url"], headers=user_rest_headers)
            if detail_resp.status_code == 200:
                detail = detail_resp.json()
                context.issue_number = detail.get("number")
                context.trigger_user = detail.get("user", {}).get("login")
                context.issue_body = (detail.get("body") or "")[:3000]
                task_description = context.issue_body
                
                if context.latest_comment_url:
                    lc = await client.get(context.latest_comment_url, headers=user_rest_headers)
                    if lc.status_code == 200:
                        lc_data = lc.json()
                        context.trigger_user = lc_data.get("user", {}).get("login") or context.trigger_user
                        task_description = lc_data.get("body")

                if subject["type"] == "PullRequest":
                    context.clone_url = detail.get("head", {}).get("repo", {}).get("clone_url")
                    context.head_ref, context.base_ref = detail.get("head", {}).get("ref"), detail.get("base", {}).get("ref")
                else:
                    context.clone_url = note["repository"]["html_url"] + ".git"

        # 3. Commit
        elif subject["type"] == "Commit":
            context.clone_url, context.commit_sha = note["repository"]["html_url"] + ".git", subject["url"].split("/")[-1]
            comm_resp = await client.get(f"{subject['url']}/comments", headers=user_rest_headers)
            if comm_resp.status_code == 200 and comm_resp.json():
                last_comm = comm_resp.json()[-1]
                context.trigger_user = last_comm["user"]["login"]
                task_description = last_comm["body"]

        # --- 权限校验 (大小写不敏感) ---
        if not context.trigger_user:
            logger.error(f"Failed to identify trigger user for {note_id}")
            return

        user_low = context.trigger_user.lower()
        if ALLOWED_USERS and user_low not in ALLOWED_USERS:
            logger.warning(f"Access Denied: User '{context.trigger_user}' not in {ALLOWED_USERS}")
            return
            
        logger.info(f"Access Granted: {context.trigger_user}. Triggering Workflow...")
        await trigger_workflow(context, task_description)

    except Exception as e:
        logger.error(f"Handle Error in handle_note: {e}", exc_info=True)

async def trigger_workflow(ctx: TaskContext, task_text: str):
    payload_str = ctx.model_dump_json()
    if len(payload_str) > 60000:
        ctx.issue_body = ctx.issue_body[:500] + "..."
        payload_str = ctx.model_dump_json()

    url = f"{GITHUB_API}/repos/{CONTROL_REPO}/actions/workflows/llm-bot-runner.yml/dispatches"
    async with httpx.AsyncClient() as client:
        r = await client.post(url, headers=user_rest_headers, json={
            "ref": "main", 
            "inputs": {"task": task_text[:2000], "context": payload_str}
        })
        if r.status_code == 204:
            # 只有成功后才标记已读
            await client.patch(f"{GITHUB_API}/notifications/threads/{ctx.event_id}", headers=bot_headers)
            mark_processed_disk(ctx.event_id)
            logger.info(f"SUCCESS: Workflow triggered for {ctx.event_id}")
        else:
            logger.error(f"FAILED to trigger workflow: {r.status_code} - {r.text}")

async def poll_loop():
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            try:
                curr_headers = bot_headers.copy()
                if state["etag"]: curr_headers["If-None-Match"] = state["etag"]
                
                resp = await client.get(f"{GITHUB_API}/notifications", headers=curr_headers, params={"all": "false"})
                state["poll_interval"] = int(resp.headers.get("X-Poll-Interval", 60))
                
                if resp.status_code == 200:
                    state["etag"] = resp.headers.get("ETag")
                    notes = resp.json()
                    for note in notes:
                        # 移除 reason 过滤，只要没处理过就处理
                        if note["id"] not in processed_cache:
                            processed_cache.add(note["id"])
                            asyncio.create_task(handle_note(client, note))
                elif resp.status_code == 304:
                    pass # 正常缓存
                elif resp.status_code == 401:
                    logger.error("Token 401! Please check your BOT_TOKEN.")
            except Exception as e:
                logger.error(f"Poll Error: {e}")
            await asyncio.sleep(state["poll_interval"])

@app.on_event("startup")
async def startup():
    load_processed_log()
    asyncio.create_task(poll_loop())

@app.get("/health")
async def health(): return {"status": "ok", "cached_notes": len(processed_cache)}