#!/usr/bin/env python3
"""
接收GitHub webhook并触发LLM机器人工作流的FastAPI服务器。

所需环境变量：
- GITHUB_TOKEN：具有仓库权限的个人访问令牌
- WEBHOOK_SECRET：用于验证的GitHub webhook密钥
- CONTROL_REPO：工作流所在的仓库（例如 "owner/llm-bot-control"）
- BOT_USER_ID：机器人账号ID（单个，不带@符号）
- ALLOWED_USERS：逗号分隔的授权用户列表（可选，为空则允许所有用户）
- CONTEXT_MAX_CHARS：上下文最大字符数，默认5000（可选）
"""

import os
import hmac
import hashlib
import json
import logging
from typing import Dict, Any, Optional

from fastapi import FastAPI, Request, HTTPException, Header, BackgroundTasks
from pydantic import BaseModel, Field
import httpx

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="LLM Bot Webhook Server")

# GitHub API 配置
GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
CONTROL_REPO = os.getenv("CONTROL_REPO")  # 必须设置，无默认值

# 机器人账号ID（单个，不带@符号）
BOT_USER_ID = os.getenv("BOT_USER_ID")

# 生成机器人提及格式（带@和不带@）
if BOT_USER_ID:
    BOT_MENTIONS = (f"@{BOT_USER_ID}", BOT_USER_ID)  # 同时检查带@和不带@的提及
else:
    BOT_MENTIONS = tuple()

# 授权用户列表（逗号分隔）
ALLOWED_USERS = [user.strip() for user in os.getenv("ALLOWED_USERS", "").split(",") if user.strip()]

# 上下文最大字符数
CONTEXT_MAX_CHARS = int(os.getenv("CONTEXT_MAX_CHARS", "5000"))

# GitHub API 请求头
headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "User-Agent": "LLM-Bot-Server"
}

# 环境变量验证
if not GITHUB_TOKEN:
    logger.warning("GITHUB_TOKEN environment variable is not set. Workflow triggering will fail.")
if not WEBHOOK_SECRET:
    logger.warning("WEBHOOK_SECRET environment variable is not set. Webhook signature verification is disabled.")
if not CONTROL_REPO:
    logger.warning("CONTROL_REPO environment variable is not set. Workflow triggering will fail.")
if not BOT_USER_ID:
    logger.warning("BOT_USER_ID environment variable is not set. Bot mention filtering will not work.")

# 用户验证函数
def is_user_authorized(username: Optional[str]) -> bool:
    """验证触发者是否在授权用户列表中。"""
    if not username:
        return False
    if not ALLOWED_USERS:  # 如果ALLOWED_USERS为空，则允许所有用户
        return True
    return username in ALLOWED_USERS

class TaskContext(BaseModel):
    """从webhook事件中提取的上下文数据。"""
    repo: str = Field(..., description="Repository full name (owner/repo)")
    event_type: str = Field(..., description="GitHub event type")
    event_id: str = Field(..., description="GitHub event ID")
    trigger_user: Optional[str] = Field(None, description="User who triggered the event")
    issue_number: Optional[int] = Field(None, description="Issue/PR number if applicable")
    comment_body: Optional[str] = Field(None, description="Comment body if applicable")
    pr_title: Optional[str] = Field(None, description="PR/Issue title if applicable")
    pr_body: Optional[str] = Field(None, description="PR body if applicable")
    pr_diff_url: Optional[str] = Field(None, description="PR diff URL if applicable")
    discussion_title: Optional[str] = Field(None, description="Discussion title if applicable")
    discussion_body: Optional[str] = Field(None, description="Discussion body if applicable")
    # 新增字段
    issue_body: Optional[str] = Field(None, description="Issue body content")
    comments_history: Optional[list] = Field(None, description="List of comments with timestamps")
    reviews_history: Optional[list] = Field(None, description="List of reviews with timestamps")
    review_comments_batch: Optional[list] = Field(None, description="Batch of review comments for specific review")
    current_comment_id: Optional[int] = Field(None, description="Current comment ID if applicable")
    current_review_id: Optional[int] = Field(None, description="Current review ID if applicable")
    is_mention_in_body: Optional[bool] = Field(None, description="Whether bot is mentioned in issue/PR body")
    is_mention_in_review: Optional[bool] = Field(None, description="Whether bot is mentioned in review body")
    is_truncated: Optional[bool] = Field(None, description="Whether context was truncated due to length limits")

    def to_json_string(self) -> str:
        """Convert context to JSON string for passing to workflow."""
        return json.dumps(self.model_dump(), ensure_ascii=False)

