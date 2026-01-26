import os, json, logging, asyncio
from typing import Dict, Any, Optional, List, Set
from fastapi import FastAPI
from pydantic import BaseModel
import httpx
from datetime import datetime

# --- 配置区 ---
GITHUB_API = "https://api.github.com"
BOT_TOKEN = os.getenv("BOT_TOKEN")          
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")    
CONTROL_REPO = os.getenv("CONTROL_REPO")
ALLOWED_USERS = [u.strip() for u in os.getenv("ALLOWED_USERS", "").split(",") if u.strip()]
PROCESSED_LOG = "/data/processed_notifications.log"
BOT_HANDLE = "@WhiteElephantIsNotARobot"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("BotWatcher")

# Headers
bot_headers = {"Authorization": f"token {BOT_TOKEN}", "Accept": "application/vnd.github.v3+json"}
user_rest_headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
diff_headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3.diff"}

app = FastAPI()
processed_cache: Set[str] = set()
state = {"last_modified": None, "poll_interval": 30}

# --- 数据模型 ---
class TimelineItem(BaseModel):
    id: Any
    body: str
    created_at: str
    user: str
    type: str # 'comment', 'code_comment', 'review_summary'

class TaskContext(BaseModel):
    repo: str
    event_type: str
    event_id: str
    trigger_user: Optional[str] = None
    issue_number: Optional[int] = None
    title: Optional[str] = None
    base_body: Optional[str] = None 
    timeline_text: Optional[str] = None 
    diff_content: Optional[str] = None 
    clone_url: Optional[str] = None

# --- 核心算法：1:3 手风琴压缩 ---
def compress_timeline(items: List[TimelineItem], max_chars: int = 12000) -> str:
    if not items: return ""
    items.sort(key=lambda x: x.created_at) # 确保按时间先后排序
    
    selected_indices = set()
    head_ptr = 0
    tail_ptr = len(items) - 1
    current_chars = 0
    
    while head_ptr <= tail_ptr:
        # 1. 头部取 1 条
        if current_chars + len(items[head_ptr].body) > max_chars: break
        selected_indices.add(head_ptr)
        current_chars += len(items[head_ptr].body)
        head_ptr += 1
        if head_ptr > tail_ptr: break

        # 2. 尾部往回取 3 条
        for _ in range(3):
            if head_ptr > tail_ptr: break
            if current_chars + len(items[tail_ptr].body) > max_chars: break
            selected_indices.add(tail_ptr)
            current_chars += len(items[tail_ptr].body)
            tail_ptr -= 1

    final_text = []
    sorted_indices = sorted(list(selected_indices))
    last_idx = -1

    for idx in sorted_indices:
        if idx > last_idx + 1:
            omitted = idx - last_idx - 1
            final_text.append(f"\n... [系统提示: 此处省略了中间 {omitted} 条讨论以节省上下文空间] ...\n")
        
        item = items[idx]
        final_text.append(f"--- {item.created_at[:19]} @{item.user} [{item.type}] ---\n{item.body}")
        last_idx = idx

    return "\n\n".join(final_text)

# --- GitHub 数据抓取 ---

async def fetch_diff(client: httpx.AsyncClient, pull_url: str) -> str:
    try:
        r = await client.get(pull_url, headers=diff_headers)
        return r.text[:4000] if r.status_code == 200 else ""
    except: return ""

async def fetch_full_pr_context(client: httpx.AsyncClient, issue_url: str, pull_url: str) -> List[TimelineItem]:
    items = []
    # 1. 普通评论
    r1 = await client.get(f"{issue_url}/comments", headers=user_rest_headers)
    if r1.status_code == 200:
        for c in r1.json():
            items.append(TimelineItem(id=c["id"], body=c.get("body") or "", created_at=c["created_at"], user=c["user"]["login"], type="comment"))
    # 2. Review 行内评论
    r2 = await client.get(f"{pull_url}/comments", headers=user_rest_headers)
    if r2.status_code == 200:
        for c in r2.json():
            items.append(TimelineItem(id=c["id"], body=f"[File: {c.get('path')}]\n{c.get('body')}", created_at=c["created_at"], user=c["user"]["login"], type="code_comment"))
    # 3. Review 总结
    r3 = await client.get(f"{pull_url}/reviews", headers=user_rest_headers)
    if r3.status_code == 200:
        for rev in r3.json():
            if rev.get("body"):
                items.append(TimelineItem(id=rev["id"], body=f"[Review: {rev['state']}]\n{rev['body']}", created_at=rev.get("submitted_at") or rev["id"], user=rev["user"]["login"], type="review_summary"))
    return items

# --- 消息处理核心 ---

