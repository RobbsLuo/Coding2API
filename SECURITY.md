# 安全策略

## 报告漏洞

请**不要**为安全问题开公开 Issue。用 GitHub 的
[私密漏洞报告](https://github.com/RobbsLuo/Coding2API/security/advisories/new)
提交，或直接邮件联系维护者（见仓库提交历史 / LICENSE 中的作者）。

请附上：

- 受影响的版本或 commit（`git rev-parse HEAD`）
- 复现步骤与最小 PoC
- 影响面（能否未授权访问、能否读写凭证、能否打内网）
- 你希望公开的时间点（如有）

## 响应

单人维护项目，尽力而为：3 个工作日内确认收到，能修的尽快修，修复随常规
release 发布并在 commit message 与 GitHub Release 说明里注明。

## 部署方须知（本项目的信任边界）

这个服务把上游账号凭证加密存在本地，并暴露管理台。部署时请确保：

- `APP_SECRET` 是随机且保密的：它既是会话签名密钥，也是凭证加密密钥的派生源
  （见 `src/db/crypto.py`）。泄露等同于凭证泄露。
- 管理台不要裸暴露到公网。若必须暴露，前面套一层带鉴权/限流的反向代理，
  并确保 `PUBLIC_BASE_URL` 与 `ALLOWED_HOSTS` 配置正确（见 README「配置」）。
- `data/`（SQLite，含凭证密文与审计）、`secrets/users.txt`（口令哈希）不得进入
  备份之外的任何地方；服务启动会把 `data/` 目录权限收到 `0700`。
- 告警 webhook 地址由 admin 配置：写入期与**投递前**都会拒绝指向内网/回环/
  链路本地（云元数据）的地址，解析失败按阻断处理。
- 安全扫描在 CI 中执行：bandit（中危+）、pip-audit、`pnpm audit --prod`。

已知与已评估的非问题（扫描器误报口径）见 `PROPOSAL.md` 的安全审查章节。
