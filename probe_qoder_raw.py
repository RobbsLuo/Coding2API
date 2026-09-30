"""一次性：打印 Qoder 模型清单接口原始返回（GET + COSY 签名）。"""
import asyncio
import json

import httpx

from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.repo import CredentialRepository
from src.provider.qoder.client import QoderClient, realm_for
from src.provider.qoder.cosy import qoder_encode
from src.provider.qoder.credential import parse_credential
from src.provider.qoder.events import EP_MODELS, MODELS_SIGN_PLAIN, gateway_candidates


async def main() -> None:
    settings = Settings()
    db = Database("data/coding2api.sqlite3")
    repo = CredentialRepository(db, CredentialCipher(settings.app_secret))
    row = db.connect().execute(
        "SELECT id FROM credentials WHERE provider='qoder' LIMIT 1").fetchone()
    cred = parse_credential(repo.credential_data(row["id"]))
    host = gateway_candidates(realm_for(cred))[0]
    client = QoderClient(host="", gateway=host)
    session = client.sessions.get(cred)
    sign_body = qoder_encode(MODELS_SIGN_PLAIN)
    url = host + EP_MODELS
    headers = session.headers(body=sign_body, raw_url=url, model_key="",
                              sse=False, accept="application/json")
    async with httpx.AsyncClient(timeout=30.0) as http:
        resp = await http.get(url, headers=headers)
    payload = resp.json()
    print("HTTP", resp.status_code)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    await client.aclose()


asyncio.run(main())