async def handle_note(client: httpx.AsyncClient, note: Dict):
    subject = note["subject"]
    raw_url = subject["url"]
    pull_url = raw_url.replace("/issues/", "/pulls/")
    issue_url = raw_url.replace("/pulls/", "/issues/")

    context = TaskContext(
        repo=note["repository"]["full_name"],
        event_type=subject["type"].lower(),
        event_id=note["id"],
        clone_url=note["repository"]["html_url"] + ".git"
    )

    try:
        # 1. 获取基础 Body
        detail_resp = await client.get(issue_url, headers=user_rest_headers)
        if detail_resp.status_code != 200:
            detail_resp = await client.get(pull_url, headers=user_rest_headers)
        if detail_resp.status_code != 200: return
        
        detail = detail_resp.json()
        context.issue_number, context.title = detail.get("number"), detail.get("title")
        context.base_body = detail.get("body") or ""

        # 2. 抓取时间线
        timeline_items = []
        if subject["type"] == "PullRequest":
            context.diff_content = await fetch_diff(client, pull_url)
            timeline_items = await fetch_full_pr_context(client, issue_url, pull_url)
        else:
            r = await client.get(f"{issue_url}/comments", headers=user_rest_headers)
            if r.status_code == 200:
                timeline_items = [TimelineItem(id=c["id"], body=c.get("body") or "", created_at=c["created_at"], user=c["user"]["login"], type="comment") for c in r.json()]

        # 3. 🎯 逆序检索：寻找最新的 @ 指令
        trigger_user, target_task = None, ""
        
        # 按时间从新到旧找第一个包含 BOT_HANDLE 的
        search_list = sorted(timeline_items, key=lambda x: x.created_at, reverse=True)
        for item in search_list:
            if BOT_HANDLE.lower() in item.body.lower():
                trigger_user, target_task = item.user, item.body
                break
        
        # 兜底：看开篇正文是否带 @
        if not trigger_user and BOT_HANDLE.lower() in context.base_body.lower():
            trigger_user, target_task = detail.get("user", {}).get("login"), context.base_body

        context.trigger_user = trigger_user

        # 4. 鉴权与过滤
        if not trigger_user or (ALLOWED_USERS and trigger_user not in ALLOWED_USERS):
            logger.warning(f"⛔ 跳过: 未找到提及或用户 '{trigger_user}' 无权限")
            await client.patch(f"{GITHUB_API}/notifications/threads/{context.event_id}", headers=bot_headers)
            return

        # 5. 压缩时间线并触发
        context.timeline_text = compress_timeline(timeline_items)
        await trigger_workflow(client, context, target_task)

    except Exception as e:
        logger.error(f"Handle Error: {e}", exc_info=True)

async def trigger_workflow(client: httpx.AsyncClient, ctx: TaskContext, task_text: str):
    payload_str = json.dumps(ctx.model_dump())
    
    # 极端情况保护：64KB 限制
    if len(payload_str) > 62000:
        ctx.diff_content = "[Diff Truncated]"
        payload_str = json.dumps(ctx.model_dump())
    if len(payload_str) > 62000:
        ctx.timeline_text = ctx.timeline_text[:5000] + "\n[Timeline Truncated]"
        payload_str = json.dumps(ctx.model_dump())

    url = f"{GITHUB_API}/repos/{CONTROL_REPO}/actions/workflows/llm-bot-runner.yml/dispatches"
    r = await client.post(url, headers=user_rest_headers, json={
        "ref": "main", 
        "inputs": {"task": task_text[:3000], "context": payload_str}
    })
    
    if r.status_code == 204:
        await client.patch(f"{GITHUB_API}/notifications/threads/{ctx.event_id}", headers=bot_headers)
        with open(PROCESSED_LOG, "a") as f: f.write(f"{ctx.event_id}\n")
        logger.info(f"🚀 已触发 Action: {ctx.trigger_user} 在 #{ctx.issue_number}")

async def poll_loop():
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            try:
                curr_headers = bot_headers.copy()
                if state["last_modified"]: curr_headers["If-Modified-Since"] = state["last_modified"]
                resp = await client.get(f"{GITHUB_API}/notifications", headers=curr_headers, params={"all": "false"})
                
                state["poll_interval"] = max(10, int(resp.headers.get("X-Poll-Interval", 30)))
                
                if resp.status_code == 200:
                    state["last_modified"] = resp.headers.get("Last-Modified")
                    for note in resp.json():
                        if note["reason"] in ["mention", "team_mention", "review_requested"] and note["id"] not in processed_cache:
                            processed_cache.add(note["id"])
                            asyncio.create_task(handle_note(client, note))
                elif resp.status_code == 403:
                    await asyncio.sleep(120)
            except Exception as e:
                logger.error(f"网络异常或轮询错误: {e}. 5秒后重试...")
                await asyncio.sleep(5)
                continue
            await asyncio.sleep(state["poll_interval"])

@app.on_event("startup")
async def startup():
    if os.path.exists(PROCESSED_LOG):
        with open(PROCESSED_LOG, "r") as f:
            for line in f: processed_cache.add(line.strip())
    asyncio.create_task(poll_loop())