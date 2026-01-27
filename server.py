import os, json, logging, asyncio
from typing import Dict, List, Optional, Any, Tuple
from fastapi import FastAPI
from pydantic import BaseModel
import httpx
from datetime import datetime

# --- 配置区 ---
GITHUB_API = "https://api.github.com/graphql"
REST_API = "https://api.github.com"

# 双 Token 架构
BOT_TOKEN = os.getenv("BOT_TOKEN")      # 机器人Token：仅用于读取通知和标记已读
GQL_TOKEN = os.getenv("GQL_TOKEN")      # 个人PAT：用于GraphQL查询和触发Workflow

CONTROL_REPO = os.getenv("CONTROL_REPO")
ALLOWED_USERS = [u.strip() for u in os.getenv("ALLOWED_USERS", "").split(",") if u.strip()]
BOT_HANDLE = "@WhiteElephantIsNotARobot"
LOG_FILE = os.getenv("PROCESSED_LOG", "/data/processed_notifications.log")

# 上下文限制
CONTEXT_MAX_CHARS = int(os.getenv("CONTEXT_MAX_CHARS", "15000"))
DIFF_MAX_CHARS = int(os.getenv("DIFF_MAX_CHARS", "4000"))

# --- 日志配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("EnhancedBot")

# --- 持久化逻辑 ---
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

if os.path.exists(LOG_FILE):
    with open(LOG_FILE, "r") as f:
        processed_cache = {line.strip() for line in f if line.strip()}
    logger.info(f"Loaded {len(processed_cache)} processed IDs from {LOG_FILE}")
else:
    processed_cache = set()
    logger.info("No log file found, starting with empty cache.")

app = FastAPI()

# --- 数据模型 (融合早期版本的丰富元数据) ---
class TimelineItem(BaseModel):
    """时间线项目，统一表示评论、审核、审核评论等"""
    id: str
    body: str
    created_at: str
    user: str
    type: str  # 'comment', 'review', 'review_comment', 'issue', 'pr'
    # 可选字段
    path: Optional[str] = None
    diff_hunk: Optional[str] = None
    state: Optional[str] = None  # 对于review
    review_id: Optional[str] = None  # 对于review_comment

class TaskContext(BaseModel):
    """丰富的上下文数据模型"""
    # 基础信息
    repo: str
    event_type: str
    event_id: str
    trigger_user: Optional[str] = None
    issue_number: Optional[int] = None
    issue_body: Optional[str] = None  # Issue/PR/Discussion正文

    # 标题和描述
    title: Optional[str] = None
    pr_title: Optional[str] = None
    pr_body: Optional[str] = None
    discussion_title: Optional[str] = None
    discussion_body: Optional[str] = None

    # 历史数据
    comments_history: Optional[List[Dict]] = None
    reviews_history: Optional[List[Dict]] = None
    review_comments_batch: Optional[List[Dict]] = None

    # 代码上下文
    diff_content: Optional[str] = None
    diff_url: Optional[str] = None
    clone_url: Optional[str] = None
    head_ref: Optional[str] = None
    base_ref: Optional[str] = None
    commit_sha: Optional[str] = None

    # 元数据
    current_comment_id: Optional[str] = None
    current_review_id: Optional[str] = None
    is_mention_in_body: Optional[bool] = None
    is_mention_in_review: Optional[bool] = None
    is_truncated: Optional[bool] = None
    latest_comment_url: Optional[str] = None

    def to_json_string(self) -> str:
        """Convert context to JSON string for passing to workflow."""
        return json.dumps(self.model_dump(), ensure_ascii=False)

