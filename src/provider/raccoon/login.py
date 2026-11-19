"""小浣熊网页登录的请求参数加密（IAM 登录接口的 phone / password）。

口径抄自官方前端 `loginWithPassword({nation_code, phone, password})`：两个字段
都过同一个 `yv()` —— **AES-CFB**、key 硬编码 `senseraccoon2023`、随机 16 字节 IV
前置、整体 Base64。这里不是猜测得到的，而是按错误信息反证过的：

* 明文手机号 → `400 100003 param phone encrypted error`（密文格式不对）；
* 按上式加密手机号 + 明文密码 → `400 100003 param password encrypted error`；
* 两个都加密、但密码只写 6 位 → `400 100002 param password invalid`（格式校验）；
* 两个都加密、密码 ≥8 位 → `401 AUTH_ACCOUNT_OR_PASSWORD_INVALID`
  （密文被正确解开、格式通过，只是账号密码不对）。

即：**加密口径正确、无验证码前置**（阿里云验证码只挂在短信表单上）。本模块
只做「字面量 → 密文」与手机号归一，不发网络请求（在 `client.login` 里）。

**2026-11-19 换链路**：上面的报文格式对 IAM 接口
`POST /api/iam/v1/auth/login/password/phone` 同样成立（加密口径没变，只有
`nation_code` 要带 `+`）。旧接口 `login_with_password` 仍能换 token，但不再
补发当天 `daily_grant`——见 `client.EP_LOGIN` 的注释。
"""

from __future__ import annotations

import base64
import os
import re

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

try:  # cryptography >= 43 把 CFB 挪进 decrepit；旧版本仍在原处
    from cryptography.hazmat.decrepit.ciphers.modes import CFB
except ImportError:  # pragma: no cover - 取决于安装的 cryptography 版本
    from cryptography.hazmat.primitives.ciphers.modes import CFB  # type: ignore[no-redef]

#: 官方前端硬编码的登录参数密钥（`senseraccoon2023` 的 UTF-8 字节，16 字节）。
LOGIN_KEY = b"senseraccoon2023"

#: 上游对密码长度的下限（实测 6 位回 `param password invalid`）。提前拦下来，
#: 省一次必然被拒的登录请求，也避免无意义的失败重试。
MIN_PASSWORD_LENGTH = 8

#: IAM 登录接口的 `nation_code` 带 `+` 前缀（浏览器实测 `"+86"`）。旧接口
#: `login_with_password` 用的是 `"86"`，换链路时一并改掉，别照抄旧值。
NATION_CODE = "+86"

#: 手机号里的分隔符：用户从通讯录复制常带 `+86`、空格、横线、括号。
_PHONE_SEPARATORS = re.compile(r"[\s\-()（）]")


def normalize_phone(raw: str) -> str:
    """手机号归一成 11 位数字：去分隔符与 `+86` / `86` 前缀。

    上游要的是不带国家码的号码（`nation_code` 单独传）。这里**不校验**位数：
    号码对不对是上游的事，归一只是为了别把 `+86 139…` 这种常见写法送进去。
    """
    text = _PHONE_SEPARATORS.sub("", raw or "")
    if text.startswith("+86"):
        text = text[3:]
    elif len(text) == 13 and text.startswith("86"):
        text = text[2:]
    return text


def encrypt_param(plain: str, *, iv: bytes | None = None,
                  key: bytes = LOGIN_KEY) -> str:
    """字段值 → Base64（16 字节 IV + AES-CFB 密文）。

    `iv` 可注入（测试用固定值断言字节流）；生产每次随机，与前端一致——固定 IV
    会让同一密码每次得到同一密文，等于给上游一个可被关联的指纹。
    """
    iv = iv if iv is not None else os.urandom(16)
    encryptor = Cipher(algorithms.AES(key), CFB(iv)).encryptor()
    payload = iv + encryptor.update(plain.encode("utf-8")) + encryptor.finalize()
    return base64.b64encode(payload).decode("ascii")


def decrypt_param(encoded: str, *, key: bytes = LOGIN_KEY) -> str:
    """密文 → 明文（只给测试/排障用；生产不需要解别人的密文）。"""
    raw = base64.b64decode(encoded.encode("ascii"))
    iv, ciphertext = raw[:16], raw[16:]
    decryptor = Cipher(algorithms.AES(key), CFB(iv)).decryptor()
    return (decryptor.update(ciphertext) + decryptor.finalize()).decode("utf-8")