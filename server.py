import os, json, logging, asyncio, datetime
from typing import Dict, Any, Optional, List, Set
from fastapi import FastAPI
from pydantic import BaseModel
import httpx

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
# 用于抓取 Diff
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
    type: str # 'comment', 'review', 'review_comment'

class TaskContext(BaseModel):
    repo: str
    event_type: str
    event_id: str
    trigger_user: Optional[str] = None
    issue_number: Optional[int] = None
    title: Optional[str] = None
    base_body: Optional[str] = None # Issue/PR 的原始 Body
    timeline_text: Optional[str] = None # 处理后的时间线文本
    diff_content: Optional[str] = None # PR Diff (截断)
    clone_url: Optional[str] = None

# --- 核心算法：1:3 手风琴折叠 ---
def compress_timeline(items: List[TimelineItem], max_chars: int = 10000) -> str:
    """
    策略：最早取1条，最新取3条，循环直到填满 max_chars。
    """
    if not items:
        return ""

    # 按时间排序
    items.sort(key=lambda x: x.created_at)
    
    selected_indices = set()
    head_ptr = 0
    tail_ptr = len(items) - 1
    current_chars = 0

    while head_ptr <= tail_ptr:
        # 1. 尝试取头部 1 条
        if current_chars + len(items[head_ptr].body) > max_chars: break
        selected_indices.add(head_ptr)
        current_chars += len(items[head_ptr].body)
        head_ptr += 1
        if head_ptr > tail_ptr: break

        # 2. 尝试取尾部 3 条
        for _ in range(3):
            if head_ptr > tail_ptr: break
            if current_chars + len(items[tail_ptr].body) > max_chars: break
            selected_indices.add(tail_ptr)
            current_chars += len(items[tail_ptr].body)
            tail_ptr -= 1

    # 重构文本
    final_text = []
    sorted_indices = sorted(list(selected_indices))
    last_idx = -1

    for idx in sorted_indices:
        # 检测断层
        if idx > last_idx + 1:
            omitted_count = idx - last_idx - 1
            final_text.append(f"\n... [系统提示: 中间省略了 {omitted_count} 条讨论] ...\n")
        
        item = items[idx]
        final_text.append(f"--- {item.created_at} @{item.user} ({item.type}) ---\n{item.body}")
        last_idx = idx

    return "\n\n".join(final_text)

# --- 数据抓取 ---

async def fetch_diff(client: httpx.AsyncClient, pull_url: str) -> str:
    try:
        r = await client.get(pull_url, headers=diff_headers)
        if r.status_code == 200:
            return r.text[:3000] # Diff 通常很长，限制一下
    except: pass
    return ""

async def fetch_issue_timeline(client: httpx.AsyncClient, issue_url: str) -> List[TimelineItem]:
    # 抓取所有 Issue Comments
    items = []
    page = 1
    while True:
        r = await client.get(f"{issue_url}/comments", headers=user_rest_headers, params={"per_page": 100, "page": page})
        if r.status_code != 200 or not r.json(): break
        for c in r.json():
            items.append(TimelineItem(id=c["id"], body=c.get("body") or "", created_at=c["created_at"], user=c["user"]["login"], type="comment"))
        if len(r.json()) < 100: break
        page += 1
    return items

async def fetch_pr_timeline_mixed(client: httpx.AsyncClient, issue_url: str, pull_url: str) -> List[TimelineItem]:
    # 混合抓取：Issue Comments + Review Comments
    items = await fetch_issue_timeline(client, issue_url)
    
    # 获取 Reviews (简化版，只抓取 top level review body)
    r = await client.get(f"{pull_url}/reviews", headers=user_rest_headers)
    if r.status_code == 200:
        for rev in r.json():
            if rev.get("body"):
                items.append(TimelineItem(id=rev["id"], body=rev["body"], created_at=rev["submitted_at"] or rev["key"], user=rev["user"]["login"], type="review"))
    return items

async def fetch_review_batch(client: httpx.AsyncClient, pull_url: str, review_id: int) -> str:
    # 专门处理 PR Review 场景：抓取同批次的 Review 详情
    text = ""
    # 1. 获取 Review 本身
    r = await client.get(f"{pull_url}/reviews/{review_id}", headers=user_rest_headers)
    if r.status_code == 200:
        data = r.json()
        text += f"--- Review Summary by @{data['user']['login']} ---\n{data.get('body') or 'No summary'}\n\n"
    
    # 2. 获取该 Review 下的具体代码评论
    r_comments = await client.get(f"{pull_url}/reviews/{review_id}/comments", headers=user_rest_headers)
    if r_comments.status_code == 200:
        for c in r_comments.json():
            text += f"[File: {c['path']}:{c.get('line', '?')}]\n{c['body']}\n---\n"
    return text

# --- 主逻辑 ---