# --- 智能节选算法 (恢复早期版本的3:1算法) ---
def truncate_context_by_chars(items: List[TimelineItem], max_chars: int) -> Tuple[List[TimelineItem], bool]:
    """
    3新1老比例抓取 + 超限撤销 + 单边终止算法
    items: 按时间正序排列（索引0是最老，索引-1是最新）
    """
    if not items:
        return [], False

    selected_indices = set()
    total_chars = 0

    left_ptr = 0
    right_ptr = len(items) - 1

    left_active = True   # 老评论端状态
    right_active = True  # 新评论端状态

    while left_ptr <= right_ptr and (left_active or right_active):
        # --- 尝试抓取新评论 (最多3条) ---
        for _ in range(3):
            if right_active and left_ptr <= right_ptr:
                item_text = items[right_ptr].body
                if total_chars + len(item_text) <= max_chars:
                    selected_indices.add(right_ptr)
                    total_chars += len(item_text)
                    right_ptr -= 1
                else:
                    # 关键逻辑：撤销本次添加并锁死右侧
                    right_active = False
                    break

        # --- 尝试抓取老评论 (最多1条) ---
        if left_active and left_ptr <= right_ptr:
            item_text = items[left_ptr].body
            if total_chars + len(item_text) <= max_chars:
                selected_indices.add(left_ptr)
                total_chars += len(item_text)
                left_ptr += 1
            else:
                # 关键逻辑：撤销本次添加并锁死左侧
                left_active = False

    # --- 后序处理：排序并生成结果 ---
    sorted_indices = sorted(list(selected_indices))
    result = []

    # 确保至少包含最新的一条（如果触发点在最新）
    if not selected_indices and len(items) > 0:
        # 至少包含最新的一条
        result = [items[-1]]
        selected_indices = {len(items) - 1}
        sorted_indices = [len(items) - 1]

    for i in range(len(sorted_indices)):
        idx = sorted_indices[i]
        result.append(items[idx])

        # 插入截断声明 (Gap Notice)
        if i < len(sorted_indices) - 1:
            next_idx = sorted_indices[i+1]
            if next_idx > idx + 1:
                omitted = next_idx - idx - 1
                # 添加系统提示
                gap_item = TimelineItem(
                    id=f"gap_{idx}_{next_idx}",
                    body=f"--- [系统提示: 此处省略了中间 {omitted} 条历史评论] ---",
                    created_at=items[idx].created_at,  # 使用前一条的时间
                    user="system",
                    type="system_notice"
                )
                result.append(gap_item)

    is_truncated = len(selected_indices) < len(items)
    return result, is_truncated

def merge_and_sort_timeline(comments: List[Dict], reviews: List[Dict]) -> List[TimelineItem]:
    """
    将评论和review合并为统一的时间线，按时间排序。
    """
    timeline = []

    # 添加评论
    for c in comments:
        if c.get("body"):
            timeline.append(TimelineItem(
                id=str(c.get("id", "")),
                body=c.get("body", ""),
                created_at=c.get("created_at", ""),
                user=c.get("author", {}).get("login", "unknown"),
                type="comment"
            ))

    # 添加审核
    for r in reviews:
        if r.get("body"):
            timeline.append(TimelineItem(
                id=str(r.get("id", "")),
                body=r.get("body", ""),
                created_at=r.get("submitted_at", r.get("created_at", "")),
                user=r.get("author", {}).get("login", "unknown"),
                type="review",
                state=r.get("state")
            ))

        # 添加审核评论
        if r.get("comments") and r["comments"].get("nodes"):
            for rc in r["comments"]["nodes"]:
                if rc.get("body"):
                    timeline.append(TimelineItem(
                        id=str(rc.get("id", "")),
                        body=rc.get("body", ""),
                        created_at=rc.get("created_at", ""),
                        user=rc.get("author", {}).get("login", "unknown"),
                        type="review_comment",
                        path=rc.get("path"),
                        diff_hunk=rc.get("diffHunk"),
                        review_id=str(r.get("id", ""))
                    ))

    # 按时间排序
    timeline.sort(key=lambda x: x.created_at)
    return timeline

# --- GraphQL查询 (增强版，获取更多上下文) ---
GQL_ENHANCED_QUERY = """
query($url: URI!, $commentsCount: Int = 50, $reviewsCount: Int = 30) {
  resource(url: $url) {
    __typename
    ... on PullRequest {
      title body number
      baseRepository { nameWithOwner }
      url
      headRefName baseRefName
      headRepository { url }
      # 普通评论
      comments(last: $commentsCount) {
        nodes {
          id author { login } body createdAt
          ... on PullRequestReviewComment {
            path diffHunk
          }
        }
      }
      # 审核
      reviews(last: $reviewsCount) {
        nodes {
          id author { login } body createdAt submittedAt state
          comments(last: $commentsCount) {
            nodes {
              id author { login } body createdAt path diffHunk
            }
          }
        }
      }
    }
    ... on Issue {
      title body number
      repository { nameWithOwner }
      url
      comments(last: $commentsCount) {
        nodes { id author { login } body createdAt }
      }
    }
    ... on Commit {
      message oid
      repository { nameWithOwner }
      url
      comments(last: $commentsCount) {
        nodes { id author { login } body createdAt path diffHunk }
      }
    }
    ... on Discussion {
      title body number
      repository { nameWithOwner }
      url
      comments(last: $commentsCount) {
        nodes {
          id author { login } body createdAt
          replies(last: 10) {
            nodes { id author { login } body createdAt }
          }
        }
      }
    }
  }
}
"""

