# GitHub AI Agent - WhiteElephantIsNotARobot

> [!WARNING]
>
> 此文档由 @WhiteElephantIsNotARobot 撰写，可能存在错误！

一个基于 Claude Code 的 GitHub AI Agent，能够自动处理 GitHub 通知、审查代码、修复问题，并通过 GitHub Actions 工作流执行任务。

## 项目概述

这是一个 AI 驱动的 GitHub 协作机器人，它监听 GitHub 通知（issues、PRs、discussions），当被 `@WhiteElephantIsNotARobot` 提及时，会自动触发 Claude Code 工作流来处理任务。

### 核心功能

- **智能通知处理**：监听 GitHub 通知，只处理被提及（mention）的事件
- **丰富的上下文构建**：自动收集 PR/Issue/Discussion 的完整上下文，包括：
  - 评论历史（智能截断算法）
  - 代码审查（reviews）和行内评论
  - PR diff 内容
  - 分支信息（head/base）
- **双 Token 架构**：使用不同的 token 分别处理通知读取和 GraphQL 查询
- **智能截断算法**：3:1 比例（3条新评论 + 1条旧评论）保留最有价值的上下文
- **GitHub Actions 集成**：通过 workflow dispatch 触发 AI 工作流

## 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                    GitHub Notifications                      │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                    FastAPI Server (server.py)                │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  • 轮询 GitHub 通知 (poll_loop)                      │   │
│  │  • 构建丰富上下文 (build_rich_context)               │   │
│  │  • 智能截断算法 (truncate_context_by_chars)          │   │
│  │  • GraphQL 查询 (fetch_resource_details)             │   │
│  └──────────────────────────────────────────────────────┘   │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│              GitHub Actions Workflow                         │
│  llm-bot-runner.yml                                         │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  1. 创建 Issue 记录任务                              │   │
│  │  2. 安装 Claude Code CLI                            │   │
│  │  3. 配置 MCP 服务器 (DuckDuckGo, Context7)          │   │
│  │  4. 运行 Claude Code 执行任务                       │   │
│  │  5. 自动提交/推送更改，创建 PR                       │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

## 项目结构

```
agent/
├── server.py                    # FastAPI 服务器主程序
├── system_prompt.md            # Claude Code 系统提示词
├── requirements.txt            # Python 依赖
├── .gitignore
├── LICENSE                     # GPL-3.0 许可证
├── README.md                   # 本文档
├── .github/
│   └── workflows/
│       ├── llm-bot-runner.yml  # AI 工作流触发器
│       └── opencode.yml        # /oc 命令触发器
└── doc/
    ├── overview.md            # Claude Code 概览文档
    ├── iam.md                 # 身份和访问管理文档
    ├── settings.md            # 设置配置文档
    └── headless.md            # 无头模式运行文档
```

## 核心组件详解

### 1. FastAPI 服务器 (`server.py`)

服务器提供以下端点：

- `GET /health` - 健康检查，返回服务状态和特性列表
- `GET /stats` - 统计信息，包括已处理通知数和日志文件大小

**主要功能**：
- 轮询 GitHub 通知 API
- 使用 GraphQL 获取 PR/Issue/Discussion 的完整上下文
- 构建 `TaskContext` 数据模型传递给工作流
- 触发 GitHub Actions workflow_dispatch

### 2. 智能截断算法

当评论历史过长时，使用 **3新1老** 算法保留最有价值的内容：

```
迭代 1: 取最新3条评论 + 最旧1条评论
迭代 2: 取次新3条评论 + 次旧1条评论
...
```

如果超出字符限制，会：
1. 撤销本次添加
2. 锁死该侧（新/旧）
3. 继续从另一侧抓取

### 3. 双 Token 架构

- **BOT_TOKEN**：仅用于读取通知和标记已读
- **GQL_TOKEN**：用于 GraphQL 查询和触发 Workflow

### 4. 上下文数据模型 (`TaskContext`)

包含丰富的任务上下文信息（基于 `pydantic.BaseModel`）：

```python
from pydantic import BaseModel
from typing import Optional, List, Dict

class TaskContext(BaseModel):
    # 基础信息
    repo: str                                   # 仓库名 (owner/repo)
    event_type: str                             # 事件类型 (pr/issue/discussion)
    event_id: str                               # 通知 ID
    trigger_user: Optional[str] = None          # 触发工作流的用户名
    issue_number: Optional[int] = None          # Issue/PR 编号

    # 标题和正文
    title: Optional[str] = None                 # Issue/Discussion 标题
    issue_body: Optional[str] = None            # Issue 正文
    pr_title: Optional[str] = None              # PR 标题
    pr_body: Optional[str] = None               # PR 正文
    discussion_title: Optional[str] = None      # Discussion 标题
    discussion_body: Optional[str] = None       # Discussion 正文

    # 历史数据
    comments_history: Optional[List[Dict]] = None      # 普通评论历史
    reviews_history: Optional[List[Dict]] = None       # 审核记录历史
    review_comments_batch: Optional[List[Dict]] = None # 行内代码评论

    # 代码上下文
    diff_content: Optional[str] = None          # PR diff 内容
    diff_url: Optional[str] = None              # PR diff 的 URL
    clone_url: Optional[str] = None             # SSH 克隆地址
    head_ref: Optional[str] = None              # head 分支
    base_ref: Optional[str] = None              # base 分支
    head_repo: Optional[str] = None             # head repo:branch 格式
    base_repo: Optional[str] = None             # base repo:branch 格式
    commit_sha: Optional[str] = None            # Commit SHA

    # 元数据
    current_comment_id: Optional[str] = None    # 触发评论的 ID
    current_review_id: Optional[str] = None     # 触发审核的 ID
    is_mention_in_body: Optional[bool] = None   # 是否在正文中被提及
    is_mention_in_review: Optional[bool] = None # 是否在审核中被提及
    is_truncated: Optional[bool] = None         # 上下文是否被截断
    latest_comment_url: Optional[str] = None    # 最新评论的 URL
```