def verify_webhook_signature(payload_body: bytes, signature_header: str) -> bool:
    """验证GitHub webhook签名。"""
    if not WEBHOOK_SECRET:
        logger.warning("WEBHOOK_SECRET not set, skipping signature verification")
        return True
    
    try:
        hash_object = hmac.new(
            WEBHOOK_SECRET.encode(),
            msg=payload_body,
            digestmod=hashlib.sha256
        )
        expected_signature = f"sha256={hash_object.hexdigest()}"
        return hmac.compare_digest(expected_signature, signature_header)
    except Exception as e:
        logger.error(f"Error verifying signature: {e}")
        return False

async def fetch_issue_comments(repo: str, issue_number: int) -> list:
    """获取issue/PR的所有评论。"""
    url = f"{GITHUB_API}/repos/{repo}/issues/{issue_number}/comments"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers, timeout=30.0)
            response.raise_for_status()
            comments = response.json()
            # 按创建时间排序，最早的在前
            sorted_comments = sorted(comments, key=lambda x: x.get("created_at", ""))
            return sorted_comments
        except Exception as e:
            logger.error(f"Error fetching comments for {repo}#{issue_number}: {e}")
            return []

async def fetch_pr_reviews(repo: str, pr_number: int) -> list:
    """获取PR的所有review。"""
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/reviews"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers, timeout=30.0)
            response.raise_for_status()
            reviews = response.json()
            # 按提交时间排序，最早的在前
            sorted_reviews = sorted(reviews, key=lambda x: x.get("submitted_at", ""))
            return sorted_reviews
        except Exception as e:
            logger.error(f"Error fetching reviews for {repo}#{pr_number}: {e}")
            return []

async def fetch_specific_review_comments(repo: str, pr_number: int, review_id: int) -> list:
    """获取特定review_id下的所有行内评论。"""
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/reviews/{review_id}/comments"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers, timeout=30.0)
            response.raise_for_status()
            comments = response.json()
            # 按时间排序，最早的在前
            sorted_comments = sorted(comments, key=lambda x: x.get("created_at", ""))
            return sorted_comments
        except Exception as e:
            logger.error(f"Error fetching specific review comments for review {review_id}: {e}")
            return []

def truncate_context_by_chars(items: list, max_chars: int) -> tuple[list, bool]:
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
                item_text = str(items[right_ptr])
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
            item_text = str(items[left_ptr])
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

    for i in range(len(sorted_indices)):
        idx = sorted_indices[i]
        result.append(items[idx])

        # 插入截断声明 (Gap Notice)
        if i < len(sorted_indices) - 1:
            next_idx = sorted_indices[i+1]
            if next_idx > idx + 1:
                omitted = next_idx - idx - 1
                result.append(json.dumps({
                    "type": "system_notice",
                    "content": f"--- [此处省略了中间 {omitted} 条历史评论] ---"
                }))

    is_truncated = len(selected_indices) < len(items)
    return result, is_truncated

