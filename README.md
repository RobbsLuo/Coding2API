# Coding2API

把 **CodeBuddy** 与 **TRAE SOLO** 两个 coding agent 渠道，统一封装为 OpenAI 兼容 API，
提供公共凭证池、统一调度与按人用量统计。

> [!WARNING]
> 逆向接口，仅供学习研究，未做安全审计；公网部署需反向代理 + 鉴权 + IP 白名单。

## 特性

- **OpenAI 兼容出口**：`/v1/chat/completions`（流式 + 非流式）、`/v1/models`、`/v1/user/balance`（DeepSeek 兼容余额查询）
- **统一调度**：扁平模型名按健康度自动选号，`模型@渠道` 强制指定；三态健康度 + 分级冷却自动避开坏号；积分 36h 内即将到期多者优先（先用掉，避免过期浪费；管理台凭证列表直接显示每个账号的到期积分）；同一对话多轮请求粘住同一凭证（对话进行中不换号，出错才轮换）
- **公共凭证池**：admin 集中维护、全员共享；按人统计用量
- **完整凭证运维**：设备码登录、多账号切换、额度探测、每日签到（含连续天数）、token 预刷新；凭证加密入库（`APP_SECRET`）
- **成长中心**（仅 CodeBuddy）：自动领 Buddy 旅行礼物、派 Buddy、领取新任务与任务奖、断登补登、连登奖励兑换、开盲盒、能量开 Buddy 盲盒；不可逆动作可用 `GROWTH_IRREVERSIBLE_ACTIONS=false` 一键关停；管理台可手动执行并查看每轮逐条结果
- **脱敏统计**：不存对话内容；明细 90 天、小时汇总永久；按人/渠道/模型可视化
- **管理台安全加固**：登录限流、CSRF 校验、请求体上限、Host 白名单

## 快速开始

### 前置要求