async def fetch_resource_details(client: httpx.AsyncClient, raw_url: str) -> Dict:
    """
    获取资源的详细信息
    """
    # 转换为GraphQL格式
    subject_url = raw_url.replace("api.github.com/repos/", "github.com/")
    subject_url = subject_url.replace("/pulls/", "/pull/")
    subject_url = subject_url.rstrip('/')

    gql_headers = {"Authorization": f"Bearer {GQL_TOKEN}"}

    try:
        resp = await client.post(
            GITHUB_API,
            json={
                "query": GQL_ENHANCED_QUERY,
                "variables": {"url": subject_url, "commentsCount": 50, "reviewsCount": 30}
            },
            headers=gql_headers
        )

        if resp.status_code != 200:
            logger.error(f"GraphQL HTTP Error {resp.status_code}: {resp.text}")
            return None

        json_resp = resp.json()
        data = json_resp.get("data", {}).get("resource")

        if not data:
            errors = json_resp.get("errors", [])
            logger.warning(f"No resource found for URL: {subject_url}, errors: {errors}")
            return None

        return data

    except Exception as e:
        logger.error(f"Exception during GraphQL call: {e}")
        return None

async def fetch_diff_content(client: httpx.AsyncClient, raw_url: str) -> str:
    """
    获取PR的diff内容
    """
    if "/pulls/" in raw_url or "/pull/" in raw_url:
        diff_url = raw_url.replace("/issues/", "/pulls/").replace("/pull/", "/pulls/")
        try:
            headers = {"Authorization": f"token {GQL_TOKEN}", "Accept": "application/vnd.github.v3.diff"}
            resp = await client.get(diff_url, headers=headers)
            if resp.status_code == 200:
                # 限制diff长度
                return resp.text[:DIFF_MAX_CHARS]
        except Exception as e:
            logger.warning(f"Failed to fetch diff: {e}")

    return ""

def find_trigger_node(nodes: List[TimelineItem], trigger_node_id: str = None) -> Tuple[Optional[TimelineItem], List[TimelineItem]]:
    """
    寻找触发节点
    策略：如果有指定的node_id，优先使用；否则寻找最新包含@的节点
    """
    if trigger_node_id:
        # 精确匹配指定的节点
        for node in nodes:
            if node.id == trigger_node_id:
                return node, nodes
    else:
        # 逆序查找最新包含@的节点
        for node in reversed(nodes):
            if BOT_HANDLE.lower() in node.body.lower():
                return node, nodes

    return None, nodes

