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

# --- 持久化逻辑 ---
# 确保目录存在
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

# 启动时从文件加载历史记录
if os.path.exists(LOG_FILE):
    with open(LOG_FILE, "r") as f:
        # 使用 set 存储已处理的原子节点 ID (Node ID)
        processed_cache = {line.strip() for line in f if line.strip()}
else:
    processed_cache = set()

async def save_to_log(node_id: str):
    """保存成功触发的任务节点 ID"""
    if node_id not in processed_cache:
        processed_cache.add(node_id)
        with open(LOG_FILE, "a") as f:
            f.write(f"{node_id}\n")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("GQLBot")

app = FastAPI()
# 注意：这里不再重新初始化 processed_cache = set()，以免覆盖加载的数据

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

    # 1. 使用具有完整权限的 GQL_TOKEN 执行 GraphQL 反查
    gql_headers = {"Authorization": f"Bearer {GQL_TOKEN}"}
    try:
        resp = await client.post(GITHUB_API, json={"query": GQL_UNIVERSAL_QUERY, "variables": {"url": subject_url}}, headers=gql_headers)
        if resp.status_code != 200: return
        data = resp.json().get("data", {}).get("resource")
        if not data: return
    except Exception as e:
        logger.error(f"GQL Error for {subject_url}: {e}")
        return

    # 2. 节点展平
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

    # 3. 找出【所有】包含 @ 且【未处理过】的节点
    # 不再只找最后一条，而是遍历所有节点
    new_mentions = [
        n for n in nodes 
        if n and BOT_HANDLE.lower() in (n.get("body") or "").lower() 
        and n.get("id") not in processed_cache
    ]

    if not new_mentions:
        # 如果没有新指令，尝试标记已读以清理无意义通知
        await client.patch(f"{REST_API}/notifications/threads/{thread_id}", 
                           headers={"Authorization": f"token {BOT_TOKEN}"})
        return

    # 4. 逐一处理每一处不同的 @
    for node in new_mentions:
        trigger_user = node["author"]["login"]
        if ALLOWED_USERS and trigger_user not in ALLOWED_USERS:
            logger.warning(f"Unauthorized user {trigger_user} in node {node['id']}")
            continue

        # 构建该节点的专属上下文
        context = {
            "type": data["__typename"],
            "node_id": node["id"],
            "trigger_user": trigger_user,
            "task_body": node["body"],
            "diff_context": node.get("diffHunk") or ""
        }

        # 如果没有精准 diff 且是代码相关，回退抓取全量 Diff
        if not context["diff_context"] and data["__typename"] in ["PullRequest", "Commit"]:
            diff_url = subject_url.replace("/issues/", "/pulls/")
            try:
                diff_resp = await client.get(diff_url, headers={"Authorization": f"token {GQL_TOKEN}", "Accept": "application/vnd.github.v3.diff"})
                if diff_resp.status_code == 200:
                    context["diff_context"] = diff_resp.text[:20000]
            except Exception: pass

        # 5. 触发 Workflow
        success = await trigger_workflow(client, context, thread_id)
        if success:
            # 只有成功后才记录该 Node ID，防止因网络波动漏掉任务
            await save_to_log(node["id"])

async def trigger_workflow(client: httpx.AsyncClient, ctx: Dict, thread_id: str) -> bool:
    dispatch_url = f"{REST_API}/repos/{CONTROL_REPO}/actions/workflows/llm-bot-runner.yml/dispatches"
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
        logger.info(f"Triggered workflow for node {ctx['node_id']} by {ctx['trigger_user']}")
        # 标记已读 (针对该通知 Thread)
        await client.patch(f"{REST_API}/notifications/threads/{thread_id}", 
                           headers={"Authorization": f"token {BOT_TOKEN}"})
        return True
    else:
        logger.error(f"Failed to trigger workflow: {r.status_code} {r.text}")
        return False

async def poll_loop():
    async with httpx.AsyncClient() as client:
        while True:
            try:
                # 轮询未读通知，不再在这一步做 cache 判定，全部交给 handle_notification 深入检查
                r = await client.get(f"{REST_API}/notifications", params={"participating": "true"}, 
                                    headers={"Authorization": f"token {BOT_TOKEN}"})
                if r.status_code == 200:
                    notes = r.json()
                    # 只要是未读通知，就去扫描内部是否有未处理的 Node
                    tasks = [handle_notification(client, n) for n in notes]
                    if tasks:
                        await asyncio.gather(*tasks)
            except Exception as e:
                logger.error(f"Poll loop error: {e}")
            await asyncio.sleep(30)

@app.on_event("startup")
async def startup():
    asyncio.create_task(poll_loop())