# 最终设计报告：可控可审计的 GitHub Agent 平台

## 1 总体概览

### 1.1 当前状态
- **服务端**：单脚本实现，负责监听通知、构建 task、构建上下文、触发鉴权。
- **GitHub Actions**：目前几乎仅负责运行 Claude Code 并在每次运行前创建日志 issue（带 `workflow` 标签）。

### 1.2 机密与权限隔离
- **账号级隔离**：为 agent 创建独立的 GitHub 用户，仅授予最小必要权限（对 fork 有写权限，对上游仓库仅允许 issue/comment/pr 等普通写操作）。
- **进程级隔离**：在 GitHub Actions 环境中，通过 **Linux 用户系统**实现 Claude Code 进程与 token 的隔离（Claude Code 不直接持有 token）。
- **MCP 代理**：所有 GitHub API 调用均由独立的 MCP 进程代为执行，MCP 持有完整权限的 agent 用户 token，并负责策略执行。

### 1.3 MCP 定位
- MCP 是一个**工具进程**，只代理 GitHub API 调用，不拦截其他工具。
- 职责：
  - 写操作次数限制
  - 重复写检测
  - 循环检测（基于 tool call 序列）
  - 只读 API 频率限制

---

## 2 目标与功能需求

### 2.1 访问控制与审计
- **写操作限制**：issue、comment、pr（禁止 review）。
- **频率限制**：只读 API 调用在 MCP 侧做滑动窗口节流。
- **追溯**：所有写操作（成功/失败）记录到日志 issue，包含指向创建内容的链接。

### 2.2 完整上下文构建
- 最终交付给 Claude Code 的上下文为 **markdown 格式**，包含：
  1. 触发处 issue/pr 的完整对话、元数据（开关状态、标签、分配人）。
  2. PR 关联 issue 的上下文。
  3. LLM 自己账户对应 fork 仓库的上下文：仓库列表、对应分支列表（含更新时间、最新 commit）、fork 的描述等元数据。
  4. 上游仓库相关元数据：分支状态、默认分支、保护规则等（触发节点必然在上游，故不重复抓取全量，仅补充必要信息）。
  5. Agent 历史（从 history issue 读取固定条数）与长期记忆（全部 memory，仅受字符数上限限制）。
- 日志 issue body 格式：
  ```markdown
  # Task
  > [触发 comment 原文]

  # Context
  > [构建的 markdown 上下文]
  ```

### 2.3 循环检测与重复写检测
- **循环定义**：连续 6 次（默认阈值）tool call 与返回完全一致，或重复发起相同参数的 gh 写操作。
- **执行位置**：MCP 侧实时监听并判定。
- **处理**：终止 Claude Code 会话，在日志 issue 中写入错误信息并添加 `bug` 标签。

### 2.4 日志 issue 升级与追溯
- **创建**：每次 workflow 启动前创建日志 issue（带 `workflow` 标签），body 包含 task 和 context（引用块）。
- **结束**：
  - 在日志 issue 的 comment 中写入：
    - 指向 agent 创建内容的链接（issue、comment、pr）。
    - Claude Code 会话记录（由 jsonl 事件流格式化为 markdown）。
  - 若 Claude Code 本体报错或检测到循环：写入错误信息 + `bug` 标签。
  - 若无错误：关闭 issue，状态为 **`complete`**（注意：`未计划` 仅用于 go away 场景）。
- **追溯**：所有写操作均被记录并可在日志 issue 中回溯。

### 2.5 自查与总结流程
- 统一通过多次 `claude --continue` 实现，由 Actions 脚本控制：
  1. **第一次（可跳过）**：若本次 workflow 中没有任何 GitHub 写操作（新建 issue/pr/comment 都算写），则提示 agent 留下消息。
  2. **第二次（无条件）**：要求确认是否推送（若有文件修改）以及 PR 是否存在。
  3. **第三次（history 总结）**：通过命令行参数精确允许 history 工具，要求 agent 用单行文本总结本次工作。
- 总次数：自查流程 1–2 次 + history 1 次 = 2–3 次。

### 2.6 消息标记与双向追溯
- **agent 创建的所有 GitHub 消息（issue、comment、pr）必须自动注入指向本次会话日志 issue 的链接**。
- 实现方式：MCP 在代理 `gh` 写操作（如 `create_comment`、`create_issue`、`create_pr`）时，自动在消息体注入标记
- 此标记帮助用户和开发者从任意 agent 活动快速回溯到完整的会话日志、上下文和审计记录。