async def extract_context_from_event(event_type: str, payload: Dict[str, Any], event_id: str = "") -> TaskContext:
    """从GitHub webhook负载中提取相关上下文，包括历史数据。"""
    repo = payload.get("repository", {}).get("full_name", "")

    context = TaskContext(
        repo=repo,
        event_type=event_type,
        event_id=event_id
    )

    # Extract based on event type
    try:
        if event_type == "issues":
            issue = payload.get("issue", {})
            context.issue_number = issue.get("number")
            context.trigger_user = issue.get("user", {}).get("login")
            context.comment_body = issue.get("body")
            context.pr_title = issue.get("title")  # issues也有title字段
            context.issue_body = issue.get("body")

            # 检查是否在body中提及机器人
            body_text = issue.get("body", "")
            context.is_mention_in_body = any(mention in body_text for mention in BOT_MENTIONS) if body_text else False

        elif event_type == "issue_comment":
            issue = payload.get("issue", {})
            comment = payload.get("comment", {})
            context.issue_number = issue.get("number")
            context.trigger_user = comment.get("user", {}).get("login")
            context.comment_body = comment.get("body")
            context.pr_title = issue.get("title")
            context.current_comment_id = comment.get("id")

            # 获取issue body
            context.issue_body = issue.get("body")

            # 检查是否在body中提及机器人
            body_text = issue.get("body", "")
            context.is_mention_in_body = any(mention in body_text for mention in BOT_MENTIONS) if body_text else False

            # 获取评论历史并截断
            if context.issue_number:
                all_comments = await fetch_issue_comments(repo, context.issue_number)
                if all_comments:
                    # 提取评论文本用于截断
                    comment_texts = []
                    for c in all_comments:
                        comment_data = {
                            "id": c.get("id"),
                            "user": c.get("user", {}).get("login"),
                            "body": c.get("body"),
                            "created_at": c.get("created_at")
                        }
                        comment_texts.append(json.dumps(comment_data))

                    truncated, is_truncated_flag = truncate_context_by_chars(comment_texts, CONTEXT_MAX_CHARS)
                    # 解析回结构化数据
                    context.comments_history = [json.loads(item) for item in truncated]
                    # 更新截断标志
                    context.is_truncated = context.is_truncated or is_truncated_flag

        elif event_type == "pull_request":
            pr = payload.get("pull_request", {})
            context.issue_number = pr.get("number")
            context.trigger_user = pr.get("user", {}).get("login")
            context.pr_title = pr.get("title")
            context.pr_body = pr.get("body")
            # GitHub webhook payload中的diff_url可能不存在, construct from html_url
            html_url = pr.get("html_url")
            context.pr_diff_url = pr.get("diff_url")
            if not context.pr_diff_url and html_url:
                context.pr_diff_url = f"{html_url}.diff"

            # 检查是否在body中提及机器人
            body_text = pr.get("body", "")
            context.is_mention_in_body = any(mention in body_text for mention in BOT_MENTIONS) if body_text else False

        elif event_type == "pull_request_review":
            pr = payload.get("pull_request", {})
            review = payload.get("review", {})
            context.issue_number = pr.get("number")
            context.trigger_user = review.get("user", {}).get("login")
            context.comment_body = review.get("body")
            context.pr_title = pr.get("title")
            context.current_review_id = review.get("id")

            # 获取PR body
            context.pr_body = pr.get("body")

            # 检查是否在body中提及机器人
            body_text = pr.get("body", "")
            context.is_mention_in_body = any(mention in body_text for mention in BOT_MENTIONS) if body_text else False

            # 检查是否在review body中提及机器人
            review_body = review.get("body", "")
            context.is_mention_in_review = any(mention in review_body for mention in BOT_MENTIONS) if review_body else False

            # 检查webhook action，只处理"submitted"动作，避免重复触发
            action = payload.get("action", "")
            if action == "submitted" and context.is_mention_in_review:
                # 机器人被提及在当前review中，获取该批次的详细评论
                if context.issue_number and context.current_review_id:
                    batch_comments = await fetch_specific_review_comments(
                        repo, context.issue_number, context.current_review_id
                    )
                    if batch_comments:
                        context.review_comments_batch = []
                        for c in batch_comments:
                            comment_data = {
                                "id": c.get("id"),
                                "user": c.get("user", {}).get("login"),
                                "body": c.get("body"),
                                "path": c.get("path"),
                                "position": c.get("position"),
                                "created_at": c.get("created_at")
                            }
                            context.review_comments_batch.append(comment_data)

            # 获取review历史（无论是否提及都获取）
            if context.issue_number:
                all_reviews = await fetch_pr_reviews(repo, context.issue_number)
                if all_reviews:
                    # 提取review文本用于截断
                    review_texts = []
                    for r in all_reviews:
                        review_data = {
                            "id": r.get("id"),
                            "user": r.get("user", {}).get("login"),
                            "body": r.get("body"),
                            "state": r.get("state"),
                            "submitted_at": r.get("submitted_at")
                        }
                        review_texts.append(json.dumps(review_data))

                    truncated, is_truncated_flag = truncate_context_by_chars(review_texts, CONTEXT_MAX_CHARS)
                    # 解析回结构化数据
                    context.reviews_history = [json.loads(item) for item in truncated]
                    # 更新截断标志
                    context.is_truncated = context.is_truncated or is_truncated_flag

        elif event_type == "pull_request_review_comment":
            pr = payload.get("pull_request", {})
            comment = payload.get("comment", {})
            context.issue_number = pr.get("number")
            context.trigger_user = comment.get("user", {}).get("login")
            context.comment_body = comment.get("body")
            context.pr_title = pr.get("title")
            context.current_comment_id = comment.get("id")

            # 获取PR body
            context.pr_body = pr.get("body")

            # 检查是否在body中提及机器人
            body_text = pr.get("body", "")
            context.is_mention_in_body = any(mention in body_text for mention in BOT_MENTIONS) if body_text else False

            # 检查是否在当前评论中提及机器人
            comment_text = comment.get("body", "")
            is_mention_in_comment = any(mention in comment_text for mention in BOT_MENTIONS) if comment_text else False

            # 获取评论历史和review历史（无论是否提及都获取，但只有提及时才触发工作流）
            if context.issue_number and is_mention_in_comment:
                # 获取PR评论
                all_comments = await fetch_issue_comments(repo, context.issue_number)
                if all_comments:
                    comment_texts = []
                    for c in all_comments:
                        comment_data = {
                            "id": c.get("id"),
                            "user": c.get("user", {}).get("login"),
                            "body": c.get("body"),
                            "created_at": c.get("created_at")
                        }
                        comment_texts.append(json.dumps(comment_data))

                    truncated, is_truncated_flag = truncate_context_by_chars(comment_texts, CONTEXT_MAX_CHARS)
                    context.comments_history = [json.loads(item) for item in truncated]
                    # 更新截断标志
                    context.is_truncated = context.is_truncated or is_truncated_flag

                # 获取review历史
                all_reviews = await fetch_pr_reviews(repo, context.issue_number)
                if all_reviews:
                    review_texts = []
                    for r in all_reviews:
                        review_data = {
                            "id": r.get("id"),
                            "user": r.get("user", {}).get("login"),
                            "body": r.get("body"),
                            "state": r.get("state"),
                            "submitted_at": r.get("submitted_at")
                        }
                        review_texts.append(json.dumps(review_data))

                    truncated, is_truncated_flag = truncate_context_by_chars(review_texts, CONTEXT_MAX_CHARS)
                    context.reviews_history = [json.loads(item) for item in truncated]
                    # 更新截断标志
                    context.is_truncated = context.is_truncated or is_truncated_flag

        elif event_type == "discussion":
            discussion = payload.get("discussion", {})
            context.trigger_user = discussion.get("user", {}).get("login")
            context.discussion_title = discussion.get("title")
            context.discussion_body = discussion.get("body")
    except Exception as e:
        logger.error(f"Error extracting context from {event_type} event: {e}")

    return context

