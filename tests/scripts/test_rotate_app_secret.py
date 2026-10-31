"""scripts/rotate_app_secret.py：APP_SECRET 轮换与凭证重加密。

`CredentialCipher` 从 APP_SECRET 派生 Fernet 密钥（无盐 SHA-256），换密钥
等于换掉全部凭证的加密键——不重加密就只能重录。本脚本是那条一次性运维路径：
全解 → 全校验 → 单事务重写，任何一条失败整体回滚。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from src.db.crypto import CredentialCipher
from tests.helpers.paths import load_script

SCRIPT = load_script("rotate_app_secret")

OLD = "old-secret-0123456789abc"
NEW = "new-secret-0123456789xyz"


def _db_with_credentials(tmp_path, count: int = 2):
    db_path = tmp_path / "coding2api.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE credentials (id TEXT PRIMARY KEY, data_enc BLOB NOT NULL)")
    cipher = CredentialCipher(OLD)
    for index in range(count):
        payload = json.dumps({"accessToken": f"tok{index}"}).encode()
        conn.execute("INSERT INTO credentials (id, data_enc) VALUES (?, ?)",
                     (f"c{index}", cipher.encrypt(payload)))
    conn.commit()
    conn.close()
    return db_path


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return dict(conn.execute("SELECT id, data_enc FROM credentials").fetchall())
    finally:
        conn.close()


def test_rotate_rewrites_all_rows_with_new_secret(tmp_path):
    db_path = _db_with_credentials(tmp_path, count=3)
    assert SCRIPT.rotate(db_path, OLD, NEW) == 3
    new_cipher = CredentialCipher(NEW)
    for key, ciphertext in _rows(db_path).items():
        expected = json.dumps({"accessToken": f"tok{key[1:]}"}).encode()
        assert new_cipher.decrypt(ciphertext) == expected
    # 旧密钥再也解不开
    from src.db.crypto import CredentialDecryptError

    for ciphertext in _rows(db_path).values():
        with pytest.raises(CredentialDecryptError):
            CredentialCipher(OLD).decrypt(ciphertext)


def test_rotate_is_idempotent_in_shape_but_uses_fresh_tokens(tmp_path):
    """重加密会生成新密文（Fernet 带随机 IV），但明文不变。"""
    db_path = _db_with_credentials(tmp_path, count=1)
    before = _rows(db_path)["c0"]
    SCRIPT.rotate(db_path, OLD, NEW)
    after = _rows(db_path)["c0"]
    assert before != after
    assert CredentialCipher(NEW).decrypt(after) == CredentialCipher(OLD).decrypt(before)


def test_rotate_aborts_without_writing_when_any_row_fails(tmp_path):
    """任一行解不开就整体中止，库保持原样（不能留下新旧混杂的库）。"""
    db_path = _db_with_credentials(tmp_path, count=2)
    # 混入一条用第三方密钥加密的行
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO credentials (id, data_enc) VALUES (?, ?)",
                 ("bad", CredentialCipher("other-secret-0123456789").encrypt(b'{"x": 1}')))
    conn.commit()
    conn.close()
    before = _rows(db_path)
    with pytest.raises(SystemExit) as caught:
        SCRIPT.rotate(db_path, OLD, NEW)
    assert "bad" in str(caught.value)
    assert _rows(db_path) == before          # 一行都没改


def test_rotate_rejects_ciphertext_that_is_not_json(tmp_path):
    """解密成功但不是 JSON：同样视为坏行，整体中止。"""
    db_path = tmp_path / "db.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE credentials (id TEXT PRIMARY KEY, data_enc BLOB NOT NULL)")
    conn.execute("INSERT INTO credentials (id, data_enc) VALUES (?, ?)",
                 ("c0", CredentialCipher(OLD).encrypt(b"not-json")))
    conn.commit()
    conn.close()
    before = _rows(db_path)
    with pytest.raises(SystemExit) as caught:
        SCRIPT.rotate(db_path, OLD, NEW)
    assert "合法 JSON" in str(caught.value)
    assert _rows(db_path) == before


def test_rotate_rejects_same_secret(tmp_path):
    db_path = _db_with_credentials(tmp_path, count=1)
    with pytest.raises(SystemExit) as caught:
        SCRIPT.rotate(db_path, OLD, OLD)
    assert "相同" in str(caught.value)


def test_read_secret_handles_env_and_plain_text(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# 注释\nAPP_SECRET='quoted-secret'\nOTHER=1\n")
    assert SCRIPT._read_secret(env_file) == "quoted-secret"

    plain = tmp_path / "secret.txt"
    plain.write_text("plain-secret\n")
    assert SCRIPT._read_secret(plain) == "plain-secret"


def test_read_secret_rejects_empty_and_multiline(tmp_path):
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("APP_SECRET=\n")
    with pytest.raises(SystemExit):
        SCRIPT._read_secret(empty_env)

    blank = tmp_path / "blank.txt"
    blank.write_text("   \n")
    with pytest.raises(SystemExit):
        SCRIPT._read_secret(blank)

    multiline = tmp_path / "multi.txt"
    multiline.write_text("a\nb\n")
    with pytest.raises(SystemExit):
        SCRIPT._read_secret(multiline)


def test_main_end_to_end(tmp_path):
    db_path = _db_with_credentials(tmp_path, count=2)
    old_file = tmp_path / "old.env"
    old_file.write_text(f"APP_SECRET={OLD}\n")
    new_file = tmp_path / "new.env"
    new_file.write_text(f"APP_SECRET={NEW}\n")
    assert SCRIPT.main(["--db", str(db_path),
                        "--old-secret-file", str(old_file),
                        "--new-secret-file", str(new_file)]) == 0
    assert CredentialCipher(NEW).decrypt(_rows(db_path)["c0"])


def test_main_rejects_missing_database(tmp_path):
    old_file = tmp_path / "old.env"
    old_file.write_text(f"APP_SECRET={OLD}\n")
    new_file = tmp_path / "new.env"
    new_file.write_text(f"APP_SECRET={NEW}\n")
    with pytest.raises(SystemExit):
        SCRIPT.main(["--db", str(tmp_path / "nope.sqlite3"),
                     "--old-secret-file", str(old_file),
                     "--new-secret-file", str(new_file)])