---

## 3 架构组件与服务端设计

### 3.1 组件划分
- **服务端**：多文件实现，负责监听、鉴权、上下文抓取、分片与 dispatch。
- **GitHub Actions**：接收数据、构建上下文、创建日志 issue、运行 Claude Code + MCP、执行自查总结、关闭日志 issue。
- **MCP**：runner 上的独立 Python 进程，代理 gh 调用并执行策略。

### 3.2 服务端职责与文件结构
- **职责**：
  - 监听通知（@ 消息中可带权限配置标记）。
  - 推算触发节点（@ 智能体的那条消息）。
  - 触发者鉴权（基于预配置的用户列表）。
  - 解析 @ 中的权限配置（格式需设计，例如 `[permissions: comment-on-this-issue, pr-limit=1]`）。
  - go away 屏蔽判定与执行（触发专用 workflow）。
  - 抓取完整原始上下文（issue/pr/discussion/fork/upstream 元数据）。
  - 构建传输信封（ContextEnvelope）。
  - 分片并触发 repository_dispatch。
- **建议目录结构**：
  - `server/main.py`
  - `server/notifications/`（poller, trigger_locator）
  - `server/auth/`（actor_auth, permission_parser, go_away）
  - `server/context/`（fetch_issue, fetch_pr, fetch_discussion, fetch_fork, fetch_upstream）
  - `server/envelope/`（build_envelope, shard, dispatch）
  - `server/state/`（blocked_repos.json, blocked_users.json, session_cache.db）
  - `server/utils/`（github_api, logger, config）

### 3.3 go away 屏蔽机制
- **触发**：用户评论 `@WhiteElephantIsNotARobot go away`。
- **范围**：将 **agent 从特定仓库或用户下的仓库中屏蔽**。
  - 触发者为协作者 → 屏蔽当前仓库。
  - 触发者为任意用户 → 屏蔽其名下所有仓库。
- **处理流程**：
  1. 服务端监听消息，验证权限。
  2. 触发专用 workflow，在一个专用 issue 中维护“屏蔽仓库及用户列表”（issue body 反复编辑更新）。
  3. 执行清理：
     - 删除对应仓库的 fork（开放 PR 因此自动关闭）。
     - 删除 agent 在该仓库下创建的 issue/pr comment（review 不可删，故禁止 agent 创建 review）。
     - 以“未计划”状态关闭日志 issue。
  4. MCP 在后续 workflow 中拒绝对这些仓库的所有写操作。

---

## 4 Actions 端设计与上下文构建

### 4.1 工作流
- **`run_agent.yml`**：主流程，接收 envelope/合并数据 → 构建 markdown → 创建日志 issue → 设置隔离环境 → 启动 Claude Code + MCP → 执行自查总结 → 写入追溯信息 → 关闭日志 issue。
- **`receive_shard.yml`**：接收分片 dispatch，保存 artifact，更新 manifest。
- **`merge_shards.yml`**：合并分片，校验 checksum，触发主流程。

### 4.2 上下文筛选与构建
- **筛选规则**：
  - issue comment：按“新旧 3:1”比例挑选，直至达到字符上限（可配置）。
  - PR：
    - 若在 review 中触发：保留同批 review 及所有 review comment。
    - 若在普通 comment 中触发：保留所有 review + 最新批次 review comment。
    - 完整 diff 存入单独文件（不塞入主上下文）。
- **Agent 历史与记忆**：
  - 历史（history）：从 history issue 读取**固定条数**（不截断，条数可配置）。
  - 记忆（memory）：读取全部 memory，仅受总字符数上限限制。
- **最终 markdown**：按 2.2 节格式组装。

### 4.3 日志 issue 操作
- **创建**：body 严格按模板，包含 task 和 context 引用块。
- **追加追溯**：会话结束时，一次性写入指向 agent 创建内容的链接和会话记录。
- **关闭**：正常完成时标记为 `complete`，出错或循环时添加 `bug` 标签并保留 open。

---

## 5 MCP 设计与策略

### 5.1 进程模型
- 由 `mcp_runner.py` 管理两个子进程：Claude Code 与 MCP 代理。
- MCP 通过监听 Claude Code 的 gh 工具调用（例如 `gh.create_comment`）进行拦截。

