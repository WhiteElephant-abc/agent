import os, json, logging, asyncio, re
from typing import Dict, Any, Optional, List, Set
from fastapi import FastAPI
from pydantic import BaseModel
import httpx
from datetime import datetime

# --- 配置 ---
GITHUB_API = "https://api.github.com"
BOT_TOKEN = os.getenv("BOT_TOKEN")          
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")    
CONTROL_REPO = os.getenv("CONTROL_REPO")
ALLOWED_USERS = [u.strip() for u in os.getenv("ALLOWED_USERS", "").split(",") if u.strip()]
PROCESSED_LOG = "/data/processed_notifications.log"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("BotWatcher")

# Headers
bot_headers = {"Authorization": f"token {BOT_TOKEN}", "Accept": "application/vnd.github.v3+json"}
user_rest_headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
diff_headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3.diff"}

app = FastAPI()
processed_cache: Set[str] = set()
state = {"last_modified": None, "poll_interval": 30}

# --- 辅助模型 ---
class TimelineItem(BaseModel):
    id: int
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

# --- 工具函数 ---
def parse_iso_time(t_str):
    try:
        return datetime.fromisoformat(t_str.replace('Z', '+00:00'))
    except:
        return datetime.now()

def compress_timeline(items: List[TimelineItem], max_chars: int = 12000) -> str:
    """
    1:3 手风琴策略：时间排序后，最早取1条，最新取3条，循环直到填满。
    """
    if not items: return ""
    
    # 必须按时间严格排序，否则上下文乱套
    items.sort(key=lambda x: x.created_at)
    
    selected_indices = set()
    head_ptr = 0
    tail_ptr = len(items) - 1
    current_chars = 0
    
    while head_ptr <= tail_ptr:
        # 尝试取头部
        if current_chars + len(items[head_ptr].body) > max_chars: break
        selected_indices.add(head_ptr)
        current_chars += len(items[head_ptr].body)
        head_ptr += 1
        if head_ptr > tail_ptr: break

        # 尝试取尾部 3 条
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
            final_text.append(f"\n... [Skipped {omitted} items] ...\n")
        
        item = items[idx]
        final_text.append(f"--- {item.created_at[:19]} @{item.user} [{item.type}] ---\n{item.body}")
        last_idx = idx

    return "\n\n".join(final_text)

# --- 抓取逻辑 ---

async def fetch_diff(client: httpx.AsyncClient, pull_url: str) -> str:
    try:
        r = await client.get(pull_url, headers=diff_headers)
        return r.text[:4000] if r.status_code == 200 else ""
    except: return ""

async def fetch_full_pr_context(client: httpx.AsyncClient, issue_url: str, pull_url: str) -> List[TimelineItem]:
    """
    抓取 PR 的全量上下文：包括普通对话、代码 Review 对话、以及 Review 总结
    """
    items = []
    
    # 1. 普通 Issue Comments
    r1 = await client.get(f"{issue_url}/comments", headers=user_rest_headers)
    if r1.status_code == 200:
        for c in r1.json():
            items.append(TimelineItem(
                id=c["id"], body=c.get("body") or "", created_at=c["created_at"], 
                user=c["user"]["login"], type="comment"
            ))

    # 2. Review Comments (代码行内评论)
    # 注意：这里必须用 /pulls/ 路径
    r2 = await client.get(f"{pull_url}/comments", headers=user_rest_headers)
    if r2.status_code == 200:
        for c in r2.json():
            items.append(TimelineItem(
                id=c["id"], body=f"[File: {c.get('path')}]\n{c.get('body')}", 
                created_at=c["created_at"], user=c["user"]["login"], type="code_comment"
            ))

    # 3. Review Summaries (Review 批次总结)
    r3 = await client.get(f"{pull_url}/reviews", headers=user_rest_headers)
    if r3.status_code == 200:
        for rev in r3.json():
            if rev.get("body"):
                items.append(TimelineItem(
                    id=rev["id"], body=f"[Review Status: {rev['state']}]\n{rev['body']}", 
                    created_at=rev.get("submitted_at") or rev["id"], # fallback
                    user=rev["user"]["login"], type="review_summary"
                ))
    
    return items