def build_rich_context(
    resource_data: Dict,
    timeline_items: List[TimelineItem],
    trigger_node: Optional[TimelineItem],
    raw_url: str,
    note_id: str
) -> TaskContext:
    """
    构建丰富的上下文数据
    """
    resource_type = resource_data.get("__typename")
    repo_full = ""

    # 获取仓库信息
    if resource_type == "PullRequest":
        repo_full = resource_data.get("baseRepository", {}).get("nameWithOwner", "")
    elif resource_type == "Issue":
        repo_full = resource_data.get("repository", {}).get("nameWithOwner", "")
    elif resource_type == "Commit":
        repo_full = resource_data.get("repository", {}).get("nameWithOwner", "")
    elif resource_type == "Discussion":
        repo_full = resource_data.get("repository", {}).get("nameWithOwner", "")

    # 基础信息
    context = TaskContext(
        repo=repo_full,
        event_type=resource_type.lower(),
        event_id=note_id,
        issue_number=resource_data.get("number"),
        title=resource_data.get("title"),
        issue_body=resource_data.get("body", "")[:3000] if resource_data.get("body") else None,
        clone_url=f"https://github.com/{repo_full}.git"
    )

    # 特定类型的信息
    if resource_type == "PullRequest":
        context.pr_title = resource_data.get("title")
        context.pr_body = resource_data.get("body", "")[:3000] if resource_data.get("body") else None
        context.head_ref = resource_data.get("headRefName")
        context.base_ref = resource_data.get("baseRefName")
        context.diff_url = raw_url.replace("/issues/", "/pulls/") + ".diff"

        # 检查是否在PR正文中被提及
        if context.pr_body:
            context.is_mention_in_body = BOT_HANDLE.lower() in context.pr_body.lower()

    elif resource_type == "Issue":
        # 检查是否在Issue正文中被提及
        if context.issue_body:
            context.is_mention_in_body = BOT_HANDLE.lower() in context.issue_body.lower()

    elif resource_type == "Commit":
        context.commit_sha = resource_data.get("oid")
        context.title = resource_data.get("message", "")[:200]

    elif resource_type == "Discussion":
        context.discussion_title = resource_data.get("title")
        context.discussion_body = resource_data.get("body", "")[:3000] if resource_data.get("body") else None

    # 触发者信息
    if trigger_node:
        context.trigger_user = trigger_node.user
        context.current_comment_id = trigger_node.id
        context.current_review_id = trigger_node.review_id

        if trigger_node.type == "review":
            context.is_mention_in_review = True

    # 分离评论历史
    if timeline_items:
        # 智能截断
        truncated_items, is_truncated = truncate_context_by_chars(timeline_items, CONTEXT_MAX_CHARS)
        context.is_truncated = is_truncated

        # 转换为历史记录格式
        comments_history = []
        reviews_history = []
        review_comments_batch = []

        for item in truncated_items:
            if item.type in ["comment", "review_comment"]:
                comments_history.append({
                    "id": item.id,
                    "user": item.user,
                    "body": item.body,
                    "created_at": item.created_at,
                    "type": item.type
                })
            elif item.type == "review":
                reviews_history.append({
                    "id": item.id,
                    "user": item.user,
                    "body": item.body,
                    "state": item.state,
                    "submitted_at": item.created_at
                })

            # 如果是审核评论，添加到批次
            if item.type == "review_comment" and item.review_id:
                review_comments_batch.append({
                    "id": item.id,
                    "user": item.user,
                    "body": item.body,
                    "path": item.path,
                    "diff_hunk": item.diff_hunk
                })

        if comments_history:
            context.comments_history = comments_history
        if reviews_history:
            context.reviews_history = reviews_history
        if review_comments_batch:
            context.review_comments_batch = review_comments_batch

    return context

