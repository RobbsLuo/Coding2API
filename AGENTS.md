# coding2api 项目级约束

全局约束见 `~/.pi/agent/AGENTS.md`；本文件是更具体的项目规则，冲突时以本文件为准。

## 质量门槛（提交前必须全部通过）

- 后端：`uv run ruff check src tests scripts` + `uv run pytest -q --cov=src --cov-report=term --cov-fail-under=100`
  - 行/分支覆盖率 100% 是硬门槛；新增/修改的代码必须带测试，覆盖率缺口一律补测试解决，
    禁止用 pragma/排除给「该测没测」的业务分支达标。允许的例外（必须带注释说明为何不可达）：
    - src/：「按构造不可达」的防御兜底——`__main__` 入口、协议演进防御分支、前置 return
      已排除的死分支
    - tests/：测试替身实现了宽于本用例所需的接口方法（注释「由 XX 调用 / 本用例不调用」）
  - 平台差异分支不用 pragma，必须 monkeypatch 测全（见下条）
- 前端（web/）：`pnpm exec tsc --noEmit` + `pnpm exec vitest run` + `pnpm build`
- 注意平台差异：本地 macOS 通过不代表 CI（ubuntu-latest）通过；平台相关分支
  （platform.system/machine 等）必须用 monkeypatch 测全所有分支
- **测试不得依赖本地 `.env`**：本地 `.env` 补上 `APP_SECRET` 等必填项，CI 没有。
  构造 `Settings` 必须显式传值；验证时临时移走 `.env` 再跑全量（B5 首条 CI 即栽于此）
- **改 `src/` 必须重启服务才算验证完成**（只改前端则重新构建即可）。否则出现
  「新前端 + 旧后端」错配：新端点 404，前端弹出与真实原因无关的兜底文案。
  具体重启命令随部署形态而定，见 `docs/local-environment.md` 或 README「部署注意」

## GitHub Actions（硬约束）

- push 到 main（或 PR）会触发 CI（.github/workflows/ci.yml）：backend / frontend / compose 三个 job
- **推送后必须确认 CI 全绿才算完成**：`gh run watch $(gh run list --limit 1 --json databaseId -q '.[0].databaseId')`
  或 `gh run list --limit 1`
- CI 失败必须修复直到通过；不允许留下红色 main 分支
- compose job 会在 Docker 内真实构建并启动服务（health 检查），改 Dockerfile / 依赖 /
  启动逻辑时先本地 `docker build` 预演

## 其他

- 覆盖率/测试之外的行为变更（API 语义、DB schema、上游解析）需同步 PROPOSAL.md / README.md
- SQLite schema 变更：schema.sql 只加不改（列注释可改）
  - 已有表的新列必须走 `src/db/migrate.py` 的 `_MIGRATION_COLUMNS` 幂等补列
  - 删表必须在 schema.sql 删定义**同时**在 `_MIGRATION_DROPS` 补一条
    （`CREATE TABLE IF NOT EXISTS` 对老库无效，不补删则遗留死表），并 `SCHEMA_VERSION + 1`
  - 两者都需附老库升级测试（`test_m15_operations.py` 有现成样板）
