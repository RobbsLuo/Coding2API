# coding2api

Unified OpenAI-compatible gateway for the **CodeBuddy** and **TRAE SOLO** coding-agent
upstream channels, with a shared credential pool, unified scheduling, and per-user usage stats.

> [!WARNING]
> This project bridges reverse-engineered third-party endpoints and is intended for study
> and research only. It has not been security-audited. Do not expose it to the public
> internet without a reverse proxy, authentication, and an IP allowlist.

## Features

- **OpenAI-compatible surface**: `/v1/chat/completions` (streaming and non-streaming), `/v1/models`
- **Two upstreams, one model namespace**: flat model names auto-route by health; `model@provider` pins an upstream
- **Three-state health + tiered cooldowns**: quota exhausted 12h, rate limited 60s, consecutive errors 10m, dead session disabled
- **Shared credential pool**: admins maintain credentials, everyone shares them; usage is tracked per user
- **Encrypted credentials at rest**: Fernet (AES-128-CBC + HMAC), key from `APP_SECRET`
- **Privacy-preserving stats**: never stores prompts, completions, headers, tokens, or tool arguments; 90-day detail, permanent hourly rollups
- **Usage charts**: request trends by upstream, per-model trends (Top N), and per-upstream breakdown, all labeled with official brand logos
- **Hardened admin surface**: login rate limiting (global/IP/username + PBKDF2 concurrency cap), CSRF checks on writes, request body limits, security headers, Host allowlist
- **Model catalog hygiene**: `MODEL_BLOCKLIST` filters placeholder/legacy models; cached model list as fallback when upstreams fail; credit rates and token limits passed through to `/v1/models` and the Playground

## Quick start

```bash
uv sync
uv run python scripts/hash_password.py admin          # prompts for a password
cd web && pnpm install && pnpm build && cd ..

APP_SECRET="pick-a-random-string" ADMIN_USERNAMES=admin \
  uv run python -m uvicorn src.main:build_app --factory --port 8000
```

Open <http://127.0.0.1:8000>, sign in, add credentials, create an API key, then:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-your-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"hello"}]}'
```

Point any OpenAI-compatible client at `http://127.0.0.1:8000/v1`.

## Documentation

Detailed documentation is written in Chinese:

| Document | Content |
|---|---|
| [`README.md`](README.md) | Full setup, usage, and configuration guide |
| [`PROPOSAL.md`](PROPOSAL.md) | Design decisions, scope, feasibility findings, risks |
| [`TECHNICAL.md`](TECHNICAL.md) | Stack, module specs, provider protocol, request flow, testing |
| [`diagrams/coding2api-architecture.html`](diagrams/coding2api-architecture.html) | Architecture diagram |

## License

MIT — see [LICENSE](LICENSE). Attribution for the upstream projects this work learned from is in [NOTICE](NOTICE).
