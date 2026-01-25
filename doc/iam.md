> ## Documentation Index
> Fetch the complete documentation index at: https://code.claude.com/docs/llms.txt
> Use this file to discover all available pages before exploring further.

# 身份和访问管理

> 了解如何为您的组织中的 Claude Code 配置用户身份验证、授权和访问控制。

## 身份验证方法

设置 Claude Code 需要访问 Anthropic 模型。对于团队，您可以通过以下方式之一设置 Claude Code 访问：

* [Claude for Teams 或 Enterprise](/zh-CN/setup#for-teams-and-organizations)（推荐）
* [Claude Console 与团队计费](/zh-CN/setup#for-teams-and-organizations)
* [Amazon Bedrock](/zh-CN/amazon-bedrock)
* [Google Vertex AI](/zh-CN/google-vertex-ai)
* [Microsoft Foundry](/zh-CN/microsoft-foundry)

### Claude for Teams 或 Enterprise（推荐）

[Claude for Teams](https://claude.com/pricing#team-&-enterprise) 和 [Claude for Enterprise](https://anthropic.com/contact-sales) 为使用 Claude Code 的组织提供最佳体验。团队成员可以访问 Claude Code 和网络版 Claude，具有集中计费和团队管理功能。

* **Claude for Teams**：自助服务计划，具有协作功能、管理工具和计费管理。最适合较小的团队。
* **Claude for Enterprise**：添加 SSO、域名捕获、基于角色的权限、合规性 API 和托管策略设置，用于组织范围的 Claude Code 配置。最适合具有安全和合规性要求的大型组织。

**设置 Claude Code 访问：**

1. 订阅 [Claude for Teams](https://claude.com/pricing#team-&-enterprise) 或联系销售部门获取 [Claude for Enterprise](https://anthropic.com/contact-sales)
2. 从管理员仪表板邀请团队成员
3. 团队成员安装 Claude Code 并使用其 Claude.ai 账户登录

### Claude Console 身份验证

对于偏好基于 API 的计费的组织，您可以通过 Claude Console 设置访问。

**通过 Claude Console 为您的团队设置 Claude Code 访问：**

1. 使用您现有的 Claude Console 账户或创建新的 Claude Console 账户
2. 您可以通过以下任一方法添加用户：
   * 从 Console 内批量邀请用户（Console -> Settings -> Members -> Invite）
   * [设置 SSO](https://support.claude.com/en/articles/13132885-setting-up-single-sign-on-sso)
3. 邀请用户时，他们需要以下角色之一：
   * "Claude Code" 角色意味着用户只能创建 Claude Code API 密钥
   * "Developer" 角色意味着用户可以创建任何类型的 API 密钥
4. 每个受邀用户需要完成以下步骤：
   * 接受 Console 邀请
   * [检查系统要求](/zh-CN/setup#system-requirements)
   * [安装 Claude Code](/zh-CN/setup#installation)
   * 使用 Console 账户凭证登录

### 云提供商身份验证

**通过 Bedrock、Vertex 或 Azure 为您的团队设置 Claude Code 访问：**

1. 按照 [Bedrock 文档](/zh-CN/amazon-bedrock)、[Vertex 文档](/zh-CN/google-vertex-ai) 或 [Microsoft Foundry 文档](/zh-CN/microsoft-foundry)
2. 将环境变量和生成云凭证的说明分发给您的用户。阅读更多关于如何[在此处管理配置](/zh-CN/settings)的信息。
3. 用户可以[安装 Claude Code](/zh-CN/setup#installation)

## 访问控制和权限

我们支持细粒度权限，以便您能够准确指定代理允许做什么（例如运行测试、运行 linter）以及不允许做什么（例如更新云基础设施）。这些权限设置可以检入版本控制并分发给组织中的所有开发人员，也可以由个别开发人员自定义。

### 权限系统

Claude Code 使用分层权限系统来平衡功能和安全性：

| 工具类型    | 示例        | 需要批准 | "是，不再询问"行为    |
| :------ | :-------- | :--- | :------------ |
| 只读      | 文件读取、Grep | 否    | 不适用           |
| Bash 命令 | Shell 执行  | 是    | 每个项目目录和命令永久有效 |
| 文件修改    | 编辑/写入文件   | 是    | 直到会话结束        |

### 配置权限

您可以使用 `/permissions` 查看和管理 Claude Code 的工具权限。此 UI 列出所有权限规则和它们来自的 settings.json 文件。

* **Allow** 规则让 Claude Code 使用指定的工具而无需手动批准。
* **Ask** 规则在 Claude Code 尝试使用指定工具时提示确认。
* **Deny** 规则防止 Claude Code 使用指定的工具。

规则按顺序评估：**deny → ask → allow**。第一个匹配的规则获胜，因此 deny 规则始终优先。

* **其他目录**将 Claude 的文件访问扩展到初始工作目录之外的目录。
* **默认模式**控制 Claude 在遇到新请求时的权限行为。

权限规则使用格式：`Tool` 或 `Tool(optional-specifier)`

仅工具名称的规则匹配该工具的任何使用。例如，将 `Bash` 添加到允许列表允许 Claude Code 使用 Bash 工具而无需用户批准。请注意 `Bash(*)` 不会\*\*匹配所有 Bash 命令。使用不带括号的 `Bash` 来匹配所有使用。

<Note>
  有关权限规则语法（包括通配符）的快速参考，请参阅设置文档中的[权限规则语法](/zh-CN/settings#permission-rule-syntax)。
</Note>

#### 权限模式

Claude Code 支持多种权限模式，可以在[设置文件](/zh-CN/settings#settings-files)中设置为 `defaultMode`：

| 模式                  | 描述                                                                                             |
| :------------------ | :--------------------------------------------------------------------------------------------- |
| `default`           | 标准行为 - 在首次使用每个工具时提示权限                                                                          |
| `acceptEdits`       | 自动接受会话的文件编辑权限                                                                                  |
| `plan`              | 计划模式 - Claude 可以分析但不能修改文件或执行命令                                                                 |
| `dontAsk`           | 自动拒绝工具，除非通过 `/permissions` 或 [`permissions.allow`](/zh-CN/settings#permission-settings) 规则预先批准 |
| `bypassPermissions` | 跳过所有权限提示（需要安全环境 - 请参阅下面的警告）                                                                    |

#### 工作目录

默认情况下，Claude 可以访问启动它的目录中的文件。您可以扩展此访问：

* **启动期间**：使用 `--add-dir <path>` CLI 参数
* **会话期间**：使用 `/add-dir` 斜杠命令
* **持久配置**：添加到[设置文件](/zh-CN/settings#settings-files)中的 `additionalDirectories`

其他目录中的文件遵循与原始工作目录相同的权限规则 - 它们变为可读的而无需提示，文件编辑权限遵循当前权限模式。

#### 工具特定的权限规则

某些工具支持更细粒度的权限控制：

**Bash**

Bash 权限规则支持带 `:*` 的前缀匹配和带 `*` 的通配符匹配：

* `Bash(npm run build)` 匹配确切的 Bash 命令 `npm run build`
* `Bash(npm run test:*)` 匹配以 `npm run test` 开头的 Bash 命令
* `Bash(npm *)` 匹配任何以 `npm ` 开头的命令（例如 `npm install`、`npm run build`）
* `Bash(* install)` 匹配任何以 ` install` 结尾的命令（例如 `npm install`、`yarn install`）
* `Bash(git * main)` 匹配诸如 `git checkout main`、`git merge main` 之类的命令

<Tip>
  Claude Code 知道 shell 操作符（如 `&&`），因此前缀匹配规则如 `Bash(safe-cmd:*)` 不会给它权限运行命令 `safe-cmd && other-cmd`
</Tip>

<Warning>
  Bash 权限模式的重要限制：

  1. `:*` 通配符仅在模式末尾用于前缀匹配
  2. `*` 通配符可以出现在任何位置并匹配任何字符序列
  3. 诸如 `Bash(curl http://github.com/:*)` 之类的模式可以通过多种方式绕过：
     * URL 前的选项：`curl -X GET http://github.com/...` 不会匹配
     * 不同的协议：`curl https://github.com/...` 不会匹配
     * 重定向：`curl -L http://bit.ly/xyz`（重定向到 github）
     * 变量：`URL=http://github.com && curl $URL` 不会匹配
     * 额外空格：`curl  http://github.com` 不会匹配

  为了更可靠的 URL 过滤，请考虑：

  * **限制 Bash 网络工具**：使用 deny 规则阻止 `curl`、`wget` 和类似命令，然后使用 WebFetch 工具与 `WebFetch(domain:github.com)` 权限用于允许的域
  * **使用 PreToolUse 钩子**：实现一个钩子来验证 Bash 命令中的 URL 并阻止不允许的域
  * 通过 CLAUDE.md 指导 Claude Code 了解您允许的 curl 模式

  请注意，仅使用 WebFetch 不会阻止网络访问。如果允许 Bash，Claude 仍然可以使用 `curl`、`wget` 或其他工具来访问任何 URL。
</Warning>

**Read & Edit**

`Edit` 规则适用于所有编辑文件的内置工具。Claude 将尽力尝试将 `Read` 规则应用于所有读取文件的内置工具，如 Grep 和 Glob。

Read & Edit 规则都遵循 [gitignore](https://git-scm.com/docs/gitignore) 规范，具有四种不同的模式类型：

| 模式                | 含义                | 示例                               | 匹配                                 |
| ----------------- | ----------------- | -------------------------------- | ---------------------------------- |
| `//path`          | 从文件系统根目录的**绝对**路径 | `Read(//Users/alice/secrets/**)` | `/Users/alice/secrets/**`          |
| `~/path`          | 从**主**目录的路径       | `Read(~/Documents/*.pdf)`        | `/Users/alice/Documents/*.pdf`     |
| `/path`           | **相对于设置文件**的路径    | `Edit(/src/**/*.ts)`             | `<settings file path>/src/**/*.ts` |
| `path` 或 `./path` | **相对于当前目录**的路径    | `Read(*.env)`                    | `<cwd>/*.env`                      |

<Warning>
  诸如 `/Users/alice/file` 之类的模式不是绝对路径 - 它相对于您的设置文件！使用 `//Users/alice/file` 表示绝对路径。
</Warning>

* `Edit(/docs/**)` - 编辑 `<project>/docs/` 中的文件（不是 `/docs/`！）
* `Read(~/.zshrc)` - 读取您主目录的 `.zshrc`
* `Edit(//tmp/scratch.txt)` - 编辑绝对路径 `/tmp/scratch.txt`
* `Read(src/**)` - 从 `<current-directory>/src/` 读取

**WebFetch**

* `WebFetch(domain:example.com)` 匹配对 example.com 的获取请求

**MCP**

* `mcp__puppeteer` 匹配由 `puppeteer` 服务器提供的任何工具（名称在 Claude Code 中配置）
* `mcp__puppeteer__*` 通配符语法，也匹配来自 `puppeteer` 服务器的所有工具
* `mcp__puppeteer__puppeteer_navigate` 匹配由 `puppeteer` 服务器提供的 `puppeteer_navigate` 工具

**Task（子代理）**

使用 `Task(AgentName)` 规则来控制 [subagents](/zh-CN/sub-agents) Claude 可以使用哪些：

* `Task(Explore)` 匹配 Explore 子代理
* `Task(Plan)` 匹配 Plan 子代理
* `Task(Verify)` 匹配 Verify 子代理

将这些规则添加到您的[设置](/zh-CN/settings#permission-settings)中的 `deny` 数组或使用 `--disallowedTools` CLI 标志来禁用特定代理。例如，要禁用 Explore 代理：

```json  theme={null}
{
  "permissions": {
    "deny": ["Task(Explore)"]
  }
}
```

### 使用钩子的其他权限控制

[Claude Code 钩子](/zh-CN/hooks-guide)提供了一种方式来注册自定义 shell 命令以在运行时执行权限评估。当 Claude Code 进行工具调用时，PreToolUse 钩子在权限系统运行之前运行，钩子输出可以确定是否批准或拒绝工具调用以代替权限系统。

### 托管设置

对于需要对 Claude Code 配置进行集中控制的组织，管理员可以将 `managed-settings.json` 文件部署到[系统目录](/zh-CN/settings#settings-files)。这些策略文件遵循与常规设置文件相同的格式，不能被用户或项目设置覆盖。

### 设置优先级

当存在多个设置源时，它们按以下顺序应用（从高到低优先级）：

1. 托管设置（`managed-settings.json`）
2. 命令行参数
3. 本地项目设置（`.claude/settings.local.json`）
4. 共享项目设置（`.claude/settings.json`）
5. 用户设置（`~/.claude/settings.json`）

此层次结构确保组织策略始终被强制执行，同时在适当的项目和用户级别仍然允许灵活性。

## 凭证管理

Claude Code 安全地管理您的身份验证凭证：

* **存储位置**：在 macOS 上，API 密钥、OAuth 令牌和其他凭证存储在加密的 macOS Keychain 中。
* **支持的身份验证类型**：Claude.ai 凭证、Claude API 凭证、Azure Auth、Bedrock Auth 和 Vertex Auth。
* **自定义凭证脚本**：[`apiKeyHelper`](/zh-CN/settings#available-settings) 设置可以配置为运行返回 API 密钥的 shell 脚本。
* **刷新间隔**：默认情况下，`apiKeyHelper` 在 5 分钟后或在 HTTP 401 响应时调用。设置 `CLAUDE_CODE_API_KEY_HELPER_TTL_MS` 环境变量以获得自定义刷新间隔。
