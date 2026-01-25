# LLM GitHub 机器人控制仓库

本仓库实现了"LLM完全控制一个独立GitHub账号"系统，如项目计划所述。

## 概述

该系统允许LLM（Claude Code）通过GitHub Actions作为一个独立的GitHub账号（`llm-bot-dev`）运行。LLM可以使用机器人账号的身份进行代码审查、修改文件、创建PR/issue以及参与讨论。

## 架构

1. **独立GitHub账号**：创建一个专用的GitHub账号（`llm-bot-dev`）并授予适当权限
2. **PAT存储**：将机器人的个人访问令牌存储在此仓库的GitHub Actions Secrets中，命名为`LLMGH_TOKEN`
3. **控制仓库**：此仓库包含：
   - 运行Claude Code与MCP工具的GitHub Actions工作流
   - Claude Code配置文件
   - MCP服务器配置
4. **触发机制**：外部服务器接收GitHub webhook并触发带任务上下文的`workflow_dispatch`
5. **执行**：Claude Code在Actions环境中使用机器人的PAT运行，对目标仓库执行操作

## 设置说明

### 1. 创建机器人账号和PAT
- 创建一个新的GitHub账号（例如`llm-bot-dev`）
- 生成细粒度PAT，授予以下权限：
  - 仓库内容（读取和写入）
  - Issues、pull requests、discussions
  - 工作流（如需要）
- 将机器人账号添加到目标仓库并设置适当的访问级别

### 2. 配置此控制仓库
- Fork或创建此仓库
- 在仓库设置 > Secrets and variables > Actions中添加以下Secrets：
  - `LLMGH_TOKEN`：机器人账号的PAT
  - `ANTHROPIC_API_KEY`：用于Claude API的Anthropic API密钥（或使用MIMO_API_KEY）
  - `MIMO_API_KEY`：用于代理调用大模型的token（如果使用自定义Claude API端点）
  - `WEINAR_API_KEY`：@WhiteElephantIsNotARobot的GitHub token

### 3. 自定义配置
- 编辑`.claude/config.json`调整Claude Code设置
- 编辑`.claude/mcp.config.json`配置MCP服务器
- 按需修改`.github/workflows/llm-bot-runner.yml`

### 4. 设置Webhook服务器
- 部署示例FastAPI服务器（`server.py`）以接收GitHub webhook
- 配置目标仓库将webhook发送到你的服务器
- 服务器验证webhook并触发LLM机器人工作流

### 5. 触发机器人
- 当事件发生时（新issue、PR、评论等），服务器触发：
  ```bash
  gh workflow run llm-bot-runner.yml -f task="Review PR #45" -f context='{"repo":"owner/project","pr_number":45,...}'
  ```
- LLM机器人将分析上下文并执行相应操作
- 对于手动测试，可以使用示例脚本：
  ```bash
  ./scripts/trigger-example.sh
  ```

## 文件结构

```
├── .github/workflows/
│   ├── llm-bot-runner.yml     # LLM执行主工作流
│   └── opencode.yml           # 现有opencode工作流（可选）
├── .claude/
│   ├── config.json            # Claude Code配置
│   └── mcp.config.json        # MCP服务器配置
├── server.py                  # 示例FastAPI webhook服务器
├── LICENSE
└── README.md
```

## 安全考虑

- 机器人账号的PAT永远不会离开GitHub Actions环境
- 所有敏感令牌都存储在GitHub Secrets中
- Claude Code配置为永不回显或记录令牌
- 机器人账号仅对你信任的仓库拥有有限权限
- 对关键分支使用分支保护规则和必要的审查

## 使用示例

1. **自动化PR审查**：LLM审查PR，提出改进建议，甚至可以提交修复
2. **Issue分类**：LLM响应新issue，请求澄清或创建后续任务
3. **代码维护**：LLM执行常规代码更新、依赖升级或重构
4. **文档更新**：LLM根据用户问题或代码变更改进文档

## 自定义

- 修改`.claude/config.json`中的系统提示词以改变LLM行为
- 添加更多MCP服务器以获得额外工具（Jira、Slack、监控系统等）
- 扩展webhook服务器以处理更多事件类型
- 为不同任务类型创建专门的工作流

## 故障排除

- 检查GitHub Actions日志获取执行详情
- 验证PAT是否具有正确的权限
- 确保webhook负载格式正确
- 确认Claude CLI安装成功（检查Anthropic安装脚本）