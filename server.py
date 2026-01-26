import os, json, logging, asyncio
from typing import Dict, Any, Optional, List, Set
from fastapi import FastAPI
from pydantic import BaseModel
import httpx

# --- 配置加载 ---
GITHUB_API = "https://api.github.com"
GITHUB_GRAPHQL = "https://api.github.com/graphql"

# 权限分离
BOT_TOKEN = os.getenv("BOT_TOKEN")          # 哨兵：负责轮询/标记已读
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")    # 主力：负责抓取详情/触发Workflow

CONTROL_REPO = os.getenv("CONTROL_REPO")
ALLOWED_USERS = [u.strip() for u in os.getenv("ALLOWED_USERS", "").split(",") if u.strip()]
PROCESSED_LOG = "/data/processed_notifications.log"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("BotWatcher")

# 认证组
bot_headers = {"Authorization": f"token {BOT_TOKEN}", "Accept": "application/vnd.github.v3+json"}
user_rest_headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
user_graphql_headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

app = FastAPI()
processed_cache: Set[str] = set()
state = {"last_modified": None, "poll_interval": 60}

# --- 缓存持久化 ---
def load_processed_log():
    if os.path.exists(PROCESSED_LOG):
        with open(PROCESSED_LOG, "r") as f:
            for line in f: processed_cache.add(line.strip())
    logger.info(f"Loaded {len(processed_cache)} IDs.")

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

# --- 上下文抓取逻辑 (全场景) ---
async def fetch_discussion_by_node(client: httpx.AsyncClient, node_id: str):
    query = "query($id:ID!){node(id:$id){...on Discussion{body number author{login}url}}}"
    # 使用个人 TOKEN 抓取详情
    resp = await client.post(GITHUB_GRAPHQL, json={"query": query, "variables": {"id": node_id}}, headers=user_graphql_headers)
    return resp.json().get("data", {}).get("node") if resp.status_code == 200 else None

async def handle_note(client: httpx.AsyncClient, note: Dict):
    repo_full = note["repository"]["full_name"]
    subject = note["subject"]
    note_id = note["id"]
    context = TaskContext(repo=repo_full, event_type=subject["type"].lower(), event_id=note_id, latest_comment_url=subject.get("latest_comment_url"))

    try:
        # A. Discussion 场景
        if subject["type"] == "Discussion":
            # 使用个人 TOKEN 换取 Node ID
            thread_resp = await client.get(note["url"], headers=user_rest_headers)
            node_id = thread_resp.json().get("subject", {}).get("node_id")
            if node_id:
                data = await fetch_discussion_by_node(client, node_id)
                if data:
                    context.issue_body, context.issue_number = data["body"][:3000], data["number"]
                    context.trigger_user = data["author"]["login"]
                    if context.latest_comment_url: # 追溯真正的艾特者
                        lc = await client.get(context.latest_comment_url, headers=user_rest_headers)
                        if lc.status_code == 200: context.trigger_user = lc.json().get("author", {}).get("login") or context.trigger_user
                    context.clone_url = note["repository"]["html_url"] + ".git"

        # B. Issue / PR 场景
        elif subject["type"] in ["Issue", "PullRequest"]:
            detail_resp = await client.get(subject["url"], headers=user_rest_headers)
            if detail_resp.status_code == 200:
                detail = detail_resp.json()
                context.issue_number, context.trigger_user = detail.get("number"), detail.get("user", {}).get("login")
                context.issue_body = (detail.get("body") or "")[:3000]
                if context.latest_comment_url:
                    lc = await client.get(context.latest_comment_url, headers=user_rest_headers)
                    if lc.status_code == 200: context.trigger_user = lc.json().get("user", {}).get("login") or context.trigger_user
                
                if subject["type"] == "PullRequest":
                    context.clone_url, context.head_ref, context.base_ref = detail.get("head", {}).get("repo", {}).get("clone_url"), detail.get("head", {}).get("ref"), detail.get("base", {}).get("ref")
                else:
                    context.clone_url = note["repository"]["html_url"] + ".git"

        # C. Commit 场景
        elif subject["type"] == "Commit":
            context.clone_url, context.commit_sha = note["repository"]["html_url"] + ".git", subject["url"].split("/")[-1]
            comm_resp = await client.get(f"{subject['url']}/comments", headers=user_rest_headers)
            if comm_resp.status_code == 200 and comm_resp.json():
                context.trigger_user = comm_resp.json()[-1]["user"]["login"]

        if ALLOWED_USERS and context.trigger_user not in ALLOWED_USERS: return
        await trigger_workflow(context)

    except Exception as e: logger.error(f"Handle Error: {e}")

async def trigger_workflow(ctx: TaskContext):
    payload_str = ctx.model_dump_json()
    if len(payload_str) > 60000:
        ctx.issue_body = ctx.issue_body[:500] + "..."
        payload_str = ctx.model_dump_json()

    url = f"{GITHUB_API}/repos/{CONTROL_REPO}/actions/workflows/llm-bot-runner.yml/dispatches"
    async with httpx.AsyncClient() as client:
        # 使用高权个人 TOKEN 触发
        r = await client.post(url, headers=user_rest_headers, json={"ref": "main", "inputs": {"task": "AI_TASK", "context": payload_str}})
        if r.status_code == 204:
            # 使用 BOT TOKEN 标记已读 (Inbox 归属者)
            await client.patch(f"{GITHUB_API}/notifications/threads/{ctx.event_id}", headers=bot_headers)
            mark_processed_disk(ctx.event_id)
            logger.info(f"Workflow dispatched and marked as read: {ctx.event_id}")

async def poll_loop():
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            try:
                curr_headers = bot_headers.copy()
                if state["last_modified"]: curr_headers["If-Modified-Since"] = state["last_modified"]
                resp = await client.get(f"{GITHUB_API}/notifications", headers=curr_headers, params={"all": "false"})
                state["poll_interval"] = int(resp.headers.get("X-Poll-Interval", 60))
                if resp.status_code == 200:
                    state["last_modified"] = resp.headers.get("Last-Modified")
                    for note in resp.json():
                        if note["reason"] in ["mention", "team_mention"] and note["id"] not in processed_cache:
                            processed_cache.add(note["id"]) # 异步前立即锁定
                            asyncio.create_task(handle_note(client, note))
            except Exception as e: logger.error(f"Poll Error: {e}")
            await asyncio.sleep(state["poll_interval"])

@app.on_event("startup")
async def startup():
    load_processed_log()
    asyncio.create_task(poll_loop())