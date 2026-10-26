# coding2api 项目级约束

全局约束见 `~/.claude/CLAUDE.md`（opencode 经 `~/.config/opencode/AGENTS.md` 软链读取）；本文件更具体，冲突以本文件为准。

## 质量门槛（提交前必须全部通过）

- 后端：`uv run ruff check src tests scripts` + `uv run pytest -q --cov=src --cov-report=term --cov-fail-under=100`
  - 行/分支覆盖率 100% 硬门槛；新增/修改代码必须带测试，缺口一律补测试；禁止用 pragma/排除给「该测没测」业务分支达标。例外（必须注释为何不可达）：
    - src/：「按构造不可达」的防御兜底——`__main__` 入口、协议演进防御分支、前置 return 已排除的死分支
    - tests/：测试替身实现宽于本用例所需的接口方法（注释「由 XX 调用 / 本用例不调用」）
  - 平台差异分支不用 pragma，必须 monkeypatch 测全（见下条）
- 前端（web/）：`pnpm exec tsc --noEmit` + `pnpm exec vitest run` + `pnpm build`
- 平台差异：本地 macOS 通过不代表 CI（ubuntu-latest）通过；平台相关分支（platform.system/machine 等）必须 monkeypatch 测全所有分支
- **测试不得依赖本地 `.env`**：本地 `.env` 补 `APP_SECRET` 等必填项，CI 没有。构造 `Settings` 必须显式传值；验证时移走 `.env` 跑全量
- **改 `src/` 必须重启服务才算验证完成**（只改前端则重新构建）。否则「新前端 + 旧后端」错配：新端点 404，前端弹无关兜底文案。重启命令随部署形态，见 `docs/local-environment.md` 或 README「部署注意」

## GitHub Actions（硬约束）

- push 到 main（或 PR）触发 CI（.github/workflows/ci.yml）：backend / frontend / compose / security 四个 job
- **推送后必须确认 CI 全绿才算完成**：`gh run watch $(gh run list --limit 1 --json databaseId -q '.[0].databaseId')` 或 `gh run list --limit 1`
- CI 失败必须修到通过；不允许红色 main 分支
- compose job 在 Docker 内真实构建并启动服务（health 检查）；改 Dockerfile/依赖/启动逻辑先本地 `docker build` 预演
- **`web/.npmrc` 与 `web/pnpm-lock.yaml` 强耦合**：`auto-install-peers=false` 必须与 lock 的 `settings.autoInstallPeers` 一致，且 `Dockerfile` 必须 `COPY web/.npmrc`。改任一处都需本地 `docker build` 验证，否则 compose job 报 `ERR_PNPM_LOCKFILE_CONFIG_MISMATCH`

## 其他

- 覆盖率/测试之外的行为变更（API 语义、DB schema、上游解析）需同步 PROPOSAL.md / README.md
- SQLite schema 变更：schema.sql 只加不改（列注释可改）
  - 已有表的新列必须走 `src/db/migrate.py` 的 `_MIGRATION_COLUMNS` 幂等补列
  - 删表必须在 schema.sql 删定义**同时**在 `_MIGRATION_DROPS` 补一条（`CREATE TABLE IF NOT EXISTS` 对老库无效，不补删则遗留死表），并 `SCHEMA_VERSION + 1`
  - 两者都需附老库升级测试（`tests/db/test_repo_migrate.py` 有现成样板）
