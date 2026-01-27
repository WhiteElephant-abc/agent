import os, json, logging, asyncio
from typing import Dict, List
from fastapi import FastAPI
import httpx

# --- 配置区 ---
GITHUB_API = "https://api.github.com/graphql"
REST_API = "https://api.github.com"

# 双 Token 逻辑
BOT_TOKEN = os.getenv("BOT_TOKEN")      # 机器人的 Token：仅用于读取通知和标记已读
GQL_TOKEN = os.getenv("GQL_TOKEN")      # 你的 PAT：拥有完整权限，用于 GraphQL 和触发 Workflow
CONTROL_REPO = os.getenv("CONTROL_REPO")
ALLOWED_USERS = [u.strip() for u in os.getenv("ALLOWED_USERS", "").split(",") if u.strip()]
BOT_HANDLE = "@WhiteElephantIsNotARobot"
LOG_FILE = os.getenv("PROCESSED_LOG", "/data/processed_notifications.log")

# 启动时加载历史记录
if os.path.exists(LOG_FILE):
    with open(LOG_FILE, "r") as f:
        processed_cache = set(f.read().splitlines())
else:
    processed_cache = set()

# 在 trigger_workflow 成功后写入
async def save_to_log(key: str):
    processed_cache.add(key)
    with open(LOG_FILE, "a") as f:
        f.write(f"{key}\n")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("GQLBot")

app = FastAPI()
processed_cache = set()

# --- GraphQL 查询语句 (保持不变) ---
GQL_UNIVERSAL_QUERY = """
query($url: URI!) {
  resource(url: $url) {
    __typename
    ... on PullRequest {
      title body number
      baseRepository { nameWithOwner }
      timelineItems(last: 30, itemTypes: [ISSUE_COMMENT, PULL_REQUEST_REVIEW, PULL_REQUEST_REVIEW_COMMENT]) {
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
      timelineItems(last: 20, itemTypes: [ISSUE_COMMENT]) {
        nodes { ... on IssueComment { id author { login } body createdAt } }
      }
    }
    ... on Commit {
      message oid
      repository { nameWithOwner }
      comments(last: 20) {
        nodes { id author { login } body path diffHunk createdAt }
      }
    }
    ... on Discussion {
      title body number
      repository { nameWithOwner }
      comments(last: 20) {
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
    
    # 1. 使用具有完整权限的 GQL_TOKEN 执行 GraphQL 反查
    gql_headers = {"Authorization": f"Bearer {GQL_TOKEN}"}
    resp = await client.post(GITHUB_API, json={"query": GQL_UNIVERSAL_QUERY, "variables": {"url": subject_url}}, headers=gql_headers)
    if resp.status_code != 200: return
    
    data = resp.json().get("data", {}).get("resource")
    if not data: return

    # 2. 节点展平与触发者判定
    nodes = []
    if "timelineItems" in data:
        nodes = data["timelineItems"]["nodes"]
    elif "comments" in data:
        nodes = data["comments"]["nodes"]
        if data["__typename"] == "Discussion":
            expanded = []
            for c in nodes:
                expanded.append(c); 
                if c.get("replies"): expanded.extend(c["replies"]["nodes"])
            nodes = expanded

    trigger_node = None
    for node in reversed(nodes):
        if BOT_HANDLE.lower() in (node.get("body") or "").lower():
            trigger_node = node
            break

    if not trigger_node: return

    trigger_user = trigger_node["author"]["login"]
    if ALLOWED_USERS and trigger_user not in ALLOWED_USERS:
        logger.warning(f"Unauthorized: {trigger_user}")
        return

    # 3. 构建上下文
    context = {
        "type": data["__typename"],
        "trigger_user": trigger_user,
        "task_body": trigger_node["body"],
        "diff_context": trigger_node.get("diffHunk") or ""
    }

    # 如果是 PR/Commit 且没有精准 diff，使用 GQL_TOKEN 回退抓取全量
    if not context["diff_context"] and data["__typename"] in ["PullRequest", "Commit"]:
        diff_url = subject_url.replace("/issues/", "/pulls/")
        diff_resp = await client.get(diff_url, headers={"Authorization": f"token {GQL_TOKEN}", "Accept": "application/vnd.github.v3.diff"})
        context["diff_context"] = diff_resp.text[:20000] if diff_resp.status_code == 200 else ""

    # 4. 使用具有完整权限的 GQL_TOKEN 触发 Workflow
    await trigger_workflow(client, context, thread_id)

async def trigger_workflow(client: httpx.AsyncClient, ctx: Dict, thread_id: str):
    dispatch_url = f"{REST_API}/repos/{CONTROL_REPO}/actions/workflows/llm-bot-runner.yml/dispatches"
    # 这里必须使用 GQL_TOKEN
    headers = {"Authorization": f"token {GQL_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    
    payload = {
        "ref": "main",
        "inputs": {
            "task": ctx["task_body"][:4000],
            "context": json.dumps(ctx, ensure_ascii=False)
        }
    }
    r = await client.post(dispatch_url, headers=headers, json=payload)
    if r.status_code == 204:
        logger.info(f"Successfully triggered workflow for {ctx['trigger_user']}")
        # 5. 标记已读动作使用 BOT_TOKEN 即可
        await client.patch(f"{REST_API}/notifications/threads/{thread_id}", 
                           headers={"Authorization": f"token {BOT_TOKEN}"})

async def poll_loop():
    async with httpx.AsyncClient() as client:
        while True:
            try:
                # 轮询通知使用权限较小的 BOT_TOKEN
                r = await client.get(f"{REST_API}/notifications", params={"participating": "true"}, 
                                    headers={"Authorization": f"token {BOT_TOKEN}"})
                if r.status_code == 200:
                    notes = r.json()
                    tasks = [handle_notification(client, n) for n in notes if f"{n['id']}_{n['updated_at']}" not in processed_cache]
                    for n in notes: processed_cache.add(f"{n['id']}_{n['updated_at']}")
                    if tasks: await asyncio.gather(*tasks)
            except Exception as e:
                logger.error(f"Poll loop error: {e}")
            await asyncio.sleep(30)

@app.on_event("startup")
async def startup():
    asyncio.create_task(poll_loop())