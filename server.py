import os, json, logging, asyncio, re
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

# GitHub Actions inputs 限制为 64KB (65536 字节)
GITHUB_INPUTS_MAX_SIZE = 64000

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

# --- 数据模型 (基于test_context.py修复) ---
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
    """丰富的上下文数据模型（基于test_context.py修复）"""
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
    head_repo: Optional[str] = None      # 基于test_context.py添加
    base_repo: Optional[str] = None      # 基于test_context.py添加
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
        # 清理null字段，只包含有实际值的字段
        data = self.model_dump()
        cleaned_data = {}
        for key, value in data.items():
            if value is not None:
                # 对于列表/字典，如果是空的也不包含
                if isinstance(value, (list, dict)) and not value:
                    continue
                # 确保字符串中的换行符被正确保留
                if isinstance(value, str):
                    # JSON会正确处理换行符为\n，这里确保没有额外处理
                    cleaned_data[key] = value
                else:
                    cleaned_data[key] = value
        # 使用ensure_ascii=False保留非ASCII字符
        # 注意：去掉indent参数以减少JSON大小（GitHub Actions有64KB限制）
        return json.dumps(cleaned_data, ensure_ascii=False)

# --- 智能节选算法 (基于test_context.py，与server.py一致) ---
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

def extract_pr_timeline_items(resource_data: Dict) -> List[TimelineItem]:
    """
    从PR资源数据中提取所有时间线项目
    """
    timeline = []

    # 1. 普通评论（Issue评论）
    comments = resource_data.get("comments", {}).get("nodes", [])
    for c in comments:
        if c.get("body"):
            created_at = c.get("createdAt", "")
            if not created_at:
                created_at = "1970-01-01T00:00:00Z"  # 默认值
            timeline.append(TimelineItem(
                id=str(c.get("id", "")),
                body=c.get("body", ""),
                created_at=created_at,
                user=(c.get("author") or {}).get("login", "unknown"),
                type="comment"
            ))

    # 2. 审核
    reviews = resource_data.get("reviews", {}).get("nodes", [])
    for r in reviews:
        # 即使 review body 为空也要添加到 timeline，因为 latest_comment_url 可能指向 review 本身
        # 例如：review comment 的 latest_comment_url 指向 review，但 @ 提及在 review comment 中
        # 如果 review 不在 timeline 中，就无法通过 ID 匹配找到它，也就无法回退搜索
        created_at = r.get("submittedAt") or r.get("createdAt") or "1970-01-01T00:00:00Z"  # 默认值
        timeline.append(TimelineItem(
            id=str(r.get("id", "")),
            body=r.get("body", ""),
            created_at=created_at,
            user=(r.get("author") or {}).get("login", "unknown"),
            type="review",
            state=r.get("state")
        ))

    # 3. 审核评论（行内代码评论）- 从reviewThreads获取
    review_threads = resource_data.get("reviewThreads", {}).get("nodes", [])
    for thread in review_threads:
        thread_comments = thread.get("comments", {}).get("nodes", [])
        for rc in thread_comments:
            if rc.get("body"):
                created_at = rc.get("createdAt", "")
                if not created_at:
                    created_at = "1970-01-01T00:00:00Z"  # 默认值
                timeline.append(TimelineItem(
                    id=str(rc.get("id", "")),
                    body=rc.get("body", ""),
                    created_at=created_at,
                    user=(rc.get("author") or {}).get("login", "unknown"),
                    type="review_comment",
                    path=rc.get("path"),
                    diff_hunk=rc.get("diffHunk"),
                    review_id=str(rc.get("pullRequestReview", {}).get("id", "")) if rc.get("pullRequestReview") else None
                ))

    # 按时间排序
    timeline.sort(key=lambda x: x.created_at)
    
    # 调试信息
    if timeline:
        logger.info(f"Timeline items extracted: {len(timeline)} total")
        # 显示不同类型数量
        comment_count = sum(1 for item in timeline if item.type == "comment")
        review_count = sum(1 for item in timeline if item.type == "review")
        review_comment_count = sum(1 for item in timeline if item.type == "review_comment")
        logger.info(f"  Comments: {comment_count}, Reviews: {review_count}, Review Comments: {review_comment_count}")
        
        # 显示最后3个项目
        for i, item in enumerate(timeline[-3:]):
            logger.info(f"  [{len(timeline)-3+i}] {item.created_at[:19]} @{item.user} ({item.type}): {item.body[:50]}...")
    
    return timeline

