# 安全策略

## 报告漏洞

安全问题**不要**开公开 Issue。走 GitHub
[私密漏洞报告](https://github.com/RobbsLuo/Coding2API/security/advisories/new)
，或邮件联系维护者（见仓库提交历史 / LICENSE 作者）。

请附：

- 受影响版本或 commit（`git rev-parse HEAD`）
- 复现步骤与最小 PoC
- 影响面（能否未授权访问、读写凭证、打内网）
- 希望公开的时间点（如有）

## 响应

单人维护，尽力而为：3 个工作日内确认收到，能修尽快修；修复随常规 release 发布，commit message 与 GitHub Release 说明注明。

## 部署方须知（本项目的信任边界）

**平台范围：仅 Linux / macOS。** 单实例锁依赖 `flock`（POSIX 专有），Windows 上没有可靠的等价物；直接启动会以一句可读中文退出，不提供服务（用 WSL2 或 Docker 部署）。Windows 上的文件权限见下条。

服务把上游账号凭证加密存本地，并暴露管理台。部署须确保：

- `APP_SECRET` 随机且保密：会话签名密钥 + 凭证加密密钥派生源（见 `src/db/crypto.py`）。泄露等同凭证泄露。
- 管理台不要裸暴露公网。必须暴露时前置带鉴权/限流的反向代理，并确保 `PUBLIC_BASE_URL` 与 `ALLOWED_HOSTS` 正确（见 README「配置」；`python3 scripts/doctor.py` 会在启动前校验两者一致）。
- `data/`（SQLite，含凭证密文与审计）、`secrets/users.txt`（口令哈希）不得进备份之外任何地方；服务启动把 `data/` 权限收到 `0700`。
  - **POSIX（Linux / macOS）**：`chmod` 后读回校验 `mode & 0o077 == 0`。
  - **Windows**：`os.chmod` 对 ACL 无效，改用 `icacls` 把 DACL 替换成「仅当前用户 + SYSTEM」并切断继承（`src/security/private_files.py`），同样读回校验。
  - 校验不通过时**启动失败**（报错给出手工修复口径）；备份 / dump 这类旁路产物只告警，不拖垮主流程。`scripts/doctor.py` 可在启动前只读复核。
- 告警 webhook 地址由 admin 配置：写入期与**投递前**都拒绝内网/回环/链路本地（云元数据）地址，解析失败按阻断处理。
- 安全扫描在 CI 执行：bandit（中危+）、pip-audit、`pnpm audit --prod`。

已知与已评估的非问题（扫描器误报口径）见 `PROPOSAL.md` 的安全审查章节。
