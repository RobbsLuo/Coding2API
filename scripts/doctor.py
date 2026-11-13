#!/usr/bin/env python3
"""启动前自检（只读）：部署卡死时先跑这一条。

与 `create_user.py` / `backup_db.py` 同目录同风格。**绝不写库、不迁移、不启服务、
不落凭证明文**——doctor 自己变成故障源就失去意义了。

用法：
    python3 scripts/doctor.py                    # 全量检查
    python3 scripts/doctor.py --skip-upstream    # 离线部署：跳过上游连通性
    python3 scripts/doctor.py --json             # 机读输出（CI 用）
    python3 scripts/doctor.py --port 8000        # 指定要探测的端口

退出码：0 = 没有 FAIL（WARN 不拦）；1 = 有 FAIL。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import doctor  # noqa: E402
from src.config import Settings, load_settings  # noqa: E402
from src.webapp.static import frontend_dist  # noqa: E402

#: 需要探测连通性的渠道端点。只取配置里的**当前**端点，不枚举白名单：
#: 白名单里可能有用户永远不用的站点，逐条探测只会制造噪声与慢启动。
_UPSTREAM_FIELDS = (
    ("codebuddy", "codebuddy_api_endpoint"),
    ("codebuddy_intl", "codebuddy_intl_api_endpoint"),
    ("zen", "zen_api_endpoint"),
    ("kilo", "kilo_api_endpoint"),
    ("qoder", "qoder_api_endpoint"),
    ("codearts", "codearts_api_endpoint"),
    ("officeace", "officeace_api_endpoint"),
    ("raccoon", "raccoon_api_endpoint"),
    ("lingxi", "lingxi_api_endpoint"),
)


def upstream_endpoints(settings: Settings) -> list[tuple[str, str]]:
    """配置里当前生效的渠道端点（去重，保持声明顺序）。"""
    seen: set[str] = set()
    endpoints: list[tuple[str, str]] = []
    for name, field in _UPSTREAM_FIELDS:
        url = getattr(settings, field, "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        endpoints.append((name, url))
    return endpoints


def collect(settings: Settings, *, port: int | None = None,
            skip_upstream: bool = False) -> list[doctor.Check]:
    """跑完全部检查；任一项内部异常都折算成 FAIL 而不是让脚本崩掉。"""
    checks = [
        doctor.check_python(sys.version_info[:3]),
        doctor.check_dependencies(),
        doctor.check_app_secret(settings),
        doctor.check_data_dir(settings),
        doctor.check_data_permissions(settings),
        doctor.check_host_config(settings),
        doctor.check_database(settings),
        doctor.check_port(settings.host, settings.port if port is None else port),
        doctor.check_frontend(frontend_dist()),
    ]
    if not skip_upstream:
        checks.extend(doctor.check_upstreams(upstream_endpoints(settings)))
    return checks


def render(checks: list[doctor.Check], *, as_json: bool = False) -> str:
    """排版：JSON 给 CI，纯文本给人。"""
    if as_json:
        return json.dumps(
            [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks],
            ensure_ascii=False, indent=2)
    width = max(len(c.name) for c in checks) if checks else 0
    lines = [f"[{c.status}] {c.name.ljust(width)}  {c.detail}".rstrip() for c in checks]
    return "\n".join(lines)


def summarize(checks: list[doctor.Check]) -> str:
    """"N 项通过 / M 项警告 / K 项失败"。"""
    passed = sum(1 for c in checks if c.status == doctor.PASS)
    warned = sum(1 for c in checks if c.status == doctor.WARN)
    failed = sum(1 for c in checks if c.status == doctor.FAIL)
    return f"{passed} 项通过，{warned} 项警告，{failed} 项失败"


def load_settings_tolerant() -> tuple[Settings, str | None]:
    """构造 Settings；`APP_SECRET` 缺失时降级为占位值并返回原因。

    doctor 的用途之一就是**诊断 `APP_SECRET` 没配**——若这里直接让
    pydantic 的 `ValidationError` 打出来，其余六项检查全被挡住，等于在最需要
    自检的场景下自检不可用。降级后用 `CredentialCipher` 判定为 FAIL 并继续跑。
    """
    try:
        return load_settings(), None
    except ValidationError:
        # `.env` 里其它键在这次降级里读不到：APP_SECRET 是唯一必填项，
        # 缺它基本等价于「还没建 .env」，其余项按默认值检查即可。
        return Settings(_env_file=None, APP_SECRET=""), "APP_SECRET 未配置"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动前自检（只读，不启服务）")
    parser.add_argument("--port", type=int, help="要探测的端口（默认取 PORT）")
    parser.add_argument("--skip-upstream", action="store_true",
                        help="跳过上游连通性探测（离线部署）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    settings, problem = load_settings_tolerant()
    if problem is not None:
        print(f"配置读取降级：{problem}；其余检查按默认值继续", file=sys.stderr)
    checks = collect(settings, port=args.port, skip_upstream=args.skip_upstream)
    print(render(checks, as_json=args.json))
    if not args.json:
        print(summarize(checks))
    return 0 if all(c.ok for c in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
