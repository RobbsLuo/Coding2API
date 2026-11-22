# 参与贡献

单人维护的逆向接口网关。**先开 issue 对齐范围再写代码**——尤其是「新渠道」与「改调度 / 计费语义」，直接提 PR 很可能因为前提不成立而白写。

## 提 issue

| 想做的事 | 入口 |
|---|---|
| 报 bug | [Bug 报告](https://github.com/RobbsLuo/Coding2API/issues/new?template=bug_report.yml) |
| 要新能力 | [功能建议](https://github.com/RobbsLuo/Coding2API/issues/new?template=feature_request.yml) |
| 接新渠道 | [渠道接入](https://github.com/RobbsLuo/Coding2API/issues/new?template=channel_request.yml) |
| 报漏洞 | [私密报告](https://github.com/RobbsLuo/Coding2API/security/advisories/new)（**不要**开公开 issue） |

`PROPOSAL.md` §1 的「非目标」是硬边界：按 token 计量计费、上游积分 → 货币换算、通用 provider 网关、语义缓存、对话正文落盘都不做。

## 提 PR

模板见 [`.github/pull_request_template.md`](.github/pull_request_template.md)。门槛与细则以 [`AGENTS.md`](AGENTS.md) 为准，核心是：

```bash
uv run ruff check src tests scripts
uv run pytest -q --cov=src --cov-report=term --cov-fail-under=100   # 行/分支 100%

cd web
pnpm exec tsc --noEmit && pnpm exec vitest run && pnpm build
```

- **覆盖率 100% 是硬门槛**；禁止用 `pragma: no cover` 给「该测没测」的业务分支达标。
- **平台差异分支必须 monkeypatch 测全**：本地 macOS 通过不代表 CI（ubuntu-latest）通过。
- **时间/时区断言必须显式固定**：同一类的坑——本地 TZ 是 Asia/Shanghai、CI 是 UTC。断言「月初 / 今天 / 非工作时段」时用 `ZoneInfo("Asia/Shanghai")` 换算，不要用 `time.localtime()`；本地验证跑一次 `TZ=UTC uv run pytest`。
- **改过 `src/` 必须重启后端再验**，否则「老后端 + 新前端」，新端点 404。
- 测试不得依赖本地 `.env`，构造 `Settings` 一律显式传值。
- **schema 只加不改**；删表要在 `_MIGRATION_DROPS` 补一条并 `SCHEMA_VERSION + 1`，附老库升级测试。
- 行为变更（API 语义、schema、上游解析）需同步 `PROPOSAL.md` / `README.md`。
- **推送后确认 CI 全绿**（backend / frontend / compose / security 四个 job）。

## 写文档

中文文档为主，`README.en.md` 为英文镜像，两篇结构一一对应。改配置项时，`README` 配置表、`.env.example`、`docker-compose.yml`、`src/config.py` 四处要一致。

## 引用与授权

引用信息见 [`CITATION.cff`](CITATION.cff)（GitHub「Cite this repository」可直接导出 APA / BibTeX）。贡献即视为以 MIT 授权（见 [`LICENSE`](LICENSE)）；上游渠道署名见 [`NOTICE`](NOTICE)。
