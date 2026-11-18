"""小浣熊登录参数加密：AES-CFB(IV 前置, Base64) 与手机号归一。"""

from __future__ import annotations

import base64

from src.provider.raccoon import login as raccoon_login


def test_normalize_phone_strips_separators_and_country_code():
    normalize = raccoon_login.normalize_phone
    assert normalize("13900000001") == "13900000001"
    assert normalize("+86 139 0000 0001") == "13900000001"
    assert normalize("86-139-0000-0001") == "13900000001"
    assert normalize("(139)0000-0001") == "13900000001"
    assert normalize("（139）00000001") == "13900000001"
    # 只有 13 位且以 86 开头才去前缀（避免把 8613900000001 之外的情况切错）
    assert normalize("861390000000") == "861390000000"
    assert normalize("") == ""
    assert normalize(None) == ""  # type: ignore[arg-type]


def test_encrypt_param_prefixes_iv_and_round_trips():
    iv = bytes(range(16))
    encoded = raccoon_login.encrypt_param("13900000001", iv=iv)
    raw = base64.b64decode(encoded)
    # 前 16 字节就是 IV，后面是 AES-CFB 密文
    assert raw[:16] == iv
    assert raccoon_login.decrypt_param(encoded) == "13900000001"


def test_encrypt_param_uses_fresh_iv_by_default():
    """每次随机 IV：同一明文得到不同密文（固定 IV 会变成可关联指纹）。"""
    first = raccoon_login.encrypt_param("secret")
    second = raccoon_login.encrypt_param("secret")
    assert first != second
    assert raccoon_login.decrypt_param(first) == "secret"
    assert raccoon_login.decrypt_param(second) == "secret"


def test_encrypt_param_keeps_utf8_intact():
    """CFB 是流模式，非 ASCII 也能原样往返（密码里偶有全角字符）。"""
    encoded = raccoon_login.encrypt_param("密码Abc12345")
    assert raccoon_login.decrypt_param(encoded) == "密码Abc12345"


def test_login_key_and_password_floor_match_upstream():
    """口径常量对齐实测：key 是前端硬编码串，密码下限 8 位。"""
    assert raccoon_login.LOGIN_KEY == b"senseraccoon2023"
    assert raccoon_login.MIN_PASSWORD_LENGTH == 8