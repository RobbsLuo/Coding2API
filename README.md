# Coding2API

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
- **用量可视化**：请求量趋势（按上游）、按模型趋势（Top N）、按上游分组、逐请求明细
  （保留 90 天，游标翻页）；8 项总览指标合并为 4 张卡片；品牌 logo 标注渠道
- **管理台安全加固**：登录三级限流（全局/IP/用户名）+ PBKDF2 并发上限、写操作 CSRF 校验、
  请求体上限（登录 8KB / 其余 16MB）、安全响应头（CSP `frame-ancestors`）与 Host 白名单
- **模型目录治理**：`MODEL_BLOCKLIST` 过滤内部/老模型；上游拉取失败用缓存兜底；
  消耗倍率与 token 上限/能力字段透传到列表，Playground 选中即览

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
# 1. 配置
cat > .env <<'EOF'
APP_SECRET=换成你自己的随机字符串
ADMIN_USERNAMES=admin
PUBLIC_BASE_URL=http://127.0.0.1:8000
EOF

# 2. 构建镜像
docker compose build

# 3. 创建管理台用户（镜像内已带脚本）
mkdir -p secrets
docker compose run --rm --entrypoint python coding2api scripts/hash_password.py admin \
  --output /app/secrets/users.txt

# 4. 启动
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

### 5. Playground 调试

「Playground」页用登录会话直接测试，无需 API Key；用量计入当前用户。
选中模型后下方显示**消耗倍率、最大输入/输出、图片与工具调用支持**，
双上游倍率不同时会按渠道（前置 icon 标注）分别显示。

## 功能对照表

管理台右上角有「功能说明」按钮，随时可查。这里列出最容易困惑的几项：

| 你想做什么 | 去哪里 | 说明 |
|---|---|---|
| 添加 CodeBuddy 凭证 | 凭证管理 → 登录 CodeBuddy | 设备码授权，页面自动轮询结果 |
| 添加 TRAE 凭证 | 凭证管理 → 登录 TRAE | 浏览器授权后回调本服务直接落库 |
| 看懂「未探测到额度」 | 凭证管理 → 健康度列 | 不是「已耗尽」；点「探测」重新获取 |
| 看懂冷却时间 | 凭证管理 → 状态列 | 到期自动恢复，不需要手动处理 |
| 固定用某个账号 | 凭证管理 → 「指定」 | 全局唯一，请求只走它 |
| 切换个人/企业账号 | 凭证管理 → 「账号」 | 仅 CodeBuddy 支持 |
| 强制走某个上游 | 模型名写 `glm-5.2@trae` | 不写则自动选健康的 |
| 接入第三方客户端 | 客户端设置 Base URL | 地址加 `/v1`，Key 用 `sk-…` |
| 查某个人用了多少 | 用量统计 → 用户名筛选 | 仅管理员；普通用户只见自己 |
| 统计里 credit 是 — | 用量统计 | 上游可选字段，经常不返回；主指标是 token |
| Token 卡片里命中/未命中是 — | 用量统计 | 上游未上报缓存命中（cached_tokens）时无法拆分输入 |
| 看某个模型消耗多快 | Playground → 选中模型 | 下方显示倍率 / token 上限 / 图片与工具支持 |
| 列表里老模型/内部模型太多 | `MODEL_BLOCKLIST` env | glob 黑名单；只影响列表展示，直连指定不受影响 |

## 配置

全部通过环境变量（见 `docker-compose.yml`）。完整清单参考 `PROPOSAL.md` §8。

| 变量 | 默认 | 说明 |
|---|---|---|
| `APP_SECRET` | **必填** | 凭证列加密密钥，至少 16 字符；丢失等于凭证全部作废 |
| `ADMIN_USERNAMES` | 空 | 逗号分隔；空则所有用户只读 |
| `USERS_FILE` | `secrets/users.txt` | 用户文件路径 |
| `DATA_DIR` | `./data` | SQLite 与运行数据目录 |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | 浏览器可达地址；TRAE 登录回调依赖它 |
| `CODEBUDDY_API_ENDPOINT` | 中国站 | 上游地址，只接受 `CODEBUDDY_ALLOWED_ENDPOINTS` 白名单内地址 |
| `CODEBUDDY_ALLOWED_ENDPOINTS` | 中/国际站 | 逗号分隔白名单；不在其中启动即失败 |
| `DEFAULT_MODEL` | `glm-5.2` | 模型为空或 `auto` 时的目标 |
| `CHECKIN_HOUR` | `9` | 每日签到时刻（容器本地时区，镜像默认 `TZ=Asia/Shanghai`） |
| `QUOTA_PROBE_MINUTES` | `60` | 额度探测周期 |
| `CODEBUDDY_CHAT_MIN_INTERVAL` | `5` | CB 聊天最小间隔（秒），避频控风控；0 关闭 |
| `MODEL_BLOCKLIST` | `custom_model_*,*sub*agent*,summary,browser_use_*` | 模型列表黑名单（fnmatch glob，逗号分隔，完全替换语义）；只影响列表展示，直连指定不受影响 |
| `ALLOWED_HOSTS` | 空 | Host 白名单（防 DNS rebinding）；空 = 本地回环 + `PUBLIC_BASE_URL` 主机 |
| `DUMP_REQUEST_BODIES` | `false` | 诊断开关：把 `/v1` 原始请求体落到 `data/dumps/`（含对话内容，勿长期开启） |

## 部署注意

- **挂载目录属主**：容器内以 uid 1001（`appuser`）运行，`./data` 与 `./secrets`
  必须可写/可读，否则 SQLite 打不开、服务崩溃重启：

  ```bash
  mkdir -p data secrets && sudo chown -R 1001:1001 data secrets
  ```

- **时区**：`CHECKIN_HOUR` 按容器本地时区解释。镜像已装 `tzdata` 并默认
  `TZ=Asia/Shanghai`；如需其它时区，显式覆盖 `TZ`，否则签到时刻会按 UTC 算。
- **APP_SECRET**：至少 16 字符，弱密钥（如 `.env.example` 里的占位串）会在
  启动时被拒绝；更换后已存凭证全部无法解密，需重新录入。

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

后端完成（M0–M1.5），前端完成（M2），`main` 分支可运行。
PROPOSAL §8 安全边界（登录限流 / CSRF / 请求体上限 / 安全头 / Host 白名单）
与 §4.4 模型目录治理（黑名单 / 失败兜底缓存 / 元数据透传）已落地。

## 授权协议

MIT，见 [LICENSE](LICENSE)。借鉴的上游项目署名见 [NOTICE](NOTICE)。