### 5.2 核心策略
- **去重**：若两次写调用（方法+参数完全一致）→ 拒绝第二次。
- **次数限制**：
  - 创建 comment：同一 issue/pr 最多 2 次。
  - 创建 issue：同一仓库最多 1 次。
  - 创建 pr：同一仓库最多 1 次。
- **禁止操作**：
  - 修改他人消息。
  - 删除仓库。
  - 删除非本次 workflow 中创建的评论。
  - 删除分支。
  - 发布 review。
- **重复写检测**：对相同参数的 gh 写操作，**第二次即拒绝**（2.3.1.a.2 确认）。
- **只读频率限制**：使用专用只读 token，MCP 本地做滑动窗口节流。
- **屏蔽仓库**：若目标仓库在屏蔽列表中，所有写操作拒绝。

### 5.3 循环检测
- 维护最近 N 次 tool call 与返回的哈希序列。
- 阈值：连续 6 次完全一致 → 判定为循环，终止 Claude Code，写入日志 issue 并加 `bug` 标签。

### 5.4 历史与记忆工具
- **history 工具**：
  - 由 Actions 脚本在第三次 `claude --continue` 时调用。
  - agent 调用该工具时，MCP 将单行文本以 **comment 形式**追加到专用 history issue。
- **memory 工具**：
  - agent 自主调用，用于写入长期记忆。
  - 默认每次 workflow 最多写入 3 条，每条有字符数上限。
  - 所有 memory 以 **comment 形式**追加到专用 memory issue（append-only）。

---

## 6 数据结构与传输协议

### 6.1 ContextEnvelope（传输格式）
- **用途**：服务端发往 Actions 的数据载体，可能被分片。
- **字段**（7.1 修正后）：
  - `version`：协议版本。
  - `jobid`：本次 workflow 唯一标识。
  - `repo`：触发仓库（owner/name）。
  - `actor`：触发者用户名。
  - `trust_level`：触发者信任级别（owner/collaborator/external）。
  - `permission_config`：从 @ 中解析出的权限配置（格式待设计，例如 `comment-on-this-issue`）。
  - `trigger`：触发节点信息（类型、id、url）。
  - `context`：**完整原始上下文**（包含一切触发节点、仓库、用户等信息，已序列化的字符串）。
  - `timestamp`：触发时间。

### 6.2 分片机制
- **触发条件**：当 `context` 字符串超过 48 KB（可配置）时触发分片。
- **分片流程**：
  1. 服务端先将上下文完整序列化为字符串（JSON）并 base64 编码。
  2. 对字符串进行**分片**（直接按大小切割）。
  3. 通过多个 `repository_dispatch` 发送分片，专用工作流进行接收处理。

### 6.3 合并
- Actions 端接收所有分片后，按索引排序、解码、拼接，校验 checksum，恢复完整上下文。
- 合并后触发主 workflow。

---

## 7 Token 与运行

### 7.1 Token 模型
- **服务端**：
  - bot token：用于监听通知（只读权限）。
  - 个人 token：用于触发 `repository_dispatch`（具有触发 workflow 的权限）。
- **Actions 与 MCP**：
  - Claude Code 进程**不接触任何 token**。
  - MCP 持有**完整权限的 agent 用户 token**，负责所有 GitHub API 调用，并执行频率与写操作限制。

### 7.2 日志与可追溯性
- 不计划引入外部日志系统。
- **GitHub Actions 原生日志** + **日志 issue 的结构化记录** 构成完整审计线索。
- 关键事件（dispatch、分片、MCP 拒绝、循环终止）均可在 Actions 日志中追溯，同时日志 issue 汇总最终结果。

---

## 8 下一步交付物建议

根据当前设计，可按以下顺序生成交付物：

1. 服务端模块函数签名与骨架（包括分片逻辑）。
2. ContextEnvelope 与 Shard JSON schema。
3. Actions workflow 模板（`run_agent.yml`、`receive_shard.yml`、`merge_shards.yml`）。
4. Agent 端 Python 模块（context_builder、logging、utils）。
5. MCP 管理器完整骨架（`mcp_runner.py`、`policy.py`、`gh_proxy.py`、`loop_detector.py`、`call_history.py`）。
6. history/memory issue 的创建与读取脚本。
7. 端到端集成测试脚本（含极端长上下文和循环模拟）。
