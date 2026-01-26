#!/usr/bin/env python3
"""
接收GitHub webhook并触发LLM机器人工作流的FastAPI服务器。

所需环境变量：
- GITHUB_TOKEN：具有仓库权限的个人访问令牌
- WEBHOOK_SECRET：用于验证的GitHub webhook密钥
- CONTROL_REPO：工作流所在的仓库（例如 "owner/llm-bot-control"）
"""

import os
import hmac
import hashlib
import logging
import json
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
CONTROL_REPO = os.getenv("CONTROL_REPO", "owner/llm-bot-control")

# Bot mentions for filtering comments
BOT_MENTIONS = ("@llm-bot-dev", "@WhiteElephantIsNotARobot")

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

def extract_context_from_event(event_type: str, payload: Dict[str, Any], event_id: str = "") -> TaskContext:
    """从GitHub webhook负载中提取相关上下文。"""
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
        elif event_type == "issue_comment":
            issue = payload.get("issue", {})
            comment = payload.get("comment", {})
            context.issue_number = issue.get("number")
            context.trigger_user = comment.get("user", {}).get("login")
            context.comment_body = comment.get("body")
            context.pr_title = issue.get("title")
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
        elif event_type == "pull_request_review":
            pr = payload.get("pull_request", {})
            review = payload.get("review", {})
            context.issue_number = pr.get("number")
            context.trigger_user = review.get("user", {}).get("login")
            context.comment_body = review.get("body")
            context.pr_title = pr.get("title")
        elif event_type == "pull_request_review_comment":
            pr = payload.get("pull_request", {})
            comment = payload.get("comment", {})
            context.issue_number = pr.get("number")
            context.trigger_user = comment.get("user", {}).get("login")
            context.comment_body = comment.get("body")
            context.pr_title = pr.get("title")
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
    context = extract_context_from_event(event_type, payload, event_id)
    
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