import os, json, logging, asyncio
from typing import Dict, List
from fastapi import FastAPI
import httpx

# --- 配置区 ---
GITHUB_API = "https://api.github.com/graphql"
REST_API = "https://api.github.com"

BOT_TOKEN = os.getenv("BOT_TOKEN")      
GQL_TOKEN = os.getenv("GQL_TOKEN")      
CONTROL_REPO = os.getenv("CONTROL_REPO")
ALLOWED_USERS = [u.strip() for u in os.getenv("ALLOWED_USERS", "").split(",") if u.strip()]
BOT_HANDLE = "@WhiteElephantIsNotARobot"
LOG_FILE = os.getenv("PROCESSED_LOG", "/data/processed_notifications.log")

# --- 日志配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("GQLBot")

# --- 持久化逻辑 ---
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

if os.path.exists(LOG_FILE):
    with open(LOG_FILE, "r") as f:
        processed_cache = {line.strip() for line in f if line.strip()}
    logger.info(f"Loaded {len(processed_cache)} processed IDs from {LOG_FILE}")
else:
    processed_cache = set()
    logger.info("No log file found, starting with empty cache.")

async def save_to_log(node_id: str):
    if node_id not in processed_cache:
        processed_cache.add(node_id)
        try:
            with open(LOG_FILE, "a") as f:
                f.write(f"{node_id}\n")
            logger.info(f"Logged node_id: {node_id}")
        except Exception as e:
            logger.error(f"Failed to write log file: {e}")

app = FastAPI()
# 【修正】删除了此处重复的 processed_cache = set()

# --- GraphQL 查询语句 ---
GQL_UNIVERSAL_QUERY = """
query($url: URI!) {
  resource(url: $url) {
    __typename
    ... on PullRequest {
      title body number
      baseRepository { nameWithOwner }
      timelineItems(last: 50, itemTypes: [ISSUE_COMMENT, PULL_REQUEST_REVIEW, PULL_REQUEST_REVIEW_COMMENT]) {
        nodes {
          __typename
          ... on IssueComment { id author { login } body createdAt }
          ... on PullRequestReview { id author { login } body createdAt }
          ... on PullRequestReviewComment { 
            id author { login } body createdAt 
            pullRequestReview { id } 
            path diffHunk 
          }
        }
      }
    }
    ... on Issue {
      title body number
      baseRepository { nameWithOwner }
      timelineItems(last: 30, itemTypes: [ISSUE_COMMENT]) {
        nodes { ... on IssueComment { id author { login } body createdAt } }
      }
    }
    ... on Commit {
      message oid
      repository { nameWithOwner }
      comments(last: 30) {
        nodes { id author { login } body path diffHunk createdAt }
      }
    }
    ... on Discussion {
      title body number
      repository { nameWithOwner }
      comments(last: 30) {
        nodes { 
          id author { login } body createdAt
          replies(last: 10) { nodes { id author { login } body createdAt } }
        }
      }
    }
  }
}
"""

async def handle_notification(client: httpx.AsyncClient, note: Dict):
    thread_id = note["id"]
    subject_url = note["subject"]["url"]
    logger.info(f"Processing notification: {note['subject']['title']} ({subject_url})")

    gql_headers = {"Authorization": f"Bearer {GQL_TOKEN}"}
    try:
        resp = await client.post(GITHUB_API, json={"query": GQL_UNIVERSAL_QUERY, "variables": {"url": subject_url}}, headers=gql_headers)
        if resp.status_code != 200:
            logger.error(f"GQL HTTP Error {resp.status_code}: {resp.text}")
            return
        
        data = resp.json().get("data", {}).get("resource")
        if not data:
            logger.warning(f"No resource found for URL: {subject_url}")
            return
    except Exception as e:
        logger.error(f"Exception during GQL call: {e}")
        return

    nodes = []
    if "timelineItems" in data:
        nodes = data["timelineItems"]["nodes"]
    elif "comments" in data:
        nodes = data["comments"]["nodes"]
        if data["__typename"] == "Discussion":
            expanded = []
            for c in nodes:
                expanded.append(c)
                if c.get("replies"): expanded.extend(c["replies"]["nodes"])
            nodes = expanded

    # 过滤掉空节点并匹配
    new_mentions = [
        n for n in nodes 
        if n and n.get("body") and BOT_HANDLE.lower() in n["body"].lower() 
        and n.get("id") not in processed_cache
    ]

    logger.info(f"Found {len(nodes)} total nodes, {len(new_mentions)} new mentions.")

    if not new_mentions:
        # 如果确实没搜到指令，才标记已读。建议调试阶段先注释掉下面这行，防止吞通知。
        # await client.patch(f"{REST_API}/notifications/threads/{thread_id}", headers={"Authorization": f"token {BOT_TOKEN}"})
        return

    for node in new_mentions:
        trigger_user = node["author"]["login"]
        if ALLOWED_USERS and trigger_user not in ALLOWED_USERS:
            logger.warning(f"User {trigger_user} not in ALLOWED_USERS. Skipping.")
            continue

        context = {
            "type": data["__typename"],
            "node_id": node["id"],
            "trigger_user": trigger_user,
            "task_body": node["body"],
            "diff_context": node.get("diffHunk") or ""
        }

        if not context["diff_context"] and data["__typename"] in ["PullRequest", "Commit"]:
            diff_url = subject_url.replace("/issues/", "/pulls/")
            try:
                dr = await client.get(diff_url, headers={"Authorization": f"token {GQL_TOKEN}", "Accept": "application/vnd.github.v3.diff"})
                if dr.status_code == 200: context["diff_context"] = dr.text[:20000]
            except Exception: pass

        success = await trigger_workflow(client, context, thread_id)
        if success:
            await save_to_log(node["id"])

async def trigger_workflow(client: httpx.AsyncClient, ctx: Dict, thread_id: str) -> bool:
    url = f"{REST_API}/repos/{CONTROL_REPO}/actions/workflows/llm-bot-runner.yml/dispatches"
    headers = {"Authorization": f"token {GQL_TOKEN}", "Accept": "application/vnd.github.v3+json"}

    payload = {
        "ref": "main",
        "inputs": {
            "task": ctx["task_body"][:4000],
            "context": json.dumps(ctx, ensure_ascii=False)
        }
    }
    
    r = await client.post(url, headers=headers, json=payload)
    if r.status_code == 204:
        logger.info(f"Successfully triggered Action for Node {ctx['node_id']}")
        # 触发成功后再标记已读
        await client.patch(f"{REST_API}/notifications/threads/{thread_id}", headers={"Authorization": f"token {BOT_TOKEN}"})
        return True
    else:
        logger.error(f"Workflow dispatch failed ({r.status_code}): {r.text}")
        return False

async def poll_loop():
    async with httpx.AsyncClient() as client:
        logger.info("Poll loop started...")
        while True:
            try:
                r = await client.get(f"{REST_API}/notifications", params={"participating": "true"}, 
                                    headers={"Authorization": f"token {BOT_TOKEN}"})
                if r.status_code == 200:
                    notes = r.json()
                    if notes:
                        logger.info(f"Fetched {len(notes)} unread notifications.")
                        tasks = [handle_notification(client, n) for n in notes]
                        await asyncio.gather(*tasks)
                elif r.status_code != 304:
                    logger.error(f"Notification API Error {r.status_code}: {r.text}")
            except Exception as e:
                logger.error(f"Poll loop exception: {e}")
            await asyncio.sleep(30)

@app.on_event("startup")
async def startup():
    asyncio.create_task(poll_loop())