Python 3.12+、[uv](https://docs.astral.sh/uv/)、Node.js 24+ 与 pnpm 10+（仅构建前端需要）。

### 本地运行

```bash
uv sync
uv run python scripts/hash_password.py admin   # 创建管理台用户（交互输入密码）
cd web && pnpm install && pnpm build && cd ..
APP_SECRET="换成你自己的随机字符串" ADMIN_USERNAMES=admin \
  uv run python -m uvicorn src.main:build_app --factory --port 8000
```

打开 <http://127.0.0.1:8000> 登录管理台。

### Docker

本地构建：

```bash
cat > .env <<'EOF'
APP_SECRET=换成你自己的随机字符串
ADMIN_USERNAMES=admin
PUBLIC_BASE_URL=http://127.0.0.1:8000
EOF
docker compose build
mkdir -p secrets
docker compose run --rm --entrypoint python coding2api scripts/hash_password.py admin \
  --output /app/secrets/users.txt
docker compose up -d
```

拉取 GHCR 镜像（推荐，跳过本地构建）：

```bash
docker compose pull
# 或指定版本：docker pull ghcr.io/robbsluo/coding2api:v0.1.1
```

推送新版本：打 tag `v*` 推到 main 即触发 publish workflow（见 `.github/workflows/publish.yml`），同时打 `<tag>` 和 `:latest` 到 GHCR。也可在 Actions 页面手动触发（填版本号）。

## 使用

1. 「凭证管理」添加凭证：CodeBuddy 走设备码登录（或粘贴 `{"token":"..."}`）；TRAE 粘贴凭证 JSON（`accessToken`/`uid`/`refreshToken`）或回调链接（凭证里的 `apiHost` 只接受官方地址，其他值会被拒绝导入）
2. 「API Key」创建 `sk-...`（明文仅显示一次）
3. 调用：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-你的key" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}]}'
```

`模型@渠道` 强制指定上游（如 `glm-5.2@trae`），不写则自动选健康渠道。

任意 OpenAI 兼容客户端可直接接入（Base URL `http://127.0.0.1:8000/v1`、Key 用 `sk-...`、模型名以 `GET /v1/models` 为准）；「Playground」页用登录会话直接测试，无需 API Key。

### 余额查询

`GET /v1/user/balance` 兼容 DeepSeek 余额接口的响应形状，Bearer `sk-...` 鉴权，供 Cherry Studio / cc-switch 等客户端显示余额。余额来自凭证池的额度探测缓存（`QUOTA_PROBE_MINUTES` 周期刷新），按可用凭证汇总，单位为上游 credits：

```bash
curl http://127.0.0.1:8000/v1/user/balance -H "Authorization: Bearer sk-你的key"
```

```json
{
  "is_available": true,
  "balance_infos": [{"currency": "credits", "total_balance": "60.00",
                     "granted_balance": "0.00", "topped_up_balance": "0.00"}],
  "balance_known": true,
  "providers": [{"provider": "codebuddy", "remaining": 50.0, "total": 150.0, "credentials": 2}]
}
```

池内凭证从未探测成功时 `balance_known` 为 `false`（余额未知，不是 0）。管理台右上角「功能说明」按钮可随时查各功能入口。

## 后台任务

额度探测、token 预刷新、每日签到、成长中心、明细清理由 `TaskRunner` 自动调度（失败互不影响），周期见 TECHNICAL.md §6.1。

成长中心仅 CodeBuddy 有：自动领取 Buddy 旅行礼物、派 Buddy 出发、领取新任务与任务奖、
断登补登、连登奖励兑换、开盲盒、能量开 Buddy 盲盒。Buddy 旅行 1–4 小时回来一次，
所以周期默认 60 分钟（`GROWTH_INTERVAL_MINUTES`），回来就领、不把礼物压到第二天。
抽奖 / 连登兑换 / 开 Buddy 盲盒 / 消耗补登卡属于**不可逆动作**，设
`GROWTH_IRREVERSIBLE_ACTIONS=false` 可全部跳过（仍会领旅行礼物与任务奖励）。
管理台凭证列表的「成长中心」列显示每个账号最近一轮的结果，行内菜单可手动执行一次。

## 配置

常用项如下；完整的可配置项见 `src/config.py`（权威），且**每一个都已透传到 `docker-compose.yml`**——`.env` 里写这些变量即可生效（compose 的 `.env` 只做插值，未透传的变量不会进容器）。

> 容器里改监听地址用 `HOST`/`PORT`（`PORT` 同时决定宿主机映射端口），入口读 `config.py`，不硬编码。

| 变量 | 默认 | 说明 |
|---|---|---|
| `APP_SECRET` | **必填** | 凭证加密密钥，≥16 字符；丢失 = 凭证全部作废，无密钥轮换 |
| `ADMIN_USERNAMES` | 空 | 管理员用户名，逗号分隔；空则全员只读 |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | 浏览器可达地址；TRAE 登录回调依赖它 |
| `DEFAULT_MODEL` | `glm-5.2` | 模型为空/`auto` 时的默认 |
| `USERS_FILE` | `secrets/users.txt` | 用户文件路径（`config.py` 的 `users_file`） |
| `DATA_DIR` | `./data` | SQLite 与运行数据目录 |
| `QUOTA_PROBE_MINUTES` | `60` | 额度探测周期 |
| `GROWTH_INTERVAL_MINUTES` | `60` | 成长中心（仅 CodeBuddy）一轮领取的周期；下限 5 分钟 |
| `GROWTH_IRREVERSIBLE_ACTIONS` | `true` | 是否允许成长中心的不可逆动作：抽奖、连登兑换、开 Buddy 盲盒、消耗补登卡。`false` 时仍会领取旅行礼物与任务奖励 |
| `QUOTA_EXPIRY_WINDOW_SECONDS` | `129600` | 到期排序窗口：把距到期 ≤ 该秒数的积分加总，多的账号先用（避免积分过期浪费）；`≤0` 关闭，退回纯健康度排序 |
| `CONVERSATION_STICKY_SECONDS` | `3600` | 会话粘性 TTL：同一对话（消息前缀延续）多轮请求固定用同一凭证（手动 pin 的凭证优先，粘性让位）；凭证出错仍会轮换，成功后重新粘定；`≤0` 关闭 |
| `MODEL_BLOCKLIST` | `custom_model_*,*sub*agent*,summary,browser_use_*` | 模型列表黑名单（仅影响列表展示） |
| `ALLOWED_HOSTS` | 空 | Host 白名单，防 DNS rebinding |

## 部署注意

- **挂载目录属主**：容器内以 uid 1001（`appuser`）运行，`./data` 与 `./secrets`
  必须可写/可读，否则 SQLite 打不开：

  ```bash
  mkdir -p data secrets && sudo chown -R 1001:1001 data secrets
  ```

- **时区**：镜像默认 `TZ=Asia/Shanghai`；如需其它时区显式覆盖 `TZ`。

## 日志

应用只写 **stdout / stderr**，不自己写文件也不自己轮转（原因见
[`src/webapp/logging.py`](src/webapp/logging.py) 顶部说明）：三种部署形态的
采集方式不同，但都靠这两个 fd 对接，轮转交给各自的平台工具。因此**日志自己
不会停止增长**——按下面对应形态配一次即可。

| 部署形态 | 日志到哪 | 轮转机制 | 需做什么 |
|---|---|---|---|
| **Docker / compose** | `docker logs coding2api` | json-file 驱动（已在 compose 配好 `10m × 5`） | **无需操作** |
| **macOS（launchd）** | `logs/launchd.{out,err}.log` | 系统自带 `newsyslog` | 跑一次 `./scripts/install-newsyslog.sh`（需 sudo） |
| **Linux（systemd）** | `journalctl -u coding2api` | journald 自带 | **无需操作**（模板见 `deploy/systemd/`） |
| **Linux（非 systemd）** | 重定向到 `/var/log/coding2api/*.log` | `logrotate` | `sudo cp deploy/logrotate/coding2api /etc/logrotate.d/` |
| **裸跑**（`uv run python -m src.main`） | 终端 stderr | 无 | 自己重定向并自备轮转 |

级别用 `LOG_LEVEL`（默认 `INFO`）控制。**审计日志（凭证增删改/pin/账号切换）
是 INFO 级**，把 `LOG_LEVEL` 调到 `WARNING` 会把它一并关掉。

```bash
# macOS：装轮转规则（单文件超 10MB 转，留 7 份，bzip2 压缩）
./scripts/install-newsyslog.sh
./scripts/install-newsyslog.sh --uninstall   # 卸载

# 看日志
tail -f logs/launchd.err.log          # macOS
docker logs -f coding2api             # 容器
journalctl -u coding2api -f           # systemd
```

> 容器里的 `data/dumps/`（诊断开关 `DUMP_REQUEST_BODIES=true` 写入）不在
> docker 日志体系内，由应用自行保持最多 200 份。

## 开发

```bash
# 后端：lint + 测试（行/分支覆盖门槛 100%）
uv run ruff check src tests scripts
uv run pytest -q --cov=src --cov-report=term --cov-fail-under=100

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

M0–M3 全部完成，`main` 分支可运行。

## 授权协议

MIT，见 [LICENSE](LICENSE)。借鉴的上游项目署名见 [NOTICE](NOTICE)。