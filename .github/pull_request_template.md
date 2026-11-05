## 改动

<!-- 一句话说清改了什么。 -->

## 为什么

<!-- 触发问题：issue 号、现场现象、上游变更。没有上下文的话评审只能靠猜。 -->

Fixes #

## 影响面

<!-- 勾选后确认对应验证已做过。未验证的项请留空并在下面说明原因。 -->

- [ ] 后端逻辑（`src/`）
- [ ] 数据库 schema（`schema.sql` / `migrate.py`）
- [ ] API 语义（端点、请求/响应字段）
- [ ] 上游解析（provider 请求体、事件解析、错误分类）
- [ ] 前端（`web/`）
- [ ] 部署产物（Dockerfile / compose / Caddy）
- [ ] 仅文档 / 注释

## 验证

```bash
# 后端（门槛：行/分支覆盖 100%）
uv run ruff check src tests scripts
uv run pytest -q --cov=src --cov-report=term --cov-fail-under=100

# 前端
cd web && pnpm exec tsc --noEmit && pnpm exec vitest run && pnpm build
```

- [ ] 上述命令本地全绿
- [ ] 改过 `src/` 的话，**已重启后端**再验（不重启 = 老后端 + 新前端，新端点 404）
- [ ] 未在 CI 覆盖的验证（真机请求、上游实测、多平台）写在下面

## schema 变更（没有就删掉本节）

- [ ] `schema.sql` 只加不改
- [ ] 已有表的新列走 `migrate.py` 的 `_MIGRATION_COLUMNS` 幂等补列
- [ ] 删表同时在 `_MIGRATION_DROPS` 补一条，并 `SCHEMA_VERSION + 1`
- [ ] 附老库升级测试（样板见 `tests/db/test_repo_migrate.py`）

## 备注

<!-- 已知取舍、后续待办、评审时需要特别看的地方。 -->
