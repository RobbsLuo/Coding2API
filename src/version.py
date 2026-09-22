"""单一版本源：一切对外暴露的版本号都从这里取。

此前版本号散落在 5 处（pyproject.toml / src/main.py 的 FastAPI(version=) /
web/package.json / README 的 docker pull 示例 / publish.yml 的提示文案），
升版时必漏——`/openapi.json` 会报出与镜像 tag 不一致的版本，排查问题时误导。

规则：**pyproject.toml 是唯一真源**，运行时读取；读不到时（精简镜像、测试临时
目录）回落到已发布版本号，绝不抛异常——版本号是展示信息，不该让服务起不来。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

# 回落值：仅在读不到 pyproject.toml 时使用。升版时与 pyproject 一起改。
FALLBACK_VERSION = "0.2.0"

# 相对本文件定位项目根：src/version.py → 上一级即项目根
_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


@lru_cache(maxsize=1)
def app_version() -> str:
    """读取项目版本号（pyproject.toml 为唯一真源，读不到则回落）。"""
    import tomllib

    try:
        with _PYPROJECT.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return FALLBACK_VERSION
    version = data.get("project", {}).get("version")
    return version if isinstance(version, str) and version else FALLBACK_VERSION
