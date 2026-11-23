# 安全策略

## 报告漏洞

安全问题**不要**开公开 Issue。走 GitHub
[私密漏洞报告](https://github.com/RobbsLuo/Coding2API/security/advisories/new)，
或邮件联系维护者（见仓库提交历史 / LICENSE 作者）。

请附：受影响版本或 commit（`git rev-parse HEAD`）、复现步骤与最小 PoC、影响面（能否未授权访问 / 读写凭证 / 打内网）、希望公开的时间点（如有）。

## 响应

单人维护，尽力而为：3 个工作日内确认收到；修复随常规 release 发布，commit message 与 GitHub Release 说明注明。

## 部署方须知（信任边界）

**平台范围：仅 Linux / macOS。** 单实例锁依赖 `flock`（POSIX 专有），Windows 上直接启动会以可读中文退出（用 WSL2 或 Docker）。

服务把上游账号凭证加密存本地并暴露管理台，部署须确保：

- **`APP_SECRET` 随机且保密**：会话签名密钥 + 凭证加密密钥派生源（`src/db/crypto.py`）。**用 ≥32 字符的随机串**（`python -c "import secrets; print(secrets.token_urlsafe(32))"`），不要用人类可记的口令——派生是无盐单次 SHA-256，库外流后可离线爆破。泄露等同凭证泄露。
- **管理台不要裸暴露公网**。必须暴露时前置带鉴权 / 限流的反向代理，并确保 `PUBLIC_BASE_URL` 与 `ALLOWED_HOSTS` 正确（`python3 scripts/doctor.py` 启动前校验：基址必须是 `http(s)://主机`，`HOST` 绑通配而基址仍是回环会直接 FAIL）。
- **`/metrics` 默认只允许回环抓取**：`METRICS_TOKEN` 留空时非回环对端一律 401。公网入口模板（Caddy / nginx）另已摘除该路径；自己写反代时别把它透出去。
- **反向代理部署必须配 `TRUSTED_PROXIES`**：`TRUST_PROXY=true` 而名单为空时采信任意对端的 `X-Forwarded-For`，`allowed_ips` 与登录限流都会失效。公网 compose 覆盖文件已把入口固定到专用子网。可置 `REQUIRE_TRUSTED_PROXIES=true` 让不合法配置（名单为空或全写坏）启动即失败，而不是只靠 doctor 的 WARN。
- **`data/`（SQLite，含凭证密文与审计）与 `secrets/users.txt`（口令哈希）不得外流**；服务启动把 `data/` 权限收到 `0700`。
  - POSIX：`chmod` 后读回校验 `mode & 0o077 == 0`。
  - Windows：`os.chmod` 对 ACL 无效，改用 `icacls` 把 DACL 替换成「仅当前用户 + SYSTEM」并切断继承（`src/security/private_files.py`），同样读回校验。
  - 校验不通过时**启动失败**（报错给出手工修复口径）；备份 / dump 这类旁路产物只告警。
- **告警 webhook 地址**由 admin 配置：写入期与**投递前**都拒绝内网 / 回环 / 链路本地（云元数据）地址，解析失败按阻断处理。officeAce 的模型网关地址同理（出口按 DNS 复核，它带的是不过期的 Basic 凭证）。
- **CI 安全扫描**：bandit（中危+）、pip-audit、`pnpm audit --prod`（另跑一次全量审计覆盖构建期 devDeps，仅 critical 阻断）。

已知与已评估的非问题（扫描器误报口径）见 `PROPOSAL.md` 的安全审查章节。
