# Claude Code 工作流检查报告

## 检查背景
根据 issue 要求，检查工作流中的 Claude Code 调用是否合法，Claude Code 安装步骤是否存在以及安装方式是否合法。

## 检查对象
- 文件：`.github/workflows/llm-bot-runner.yml`
- 相关文档：`doc/overview.md`, `doc/settings.md`, `doc/headless.md`, `doc/iam.md`

## 检查结果

### 1. Claude Code 调用合法性检查
**调用位置**：步骤"以GitHub机器人身份运行Claude Code"（第85-104行）

**调用方式**：
```yaml
claude -p "$LLM_TASK" \
  --append-user-prompt "$LLM_CONTEXT" \
  --system-prompt-file ~/.claude/system_prompt.txt \
  --allowedTools "Bash, TaskOutput, Edit, ExitPlanMode, Glob, Grep, KillShell, MCPSearch, NotebookEdit, Read, Skill, Task, TaskCreate, TaskGet, TaskList, TaskUpdate, WebFetch, WebSearch, Write"
```

**合法性分析**：
- ✅ 使用 `-p` 参数以编程方式运行 Claude Code，符合 `doc/headless.md` 文档描述
- ⚠️ `--append-user-prompt` 参数在官方文档中未提及，可能是自定义扩展
- ⚠️ `--system-prompt-file` 参数在官方文档中未提及，可能是自定义扩展
- ⚠️ `--allowedTools` 列表中包含多个未在文档中明确列出的工具名称（如 `TaskOutput`, `ExitPlanMode`, `KillShell`, `MCPSearch`, `NotebookEdit`, `Skill`, `TaskCreate`, `TaskGet`, `TaskList`, `TaskUpdate`, `WebSearch`），可能是内部或扩展工具
- ⚠️ 环境变量 `ANTHROPIC_BASE_URL` 设置为第三方代理端点 `https://api.xiaomimimo.com/anthropic`，官方文档仅支持以下身份验证方式：
  - Claude for Teams/Enterprise
  - Claude Console
  - Amazon Bedrock
  - Google Vertex AI
  - Microsoft Foundry
  使用未经验证的第三方代理可能违反 Anthropic 服务条款。

**结论**：Claude Code 调用方式基本符合编程式运行模式，但使用了未文档化的参数和第三方代理端点，存在合法性问题。

### 2. Claude Code 安装步骤检查
**安装位置**：步骤"安装Claude CLI"（第81-84行）

**安装方式**：
```bash
curl -sSL https://install.anthropic.com | sh
```

**合法性分析**：
- ❌ 官方安装脚本应为 `https://claude.ai/install.sh`（见 `doc/overview.md` 第24行）
- ❌ `install.anthropic.com` 域名无法解析（测试结果：`curl: (6) Could not resolve host: install.anthropic.com`）
- ⚠️ 使用 `sh` 而非官方推荐的 `bash`，可能存在兼容性问题
- ⚠️ 安装后未验证 `claude` 命令是否成功安装

**结论**：安装步骤存在，但使用错误的安装 URL 且该 URL 无法访问，安装方式不合法。

### 3. 配置准备步骤检查
**配置准备**：步骤"准备Claude Code配置"（第33-45行）

**合法性分析**：
- ✅ 正确创建 `~/.claude` 目录并复制配置文件
- ✅ 包含 `config.json` 和 `mcp.config.json` 配置验证
- ✅ 使用 `system_prompt.txt` 自定义系统提示词

## 风险总结
1. **安全风险**：从无法解析的域名安装软件，可能导致安装失败或安装恶意软件
2. **合规风险**：使用第三方代理端点可能违反 Anthropic API 使用条款
3. **可靠性风险**：安装步骤可能失败，导致整个工作流运行失败
4. **维护风险**：使用未文档化的 CLI 参数，未来版本可能不兼容

## 建议改进措施
1. **修正安装脚本**：使用官方安装 URL `https://claude.ai/install.sh`
2. **使用官方身份验证**：按照 `doc/iam.md` 文档选择官方支持的身份验证方式
3. **验证安装结果**：安装后添加 `claude --version` 验证步骤
4. **移除未文档化参数**：检查并替换为文档支持的 CLI 参数
5. **添加错误处理**：安装失败时工作流应优雅退出

## 原始 issue 上下文
```
Title: 检查workflow
Body: 根据doc/下的文档，检查工作流中的Claude Code调用是否合法，Claude Code安装步骤是否存在以及安装方式是否合法
另外接收上下文后要写入文件（llm可自行读取）（避免上下文影响llm理解用户提示词）
Author: WhiteElephant-abc
Created At: 2026-01-25T18:51:36Z
State: OPEN
```

---
*检查时间：2026-01-25*
*检查者：opencode（GitHub Actions 环境）*