async def handle_note(client: httpx.AsyncClient, note: Dict):
    subject = note["subject"]
    context = TaskContext(
        repo=note["repository"]["full_name"],
        event_type=subject["type"].lower(),
        event_id=note["id"],
        clone_url=note["repository"]["html_url"] + ".git"
    )

    try:
        # 1. 获取基础详情 (Issue/PR/Discussion)
        detail_resp = await client.get(subject["url"], headers=user_rest_headers)
        if detail_resp.status_code != 200: return
        detail = detail_resp.json()
        
        context.issue_number = detail.get("number")
        context.title = detail.get("title")
        context.base_body = detail.get("body") or ""
        context.trigger_user = detail.get("user", {}).get("login") # 默认为作者

        # 确定触发源 (Trigger Source)
        latest_comment_url = subject.get("latest_comment_url")
        trigger_type = "unknown"
        trigger_body = ""
        
        # 尝试通过 latest_comment_url 锁定触发者
        if latest_comment_url:
            lc_resp = await client.get(latest_comment_url, headers=user_rest_headers)
            if lc_resp.status_code == 200:
                lc_data = lc_resp.json()
                # 区分是普通评论还是 Review 评论
                if "pull_request_review_id" in lc_data:
                    trigger_type = "review_comment"
                    context.trigger_user = lc_data["user"]["login"]
                    # 记录 Review ID 用于后续抓取
                    review_id = lc_data["pull_request_review_id"]
                else:
                    trigger_type = "comment"
                    context.trigger_user = lc_data.get("user", {}).get("login") or lc_data.get("author", {}).get("login")
                    trigger_body = lc_data.get("body")

        # 权限校验
        if ALLOWED_USERS and context.trigger_user not in ALLOWED_USERS: return

        # --- 分支逻辑：PR Review vs 时间线 ---
        
        # 场景 A: PR 且来自 Review (Review Batch 模式)
        if subject["type"] == "PullRequest" and trigger_type == "review_comment":
            logger.info("Processing PR Review Batch context...")
            # 抓取 Diff
            context.diff_content = await fetch_diff(client, subject["url"])
            # 抓取同批次 Review
            context.timeline_text = await fetch_review_batch(client, subject["url"], review_id)
            final_task = "Attention: Focus on the code review comments provided in the timeline."

        # 场景 B: Issue / Discussion / PR 普通评论 (时间线模式)
        else:
            logger.info("Processing Timeline Mode (1:3 Strategy)...")
            timeline_items = []
            
            if subject["type"] == "PullRequest":
                # PR 需要混合抓 Diff, Comment 和 Review
                context.diff_content = await fetch_diff(client, subject["url"])
                timeline_items = await fetch_pr_timeline_mixed(client, detail["url"], subject["url"]) # detail['url'] is issue_url
            
            elif subject["type"] == "Issue":
                timeline_items = await fetch_issue_timeline(client, subject["url"])
                
            elif subject["type"] == "Discussion":
                # Discussion 逻辑保持简单抓取，或者需要 GraphQL 实现完整时间线，这里暂略，使用基础正文
                trigger_body = trigger_body or context.base_body

            # 执行 1:3 压缩策略
            context.timeline_text = compress_timeline(timeline_items, max_chars=10000)
            
            # 任务指令 = 触发那条评论的内容 (如果能抓到)
            final_task = trigger_body or context.base_body

        # --- 触发 Workflow ---
        await trigger_workflow(client, context, final_task)

    except Exception as e:
        logger.error(f"Handle Error: {e}", exc_info=True)

async def trigger_workflow(client: httpx.AsyncClient, ctx: TaskContext, task_text: str):
    # 构造发给 LLM 的最终 prompt
    # 我们将 "时间线" 和 "Diff" 组装进 Context
    # Task 字段只放核心指令
    
    payload = ctx.model_dump()
    payload_str = json.dumps(payload) # 使用 json.dumps 而不是 model_dump_json 以便控制
    
    # 再次兜底检查总长度，如果太长，砍掉 Diff
    if len(payload_str) > 60000 and ctx.diff_content:
        ctx.diff_content = "[Diff Truncated due to size limit]"
        payload_str = ctx.model_dump_json()

    url = f"{GITHUB_API}/repos/{CONTROL_REPO}/actions/workflows/llm-bot-runner.yml/dispatches"
    r = await client.post(url, headers=user_rest_headers, json={
        "ref": "main", 
        "inputs": {
            "task": task_text[:2000], 
            "context": payload_str
        }
    })
    
    if r.status_code == 204:
        await client.patch(f"{GITHUB_API}/notifications/threads/{ctx.event_id}", headers=bot_headers)
        with open(PROCESSED_LOG, "a") as f: f.write(f"{ctx.event_id}\n")
        logger.info(f"Triggered: {ctx.event_id} | User: {ctx.trigger_user}")

# --- 轮询逻辑 (含重试) ---
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
                logger.error(f"Poll Error: {e}. Retry in 5s...")
                await asyncio.sleep(5)
                continue
            await asyncio.sleep(state["poll_interval"])

@app.on_event("startup")
async def startup():
    if os.path.exists(PROCESSED_LOG):
        with open(PROCESSED_LOG, "r") as f:
            for line in f: processed_cache.add(line.strip())
    asyncio.create_task(poll_loop())