def generate_task_description(event_type: str, context: TaskContext) -> str:
    """根据事件类型生成自然语言任务描述。"""
    if event_type == "pull_request":
        title_suffix = f" - {context.pr_title}" if context.pr_title else ""
        return f"Review PR #{context.issue_number}{title_suffix} in {context.repo} and provide feedback or code improvements."
    elif event_type == "pull_request_review":
        return f"Review the review comment on PR #{context.issue_number} in {context.repo} and respond appropriately."
    elif event_type == "pull_request_review_comment":
        if context.comment_body and any(mention in context.comment_body for mention in BOT_MENTIONS):
            return f"Respond to review comment on PR #{context.issue_number} in {context.repo}."
        # Only process comments that mention the bot, otherwise return a skip indicator
        return f"SKIP: Review comment on PR #{context.issue_number} in {context.repo} does not mention the bot."
    elif event_type == "issue_comment":
        if context.comment_body and any(mention in context.comment_body for mention in BOT_MENTIONS):
            return f"Respond to comment on issue #{context.issue_number} in {context.repo}."
        # Only process comments that mention the bot, otherwise return a skip indicator
        return f"SKIP: Comment on issue #{context.issue_number} in {context.repo} does not mention the bot."
    elif event_type == "issues":
        title_suffix = f" - {context.pr_title}" if context.pr_title else ""
        return f"Analyze new issue #{context.issue_number}{title_suffix} in {context.repo} and provide initial response."
    elif event_type == "discussion":
        return f"Participate in discussion '{context.discussion_title}' in {context.repo}."
    else:
        return f"Handle {event_type} event in {context.repo}."

