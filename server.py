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
    resp = await client.post(GITHUB_GRAPHQL, json={"query": query, "variables": {"id": node_id}}, headers=user_graphql_headers)
    return resp.json().get("data", {}).get("node") if resp.status_code == 200 else None

async def handle_note(client: httpx.AsyncClient, note: Dict):
    repo_full = note["repository"]["full_name"]
    subject = note["subject"]
    note_id = note["id"]
    context = TaskContext(repo=repo_full, event_type=subject["type"].lower(), event_id=note_id, latest_comment_url=subject.get("latest_comment_url"))
    
    # 【新增】用于存放最终提取出的任务指令
    target_task = ""

    try:
        # A. Discussion 场景
        if subject["type"] == "Discussion":
            thread_resp = await client.get(note["url"], headers=user_rest_headers)
            node_id = thread_resp.json().get("subject", {}).get("node_id")
            if node_id:
                data = await fetch_discussion_by_node(client, node_id)
                if data:
                    context.issue_body, context.issue_number = data["body"][:3000], data["number"]
                    context.trigger_user = data["author"]["login"]
                    target_task = data["body"] # 默认任务是正文
                    if context.latest_comment_url:
                        lc = await client.get(context.latest_comment_url, headers=user_rest_headers)
                        if lc.status_code == 200:
                            lc_json = lc.json()
                            context.trigger_user = lc_json.get("author", {}).get("login") or context.trigger_user
                            target_task = lc_json.get("body") # 更新为特定评论内容
                    context.clone_url = note["repository"]["html_url"] + ".git"

        # B. Issue / PR 场景
        elif subject["type"] in ["Issue", "PullRequest"]:
            detail_resp = await client.get(subject["url"], headers=user_rest_headers)
            if detail_resp.status_code == 200:
                detail = detail_resp.json()
                context.issue_number, context.trigger_user = detail.get("number"), detail.get("user", {}).get("login")
                context.issue_body = (detail.get("body") or "")[:3000]
                target_task = context.issue_body # 默认任务是正文
                if context.latest_comment_url:
                    lc = await client.get(context.latest_comment_url, headers=user_rest_headers)
                    if lc.status_code == 200:
                        lc_json = lc.json()
                        context.trigger_user = lc_json.get("user", {}).get("login") or context.trigger_user
                        target_task = lc_json.get("body") # 更新为特定评论内容
                
                if subject["type"] == "PullRequest":
                    context.clone_url, context.head_ref, context.base_ref = detail.get("head", {}).get("repo", {}).get("clone_url"), detail.get("head", {}).get("ref"), detail.get("base", {}).get("ref")
                else:
                    context.clone_url = note["repository"]["html_url"] + ".git"

        # C. Commit 场景
        elif subject["type"] == "Commit":
            context.clone_url, context.commit_sha = note["repository"]["html_url"] + ".git", subject["url"].split("/")[-1]
            comm_resp = await client.get(f"{subject['url']}/comments", headers=user_rest_headers)
            if comm_resp.status_code == 200 and comm_resp.json():
                last_comment = comm_resp.json()[-1]
                context.trigger_user = last_comment["user"]["login"]
                target_task = last_comment["body"]

        if ALLOWED_USERS and context.trigger_user not in ALLOWED_USERS: return
        # 【修改】将提取出的 target_task 传给触发函数
        await trigger_workflow(context, target_task)

    except Exception as e: logger.error(f"Handle Error: {e}")

async def trigger_workflow(ctx: TaskContext, task_content: str):
    payload_str = ctx.model_dump_json()
    if len(payload_str) > 60000:
        ctx.issue_body = ctx.issue_body[:500] + "..."
        payload_str = ctx.model_dump_json()

    # 兜底：如果没提取到内容，使用默认值
    final_task = task_content if task_content else "New AI Task"

    url = f"{GITHUB_API}/repos/{CONTROL_REPO}/actions/workflows/llm-bot-runner.yml/dispatches"
    async with httpx.AsyncClient() as client:
        # 【核心修正】inputs.task 现在使用动态提取的内容
        r = await client.post(url, headers=user_rest_headers, json={
            "ref": "main", 
            "inputs": {"task": final_task[:1000], "context": payload_str}
        })
        if r.status_code == 204:
            await client.patch(f"{GITHUB_API}/notifications/threads/{ctx.event_id}", headers=bot_headers)
            mark_processed_disk(ctx.event_id)
            logger.info(f"Workflow dispatched. Task: {final_task[:30]}... ID: {ctx.event_id}")

async def poll_loop():
    # 你可以手动指定一个基础频率，比如 30 秒
    base_interval = 30 
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            try:
                curr_headers = bot_headers.copy()
                if state["last_modified"]: 
                    curr_headers["If-Modified-Since"] = state["last_modified"]
                
                resp = await client.get(f"{GITHUB_API}/notifications", headers=curr_headers, params={"all": "false"})
                
                # 更新下一次轮询的间隔（优先遵循 GitHub 建议，但设定上限和下限）
                github_interval = int(resp.headers.get("X-Poll-Interval", base_interval))
                state["poll_interval"] = max(10, min(github_interval, 60)) 
                
                if resp.status_code == 200:
                    state["last_modified"] = resp.headers.get("Last-Modified")
                    notes = resp.json()
                    if notes:
                        logger.info(f"Got {len(notes)} new notifications.")
                    for note in notes:
                        if note["reason"] in ["mention", "team_mention"] and note["id"] not in processed_cache:
                            processed_cache.add(note["id"])
                            asyncio.create_task(handle_note(client, note))
                
                elif resp.status_code == 304:
                    # 304 意味着没变，我们可以保持安静，或者打印一个点表示在运行
                    pass 
                
                elif resp.status_code == 403:
                    logger.warning("Rate limit hit or forbidden. Sleeping longer...")
                    state["poll_interval"] = 120 # 被限流了就多歇会儿

            except (httpx.ConnectError, httpx.RemoteProtocolError) as e:
                logger.error(f"Network Error: {e}. Retrying in 5s...")
                await asyncio.sleep(5) # 网络波动导致的断开，5秒后重试
                continue # 跳过下面的正常 sleep

            except Exception as e:
                logger.error(f"Poll Error: {e}. Retrying in 10s...")
                await asyncio.sleep(10)
                continue

            # 正常情况下等待设定的间隔
            await asyncio.sleep(state["poll_interval"])

@app.on_event("startup")
async def startup():
    load_processed_log()
    asyncio.create_task(poll_loop())