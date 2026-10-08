# Coding2API

Unified OpenAI-compatible gateway for the **CodeBuddy**, **TRAE SOLO**, **OpenCode Zen**,
**Kilo Gateway**, **Qoder**, and **CodeArts** coding-agent upstreams, with a shared
credential pool, unified scheduling, and per-user usage stats.

> [!WARNING]
> This project bridges reverse-engineered third-party endpoints and is intended for study
> and research only. It has not been security-audited. Do not expose it to the public
> internet without a reverse proxy, authentication, and an IP allowlist.

## Features

- **OpenAI-compatible surface**: `/v1/chat/completions` (streaming + non-streaming), `/v1/responses` (for the Codex CLI), `/v1/messages` (Anthropic Messages / Claude Code), `/v1/models`, `/v1/user/balance` (DeepSeek-compatible)
- **Six upstreams, one flat model namespace**: auto-routed by expiring credits, then health (ties broken by the larger remaining balance); `model@provider` pins an upstream
- **OpenCode Zen free tier** (third channel, `zen`): the free models at `opencode.ai/zen`, standard OpenAI protocol, no login. The upstream list mixes in paid models with no free/paid marker, so the gateway narrows by the `-free` suffix and probes each candidate, exposing **only the free models that actually answer anonymously** (fetched and probed live, no static allowlist). Requests automatically satisfy the free-tier gate; responses have the gate's injected pseudo-tool calls filtered out. Zen has no credentials — one virtual pool row lets it be scheduled, paused, and counted like any other channel.
- **Kilo Gateway free tier** (fourth channel, `kilo`): the free models at `api.kilo.ai/api/gateway`, standard OpenAI protocol, no login. Filtered by the authoritative per-model `isFree` flag (no probing, to conserve the small free quota); free models are marked **x0**. Same credential-less virtual-row model as Zen.
- **Qoder** (fifth channel, `qoder`): a **real-account** upstream ([Qoder](https://qoder.com)) reached via device-code PKCE login. The upstream speaks a private COSY protocol (custom Base64 body + envelope SSE); the gateway signs and unwraps it, so the surface stays standard OpenAI. Supports quota probing and campaign-based daily check-in. The campaign refreshes **daily at 10:00 (UTC+8)** (valid 30 days after claiming), and the background task seals per that window rather than per calendar day: a "already claimed" seen just after midnight seals only until 09:59, so the new campaign is claimed automatically at 10:00 without a manual click. Qoder sometimes wraps its own inference-node failures in a 400 (`[FAIL]node:…`); the gateway classifies these as a **model-scoped transient fault** and returns a "model temporarily unavailable" 503 instead of misreporting a missing model or exhausted pool.
- **CodeArts** (sixth channel, `codearts`): a **real-account** upstream ([Huawei Cloud CodeArts](https://codearts.huaweicloud.com)) reached via OAuth2 PKCE → STS (AK/SK signing + DPoP refresh). The portal redirects the authorization code to the *user's* `127.0.0.1` callback, so the login flow asks the user to paste that URL back and exchanges the code server-side. The upstream emits cumulative-text SSE, which the gateway reduces to incremental events. **No daily check-in** — the free quota is a **daily pool of 10M tokens that resets at midnight (no rollover)**, so token auto-refresh is the keep-alive; the day's remainder is registered as quota expiring at midnight so scheduling **burns it first**. The upstream meters in tokens, normalized to **credits** (1 credit = 10,000 tokens → the pool caps at 1000 credits).
- **Expiry-aware scheduling**: among credits expiring within `QUOTA_EXPIRY_WINDOW_SECONDS` (default 36h), the largest balance is burned first; ties fall to `QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS` (default 7 days), so near-expiry quota is not wasted. CodeArts' daily pool lands in this ladder every day (its remainder expires at midnight), so it is consumed before other channels; units are unified to credits
- **Conversation stickiness**: a multi-turn conversation keeps one credential and only rotates on error. Identified by an explicit id when the client sends one (`conversation_id` / `conversationId` / `prompt_cache_key`, top-level or in `metadata`), otherwise by a message-prefix fingerprint. Pinned credentials always win
- **Cached-token accounting**: TRAE's `cache_read_input_tokens` / `cache_creation_input_tokens` are mapped to per-request `cached_tokens` and surfaced in stats; the "Token usage" card also shows a **cache hit rate** (cached ÷ input, 1 decimal), or `—` when cache was never reported / input is 0
- **Three-state health + tiered cooldowns**: quota exhausted 12h, rate-limited 60s, consecutive errors 10m, dead session disabled; model-scoped limits sideline only that model
- **Shared credential pool**: admins maintain credentials, everyone shares them; usage tracked per user
- **Three-role accounts**: `admin` / `operator` / `viewer` stored in SQLite. New users get a one-time activation link to set their own password (no shared initial secret) and must change an admin-reset password on first login; changing a role or disabling an account revokes its sessions immediately. Logins and write operations are audited
- **Credential automation**: device-code login, account switching, quota probing, daily check-in (with streak), token pre-refresh, growth-center jobs (CodeBuddy only: travel gifts, Buddy dispatch, task accept/claim, streak redemption, lottery, blind boxes; irreversible steps off via `GROWTH_IRREVERSIBLE_ACTIONS=false`)
- **Per-credential pause**: "Pause" removes one credential from *chat traffic only* — probing, refresh, check-in, growth and activity tasks keep running (they honor only the system hard-disable `disabled`). Distinct from "Disabled", which means the upstream rejected the session and needs re-login + "Restore". OpenCode Zen is the one credential-less channel: pausing its virtual row is the only way to disable it permanently — deleting it just re-seeds on restart
- **Activity reporting** (CodeBuddy only, **off by default**): `ACTIVITY_REPORT_ENABLED=true` posts one chat-activity event per account per day to keep the growth-center streak alive. The upstream needs a `userId` and silently drops reports without one (HTTP 200 `{"code":0}`, streak unchanged); when the credential has no `user_id`, the gateway falls back to the bearer JWT `sub`. Upstream-internal and may break without notice — not a reliability feature (terms forbid scripted tampering: disqualification + clawback)
- **Pool health endpoint**: `GET /healthz` returns `{status, service, version, credentials:{total,ready,cooling,paused,disabled}}` (unauthenticated) so monitors can alert when the pool is exhausted (`ready=0`: alive but unusable). `GET /health` stays a pure liveness probe. Buckets are mutually exclusive and sum to `total`, using the scheduler's own "selectable" rule
- **Per-key routing policy**: a key can be bound to one provider (`provider_binding`) and/or restricted by source IP (`allowed_ips`, comma-separated IP/CIDR, empty = unrestricted). A bound key requesting a model owned by the other provider gets a 400 naming the real owner. IPs are checked at auth time; `X-Forwarded-For` is **ignored by default** and only honored with `TRUST_PROXY=true`, using the *last* entry (fits exactly one trusted reverse proxy). No per-key quotas / multi-tenancy
- **Runtime settings & background-task view** (*Tasks & Settings* page): 36 settings (default model, blocklist, context-compression knobs, fallback groups, both expiry windows, sticky TTL, irreversible growth actions, growth/probe/refresh/catalog intervals, both pacer bounds, the per-channel chat intervals, activity toggle/hour, and the alerting rules) apply immediately without a restart. **DB overrides beat `.env`**; rows are marked "DB override" and can be reset. Startup-only knobs (`APP_SECRET`, `PORT`, `DATA_DIR`, allowlists) are excluded. The same page lists the 8 background tasks with interval, last run, latest result and error — task state is **in-process only** (`GET /api/tasks`, admin, 30s refresh), resets on restart, and a no-op wake-up is not counted as a run
- **Token-expiry visibility**: a "token remaining" column, red below `TOKEN_EXPIRY_WARNING_SECONDS`. Expiry comes from explicit `expires_at` and **falls back to the JWT `exp`** — CodeBuddy's token responses carry no expiry (measured), so without the fallback it is always 0 and CodeBuddy tokens would never pre-refresh (they'd only be hard-disabled on a 401). Both missing → `—`, never guessed; `iat` is persisted to `credentials.token_issued_at` for diagnostics but not shown
- **Credit-change log**: the per-credential "credit record" drawer lists the **net change between consecutive quota probes** (`credit_events`, 90-day retention). It does **not** attribute changes to check-in / growth / chat — upstream logs nothing for those calls. The UI says "net change", never "check-in +5". The first probe only records a baseline (`sync`); unchanged balances are skipped; a balance going *unknown* still records a row with no delta, because that is an anomaly worth chasing rather than "no change"
- **Credit in usage stats, and `≈`**: CodeBuddy credit is the **real upstream value**; TRAE's `token_usage` frame carries tokens only, so its credit is **estimated** from official unit prices (with measured overrides where billed cache prices diverge from the published table) and marked `≈`. CodeArts benefit models are estimated 1:1 against the daily pool, also `≈`. The formulas and price tables live in `src/provider/trae/pricing.py` and `src/provider/codearts/units.py`; historical rows are backfilled with `scripts/backfill_trae_credit.py` / `scripts/convert_codearts_credit_unit.py` (`--apply` to write)
- **Encrypted credentials at rest**: Fernet (AES-128-CBC + HMAC), key from `APP_SECRET`
- **Privacy-preserving stats**: never stores prompts, completions, headers, tokens, or tool arguments; 90-day detail, permanent hourly rollups; charts by upstream and model (Top N trends, official brand logos)
- **Hardened admin surface**: login rate limiting (global/IP/username + PBKDF2 concurrency cap), CSRF checks on writes, body limits, security headers, Host allowlist
- **Model catalog hygiene**: `MODEL_BLOCKLIST` filters placeholder/legacy models; cached list as fallback when upstreams fail; credit rates and token limits passed through to `/v1/models` and the Playground
- **Credential-gated catalog**: `/v1/models` and the Playground model list only merge channels that currently have a usable credential (not paused, not hard-disabled) — never-connected channels do not show phantom models, and pausing/failing a channel drops its models until it comes back
- **Catalog persisted across restarts**: besides the in-process TTL cache, every successful fetch is snapshotted to `DATA_DIR/model_catalog.json` and **read back synchronously at startup**, so the model→channel alias table is usable in the service's first second. Flat model names no longer wait for the background warm-up (zen's free-model liveness probes take 10+ seconds) — previously a flat-name request in that window fanned out to every channel and really hit upstreams that do not serve the model (CodeBuddy answering `11102`, TRAE `4001`). Aliases are also published **per channel** as each fetch lands, and the fallback cache for a failing channel now survives a restart. A background task (`MODEL_CATALOG_MINUTES`, default 30, floor 5) keeps the catalog fresh even when nobody calls `/v1/models` — otherwise an API-only deployment (client caches its own model list) lets both the alias table and the snapshot go stale
- **Cross-channel fallback groups**: `MODEL_FALLBACK_GROUPS` (hot-updatable, default off) maps a group name to interchangeable models, e.g. `fast=glm-4.6,glm-5`. When the requested model's channels are all unavailable, the executor retries the group's other members in order (each member runs the full pick / cooldown / rotation / affinity / stats path). Streaming switches models **only before any response frame has been emitted** — a half-sent reply cannot be rolled back. `@channel` and API-key channel binding disable fallback; members absent from the model catalog are skipped (catalog not ready → all allowed)
- **Operations alerts**: a background task periodically evaluates four risks — **pool exhausted** (usable credentials below `ALERT_POOL_READY_MIN`), **a background task failing repeatedly** (`ALERT_TASK_FAILURES` consecutive failures, reset by one success), **a credential's token nearing expiry** (`ALERT_TOKEN_EXPIRY_HOURS`), and **an upstream error-rate spike** (`ALERT_ERROR_RATE_THRESHOLD` within `ALERT_ERROR_RATE_WINDOW_MINUTES`, over `usage_events` detail, with a minimum sample of `ALERT_ERROR_RATE_MIN_REQUESTS`). Every hit is persisted to `alert_events` and shown on the admin **Operations alerts** page; with `ALERT_WEBHOOK_URL` set (comma-separated for multiple) it also POSTs a JSON payload. A hit is reported once per `ALERT_SILENCE_MINUTES` window so a persistent condition does not flood the log or the chat channel; a failed webhook delivery is recorded but never fails the task. Records follow the same 90-day retention as request detail
- **Per-channel outbound proxy**: `PROVIDER_PROXIES` (startup-only, default direct) routes a channel's outbound traffic through its own proxy, e.g. `codebuddy=http://127.0.0.1:7890;qoder=socks5://127.0.0.1:1080`. Channels are `codebuddy/trae/zen/kilo/qoder/codearts`; schemes are `http/https/socks5/socks5h` (SOCKS via `httpx[socks]`). It covers **all** of that channel's outbound requests — chat streaming, quota/model fetches, background tasks (check-in / growth / refresh / activity) and OAuth login. Parsing is strict: an unknown channel, an unsupported scheme or a malformed segment fails startup rather than silently going direct. Environment proxies (`HTTP_PROXY`, …) are never honoured (`trust_env=False`), so only an explicit entry here takes effect; changing it needs a restart (it binds the connection pools)

## Quick start

```bash
uv sync
# Create the first admin (add the rest from the "Users" page later)
uv run python scripts/create_user.py admin --role admin
cd web && pnpm install && pnpm build && cd ..

APP_SECRET="pick-a-random-string" \
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

The model list is **credential-gated**: only channels that currently have a usable credential (not paused, not session-expired) are fetched and shown, so a channel you never connected never shows phantom models. A cold start lists only `zen` (its virtual credential is seeded); connecting CodeBuddy / TRAE brings their models in on the next list request. Pausing a channel or losing its credentials temporarily hides its models until it recovers.

`model@provider` pins an upstream (`glm-5.2@trae`, `mimo-v2.5-free@zen`); bare model names route automatically.

### OpenCode Zen free tier

The `zen` channel is a credential-less free tier (see the Features list). Worth knowing:

- **No credentials**: Zen needs no token. The `OpenCode Zen` pool row is a virtual placeholder (so scheduling, cooldown, and stats work as usual); its quota column reads "free tier (no quota API)". It **re-seeds on restart after deletion** — to disable it permanently, *pause* it instead of deleting. Pausing also hides zen's models from the list until you resume.
- **Free-tier gate**: the upstream wants to believe it is talking to the official client (UA version, session header, `stream:true`, `tools` containing `bash`/`read`). The gateway satisfies this for you; the injected `bash`/`read` are empty shells, and if the model actually calls them those tool calls are filtered from the response so you never see functions you did not declare. If *you* declare `bash`/`read`, they pass through untouched.
- **The upstream changes**: both the gate threshold and the free list may move. Tune the UA version with `ZEN_OPENCODE_VERSION`; change the endpoint with `ZEN_API_ENDPOINT` (must be inside `ZEN_ALLOWED_ENDPOINTS`).
- **No quota API**: health stays "no probe" (the free tier has no quota API, so probing is pointless) — kept distinct from "not probed" (a paid channel whose probe failed). The admin UI offers no *Probe* button (unknown ≠ exhausted).

> `scripts/create_user.py` writes directly to SQLite (`--db`, or `DATA_DIR`; default
> `data/coding2api.sqlite3`). On startup, an existing `secrets/users.txt` is imported
> once (existing usernames are never overwritten), and `ADMIN_USERNAMES` promotes the
> named users to `admin`. Both are **bootstrap-only** afterwards — day-to-day user and
> role management happens on the *Users* page. See [`README.md`](README.md) (Chinese)
> for the role matrix, activation flow, and the audit log.

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

### Anthropic Messages API (Claude Code)

`POST /v1/messages` serves an Anthropic Messages subset for clients that only speak the Anthropic protocol, such as [Claude Code](https://docs.anthropic.com/en/docs/claude-code). It shares the same credential selection, cooldown, rotation, accounting, and session affinity; only the inbound mapping and outbound translation differ (see [`TECHNICAL.md` §3.18](TECHNICAL.md), in Chinese):

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_AUTH_TOKEN=sk-your-key     # or ANTHROPIC_API_KEY (sent via x-api-key)
claude
```

- **Auth**: both `x-api-key` (`ANTHROPIC_API_KEY`) and `Authorization: Bearer` (`ANTHROPIC_AUTH_TOKEN`) are accepted.
- **Streaming**: `message_start` → `content_block_start/delta/stop` → `message_delta` (with `stop_reason` and usage) → `message_stop`. Anthropic has no `[DONE]` sentinel; `message_stop` ends the stream. Thinking blocks get a placeholder `signature_delta` before `content_block_stop`.
- **Non-streaming**: reuses `executor.complete` and reshapes the result.
- **`count_tokens`**: `POST /v1/messages/count_tokens` estimates input tokens locally (never calls upstream; same heuristic as context compression).
- **Not supported**: image/document blocks and Anthropic-only server tools (`web_search`, `computer`, …) are rejected with an explicit 400.

> Verification boundary: wire shapes come from the official `anthropic` Python SDK types and were validated end-to-end using that SDK as the client. No end-to-end run with the actual Claude Code has been performed.

## Upgrading

**Restart the process after changing anything under `src/`.** The backend loads routes and
assembly at startup only; the process manager restarts a process when it *exits* —
a keep-alive policy is not a hot reload. The frontend is different: the backend serves
`web/dist` with `FileResponse`, reading from disk on every request, so a rebuild just needs
a browser refresh.

Updating them independently produces a **new frontend against an old backend**. The page
loads (static files are current) but new endpoints fail: the old process has no such route,
unmatched `/api/*` returns a JSON `404`, and the client collapses that into a generic error
with an unrelated message. This happened on the B5 rollout — creating a user reported
"username may already exist, or the role is invalid" when the real cause was `/api/users`
being a `404` because the running process predated the migration (the old DB still had
`PRAGMA user_version` = 13 and no `users` table).

```bash
docker compose up -d --force-recreate                # Docker / compose
sudo systemctl restart coding2api                    # systemd

# Confirm the upgrade took effect (check the version first, then the routes)
sqlite3 data/coding2api.sqlite3 "PRAGMA user_version;"                     # expect 15
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/users   # expect 401; 404 = old backend
```

Schema upgrades are additive — `users` / `audit_events` are new tables and existing rows
(credentials, usage) are preserved. The restart performs the migration and bootstrap in one
step. Details in [`TECHNICAL.md` §6.4](TECHNICAL.md) and [`README.md`](README.md) (Chinese).

## Documentation

Detailed documentation is written in Chinese:

| Document | Content |
|---|---|
| [`README.md`](README.md) | Full setup, usage, and configuration guide |
| [`PROPOSAL.md`](PROPOSAL.md) | Design decisions, scope, feasibility findings, risks |
| [`TECHNICAL.md`](TECHNICAL.md) | Stack, module specs, provider protocol, request flow, testing |
| [`diagrams/coding2api-architecture.html`](diagrams/coding2api-architecture.html) | Architecture diagram |
| [`diagrams/coding2api-request-sequence.html`](diagrams/coding2api-request-sequence.html) | Request main-chain sequence diagram |
| [`diagrams/coding2api-credential-lifecycle.html`](diagrams/coding2api-credential-lifecycle.html) | Credential scheduling lifecycle |

## License

MIT — see [LICENSE](LICENSE). Attribution for the upstream projects this work learned from is in [NOTICE](NOTICE).