## 环境变量配置

### 必需环境变量

| 变量名 | 说明 |
|--------|------|
| `BOT_TOKEN` | GitHub Bot Token (读取通知) |
| `GQL_TOKEN` | GitHub Personal Access Token (GraphQL/Workflow) |
| `CONTROL_REPO` | 触发工作流的目标仓库 (owner/repo) |
| `SYSTEM_PROMPT` | Claude Code 系统提示词 |

### 可选环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `ALLOWED_USERS` | 无限制 | 允许触发的用户列表 (逗号分隔) |
| `PROCESSED_LOG` | `/data/processed_notifications.log` | 已处理通知日志路径 |
| `CONTEXT_MAX_CHARS` | 15000 | 上下文最大字符数 |
| `DIFF_MAX_CHARS` | 4000 | Diff 内容最大字符数 |
| `PORT` | 8000 | FastAPI 服务器端口 |

## GitHub Actions 工作流

### llm-bot-runner.yml

当通过 `workflow_dispatch` 触发时，执行以下步骤：

1. **创建 Issue**：记录任务和上下文
2. **安装 Claude CLI**：从官方脚本安装
3. **配置 MCP 服务器**：
   - **MCP (Model Context Protocol)**：模型上下文协议，用于扩展 AI 模型的能力
   - DuckDuckGo 搜索 (`ddg-search`)：提供网络搜索能力
   - Context7 文档 (`context7`)：提供实时文档查询能力
4. **预配置 Git**：
   - 用户名/邮箱
   - SSH 认证（可选）
   - GPG 签名（可选）
5. **运行 Claude Code**：
   - 读取 `/app/context.json` 上下文
   - 执行任务
   - 自动提交/推送更改
   - 创建 PR（如果修改了代码）

### opencode.yml

通过 `/oc` 或 `/opencode` 命令触发，使用 DeepSeek 模型处理任务。

## 使用方式

### 1. 通过 GitHub 提及触发

在 GitHub Issue/PR/Discussion 中评论：
```
@WhiteElephantIsNotARobot 请审查这个 PR
```

机器人会：
1. 读取通知
2. 构建完整上下文
3. 触发 GitHub Actions 工作流
4. AI 代理处理任务并回复

### 2. 通过 GitHub Actions 手动触发

```bash
gh workflow run llm-bot-runner.yml \
  -f task="修复登录页面的 bug" \
  -f context='{"repo": "owner/repo", "event_type": "issue", ...}'
```

### 3. 通过 `/oc` 命令触发

在 PR 评论中输入：
```
/oc 请审查这段代码
```

## API 端点

### `GET /health`

健康检查端点。

**响应示例**：
```json
{
  "status": "healthy",
  "service": "enhanced-llm-bot-server",
  "processed_cache_size": 42,
  "context_max_chars": 15000,
  "features": [
    "smart_truncation_3_1",
    "rich_context",
    "graphql_enhanced",
    "dual_token",
    "test_context_fix",
    "direct_trigger_task"
  ]
}
```

### `GET /stats`

统计信息端点。

**响应示例**：
```json
{
  "processed_notifications": 42,
  "log_file_size_bytes": 12345,
  "log_file_path": "/data/processed_notifications.log",
  "bot_handle": "@WhiteElephantIsNotARobot"
}
```

## 特性列表

| 特性 | 说明 |
|------|------|
| `smart_truncation_3_1` | 3新1老智能截断算法 |
| `rich_context` | 丰富的上下文数据模型 |
| `graphql_enhanced` | 增强的 GraphQL 查询 |
| `dual_token` | 双 Token 架构 |
| `test_context_fix` | 基于测试修复的上下文逻辑 |
| `direct_trigger_task` | 直接使用触发消息作为任务描述 |

## 依赖

```txt
fastapi>=0.104.0
uvicorn[standard]>=0.24.0
httpx>=0.25.0
pydantic>=2.5.0
python-multipart>=0.0.6
```

## 运行方式

### 本地运行

```bash
# 安装依赖
pip install -r requirements.txt

# 设置环境变量
export BOT_TOKEN="your_bot_token"
export GQL_TOKEN="your_gql_token"
export CONTROL_REPO="owner/repo"
export SYSTEM_PROMPT="your_system_prompt"

# 启动服务器
python server.py
# 或
uvicorn server:app --host 0.0.0.0 --port 8000
```

### Docker 部署

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY system_prompt.md .

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
```

## 许可证

本项目采用 [GPL-3.0 许可证](LICENSE)。

## 相关文档

- [Claude Code 概览](doc/overview.md)
- [身份和访问管理](doc/iam.md)
- [设置配置](doc/settings.md)
- [无头模式运行](doc/headless.md)

## 贡献

欢迎提交 Issue 和 Pull Request！

## 联系

- GitHub: [@WhiteElephantIsNotARobot](https://github.com/WhiteElephantIsNotARobot)
