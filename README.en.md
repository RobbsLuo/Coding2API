# Coding2API

Unified OpenAI-compatible gateway for the **CodeBuddy** and **TRAE SOLO** coding-agent
upstream channels, with a shared credential pool, unified scheduling, and per-user usage stats.

> [!WARNING]
> This project bridges reverse-engineered third-party endpoints and is intended for study
> and research only. It has not been security-audited. Do not expose it to the public
> internet without a reverse proxy, authentication, and an IP allowlist.

## Features

- **OpenAI-compatible surface**: `/v1/chat/completions` (streaming + non-streaming), `/v1/responses` (for the Codex CLI), `/v1/models`, `/v1/user/balance` (DeepSeek-compatible)
- **Two upstreams, one flat model namespace**: auto-routed by expiring credits, then health; `model@provider` pins an upstream
- **Expiry-aware scheduling**: among credits expiring within `QUOTA_EXPIRY_WINDOW_SECONDS` (default 36h), the largest balance is burned first; ties fall to `QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS` (default 7 days), so near-expiry quota is not wasted
- **Conversation stickiness**: a multi-turn conversation keeps one credential and only rotates on error. Identified by an explicit id when the client sends one (`conversation_id` / `conversationId` / `prompt_cache_key`, top-level or in `metadata`), otherwise by a message-prefix fingerprint. Pinned credentials always win
- **Cached-token accounting**: TRAE's `cache_read_input_tokens` / `cache_creation_input_tokens` are mapped to per-request `cached_tokens` and surfaced in stats
- **Three-state health + tiered cooldowns**: quota exhausted 12h, rate-limited 60s, consecutive errors 10m, dead session disabled; model-scoped limits sideline only that model
- **Shared credential pool**: admins maintain credentials, everyone shares them; usage tracked per user
- **Credential automation**: device-code login, account switching, quota probing, daily check-in (with streak), token pre-refresh, growth-center jobs (CodeBuddy only: travel gifts, Buddy dispatch, task accept/claim, streak redemption, lottery, blind boxes; irreversible steps off via `GROWTH_IRREVERSIBLE_ACTIONS=false`)
- **Per-credential pause**: "Pause" removes one credential from *chat traffic only* — probing, refresh, check-in, growth and activity tasks keep running (they honor only the system hard-disable `disabled`). Distinct from "Disabled", which means the upstream rejected the session and needs re-login + "Restore"
- **Activity reporting** (CodeBuddy only, **off by default**): `ACTIVITY_REPORT_ENABLED=true` posts one chat-activity event per account per day to keep the growth-center streak alive. The upstream needs a `userId` and silently drops reports without one (HTTP 200 `{"code":0}`, streak unchanged); when the credential has no `user_id`, the gateway falls back to the bearer JWT `sub`. Upstream-internal and may break without notice — not a reliability feature (terms forbid scripted tampering: disqualification + clawback)
- **Pool health endpoint**: `GET /healthz` returns `{status, service, version, credentials:{total,ready,cooling,paused,disabled}}` (unauthenticated) so monitors can alert when the pool is exhausted (`ready=0`: alive but unusable). `GET /health` stays a pure liveness probe. Buckets are mutually exclusive and sum to `total`, using the scheduler's own "selectable" rule
- **Per-key routing policy**: a key can be bound to one provider (`provider_binding`) and/or restricted by source IP (`allowed_ips`, comma-separated IP/CIDR, empty = unrestricted). A bound key requesting a model owned by the other provider gets a 400 naming the real owner. IPs are checked at auth time; `X-Forwarded-For` is **ignored by default** and only honored with `TRUST_PROXY=true`, using the *last* entry (fits exactly one trusted reverse proxy). No per-key quotas / multi-tenancy
- **Runtime settings & background-task view** (*Tasks & Settings* page): 13 settings (default model, blocklist, both expiry windows, sticky TTL, irreversible growth actions, growth/probe intervals, both pacer bounds, CodeBuddy chat interval, activity toggle/hour) apply immediately without a restart. **DB overrides beat `.env`**; rows are marked "DB override" and can be reset. Startup-only knobs (`APP_SECRET`, `PORT`, `DATA_DIR`, allowlists) are excluded. The same page lists the 6 background tasks with interval, last run, latest result and error — task state is **in-process only** (`GET /api/tasks`, admin, 30s refresh), resets on restart, and a no-op wake-up is not counted as a run
- **Token-expiry visibility**: a "token remaining" column, red below `TOKEN_EXPIRY_WARNING_SECONDS`. Expiry comes from explicit `expires_at` and **falls back to the JWT `exp`** — CodeBuddy's token responses carry no expiry (measured), so without the fallback it is always 0 and CodeBuddy tokens would never pre-refresh (they'd only be hard-disabled on a 401). Both missing → `—`, never guessed; `iat` is persisted to `credentials.token_issued_at` for diagnostics but not shown
- **Credit-change log**: the per-credential "credit record" drawer lists the **net change between consecutive quota probes** (`credit_events`, 90-day retention). It does **not** attribute changes to check-in / growth / chat — upstream logs nothing for those calls. The UI says "net change", never "check-in +5". The first probe only records a baseline (`sync`); unchanged balances are skipped; a balance going *unknown* still records a row with no delta, because that is an anomaly worth chasing rather than "no change"
- **Encrypted credentials at rest**: Fernet (AES-128-CBC + HMAC), key from `APP_SECRET`
- **Privacy-preserving stats**: never stores prompts, completions, headers, tokens, or tool arguments; 90-day detail, permanent hourly rollups; charts by upstream and model (Top N trends, official brand logos)
- **Hardened admin surface**: login rate limiting (global/IP/username + PBKDF2 concurrency cap), CSRF checks on writes, body limits, security headers, Host allowlist
- **Model catalog hygiene**: `MODEL_BLOCKLIST` filters placeholder/legacy models; cached list as fallback when upstreams fail; credit rates and token limits passed through to `/v1/models` and the Playground

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

### Responses API (Codex CLI)

`POST /v1/responses` serves a Responses subset for clients that only speak the Responses API, such as the [Codex CLI](https://github.com/openai/codex). It shares the same credential selection, cooldown, rotation, accounting, and session affinity as `/v1/chat/completions`; only the inbound mapping and outbound translation differ (see [`TECHNICAL.md` §3.7](TECHNICAL.md), in Chinese):

```bash
export CODING2API_KEY=sk-your-key
codex -c "model_providers.coding2api={ name='coding2api', base_url='http://127.0.0.1:8000/v1', wire_api='responses', env_key='CODING2API_KEY' }" \
      -c model_provider=coding2api \
      -c model='glm-5.2' \
      'your task'
```

Streaming text, reasoning summaries, function tool calls, and `finish_reason=length` → `response.incomplete` are supported. `store=true`, `previous_response_id`, and Responses-only tools (`web_search`, `computer`, `custom`, …) are rejected with an explicit 400 rather than silently degraded. `include=["reasoning.encrypted_content"]`, which Codex always sends, is accepted and ignored.

> Verification boundary: no Codex CLI was available on the development machine. Wire shapes come from the official `openai` Python SDK types and were validated end-to-end using that SDK as the client, plus a smoke test against the real upstream. No end-to-end run with the actual Codex CLI has been performed.

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