def merge_comments_to_timeline(comments: List[Dict]) -> List[TimelineItem]:
    """
    将评论列表转换为时间线项目
    """
    timeline = []
    for c in comments:
        if c.get("body"):
            created_at = c.get("createdAt", "")
            if not created_at:
                created_at = "1970-01-01T00:00:00Z"
            timeline.append(TimelineItem(
                id=str(c.get("id", "")),
                body=c.get("body", ""),
                created_at=created_at,
                user=(c.get("author") or {}).get("login", "unknown"),
                type="comment"
            ))
    timeline.sort(key=lambda x: x.created_at)
    return timeline

# --- GraphQL查询 (增强版，获取更多上下文) ---
GQL_ENHANCED_QUERY = """
query($url: URI!, $commentsCount: Int = 50, $reviewsCount: Int = 30) {
  resource(url: $url) {
    __typename
    ... on PullRequest {
      title body number id
      author { login }
      createdAt
      baseRepository { nameWithOwner }
      url
      headRefName baseRefName
      headRepository { url nameWithOwner }
      # 普通评论（Issue评论）
      comments(last: $commentsCount) {
        nodes {
          id author { login } body createdAt
        }
      }
      # 审核评论（行内代码评论）- 单独查询
      reviewThreads(last: $reviewsCount) {
        nodes {
          comments(last: 10) {
            nodes {
              id author { login } body createdAt path diffHunk
              pullRequestReview { id }
            }
          }
        }
      }
      # 审核
      reviews(last: $reviewsCount) {
        nodes {
          id author { login } body createdAt submittedAt state
        }
      }
    }
    ... on Issue {
      title body number id
      author { login }
      createdAt
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
        nodes { id author { login } body createdAt path }
      }
    }
    # Discussion 类型不支持在 resource(url:) 查询中，需要特殊处理
    # 对于Discussion，我们将回退到REST API
  }
}
"""