async def trigger_workflow_dispatch(task: str, context: TaskContext):
    """在控制仓库中触发 workflow_dispatch 事件。"""
    # Skip if task starts with "SKIP:"
    if task.startswith("SKIP:"):
        logger.info(f"Skipping workflow dispatch: {task}")
        return True

    url = f"{GITHUB_API}/repos/{CONTROL_REPO}/actions/workflows/llm-bot-runner.yml/dispatches"

    payload = {
        "ref": "main",
        "inputs": {
            "task": task,
            "context": context.to_json_string()
        }
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, headers=headers, json=payload, timeout=30.0)
            response.raise_for_status()
            logger.info(f"Workflow dispatch triggered successfully: {response.status_code}")
            return True
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error triggering workflow: {e.response.status_code} - {e.response.text}")
            return False
        except Exception as e:
            logger.error(f"Error triggering workflow: {e}")
            return False

@app.post("/webhook")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: Optional[str] = Header(None),
    x_github_event: Optional[str] = Header(None),
    x_github_delivery: Optional[str] = Header(None)
):
    """GitHub webhooks 端点。"""
    # 读取原始请求体用于签名验证
    body_bytes = await request.body()
    
    # 如果配置了密钥则验证签名
    if x_hub_signature_256 and not verify_webhook_signature(body_bytes, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    
    # 解析JSON负载
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    
    event_type = x_github_event or "unknown"
    logger.info(f"Received {event_type} event for {payload.get('repository', {}).get('full_name', 'unknown')}")
    
    # 从负载中提取上下文
    event_id = x_github_delivery or ""
    context = await extract_context_from_event(event_type, payload, event_id)

    # 验证触发者权限
    if not is_user_authorized(context.trigger_user):
        logger.warning(f"User {context.trigger_user} is not authorized to trigger bot")
        return {
            "status": "rejected",
            "event": event_type,
            "repo": context.repo,
            "reason": f"User {context.trigger_user} is not in allowed list",
            "allowed_users": ALLOWED_USERS
        }

    # 生成任务描述
    task = generate_task_description(event_type, context)

    # 在后台触发工作流
    background_tasks.add_task(trigger_workflow_dispatch, task, context)

    return {
        "status": "processing",
        "event": event_type,
        "repo": context.repo,
        "task": task
    }

@app.get("/health")
async def health_check():
    """健康检查端点。"""
    return {"status": "healthy", "service": "llm-bot-webhook-server"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)