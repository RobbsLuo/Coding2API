# Coding2API

Unified OpenAI-compatible gateway for the **CodeBuddy** and **TRAE SOLO** coding-agent
upstream channels, with a shared credential pool, unified scheduling, and per-user usage stats.

> [!WARNING]
> This project bridges reverse-engineered third-party endpoints and is intended for study
> and research only. It has not been security-audited. Do not expose it to the public
> internet without a reverse proxy, authentication, and an IP allowlist.

## Features

- **OpenAI-compatible surface**: `/v1/chat/completions` (streaming and non-streaming), `/v1/responses` (Responses API, for the Codex CLI), `/v1/models`, `/v1/user/balance` (DeepSeek-compatible balance query)
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
- **Per-credential pause**: the admin credential menu's "Pause" removes one credential from *chat traffic only* — quota probing, token refresh, daily check-in, growth-center and activity-report tasks keep running (they only honor the system hard-disable `disabled`). Unlike "Disabled", which means the upstream rejected the session and requires re-login plus "Restore"
- **Pool health endpoint**: `GET /healthz` reports `{status, service, version, credentials:{total,ready,cooling,paused,disabled}}` (unauthenticated) so monitors can alert when the pool is exhausted (`ready=0`, i.e. alive but unusable) — the bare `GET /health` stays as a pure liveness probe. The five buckets are mutually exclusive and sum to `total`, using the scheduler's own "selectable" definition
- **Per-key routing policy**: an API key can be bound to one provider (`provider_binding`) and/or restricted to source IPs (`allowed_ips`, comma-separated IP/CIDR, empty = unrestricted). A bound key that requests a model owned by the other provider gets a 400 naming the real owner instead of silently re-routing or wasting an upstream call. Source IPs are checked at auth time; `X-Forwarded-For` is **ignored by default** (clients can forge it) and only honored with `TRUST_PROXY=true`, where the *last* XFF entry is used — so that flag fits exactly one trusted reverse proxy in front. No per-key quotas / multi-tenancy
- **Runtime-configurable settings**: 13 settings (default model, model blocklist, both expiry windows, conversation-sticky TTL, irreversible growth actions, growth/probe intervals, both pacer bounds, CodeBuddy chat interval, activity-report toggle/hour) can be changed from the admin UI's *Runtime settings* page and take effect immediately — no restart. **DB overrides win over `.env`**; the page marks each row as "DB override" and offers "Reset to default" to fall back to `.env`. Startup-only knobs (`APP_SECRET`, `PORT`, `DATA_DIR`, allowlists) are deliberately excluded
- **Token-expiry visibility**: each credential shows a remaining-lifetime bar plus its last-renewal time, red-flagged below `TOKEN_EXPIRY_WARNING_SECONDS`. The expiry comes from the credential's explicit `expires_at` and **falls back to the access token's JWT `exp`** — CodeBuddy's token responses carry no expiry at all (measured), so without the fallback the value is always 0 and CodeBuddy tokens would never pre-refresh (only hard-disabled on a 401). Last-renewal comes from the JWT `iat`, and the bar is scaled to that token's own lifetime (`exp - iat`) — CodeBuddy lives 50+ days and TRAE ~12, so a fixed scale would pin the former at 100%. Missing `iat` → no bar, numbers only. Both sources missing → shown as unknown, never guessed
- **Credit-change log**: the per-credential "credit record" drawer lists the **net change between consecutive quota probes** (`credit_events`, same 90-day retention as usage detail). It deliberately does **not** attribute changes to check-in / growth / chat: upstream logs nothing for those calls, so a diff cannot tell who added the points. The UI says "net change", never "check-in +5". First probe only records a baseline (`sync`); unchanged balances are skipped; a balance going *unknown* still records a row with no delta, because that is an anomaly worth chasing rather than "no change"

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