async def fetch_resource_details(client: httpx.AsyncClient, raw_url: str) -> Dict:
    """
    获取资源的详细信息
    """
    # 检查是否为Discussion类型（GraphQL的resource(url:)查询不支持Discussion）
    if "/discussions/" in raw_url:
        try:
            # 使用REST API获取discussion详情
            headers = {"Authorization": f"token {GQL_TOKEN}", "Accept": "application/vnd.github.v3+json"}

            # 1. 获取discussion基本信息
            discussion_resp = await client.get(raw_url, headers=headers)
            if discussion_resp.status_code != 200:
                logger.warning(f"Failed to fetch discussion: {discussion_resp.status_code} - {discussion_resp.text}")
                return None

            discussion_data = discussion_resp.json()

            # 2. 获取discussion评论
            comments_url = f"{raw_url}/comments"
            comments_resp = await client.get(comments_url, headers=headers, params={"per_page": 50})
            comments = []
            if comments_resp.status_code == 200:
                comments = comments_resp.json()

            # 转换为与GraphQL类似的结构，方便后续统一处理
            # GitHub REST API字段名转换为驼峰命名以保持一致性
            data = {
                "__typename": "Discussion",
                "title": discussion_data.get("title", ""),
                "body": discussion_data.get("body", ""),
                "number": discussion_data.get("number", 0),
                "author": {
                    "login": (discussion_data.get("user") or {}).get("login", "unknown")
                },
                "createdAt": discussion_data.get("created_at", ""),
                "repository": {
                    "nameWithOwner": discussion_data.get("repository", {}).get("full_name", "")
                },
                "url": discussion_data.get("html_url", "").replace("github.com/", "api.github.com/repos/"),
                "comments": {
                    "nodes": [
                        {
                            "id": str(c.get("id", "")),
                            "author": {
                                "login": (c.get("user") or {}).get("login", "unknown")
                            },
                            "body": c.get("body", ""),
                            "createdAt": c.get("created_at", "")
                        }
                        for c in comments
                    ]
                }
            }
            return data
        except Exception as e:
            logger.error(f"Exception during Discussion REST API call: {e}")
            return None

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
                # 检查节点是否包含@机器人（只有提及才应该触发）
                if node.body and BOT_HANDLE.lower() in node.body.lower():
                    logger.info(f"Found trigger node by ID: {node.id} by @{node.user} (type: {node.type})")
                    return node, nodes
                else:
                    # 当通过ID找到的节点不包含@时，继续搜索其他包含@的节点
                    # 例如：latest_comment_url 可能指向 review 本身，但 @ 提及在 review comment 中
                    logger.warning(f"Node {node.id} matched by ID but does not contain @{BOT_HANDLE}. Falling back to search for @ mention.")
                    break  # 退出精确匹配循环，继续下面的搜索

    # 逆序查找最新包含@的节点
    # 无论是否指定了 trigger_node_id，都搜索包含@的节点
    for node in reversed(nodes):
        if node.body and BOT_HANDLE.lower() in node.body.lower():
            logger.info(f"Found trigger node: {node.id} by @{node.user} (type: {node.type})")
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
    构建丰富的上下文数据（基于test_context.py修复）
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
    
    # 如果无法从GraphQL获取repo信息，尝试从URL解析
    if not repo_full and raw_url:
        try:
            # 从URL解析：https://api.github.com/repos/owner/repo/issues/123
            match = re.search(r'repos/([^/]+/[^/]+)', raw_url)
            if match:
                repo_full = match.group(1)
        except:
            pass

    # 基础信息
    # 对于PR，issue_body应该为空，使用pr_body
    issue_body_value = None
    title_value = None
    if resource_type == "PullRequest":
        # PR不设置issue_body，避免与pr_body重复
        issue_body_value = None
        # 对于PR，title应该为空，使用pr_title
        title_value = None
    elif resource_type == "Discussion":
        # Discussion不设置issue_body和title，使用discussion_title和discussion_body
        issue_body_value = None
        title_value = None
    else:
        issue_body_value = resource_data.get("body", "")[:3000] if resource_data.get("body") else None
        title_value = resource_data.get("title")

    context = TaskContext(
        repo=repo_full,
        event_type=resource_type.lower(),
        event_id=note_id,
        issue_number=resource_data.get("number"),
        title=title_value,
        issue_body=issue_body_value,
        # 使用SSH格式的克隆URL：git@github.com:owner/repo.git
        clone_url=f"git@github.com:{repo_full}.git" if repo_full else None
    )

    # 特定类型的信息
    if resource_type == "PullRequest":
        context.pr_title = resource_data.get("title")
        context.pr_body = resource_data.get("body", "")[:3000] if resource_data.get("body") else None
        context.head_ref = resource_data.get("headRefName")
        context.base_ref = resource_data.get("baseRefName")
        context.diff_url = raw_url.replace("/issues/", "/pulls/") + ".diff"
        
        # 获取PR分支仓库信息（基于test_context.py修复）
        head_repo = resource_data.get("headRepository", {})
        repo_path = None

        # 优先使用nameWithOwner字段
        if head_repo and head_repo.get("nameWithOwner"):
            repo_path = head_repo.get("nameWithOwner")
        # 如果没有nameWithOwner，尝试从url解析
        elif head_repo and head_repo.get("url"):
            api_url = head_repo.get("url")
            # https://api.github.com/repos/owner/repo -> git@github.com:owner/repo.git
            if "api.github.com/repos/" in api_url:
                # 提取owner/repo部分
                repo_match = re.search(r'repos/([^/]+/[^/]+)', api_url)
                if repo_match:
                    repo_path = repo_match.group(1)

        if repo_path:
            clone_url = f"git@github.com:{repo_path}.git"
            context.clone_url = clone_url
            # 设置 head_repo 为 repo:branch 格式（基于test_context.py修复）
            if context.head_ref:
                context.head_repo = f"{repo_path}:{context.head_ref}"
            logger.info(f"PR branch clone_url (SSH): {clone_url}")
            logger.info(f"PR head_repo (repo:branch): {context.head_repo}")

        # 获取基础仓库信息（基于test_context.py添加）
        base_repo = resource_data.get("baseRepository", {})
        if base_repo and base_repo.get("nameWithOwner"):
            base_repo_name = base_repo.get("nameWithOwner")
            if context.base_ref:
                context.base_repo = f"{base_repo_name}:{context.base_ref}"
                logger.info(f"PR base_repo (repo:branch): {context.base_repo}")

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
        # 设置Discussion的标题和正文
        discussion_title = resource_data.get("title")
        context.discussion_title = discussion_title
        context.title = discussion_title  # 同时设置通用title字段
        context.discussion_body = resource_data.get("body", "")[:3000] if resource_data.get("body") else None
        # 检查是否在Discussion正文中被提及
        if context.discussion_body:
            context.is_mention_in_body = BOT_HANDLE.lower() in context.discussion_body.lower()

    # Discussion 类型已通过REST API支持

    # 触发者信息
    if trigger_node:
        context.trigger_user = trigger_node.user
        context.current_comment_id = trigger_node.id
        context.current_review_id = trigger_node.review_id

        if trigger_node.type == "review":
            context.is_mention_in_review = True
        
        # 记录触发消息（用于调试）
        logger.info(f"Trigger message: '{trigger_node.body[:100]}{'...' if len(trigger_node.body) > 100 else ''}'")
        logger.info(f"Trigger node type: {trigger_node.type}")

    # 分离评论历史（基于test_context.py修复review触发过滤逻辑）
    if timeline_items:
        logger.info(f"Applying smart truncation to {len(timeline_items)} timeline items (max: {CONTEXT_MAX_CHARS} chars)")
        # 智能截断（仅用于评论历史）
        truncated_items, is_truncated = truncate_context_by_chars(timeline_items, CONTEXT_MAX_CHARS)
        context.is_truncated = is_truncated
        logger.info(f"Truncation result: {len(truncated_items)} items selected (truncated: {is_truncated})")

        # 转换为历史记录格式
        comments_history = []
        reviews_history = []
        review_comments_batch = []

        # 获取触发类型
        trigger_type = trigger_node.type if trigger_node else None

        # reviews_history: 始终保留所有 review 批次（完整历史）
        for item in timeline_items:
            if item.type == "review":
                reviews_history.append({
                    "id": item.id,
                    "user": item.user,
                    "body": item.body,
                    "state": item.state,
                    "submitted_at": item.created_at
                })
                logger.info(f"Including review {item.id} in reviews_history")

        # review批次使用原始timeline_items确保完整保留（不截断）
        if trigger_type in ["review", "review_comment"]:
            # review/review_comment触发：保留所有review批次，同时精确过滤review comments
            trigger_review_id = trigger_node.review_id if trigger_node.review_id else trigger_node.id

            for item in timeline_items:
                # review_comments_batch: 只保留与当前触发 review 相关的 review comments
                if item.type == "review_comment" and item.review_id and item.review_id == trigger_review_id:
                    review_comments_batch.append({
                        "id": item.id,
                        "user": item.user,
                        "body": item.body,
                        "path": item.path,
                        "diff_hunk": item.diff_hunk
                    })
                    logger.info(f"Including review comment {item.id} for review {trigger_review_id}")
        else:
            # comment触发：使用truncated_items处理评论
            for item in truncated_items:
                if item.type == "comment":
                    comments_history.append({
                        "id": item.id,
                        "user": item.user,
                        "body": item.body,
                        "created_at": item.created_at,
                        "type": item.type
                    })

            # review_comments_batch: 只保留最新批次的 review comments
            # 找到最新一次 review 的 ID（按时间顺序，最后一个是最新）
            latest_review_id = None
            for item in timeline_items:
                if item.type == "review":
                    latest_review_id = item.id

            # 只保留与最新 review 相关的 review comments
            if latest_review_id:
                for item in timeline_items:
                    if item.type == "review_comment" and item.review_id == latest_review_id:
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

    # 检查通知原因 - 只处理提及通知
    reason = note.get("reason")
    allowed_reasons = ["mention", "team_mention"]
    if reason not in allowed_reasons:
        logger.info(f"Ignoring notification with reason '{reason}'. Only processing mentions (allowed: {allowed_reasons})")
        # 标记为已读但不再处理
        try:
            await client.patch(
                f"{REST_API}/notifications/threads/{thread_id}",
                headers={"Authorization": f"token {BOT_TOKEN}"}
            )
        except:
            pass
        return

    # 1. 获取资源详情
    resource_data = await fetch_resource_details(client, raw_url)
    if not resource_data:
        logger.warning(f"Failed to fetch resource details for: {raw_url}")
        return

    # 2. 构建时间线
    timeline_items = []
    
    if resource_data["__typename"] == "PullRequest":
        # 提取所有时间线项目
        timeline_items = extract_pr_timeline_items(resource_data)
        logger.info(f"Extracted {len(timeline_items)} timeline items for PR #{resource_data.get('number')}")

    elif resource_data["__typename"] == "Issue":
        comments = resource_data.get("comments", {}).get("nodes", [])
        timeline_items = merge_comments_to_timeline(comments)

        # 如果issue body中有@机器人，也添加到时间线中
        issue_body = resource_data.get("body", "")
        if issue_body and BOT_HANDLE.lower() in issue_body.lower():
            # 获取仓库名称用于唯一ID生成
            repo_name = resource_data.get("repository", {}).get("nameWithOwner", "").replace("/", "_")
            issue_number = resource_data.get('number', '')
            timeline_items.append(TimelineItem(
                id=f"issue_body_{repo_name}_{issue_number}" if repo_name else f"issue_body_{issue_number}",
                body=issue_body,
                created_at=resource_data.get("createdAt", "1970-01-01T00:00:00Z"),
                user=(resource_data.get("author") or {}).get("login", "unknown"),
                type="issue_body"
            ))
            # 按时间排序
            timeline_items.sort(key=lambda x: x.created_at)

    elif resource_data["__typename"] == "Commit":
        comments = resource_data.get("comments", {}).get("nodes", [])
        # 将commit评论转换为TimelineItem
        for c in comments:
            if c.get("body"):
                created_at = c.get("createdAt", "")
                if not created_at:
                    created_at = "1970-01-01T00:00:00Z"
                timeline_items.append(TimelineItem(
                    id=str(c.get("id", "")),
                    body=c.get("body", ""),
                    created_at=created_at,
                    user=(c.get("author") or {}).get("login", "unknown"),
                    type="comment",
                    path=c.get("path"),
                    diff_hunk=None  # Commit评论没有diffHunk字段
                ))
    
    elif resource_data["__typename"] == "Discussion":
        comments = resource_data.get("comments", {}).get("nodes", [])
        timeline_items = merge_comments_to_timeline(comments)

        # 如果discussion body中有@机器人，也添加到时间线中
        discussion_body = resource_data.get("body", "")
        if discussion_body and BOT_HANDLE.lower() in discussion_body.lower():
            # 获取仓库名称用于唯一ID生成
            repo_name = resource_data.get("repository", {}).get("nameWithOwner", "").replace("/", "_")
            discussion_number = resource_data.get('number', '')
            timeline_items.append(TimelineItem(
                id=f"discussion_body_{repo_name}_{discussion_number}" if repo_name else f"discussion_body_{discussion_number}",
                body=discussion_body,
                created_at=resource_data.get("createdAt", "1970-01-01T00:00:00Z"),
                user=(resource_data.get("author") or {}).get("login", "unknown"),
                type="discussion_body"
            ))
            # 按时间排序
            timeline_items.sort(key=lambda x: x.created_at)

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

    # 如果还是没有找到触发节点，检查issue body是否包含@机器人
    if not trigger_node and resource_data["__typename"] == "Issue":
        issue_body = resource_data.get("body", "")
        if issue_body and BOT_HANDLE.lower() in issue_body.lower():
            # 获取仓库名称用于唯一ID生成
            repo_name = resource_data.get("repository", {}).get("nameWithOwner", "").replace("/", "_")
            issue_number = resource_data.get('number', '')
            trigger_node = TimelineItem(
                id=f"issue_body_{repo_name}_{issue_number}" if repo_name else f"issue_body_{issue_number}",
                body=issue_body,
                created_at=resource_data.get("createdAt", "1970-01-01T00:00:00Z"),
                user=(resource_data.get("author") or {}).get("login", "unknown"),
                type="issue_body"
            )
            logger.info(f"Found trigger node in issue body: {trigger_node.id} by @{trigger_node.user} (type: {trigger_node.type})")

    # 如果还是没有找到触发节点，检查discussion body是否包含@机器人
    if not trigger_node and resource_data["__typename"] == "Discussion":
        discussion_body = resource_data.get("body", "")
        if discussion_body and BOT_HANDLE.lower() in discussion_body.lower():
            # 获取仓库名称用于唯一ID生成
            repo_name = resource_data.get("repository", {}).get("nameWithOwner", "").replace("/", "_")
            discussion_number = resource_data.get('number', '')
            trigger_node = TimelineItem(
                id=f"discussion_body_{repo_name}_{discussion_number}" if repo_name else f"discussion_body_{discussion_number}",
                body=discussion_body,
                created_at=resource_data.get("createdAt", "1970-01-01T00:00:00Z"),
                user=(resource_data.get("author") or {}).get("login", "unknown"),
                type="discussion_body"
            )
            logger.info(f"Found trigger node in discussion body: {trigger_node.id} by @{trigger_node.user} (type: {trigger_node.type})")

    # 4. 权限检查和触发节点验证
    if not trigger_node:
        logger.error(f"No trigger node found for notification {thread_id}. Workflow triggered by @ mention, but no @ message found.")
        # 如果没有找到触发节点，尝试标记为已读
        try:
            await client.patch(
                f"{REST_API}/notifications/threads/{thread_id}",
                headers={"Authorization": f"token {BOT_TOKEN}"}
            )
        except:
            pass
        return

    # 验证触发消息是否存在
    if not trigger_node.body or not trigger_node.body.strip():
        logger.error(f"Trigger node {trigger_node.id} has empty body. Cannot proceed with workflow.")
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

    # 5. 构建完整上下文（使用修复后的build_rich_context）
    context = build_rich_context(resource_data, timeline_items, trigger_node, raw_url, thread_id)

    # 6. 获取diff内容（根据触发类型决定）
    if resource_data["__typename"] in ["PullRequest", "Commit"]:
        # 如果是review或review_comment触发，不获取完整PR diff（使用review comments的diff_hunk）
        if trigger_node and trigger_node.type in ["review", "review_comment"]:
            logger.info("Review/review_comment trigger detected, skipping full PR diff")
            # review触发时不提供完整diff，review_comments_batch中已经有具体diff_hunk
        else:
            # 普通触发：获取完整diff
            diff_content = await fetch_diff_content(client, raw_url)
            if diff_content:
                context.diff_content = diff_content
                logger.info(f"Full diff fetched: {len(diff_content)} chars")
            else:
                logger.info("No diff content available")

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

    # 8. 使用触发消息直接作为任务描述
    # 整个工作流由@触发，必定存在一个包含@的消息可以放入task
    task_description = trigger_node.body
    logger.info(f"Using trigger message directly as task description: '{task_description[:200]}{'...' if len(task_description) > 200 else ''}'")

    # 9. 触发工作流
    await trigger_workflow(client, context, task_description, trigger_node.id, thread_id)

