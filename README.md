# coding2api

把 **CodeBuddy** 与 **TRAE SOLO** 两个 coding agent 上游通道，统一封装为 OpenAI 兼容 API，
并提供公共凭证池、统一调度与按人用量统计。

> [!WARNING]
> 本项目接通的是第三方服务的逆向接口，仅供学习研究。未做安全审计，不建议直接暴露在公网；
> 如确需公网访问，请置于反向代理之后并加鉴权与 IP 白名单。

## 特性

- **OpenAI 兼容出口**：`/v1/chat/completions`（流式 + 非流式）、`/v1/models`
- **双上游统一调度**：同一扁平模型名可按健康度自动选号，`model@provider` 可强制指定
- **三态健康度 + 三级冷却**：权益耗尽 12h / 限流 60s / 连续错误 10m / 会话失效硬禁用
- **公共凭证池**：管理员集中维护，全员共享；按人统计用量
- **凭证加密入库**：Fernet（AES-128-CBC + HMAC），密钥走 `APP_SECRET`
- **脱敏统计**：不保存提示词、回答、请求头、Token、工具参数；明细 90 天、小时汇总永久
- **完整凭证运维**：OAuth 设备码登录、多账号切换、额度探测、每日签到、token 预刷新

## 快速开始

### 前置要求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Node.js 24+ 与 pnpm 10+（仅构建前端需要）

### 本地运行

```bash
# 1. 安装后端依赖
uv sync

# 2. 创建管理台用户（交互输入密码）
uv run python scripts/hash_password.py admin

# 3. 构建前端
cd web && pnpm install && pnpm build && cd ..

# 4. 启动
APP_SECRET="换成你自己的随机字符串" ADMIN_USERNAMES=admin uv run python -m uvicorn src.main:build_app --factory --port 8000
```

打开 <http://127.0.0.1:8000> 登录管理台。

### Docker

```bash
cat > .env <<'EOF'
APP_SECRET=换成你自己的随机字符串
ADMIN_USERNAMES=admin
EOF

mkdir -p secrets
docker run --rm -it -v "$PWD/secrets:/app/secrets" \
  --entrypoint python coding2api:local scripts/hash_password.py admin

docker compose up -d
```

`APP_SECRET` 丢失会导致已存凭证全部无法解密，只能重新录入——请备份。

## 使用

### 1. 添加凭证

管理台「凭证管理」页：

- **CodeBuddy**：点「登录 CodeBuddy」走设备码授权；也可手动粘贴 `{"token":"..."}`
- **TRAE**：粘贴凭证 JSON（`accessToken` / `uid` / `refreshToken`）或回调链接

### 2. 创建 API Key

「API Key」页创建，明文只显示一次。

### 3. 调用

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-你的key" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}]}'
```

强制走某个上游：

```bash
-d '{"model":"glm-5.2@trae", ...}'      # 只走 TRAE
-d '{"model":"glm-5.2@codebuddy", ...}' # 只走 CodeBuddy
```

### 4. 在客户端中使用

任何支持自定义 OpenAI 端点的客户端都可接入：

- Base URL：`http://127.0.0.1:8000/v1`
- API Key：上一步创建的 `sk-...`
- 模型名：以 `GET /v1/models` 返回为准

## 配置

全部通过环境变量（见 `docker-compose.yml`）。完整清单参考 `TECHNICAL.md` §8。

| 变量 | 默认 | 说明 |
|---|---|---|
| `APP_SECRET` | **必填** | 凭证列加密密钥；丢失等于凭证全部作废 |
| `ADMIN_USERNAMES` | 空 | 逗号分隔；空则所有用户只读 |
| `USERS_FILE` | `secrets/users.txt` | 用户文件路径 |
| `DATA_DIR` | `./data` | SQLite 与运行数据目录 |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | 浏览器可达地址；TRAE 登录回调依赖它 |
| `CODEBUDDY_API_ENDPOINT` | 中国站 | 上游地址，只接受白名单内地址 |
| `DEFAULT_MODEL` | `glm-5.2` | 模型为空或 `auto` 时的目标 |
| `CHECKIN_HOUR` | `9` | 每日签到时刻（服务器本地时区） |
| `QUOTA_PROBE_MINUTES` | `60` | 额度探测周期 |

## 开发

```bash
# 后端测试（行/分支覆盖门槛 100%）
uv run pytest -q --cov=src --cov-report=term --cov-fail-under=100

# 代码检查
uv run ruff check src tests scripts

# 前端
cd web
pnpm exec tsc --noEmit
pnpm exec vitest run
pnpm build
```

## 文档

| 文档 | 内容 |
|---|---|
| [`PROPOSAL.md`](PROPOSAL.md) | 立项决策、目标与非目标、可行性核实、风险清单 |
| [`TECHNICAL.md`](TECHNICAL.md) | 技术栈、模块规格、Provider 协议、请求时序、测试策略 |
| [`diagrams/coding2api-architecture.html`](diagrams/coding2api-architecture.html) | 系统架构图（浏览器打开） |

## 状态

后端完成（M0–M1.5），前端完成（M2）。当前 `main` 分支可运行。

## 授权协议

MIT，见 [LICENSE](LICENSE)。借鉴的上游项目署名见 [NOTICE](NOTICE)。