async def handle_notification(client: httpx.AsyncClient, note: Dict):
    """
    处理通知的核心逻辑
    """
    thread_id = note["id"]
    raw_url = note["subject"].get("url")

    if not raw_url:
        logger.warning(f"Empty URL in notification: {note}")
        return

    logger.info(f"Processing notification: {note['subject']['title']} ({raw_url})")

    # 1. 获取资源详情
    resource_data = await fetch_resource_details(client, raw_url)
    if not resource_data:
        logger.warning(f"Failed to fetch resource details for: {raw_url}")
        return

    # 2. 构建时间线
    timeline_items = []

    if resource_data["__typename"] == "PullRequest":
        # 提取评论和审核
        comments = resource_data.get("comments", {}).get("nodes", [])
        reviews = resource_data.get("reviews", {}).get("nodes", [])
        timeline_items = merge_and_sort_timeline(comments, reviews)

    elif resource_data["__typename"] == "Issue":
        comments = resource_data.get("comments", {}).get("nodes", [])
        timeline_items = merge_and_sort_timeline(comments, [])

    elif resource_data["__typename"] == "Commit":
        comments = resource_data.get("comments", {}).get("nodes", [])
        # 将commit评论转换为TimelineItem
        for c in comments:
            if c.get("body"):
                timeline_items.append(TimelineItem(
                    id=str(c.get("id", "")),
                    body=c.get("body", ""),
                    created_at=c.get("created_at", ""),
                    user=c.get("author", {}).get("login", "unknown"),
                    type="comment",
                    path=c.get("path"),
                    diff_hunk=c.get("diffHunk")
                ))

    elif resource_data["__typename"] == "Discussion":
        comments = resource_data.get("comments", {}).get("nodes", [])
        # 展开Discussion的回复
        all_comments = []
        for c in comments:
            if c.get("body"):
                all_comments.append({
                    "id": c.get("id"),
                    "author": c.get("author", {}),
                    "body": c.get("body"),
                    "created_at": c.get("created_at")
                })
            # 添加回复
            if c.get("replies") and c["replies"].get("nodes"):
                for reply in c["replies"]["nodes"]:
                    if reply.get("body"):
                        all_comments.append({
                            "id": reply.get("id"),
                            "author": reply.get("author", {}),
                            "body": reply.get("body"),
                            "created_at": reply.get("created_at")
                        })
        timeline_items = merge_and_sort_timeline(all_comments, [])

    # 3. 寻找触发节点
    trigger_node = None
    # 首先尝试从latest_comment_url获取触发节点
    if note["subject"].get("latest_comment_url"):
        try:
            lc_resp = await client.get(
                note["subject"]["latest_comment_url"],
                headers={"Authorization": f"token {BOT_TOKEN}"}
            )
            if lc_resp.status_code == 200:
                lc_data = lc_resp.json()
                trigger_node_id = str(lc_data.get("id", ""))
                trigger_node, _ = find_trigger_node(timeline_items, trigger_node_id)
        except Exception as e:
            logger.warning(f"Failed to fetch latest comment: {e}")

    # 如果没有找到，则寻找包含@的节点
    if not trigger_node:
        trigger_node, _ = find_trigger_node(timeline_items)

    # 4. 权限检查
    if not trigger_node:
        logger.info(f"No trigger node found for notification {thread_id}")
        # 如果没有找到触发节点，尝试标记为已读
        try:
            await client.patch(
                f"{REST_API}/notifications/threads/{thread_id}",
                headers={"Authorization": f"token {BOT_TOKEN}"}
            )
        except:
            pass
        return

    if ALLOWED_USERS and trigger_node.user not in ALLOWED_USERS:
        logger.warning(f"User {trigger_node.user} not in ALLOWED_USERS. Skipping.")
        # 标记为已读但不再处理
        try:
            await client.patch(
                f"{REST_API}/notifications/threads/{thread_id}",
                headers={"Authorization": f"token {BOT_TOKEN}"}
            )
        except:
            pass
        return

    # 5. 构建完整上下文
    context = build_rich_context(resource_data, timeline_items, trigger_node, raw_url, thread_id)

    # 6. 获取diff内容（对于PR）
    if resource_data["__typename"] in ["PullRequest", "Commit"]:
        context.diff_content = await fetch_diff_content(client, raw_url)

    # 7. 检查是否已处理
    if trigger_node.id in processed_cache:
        logger.info(f"Node {trigger_node.id} already processed, skipping.")
        # 标记为已读
        try:
            await client.patch(
                f"{REST_API}/notifications/threads/{thread_id}",
                headers={"Authorization": f"token {BOT_TOKEN}"}
            )
        except:
            pass
        return

    # 8. 生成任务描述（恢复早期版本的智能描述生成）
    task_description = generate_task_description(resource_data["__typename"], context, trigger_node)

    # 9. 触发工作流
    await trigger_workflow(client, context, task_description, trigger_node.id, thread_id)

def generate_task_description(event_type: str, context: TaskContext, trigger_node: TimelineItem) -> str:
    """
    根据事件类型生成自然语言任务描述
    """
    base_desc = ""

    if event_type == "PullRequest":
        title_suffix = f" - {context.pr_title}" if context.pr_title else ""
        base_desc = f"Review PR #{context.issue_number}{title_suffix} in {context.repo}"

    elif event_type == "Issue":
        title_suffix = f" - {context.title}" if context.title else ""
        base_desc = f"Analyze issue #{context.issue_number}{title_suffix} in {context.repo}"

    elif event_type == "Commit":
        base_desc = f"Review commit {context.commit_sha[:8]} in {context.repo}"

    elif event_type == "Discussion":
        base_desc = f"Participate in discussion '{context.discussion_title}' in {context.repo}"

    # 添加触发信息
    if trigger_node:
        user_info = f" (triggered by @{trigger_node.user})"
        base_desc += user_info

    return base_desc