async def trigger_workflow(client: httpx.AsyncClient, ctx: TaskContext, task_text: str, node_id: str, thread_id: str) -> bool:
    """
    触发GitHub Actions工作流
    """
    # 检查上下文大小
    context_str = ctx.to_json_string()
    logger.info(f"Context size: {len(context_str)} chars")

    # 调试信息
    if ctx.comments_history:
        logger.info(f"Comments history: {len(ctx.comments_history)} items")
    if ctx.reviews_history:
        logger.info(f"Reviews history: {len(ctx.reviews_history)} items")
        for i, review in enumerate(ctx.reviews_history):
            logger.info(f"  Review[{i}]: @{review.get('user')} - {review.get('body', '')[:50]}...")
    if ctx.review_comments_batch:
        logger.info(f"Review comments batch: {len(ctx.review_comments_batch)} items")
        for i, comment in enumerate(ctx.review_comments_batch):
            logger.info(f"  ReviewComment[{i}]: @{comment.get('user')} - {comment.get('path')}: {comment.get('body', '')[:50]}...")
    if ctx.is_truncated is not None:
        logger.info(f"Context was truncated: {ctx.is_truncated}")

    # 检查是否有重复/空字段
    logger.info(f"diff_content present: {bool(ctx.diff_content)}")
    logger.info(f"clone_url: {ctx.clone_url}")
    logger.info(f"head_ref: {ctx.head_ref}, base_ref: {ctx.base_ref}")
    logger.info(f"head_repo: {ctx.head_repo}, base_repo: {ctx.base_repo}")

    # 记录任务描述
    logger.info(f"LLM_TASK to send: '{task_text[:200]}{'...' if len(task_text) > 200 else ''}'")

    # 定义缩减步骤函数
    def drop_diff():
        logger.info(f"Dropping diff_content to reduce size (was {len(ctx.diff_content)} chars)")
        ctx.diff_content = None

    def reduce_comments():
        original_count = len(ctx.comments_history)
        ctx.comments_history = ctx.comments_history[-5:]
        logger.info(f"Reducing comments history from {original_count} to 5 items")

    def drop_review_comments():
        logger.info(f"Dropping review_comments_batch (was {len(ctx.review_comments_batch)} items)")
        ctx.review_comments_batch = None

    def drop_reviews_history():
        logger.info(f"Dropping reviews_history (was {len(ctx.reviews_history)} items)")
        ctx.reviews_history = None

    # 按优先级丢弃数据，直到上下文大小满足要求
    if len(context_str) > GITHUB_INPUTS_MAX_SIZE:
        logger.warning(f"Context too large ({len(context_str)} chars), attempting to reduce...")

        reduction_steps = []

        # 1. 优先丢弃 diff_content（diff 通常很大）
        if ctx.diff_content:
            reduction_steps.append(drop_diff)

        # 2. 截断评论历史（只保留最近 5 条）
        if ctx.comments_history:
            reduction_steps.append(reduce_comments)

        # 3. 丢弃 review_comments_batch
        if ctx.review_comments_batch:
            reduction_steps.append(drop_review_comments)

        # 4. 丢弃 reviews_history
        if ctx.reviews_history:
            reduction_steps.append(drop_reviews_history)

        for step in reduction_steps:
            if len(context_str) <= GITHUB_INPUTS_MAX_SIZE:
                break
            step()
            context_str = ctx.to_json_string()
            logger.info(f"Context size after reduction: {len(context_str)} chars")

        # 最终检查
        if len(context_str) > GITHUB_INPUTS_MAX_SIZE:
            logger.error(f"Context still too large ({len(context_str)} chars) after all reductions. Workflow dispatch may fail.")
            # 继续尝试，让 GitHub 返回错误，而不是在这里失败
        else:
            logger.info(f"Context reduced to acceptable size: {len(context_str)} chars")

    url = f"{REST_API}/repos/{CONTROL_REPO}/actions/workflows/llm-bot-runner.yml/dispatches"
    headers = {"Authorization": f"token {GQL_TOKEN}", "Accept": "application/vnd.github.v3+json"}

    # 构建 payload
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

        # 默认轮询间隔（秒），GitHub将通过X-Poll-Interval头部动态调整
        poll_interval = 60

        while True:
            try:
                # 获取未读通知
                r = await client.get(
                    f"{REST_API}/notifications",
                    params={"participating": "true", "all": "false"},
                    headers={"Authorization": f"token {BOT_TOKEN}"}
                )

                if r.status_code == 200:
                    # 解析并更新轮询间隔（遵守GitHub的X-Poll-Interval要求）
                    if "X-Poll-Interval" in r.headers:
                        try:
                            new_interval = int(r.headers["X-Poll-Interval"])
                            if new_interval != poll_interval:
                                logger.info(f"Updating poll interval: {poll_interval} -> {new_interval}s")
                                poll_interval = new_interval
                        except (ValueError, TypeError) as e:
                            logger.warning(f"Invalid X-Poll-Interval header: {r.headers['X-Poll-Interval']}: {e}")

                    notes = r.json()
                    if notes:
                        logger.info(f"Fetched {len(notes)} unread notifications.")
                        tasks = [handle_notification(client, n) for n in notes]
                        await asyncio.gather(*tasks)
                    else:
                        logger.debug("No new notifications.")

                elif r.status_code == 304:
                    logger.debug("No changes in notifications (304).")
                    # 即使返回304，GitHub可能仍然包含X-Poll-Interval头部
                    if "X-Poll-Interval" in r.headers:
                        try:
                            new_interval = int(r.headers["X-Poll-Interval"])
                            if new_interval != poll_interval:
                                logger.info(f"Updating poll interval (304): {poll_interval} -> {new_interval}s")
                                poll_interval = new_interval
                        except (ValueError, TypeError) as e:
                            logger.warning(f"Invalid X-Poll-Interval header (304): {r.headers['X-Poll-Interval']}: {e}")

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

            # 遵守GitHub指定的轮询间隔
            await asyncio.sleep(poll_interval)

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
        "features": ["smart_truncation_3_1", "rich_context", "graphql_enhanced", "dual_token", "test_context_fix", "direct_trigger_task"]
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