async def handle_note(client: httpx.AsyncClient, note: Dict):
    subject = note["subject"]
    repo_full = note["repository"]["full_name"]
    # 强制修正：如果 API 返回的 url 包含 /issues/ 但类型是 PullRequest，替换为 /pulls/
    # GitHub 通知里，PR 的 subject.url 经常是 https://api.github.com/repos/x/y/issues/123
    # 我们需要 https://api.github.com/repos/x/y/pulls/123 来抓 Diff 和 Reviews
    raw_url = subject["url"]
    pull_url = raw_url.replace("/issues/", "/pulls/")
    issue_url = raw_url.replace("/pulls/", "/issues/") # 确保能访问 comments

    context = TaskContext(
        repo=repo_full,
        event_type=subject["type"].lower(),
        event_id=note["id"],
        clone_url=note["repository"]["html_url"] + ".git"
    )

    try:
        # 1. 抓取基础详情 (Title, Base Body)
        # 尽量用 issue_url 抓基础信息，通用性好
        detail_resp = await client.get(issue_url, headers=user_rest_headers)
        if detail_resp.status_code != 200:
            # 如果失败，可能是真正的 Issue 没转过来，尝试 pull_url
            detail_resp = await client.get(pull_url, headers=user_rest_headers)
            if detail_resp.status_code != 200:
                logger.error(f"Failed to fetch details: {detail_resp.status_code}")
                return
        
        detail = detail_resp.json()
        context.issue_number = detail.get("number")
        context.title = detail.get("title")
        context.base_body = detail.get("body") or ""
        context.trigger_user = detail.get("user", {}).get("login") # 默认触发者为作者

        # 2. 锁定触发者和具体任务指令
        target_task = ""
        if subject.get("latest_comment_url"):
            lc_resp = await client.get(subject["latest_comment_url"], headers=user_rest_headers)
            if lc_resp.status_code == 200:
                lc_data = lc_resp.json()
                context.trigger_user = lc_data.get("user", {}).get("login") or context.trigger_user
                target_task = lc_data.get("body") or ""
                logger.info(f"Trigger found from comment: {context.trigger_user}")
            else:
                logger.warning(f"Could not fetch latest comment: {lc_resp.status_code}")

        # 3. 权限拦截 (增加显式日志)
        if ALLOWED_USERS:
            if context.trigger_user not in ALLOWED_USERS:
                logger.warning(f"⛔ Ignored: User '{context.trigger_user}' not in allowed list.")
                return
            else:
                logger.info(f"✅ User '{context.trigger_user}' authorized.")

        # 4. 构建上下文 (Diff & Timeline)
        timeline_items = []
        
        if subject["type"] == "PullRequest":
            logger.info(f"Processing PR #{context.issue_number}...")
            # 抓取 Diff
            context.diff_content = await fetch_diff(client, pull_url)
            # 混合抓取所有评论和Review
            timeline_items = await fetch_full_pr_context(client, issue_url, pull_url)
        
        elif subject["type"] == "Issue":
            # 抓取 Issue Comments
            r = await client.get(f"{issue_url}/comments", headers=user_rest_headers)
            if r.status_code == 200:
                for c in r.json():
                    timeline_items.append(TimelineItem(
                        id=c["id"], body=c.get("body") or "", created_at=c["created_at"],
                        user=c["user"]["login"], type="comment"
                    ))
        
        elif subject["type"] == "Discussion":
            target_task = target_task or context.base_body

        # 5. 执行 1:3 压缩
        if timeline_items:
            context.timeline_text = compress_timeline(timeline_items)
            logger.info(f"Timeline compressed. Total items: {len(timeline_items)}")

        # 6. 兜底任务文本
        final_task = target_task or context.base_body or "Analyze this context"
        
        await trigger_workflow(client, context, final_task)

    except Exception as e:
        logger.error(f"Handle Error: {e}", exc_info=True)

async def trigger_workflow(client: httpx.AsyncClient, ctx: TaskContext, task_text: str):
    payload = ctx.model_dump()
    payload_str = json.dumps(payload)
    
    # 保护截断
    if len(payload_str) > 60000:
        if ctx.diff_content:
            ctx.diff_content = "[Diff Truncated]"
            payload_str = json.dumps(ctx.model_dump())
        if len(payload_str) > 60000:
            ctx.timeline_text = ctx.timeline_text[:5000] + "\n[Timeline Truncated]"
            payload_str = json.dumps(ctx.model_dump())

    url = f"{GITHUB_API}/repos/{CONTROL_REPO}/actions/workflows/llm-bot-runner.yml/dispatches"
    logger.info(f"Dispatching workflow for {ctx.event_id}...")
    
    r = await client.post(url, headers=user_rest_headers, json={
        "ref": "main", 
        "inputs": {
            "task": task_text[:3000], 
            "context": payload_str
        }
    })
    
    if r.status_code == 204:
        await client.patch(f"{GITHUB_API}/notifications/threads/{ctx.event_id}", headers=bot_headers)
        with open(PROCESSED_LOG, "a") as f: f.write(f"{ctx.event_id}\n")
        logger.info(f"🚀 Triggered successfully! Task: {task_text[:20]}...")
    else:
        logger.error(f"❌ Dispatch failed: {r.status_code} {r.text}")

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
                    notes = resp.json()
                    if notes: logger.info(f"Received {len(notes)} notifications.")
                    for note in notes:
                        # 增加 review_requested 的支持，如果你想让 bot 被 review 时也触发
                        if note["reason"] in ["mention", "team_mention"] and note["id"] not in processed_cache:
                            processed_cache.add(note["id"])
                            asyncio.create_task(handle_note(client, note))
                elif resp.status_code == 403:
                    logger.warning("Rate limit hit, sleeping 2m.")
                    await asyncio.sleep(120)
            except Exception as e:
                logger.error(f"Poll loop error: {e}")
                await asyncio.sleep(5)
                continue
            await asyncio.sleep(state["poll_interval"])

@app.on_event("startup")
async def startup():
    if os.path.exists(PROCESSED_LOG):
        with open(PROCESSED_LOG, "r") as f:
            for line in f: processed_cache.add(line.strip())
    asyncio.create_task(poll_loop())