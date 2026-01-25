> ## Documentation Index
> Fetch the complete documentation index at: https://code.claude.com/docs/llms.txt
> Use this file to discover all available pages before exploring further.

# Claude Code 概览

> 了解 Claude Code，Anthropic 的代理编码工具，它存在于您的终端中，帮助您比以往任何时候都更快地将想法转化为代码。

## 30 秒内开始使用

前置条件：

* 一个 [Claude.ai](https://claude.ai)（推荐）或 [Claude Console](https://console.anthropic.com/) 账户

**安装 Claude Code：**

To install Claude Code, use one of the following methods:

<Tabs>
  <Tab title="Native Install (Recommended)">
    **macOS, Linux, WSL:**

    ```bash  theme={null}
    curl -fsSL https://claude.ai/install.sh | bash
    ```

    **Windows PowerShell:**

    ```powershell  theme={null}
    irm https://claude.ai/install.ps1 | iex
    ```

    **Windows CMD:**

    ```batch  theme={null}
    curl -fsSL https://claude.ai/install.cmd -o install.cmd && install.cmd && del install.cmd
    ```

    <Info>
      Native installations automatically update in the background to keep you on the latest version.
    </Info>
  </Tab>

  <Tab title="Homebrew">
    ```sh  theme={null}
    brew install --cask claude-code
    ```

    <Info>
      Homebrew installations do not auto-update. Run `brew upgrade claude-code` periodically to get the latest features and security fixes.
    </Info>
  </Tab>

  <Tab title="WinGet">
    ```powershell  theme={null}
    winget install Anthropic.ClaudeCode
    ```

    <Info>
      WinGet installations do not auto-update. Run `winget upgrade Anthropic.ClaudeCode` periodically to get the latest features and security fixes.
    </Info>
  </Tab>
</Tabs>

**开始使用 Claude Code：**

```bash  theme={null}
cd your-project
claude
```

首次使用时，系统会提示您登录。就这么简单！[继续快速入门（5 分钟）→](/zh-CN/quickstart)

<Tip>
  Claude Code 会自动保持最新状态。有关安装选项、手动更新或卸载说明，请参阅[高级设置](/zh-CN/setup)。如果遇到问题，请访问[故障排除](/zh-CN/troubleshooting)。
</Tip>

## Claude Code 为您做什么

* **从描述构建功能**：用纯英文告诉 Claude 您想构建什么。它将制定计划、编写代码并确保其正常工作。
* **调试和修复问题**：描述一个错误或粘贴错误消息。Claude Code 将分析您的代码库、识别问题并实施修复。
* **导航任何代码库**：询问有关您团队代码库的任何内容，并获得深思熟虑的答案。Claude Code 维护对整个项目结构的认识，可以从网络上查找最新信息，并且通过 [MCP](/zh-CN/mcp) 可以从 Google Drive、Figma 和 Slack 等外部数据源提取数据。
* **自动化繁琐任务**：修复棘手的 lint 问题、解决合并冲突和编写发行说明。从您的开发机器上用一个命令完成所有这些，或在 CI 中自动完成。

## 为什么开发者喜欢 Claude Code

* **在您的终端中工作**：不是另一个聊天窗口。不是另一个 IDE。Claude Code 在您已经工作的地方与您相遇，使用您已经喜欢的工具。
* **采取行动**：Claude Code 可以直接编辑文件、运行命令和创建提交。需要更多？[MCP](/zh-CN/mcp) 让 Claude 读取您在 Google Drive 中的设计文档、更新您在 Jira 中的工单，或使用\_您的\_自定义开发者工具。
* **Unix 哲学**：Claude Code 是可组合和可脚本化的。`tail -f app.log | claude -p "Slack me if you see any anomalies appear in this log stream"` *有效*。您的 CI 可以运行 `claude -p "If there are new text strings, translate them into French and raise a PR for @lang-fr-team to review"`。
* **企业就绪**：使用 Claude API，或在 AWS 或 GCP 上托管。企业级[安全](/zh-CN/security)、[隐私](/zh-CN/data-usage)和[合规性](https://trust.anthropic.com/)是内置的。

## 后续步骤

<CardGroup>
  <Card title="快速入门" icon="rocket" href="/zh-CN/quickstart">
    通过实际示例查看 Claude Code 的实际应用
  </Card>

  <Card title="常见工作流程" icon="graduation-cap" href="/zh-CN/common-workflows">
    常见工作流程的分步指南
  </Card>

  <Card title="故障排除" icon="wrench" href="/zh-CN/troubleshooting">
    Claude Code 常见问题的解决方案
  </Card>

  <Card title="IDE 设置" icon="laptop" href="/zh-CN/vs-code">
    将 Claude Code 添加到您的 IDE
  </Card>
</CardGroup>

## 其他资源

<CardGroup>
  <Card title="关于 Claude Code" icon="sparkles" href="https://claude.com/product/claude-code">
    在 claude.com 上了解有关 Claude Code 的更多信息
  </Card>

  <Card title="使用 Agent SDK 构建" icon="code-branch" href="https://docs.claude.com/en/docs/agent-sdk/overview">
    使用 Claude Agent SDK 创建自定义 AI 代理
  </Card>

  <Card title="在 AWS 或 GCP 上托管" icon="cloud" href="/zh-CN/third-party-integrations">
    使用 Amazon Bedrock 或 Google Vertex AI 配置 Claude Code
  </Card>

  <Card title="设置" icon="gear" href="/zh-CN/settings">
    为您的工作流程自定义 Claude Code
  </Card>

  <Card title="命令" icon="terminal" href="/zh-CN/cli-reference">
    了解 CLI 命令和控制
  </Card>

  <Card title="参考实现" icon="code" href="https://github.com/anthropics/claude-code/tree/main/.devcontainer">
    克隆我们的开发容器参考实现
  </Card>

  <Card title="安全" icon="shield" href="/zh-CN/security">
    发现 Claude Code 的保护措施和安全使用的最佳实践
  </Card>

  <Card title="隐私和数据使用" icon="lock" href="/zh-CN/data-usage">
    了解 Claude Code 如何处理您的数据
  </Card>
</CardGroup>
