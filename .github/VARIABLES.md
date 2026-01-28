# GitHub Actions Repository Variables

This document describes the required repository variables for GitHub Actions workflows.

## Required Variables

### SYSTEM_PROMPT

- **Name**: `SYSTEM_PROMPT`
- **Type**: Repository variable
- **Required**: Yes
- **Description**: The system prompt content used by the LLM bot runner workflow. This is the core instruction set that guides the AI agent's behavior.
- **Source**: Content from `system_prompt.md` file
- **Workflow Reference**: `.github/workflows/llm-bot-runner.yml`

#### Setup

To set this variable:

```bash
# Using GitHub CLI
gh api --method POST /repos/{owner}/{repo}/actions/variables \
  -f name=SYSTEM_PROMPT \
  -f value="$(cat system_prompt.md)"
```

Or update via GitHub UI:
1. Go to Repository Settings
2. Navigate to Secrets and variables → Actions
3. Click "New repository variable"
4. Name: `SYSTEM_PROMPT`
5. Value: Copy contents of `system_prompt.md`

## Required Secrets

The following secrets are also required for the workflow:

- `WEINAR_API_KEY`: GitHub API token for the agent
- `MIMO_API_KEY`: Anthropic API key for Claude
- `CONTEXT7_API_KEY`: Context7 API key (optional)
- `SSH_PRIVATE_KEY`: SSH private key for git operations (optional)
- `GPG_PRIVATE_KEY`: GPG private key for commit signing (optional)