async def trigger_workflow(client: httpx.AsyncClient, ctx: TaskContext, task_text: str, node_id: str, thread_id: str) -> bool:
    """
    触发GitHub Actions工作流
    """
    # 检查上下文大小
    context_str = ctx.to_json_string()
    if len(context_str) > 60000:  # GitHub限制
        logger.warning(f"Context too large ({len(context_str)} chars), truncating...")
        # 简化上下文
        ctx.diff_content = "[Diff truncated due to size limits]"
        if ctx.comments_history and len(ctx.comments_history) > 10:
            ctx.comments_history = ctx.comments_history[-10:]  # 只保留最近10条
        context_str = ctx.to_json_string()

    url = f"{REST_API}/repos/{CONTROL_REPO}/actions/workflows/llm-bot-runner.yml/dispatches"
    headers = {"Authorization": f"token {GQL_TOKEN}", "Accept": "application/vnd.github.v3+json"}

    payload = {
        "ref": "main",
        "inputs": {
            "task": task_text[:2000],
            "context": context_str
        }
    }

    try:
        r = await client.post(url, headers=headers, json=payload)

        if r.status_code == 204:
            logger.info(f"Successfully triggered workflow for node {node_id} by {ctx.trigger_user}")

            # 标记为已读
            await client.patch(
                f"{REST_API}/notifications/threads/{thread_id}",
                headers={"Authorization": f"token {BOT_TOKEN}"}
            )

            # 保存到日志
            if node_id not in processed_cache:
                processed_cache.add(node_id)
                try:
                    with open(LOG_FILE, "a") as f:
                        f.write(f"{node_id}\n")
                    logger.info(f"Logged node_id: {node_id}")
                except Exception as e:
                    logger.error(f"Failed to write to log file: {e}")

            return True
        else:
            logger.error(f"Workflow dispatch failed ({r.status_code}): {r.text}")
            return False

    except Exception as e:
        logger.error(f"Exception during workflow dispatch: {e}")
        return False

# --- 轮询逻辑 ---
async def poll_loop():
    """
    轮询GitHub通知
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        logger.info("Enhanced polling loop started...")

        while True:
            try:
                # 获取未读通知
                r = await client.get(
                    f"{REST_API}/notifications",
                    params={"participating": "true", "all": "false"},
                    headers={"Authorization": f"token {BOT_TOKEN}"}
                )

                if r.status_code == 200:
                    notes = r.json()
                    if notes:
                        logger.info(f"Fetched {len(notes)} unread notifications.")
                        tasks = [handle_notification(client, n) for n in notes]
                        await asyncio.gather(*tasks)
                    else:
                        logger.debug("No new notifications.")

                elif r.status_code == 304:
                    logger.debug("No changes in notifications (304).")

                elif r.status_code == 403:
                    logger.warning("Rate limit hit or forbidden. Sleeping for 120s...")
                    await asyncio.sleep(120)

                elif r.status_code != 200:
                    logger.error(f"Notification API Error {r.status_code}: {r.text}")

            except httpx.TimeoutException:
                logger.warning("Request timeout, retrying in 10s...")
                await asyncio.sleep(10)
                continue

            except Exception as e:
                logger.error(f"Poll loop exception: {e}")
                await asyncio.sleep(10)
                continue

            # 正常轮询间隔
            await asyncio.sleep(30)

@app.on_event("startup")
async def startup():
    """
    启动服务
    """
    # 配置验证
    if not GQL_TOKEN:
        logger.error("GQL_TOKEN environment variable is not set!")
        logger.error("Please set GQL_TOKEN with a GitHub Personal Access Token that has 'repo' scope.")
    else:
        token_preview = GQL_TOKEN[:8] + "..." + GQL_TOKEN[-4:] if len(GQL_TOKEN) > 12 else "***"
        logger.info(f"GQL_TOKEN is set: {token_preview}")

    if not BOT_TOKEN:
        logger.warning("BOT_TOKEN environment variable is not set!")

    if not CONTROL_REPO:
        logger.error("CONTROL_REPO environment variable is not set!")
    else:
        logger.info(f"CONTROL_REPO: {CONTROL_REPO}")

    logger.info(f"ALLOWED_USERS: {ALLOWED_USERS}")
    logger.info(f"LOG_FILE: {LOG_FILE}")
    logger.info(f"CONTEXT_MAX_CHARS: {CONTEXT_MAX_CHARS}")
    logger.info(f"DIFF_MAX_CHARS: {DIFF_MAX_CHARS}")

    # 启动轮询
    asyncio.create_task(poll_loop())

@app.get("/health")
async def health_check():
    """
    健康检查端点
    """
    return {
        "status": "healthy",
        "service": "enhanced-llm-bot-server",
        "processed_cache_size": len(processed_cache),
        "context_max_chars": CONTEXT_MAX_CHARS,
        "features": ["smart_truncation_3_1", "rich_context", "graphql_enhanced", "dual_token"]
    }

@app.get("/stats")
async def get_stats():
    """
    获取统计信息
    """
    log_size = 0
    if os.path.exists(LOG_FILE):
        log_size = os.path.getsize(LOG_FILE)

    return {
        "processed_notifications": len(processed_cache),
        "log_file_size_bytes": log_size,
        "log_file_path": LOG_FILE,
        "bot_handle": BOT_HANDLE
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
