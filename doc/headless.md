> ## Documentation Index
> Fetch the complete documentation index at: https://code.claude.com/docs/llms.txt
> Use this file to discover all available pages before exploring further.

# 以编程方式运行 Claude Code

> 使用 Agent SDK 从 CLI、Python 或 TypeScript 以编程方式运行 Claude Code。

[Agent SDK](https://platform.claude.com/docs/zh-CN/agent-sdk/overview) 为您提供了支持 Claude Code 的相同工具、代理循环和上下文管理。它可作为 CLI 用于脚本和 CI/CD，或作为 [Python](https://platform.claude.com/docs/zh-CN/agent-sdk/python) 和 [TypeScript](https://platform.claude.com/docs/zh-CN/agent-sdk/typescript) 包用于完整的编程控制。

<Note>
  CLI 之前称为"无头模式"。`-p` 标志和所有 CLI 选项的工作方式相同。
</Note>

要从 CLI 以编程方式运行 Claude Code，请使用 `-p` 传递您的提示和任何 [CLI 选项](/zh-CN/cli-reference)：

```bash  theme={null}
claude -p "Find and fix the bug in auth.py" --allowedTools "Read,Edit,Bash"
```

本页面介绍通过 CLI (`claude -p`) 使用 Agent SDK。对于具有结构化输出、工具批准回调和本机消息对象的 Python 和 TypeScript SDK 包，请参阅 [完整 Agent SDK 文档](https://platform.claude.com/docs/zh-CN/agent-sdk/overview)。

## 基本用法

将 `-p`（或 `--print`）标志添加到任何 `claude` 命令以非交互方式运行它。所有 [CLI 选项](/zh-CN/cli-reference) 都适用于 `-p`，包括：

* `--continue` 用于 [继续对话](#continue-conversations)
* `--allowedTools` 用于 [自动批准工具](#auto-approve-tools)
* `--output-format` 用于 [获取结构化输出](#get-structured-output)

此示例询问 Claude 关于您的代码库的问题并打印响应：

```bash  theme={null}
claude -p "What does the auth module do?"
```

## 示例

这些示例突出了常见的 CLI 模式。

### 获取结构化输出

使用 `--output-format` 控制如何返回响应：

* `text`（默认）：纯文本输出
* `json`：包含结果、会话 ID 和元数据的结构化 JSON
* `stream-json`：用于实时流式传输的换行符分隔的 JSON

此示例以 JSON 格式返回项目摘要以及会话元数据，文本结果在 `result` 字段中：

```bash  theme={null}
claude -p "Summarize this project" --output-format json
```

要获得符合特定架构的输出，请使用 `--output-format json` 与 `--json-schema` 和 [JSON Schema](https://json-schema.org/) 定义。响应包括有关请求的元数据（会话 ID、使用情况等），结构化输出在 `structured_output` 字段中。

此示例从 auth.py 中提取函数名称并将其作为字符串数组返回：

```bash  theme={null}
claude -p "Extract the main function names from auth.py" \
  --output-format json \
  --json-schema '{"type":"object","properties":{"functions":{"type":"array","items":{"type":"string"}}},"required":["functions"]}'
```

<Tip>
  使用 [jq](https://jqlang.github.io/jq/) 之类的工具来解析响应并提取特定字段：

  ```bash  theme={null}
  # Extract the text result
  claude -p "Summarize this project" --output-format json | jq -r '.result'

  # Extract structured output
  claude -p "Extract function names from auth.py" \
    --output-format json \
    --json-schema '{"type":"object","properties":{"functions":{"type":"array","items":{"type":"string"}}},"required":["functions"]}' \
    | jq '.structured_output'
  ```
</Tip>

### 自动批准工具

使用 `--allowedTools` 让 Claude 使用某些工具而无需提示。此示例运行测试套件并修复失败，允许 Claude 执行 Bash 命令和读取/编辑文件而无需请求权限：

```bash  theme={null}
claude -p "Run the test suite and fix any failures" \
  --allowedTools "Bash,Read,Edit"
```

### 创建提交

此示例审查暂存的更改并创建具有适当消息的提交：

```bash  theme={null}
claude -p "Look at my staged changes and create an appropriate commit" \
  --allowedTools "Bash(git diff:*),Bash(git log:*),Bash(git status:*),Bash(git commit:*)"
```

`--allowedTools` 标志使用 [权限规则语法](/zh-CN/settings#permission-rule-syntax)。`:*` 后缀启用前缀匹配，因此 `Bash(git diff:*)` 允许任何以 `git diff` 开头的命令。

<Note>
  [斜杠命令](/zh-CN/slash-commands)（如 `/commit`）仅在交互模式下可用。在 `-p` 模式下，改为描述您想要完成的任务。
</Note>

### 自定义系统提示

使用 `--append-system-prompt` 添加指令同时保持 Claude Code 的默认行为。此示例将 PR diff 传递给 Claude 并指示其审查安全漏洞：

```bash  theme={null}
gh pr diff "$1" | claude -p \
  --append-system-prompt "You are a security engineer. Review for vulnerabilities." \
  --output-format json
```

有关更多选项（包括 `--system-prompt` 以完全替换默认提示），请参阅 [系统提示标志](/zh-CN/cli-reference#system-prompt-flags)。

### 继续对话

使用 `--continue` 继续最近的对话，或使用 `--resume` 与会话 ID 继续特定对话。此示例运行审查，然后发送后续提示：

```bash  theme={null}
# First request
claude -p "Review this codebase for performance issues"

# Continue the most recent conversation
claude -p "Now focus on the database queries" --continue
claude -p "Generate a summary of all issues found" --continue
```

如果您运行多个对话，请捕获会话 ID 以恢复特定对话：

```bash  theme={null}
session_id=$(claude -p "Start a review" --output-format json | jq -r '.session_id')
claude -p "Continue that review" --resume "$session_id"
```

## 后续步骤

<CardGroup cols={2}>
  <Card title="Agent SDK 快速入门" icon="play" href="https://platform.claude.com/docs/zh-CN/agent-sdk/quickstart">
    使用 Python 或 TypeScript 构建您的第一个代理
  </Card>

  <Card title="CLI 参考" icon="terminal" href="/zh-CN/cli-reference">
    探索所有 CLI 标志和选项
  </Card>

  <Card title="GitHub Actions" icon="github" href="/zh-CN/github-actions">
    在 GitHub 工作流中使用 Agent SDK
  </Card>

  <Card title="GitLab CI/CD" icon="gitlab" href="/zh-CN/gitlab-ci-cd">
    在 GitLab 管道中使用 Agent SDK
  </Card>
</CardGroup>
