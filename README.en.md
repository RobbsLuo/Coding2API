# Coding2API

Unified OpenAI-compatible gateway for the **CodeBuddy** and **TRAE SOLO** coding-agent
upstream channels, with a shared credential pool, unified scheduling, and per-user usage stats.

> [!WARNING]
> This project bridges reverse-engineered third-party endpoints and is intended for study
> and research only. It has not been security-audited. Do not expose it to the public
> internet without a reverse proxy, authentication, and an IP allowlist.

## Features

- **OpenAI-compatible surface**: `/v1/chat/completions` (streaming and non-streaming), `/v1/models`, `/v1/user/balance` (DeepSeek-compatible balance query)
- **Two upstreams, one model namespace**: flat model names auto-route — credits expiring within the window first, then health; `model@provider` pins an upstream
- **DeepSeek-compatible balance query**: `GET /v1/user/balance` aggregates the pool's probed quotas, so clients like Cherry Studio / cc-switch can display remaining credits
- **CodeBuddy growth-center automation** (CodeBuddy only): claims Buddy travel gifts, departs Buddy, accepts/claims tasks, streak redemption, lottery and blind boxes; irreversible actions can be switched off via `GROWTH_IRREVERSIBLE_ACTIONS=false`
- **Activity reporting** (CodeBuddy only, **off by default**): `ACTIVITY_REPORT_ENABLED=true` posts one chat-activity event per account per day to keep the growth-center streak alive. The upstream requires a `userId` and silently drops reports without one (HTTP 200 `{"code":0}`, no streak change); when the OAuth credential has no `user_id`, the gateway falls back to the bearer JWT `sub`. Risk: the activity terms forbid scripted data tampering (penalty: disqualification and clawback), and the event shape is upstream-internal and may break without notice — not a reliability feature. Admins can also trigger one report manually from the credential menu.
- **Expiry-aware scheduling**: burns the largest first among credits expiring within `QUOTA_EXPIRY_WINDOW_SECONDS` (default 36h); when that ties, a second level over `QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS` (default 7 days) breaks the tie, so near-expiry quota is never wasted
- **Conversation stickiness**: multi-turn conversations keep the same credential (matched by message-prefix fingerprint) and only rotate on errors, avoiding upstream risk control and lost prompt cache; pinned credentials always win over stickiness
- **Cached-token accounting**: TRAE's `cache_read_input_tokens` / `cache_creation_input_tokens` are mapped to per-request `cached_tokens` and surfaced in stats
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
