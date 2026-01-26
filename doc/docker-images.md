# Docker 镜像选型指南

## 概述

本文档为项目选择合适的 Docker 基础镜像提供参考，针对 Python FastAPI + MCP 服务器的部署场景。

## 镜像对比

### 1. Python 3.12 Slim (推荐 - 平衡方案)

**镜像**: `python:3.12-slim-bookworm`

| 项目 | 值 |
|------|-----|
| **大小** | ~124 MB (压缩后 ~118 MB) |
| **Python 版本** | 3.12.12 |
| **基础系统** | Debian 12 (Bookworm) |
| **优势** | ✅ 官方维护<br>✅ 包含完整 glibc<br>✅ 良好的包兼容性<br>✅ 易于调试<br>✅ 定期安全更新 |
| **劣势** | ❌ 比 distroless 大 ~2.5 倍 |

**适用场景**:
- 生产环境部署
- 需要调试能力
- 依赖较多第三方库
- 需要 shell 访问进行故障排查

**使用示例**:
```dockerfile
FROM python:3.12-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable
COPY . .
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm AS runtime
RUN groupadd -r app && useradd -r -g app app
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
USER app
CMD ["fastapi", "run", "server.py", "--host", "0.0.0.0", "--port", "8000"]
```

---

### 2. Google Distroless Python 3 (最小攻击面)

**镜像**: `gcr.io/distroless/python3` 或 `gcr.io/distroless/cc`

| 项目 | 值 |
|------|-----|
| **大小** | ~53 MB (python3) / ~23 MB (cc) |
| **Python 版本** | 3.11.2 (python3) |
| **基础系统** | Debian 12 (无 shell, 无包管理器) |
| **优势** | ✅ 极小攻击面<br>✅ 无 shell 无法被利用<br>✅ 无多余工具<br>✅ 符合最小权限原则 |
| **劣势** | ❌ Python 3.11 (非最新)<br>❌ 难以调试<br>❌ 需要多阶段构建<br>❌ 某些库可能需要额外配置 |

**适用场景**:
- 安全要求极高的生产环境
- 不需要 shell 调试
- 依赖相对简单
- 符合零信任安全模型

**使用示例**:
```dockerfile
FROM ghcr.io/astral-sh/uv:bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV UV_PYTHON_INSTALL_DIR=/python
ENV UV_PYTHON_PREFERENCE=only-managed
WORKDIR /app
RUN uv python install 3.12
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --no-editable
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM gcr.io/distroless/cc AS runtime
WORKDIR /app
COPY --from=builder --chown=nonroot:nonroot /python /python
COPY --from=builder --chown=nonroot:nonroot /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:/python/bin:$PATH"
USER nonroot
CMD ["fastapi", "run", "server.py", "--host", "0.0.0.0", "--port", "8000"]
```

---

### 3. Alpine Linux (最小体积 - 不推荐)

**镜像**: `python:3.12-alpine`

| 项目 | 值 |
|------|-----|
| **大小** | ~60 MB |
| **Python 版本** | 3.12.x |
| **基础系统** | Alpine Linux (musl libc) |
| **优势** | ✅ 体积小<br>✅ 基于 musl libc |
| **劣势** | ❌ 性能问题 (musl vs glibc)<br>❌ 构建时间长<br>❌ 某些库不兼容<br>❌ 调试困难 |

**适用场景**:
- 仅适用于非常简单的应用
- 不推荐用于生产环境

**原因**: Alpine 使用 musl libc，与许多 Python 包（尤其是科学计算、机器学习相关）存在兼容性问题，且性能可能不如 glibc。

---

## 推荐方案

### 生产环境推荐: **Python 3.12 Slim**

```yaml
# docker-compose.yml 示例
services:
  agent:
    build:
      context: .
      dockerfile: Dockerfile.example  # 使用 python:3.12-slim-bookworm
    image: whiteelephant/agent:latest
    ports:
      - "8000:8000"
    environment:
      - GITHUB_TOKEN=${GITHUB_TOKEN}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import httpx; httpx.get('http://localhost:8000/health', timeout=2)\" || exit 1"]
      interval: 30s
      timeout: 3s
      retries: 3
```

### 安全优先推荐: **Distroless**

如果安全是首要考虑，且不需要 shell 调试：

```yaml
services:
  agent:
    build:
      context: .
      dockerfile: Dockerfile.distroless
    image: whiteelephant/agent:distroless
    ports:
      - "8000:8000"
    environment:
      - GITHUB_TOKEN=${GITHUB_TOKEN}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import httpx; httpx.get('http://localhost:8000/health', timeout=2)\" || exit 1"]
      interval: 30s
      timeout: 3s
      retries: 3
```

---

## 镜像大小对比

| 镜像 | 压缩后大小 | Python 版本 | 适用性 |
|------|-----------|------------|--------|
| `python:3.12-slim-bookworm` | ~118 MB | 3.12.12 | ⭐⭐⭐⭐⭐ 推荐 |
| `gcr.io/distroless/cc` | ~23 MB | 需要单独安装 | ⭐⭐⭐⭐ 安全优先 |
| `gcr.io/distroless/python3` | ~50 MB | 3.11.2 | ⭐⭐⭐ 安全优先 |
| `python:3.12-alpine` | ~60 MB | 3.12.x | ⭐⭐ 不推荐 |

---

## 安全最佳实践

1. **使用非 root 用户运行**
   ```dockerfile
   RUN groupadd -r app && useradd -r -g app app
   USER app
   ```

2. **定期更新基础镜像**
   ```bash
   docker pull python:3.12-slim-bookworm
   docker build --no-cache -t your-app:latest .
   ```

3. **使用多阶段构建减少攻击面**
   - 构建阶段包含编译工具
   - 运行时阶段仅包含必要文件

4. **扫描镜像漏洞**
   ```bash
   docker scan your-app:latest
   # 或使用 trivy
   trivy image your-app:latest
   ```

5. **最小权限原则**
   - 不挂载敏感文件到容器
   - 使用 secrets 管理敏感信息
   - 限制容器能力

---

## 参考资料

- [Python Docker 官方镜像](https://hub.docker.com/_/python)
- [Google Distroless](https://github.com/GoogleContainerTools/distroless)
- [Docker 最佳实践](https://docs.docker.com/develop/develop-images/dockerfile_best-practices/)
- [Python on Docker Production Handbook](https://pythonspeed.com/docker/)
- [Building a Python Docker Image with Distroless and Uv](https://www.joshkasuboski.com/posts/distroless-python-uv/)
