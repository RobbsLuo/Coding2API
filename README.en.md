# Coding2API

Unified OpenAI-compatible gateway for the **CodeBuddy**, **TRAE SOLO**, **OpenCode Zen**,
**Kilo Gateway**, **Qoder**, and **CodeArts** coding-agent upstreams, with a shared
credential pool, unified scheduling, and per-user usage stats.

> [!WARNING]
> This project bridges reverse-engineered third-party endpoints; study and research use
> only. Not security-audited. Do not expose it to the public internet without a reverse
> proxy, authentication, and an IP allowlist.

## Features

- **OpenAI-compatible surface**: `/v1/chat/completions` (streaming + non-streaming), `/v1/responses` (for the Codex CLI), `/v1/messages` (Anthropic Messages / Claude Code), `/v1/models`, `/v1/user/balance` (DeepSeek-compatible).
- **Six upstreams, one flat model namespace, expiry-aware scheduling**: bare model names auto-route, `model@provider` pins an upstream. Credits expiring within `QUOTA_EXPIRY_WINDOW_SECONDS` (default 36h) burn largest-balance-first (near-expiry quota not wasted), ties fall to `QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS` (default 7 days), then three-state health with tiered cooldowns (quota exhausted 12h, rate-limited 60s, consecutive errors 10m, dead session disabled; model-scoped limits sideline only that model). Units unify to credits; CodeArts' daily pool (midnight-expiring) enters this ladder daily, consumed first.
- **Two free tiers, no credentials**: `zen` (free models at `opencode.ai/zen`) and `kilo` (free models at `api.kilo.ai/api/gateway`) — both standard OpenAI protocol, no login, both **x0**. Zen's upstream list mixes in unmarked paid models, so the gateway narrows by the `-free` suffix and probes each candidate, exposing **only free models that answer anonymously** (live, no static allowlist); requests satisfy the free-tier gate, and the gate's injected pseudo-tool calls are filtered from responses. Kilo uses the authoritative per-model `isFree` flag, **no probing** (to conserve the small quota). Each gets a credential-less virtual pool row, schedulable/pausable/counted like any other channel.
- **Qoder** (fifth channel, `qoder`): a **real-account** upstream ([Qoder](https://qoder.com)) via device-code PKCE login. It speaks a private COSY protocol (custom Base64 body + envelope SSE), signed and unwrapped by the gateway, keeping the surface standard OpenAI. Supports quota probing and campaign-based daily check-in, refreshed **daily at 10:00 (UTC+8)** (valid 30 days after claiming); the task seals per that window, not per calendar day, so an "already claimed" just after midnight seals only until 09:59 and the new campaign is claimed at 10:00. Qoder sometimes wraps its own inference-node failures in a 400 (`[FAIL]node:…`); the gateway treats these as a **model-scoped transient fault**, returning a "model temporarily unavailable" 503 instead of misreporting a missing model or exhausted pool.
- **CodeArts** (sixth channel, `codearts`): a **real-account** upstream ([Huawei Cloud CodeArts](https://codearts.huaweicloud.com)) via OAuth2 PKCE → STS (AK/SK signing + DPoP refresh). The portal redirects the authorization code to the *user's* `127.0.0.1` callback, so login has the user paste that URL back, exchanged server-side. The gateway reduces the upstream's cumulative-text SSE to incremental events. **Daily check-in** claims 1000 credits/day through 2026-12-31 (`GET /v1/ops/delivery` → `POST /v1/ops/claim` → `POST /v1/ops/confirm`); `claim` alone is **not** enough — `CLAIMED` is a "claimed but unconfirmed" state upstream, so both steps always run and unconfirmed accounts get repaired. This credit ledger is **separate from** the daily token pool: the pool feeds benefit models (e.g. `deepseek-v4.1-flash`), the credits feed built-in models (GLM-5.2 / OpenPangu). The pool is 10M tokens/day, resets at midnight (no rollover), and its remainder registers as midnight-expiring quota so scheduling **burns it first**; the upstream meters tokens, normalized to **credits** (1 credit = 10,000 tokens → the pool caps at 1000) — a synthetic unit for cross-channel ordering, **not** the upstream credits.
- **Conversation stickiness**: a multi-turn conversation keeps one credential and rotates only on error, identified by an explicit client id (`conversation_id` / `conversationId` / `prompt_cache_key`, top-level or in `metadata`) or, failing that, a message-prefix fingerprint. Pinned credentials win.
- **Streaming and accounting details**: the OpenAI-compatible stream ends with the finish chunk, then a `choices: []` usage frame, then `data: [DONE]` — the shape stream-reading clients like pi-ai / DSH need for tok/s, context occupancy, and session token totals; no frame is sent when the upstream reported no usage (never a fake `0`), and unreported fields are `null` in non-streaming `usage` too. TRAE's `cache_read_input_tokens` / `cache_creation_input_tokens` map to per-request `cached_tokens`, and the "Token usage" card shows a **cache hit rate** (cached ÷ input, 1 decimal; `—` when never reported / input is 0). On CodeBuddy, `reasoning_effort` defaults to `medium` when the client omits it — without it the upstream writes the whole reasoning pass into the visible answer — while an explicit value (e.g. `low`) passes through untouched.
- **Shared credential pool**: admins maintain credentials; everyone shares them, usage tracked per user. Automation covers device-code login, account switching, quota probing, daily check-in (with streak), token pre-refresh, and growth-center jobs (CodeBuddy only: travel gifts, Buddy dispatch, task accept/claim, streak redemption, lottery, blind boxes; irreversible steps off via `GROWTH_IRREVERSIBLE_ACTIONS=false`). A **per-credential pause** removes one credential from *chat traffic only* — probing, refresh, check-in, growth and activity tasks keep running (they honor only the system hard-disable `disabled`), unlike "Disabled" (the upstream rejected the session; needs re-login + "Restore"). Zen / Kilo are credential-less: only a *pause* disables them permanently (deletion re-seeds on restart).
- **Activity reporting** (CodeBuddy only, **off by default**): `ACTIVITY_REPORT_ENABLED=true` posts one chat-activity event per account per day to keep the growth-center streak alive. The upstream needs a `userId` and silently drops reports without one (HTTP 200 `{"code":0}`, streak unchanged); with no `user_id` on the credential, the gateway falls back to the bearer JWT `sub`. Upstream-internal, may break without notice — not a reliability feature (terms forbid scripted tampering: disqualification + clawback).
- **Three-role accounts**: `admin` / `operator` / `viewer` in SQLite. New users get a one-time activation link to set their own password (no shared initial secret) and must change an admin-reset password on first login; changing a role or disabling an account revokes its sessions immediately. Logins and writes are audited. Credentials are **encrypted at rest** (Fernet: AES-128-CBC + HMAC, key from `APP_SECRET`).
- **Admin security**: login rate limiting (global/IP/username + PBKDF2 concurrency cap), CSRF checks on writes, body limits, security headers, Host allowlist. A **per-key routing policy** can bind a key to one provider (`provider_binding`) and/or restrict by source IP (`allowed_ips`, comma-separated IP/CIDR, empty = unrestricted); a bound key requesting a model owned by the other provider gets a 400 naming the real owner. IPs are checked at auth time; `X-Forwarded-For` is **ignored by default**, honored only with `TRUST_PROXY=true`, using the *last* entry (exactly one trusted reverse proxy). No per-key quotas / multi-tenancy.
- **Model catalog**: `MODEL_BLOCKLIST` filters placeholder/legacy models; a cached list is the fallback when upstreams fail. The list is **credential-gated** — only channels with a currently usable credential (not paused, not hard-disabled) are fetched and shown, so a never-connected channel never shows phantom models, and pausing/failing one drops its models until it recovers. Besides the in-process TTL cache, every successful fetch is snapshotted to `DATA_DIR/model_catalog.json` and **read back synchronously at startup**, so the model→channel alias table is usable in the first second (flat model names no longer wait for zen's 10+ second liveness probes, which fanned out to upstreams that do not serve the model — CodeBuddy `11102`, TRAE `4001`). Aliases publish **per channel** as each fetch lands, and a background task (`MODEL_CATALOG_MINUTES`, default 30, floor 5) keeps the catalog fresh even when nobody calls `/v1/models`. Credit rates and token limits pass through to `/v1/models` and the Playground.
- **Cross-channel fallback groups**: `MODEL_FALLBACK_GROUPS` (hot-updatable, default off) maps a group name to interchangeable models, e.g. `fast=glm-4.6,glm-5`. When the requested model's channels are all unavailable, the executor retries the group's other members in order (each runs the full pick / cooldown / rotation / affinity / stats path). Streaming switches models **only before any response frame is emitted** — a half-sent reply cannot be rolled back. `@channel` and API-key channel binding disable fallback; members missing from the catalog are skipped (catalog not ready → all allowed).
- **Operations alerts**: a background task evaluates four risks — **pool exhausted** (usable credentials below `ALERT_POOL_READY_MIN`), **a background task failing repeatedly** (`ALERT_TASK_FAILURES` consecutive failures, reset by one success), **a credential's token nearing expiry** (`ALERT_TOKEN_EXPIRY_HOURS`), and **an upstream error-rate spike** (`ALERT_ERROR_RATE_THRESHOLD` within `ALERT_ERROR_RATE_WINDOW_MINUTES`, over `usage_events` detail, minimum sample `ALERT_ERROR_RATE_MIN_REQUESTS`). Every hit is persisted to `alert_events` and shown on the admin **Operations alerts** page; with `ALERT_WEBHOOK_URL` set (comma-separated) it also POSTs JSON. A hit reports once per `ALERT_SILENCE_MINUTES` window so a persistent condition does not flood; a failed webhook delivery is recorded but never fails the task. Records follow request detail's 90-day retention.
- **Per-channel outbound proxy**: `PROVIDER_PROXIES` (startup-only, default direct) routes a channel's outbound traffic through its own proxy, e.g. `codebuddy=http://127.0.0.1:7890;qoder=socks5://127.0.0.1:1080`. Channels are `codebuddy/trae/zen/kilo/qoder/codearts`; schemes are `http/https/socks5/socks5h` (SOCKS via `httpx[socks]`). It covers **all** of that channel's outbound requests — chat streaming, quota/model fetches, background tasks, and OAuth login. Parsing is strict: an unknown channel, unsupported scheme, or malformed segment fails startup rather than silently going direct. Environment proxies (`HTTP_PROXY`, …) are never honoured (`trust_env=False`); changing it needs a restart (it binds the connection pools).
- **Runtime settings & background-task view** (*Tasks & Settings* page): 38 settings (default model, blocklist, context-compression knobs, fallback groups, both expiry windows, sticky TTL, irreversible growth actions, growth/probe/refresh/catalog intervals, both pacer bounds, the per-channel chat intervals, activity toggle/hour, and the alerting rules) apply immediately without a restart. **DB overrides beat `.env`**; rows are marked "DB override" and can be reset. Startup-only knobs (`APP_SECRET`, `PORT`, `DATA_DIR`, allowlists) are excluded. The same page lists the 9 background tasks with interval, last run, latest result and error — task state is **in-process only** (`GET /api/tasks`, admin, 30s refresh), resets on restart, and a no-op wake-up is not counted as a run. The page is tabbed (one tab per task plus gateway groups); **tabs are compact single-line** (`text-xs`, horizontal scroll instead of wrapping), long task names shortened on the tab (`渠道模型刷新` / `模型列表刷新`) while card titles and alerts keep the full names.
- **Token-expiry visibility**: a "token remaining" column, red below `TOKEN_EXPIRY_WARNING_SECONDS`. Expiry comes from explicit `expires_at` and **falls back to the JWT `exp`** — CodeBuddy's token responses carry no expiry (measured), so without the fallback it would always be 0 and CodeBuddy tokens would never pre-refresh (only hard-disabled on a 401). Both missing → `—`, never guessed; `iat` is persisted to `credentials.token_issued_at` for diagnostics but not shown.
- **Pool health endpoint**: `GET /healthz` returns `{status, service, version, credentials:{total,ready,cooling,paused,disabled}}` (unauthenticated) so monitors can alert when the pool is exhausted (`ready=0`: alive but unusable). `GET /health` stays a pure liveness probe. Buckets are mutually exclusive and sum to `total`, using the scheduler's own "selectable" rule.
- **Stats: credit, cost, and privacy**: the "credit record" drawer lists the **net change between consecutive quota probes** (`credit_events`, 90-day retention), attributing nothing to check-in / growth / chat (upstream logs nothing there), so the UI says "net change", never "check-in +5"; the first probe records only a baseline, unchanged balances are skipped, and a balance going *unknown* still records a row with no delta (an anomaly worth chasing). CodeBuddy credit is the **real upstream value**; TRAE's `token_usage` frame carries tokens only, so its credit is **estimated** from official unit prices (measured overrides where billed cache prices diverge), marked `≈`; CodeArts benefit models are estimated 1:1 against the daily pool, also `≈` (formulas in `src/provider/trae/pricing.py` and `src/provider/codearts/units.py`; backfill with `scripts/backfill_trae_credit.py` / `scripts/convert_codearts_credit_unit.py`, `--apply` to write). The **Cost (est.)** card and **Cost** columns **estimate** tokens × public list prices, *not* the real charge: prices from the [models.dev](https://models.dev) catalog (`input` / `output` / `cache_read`, **USD per million tokens**) are computed as `(input − cached) × input + cached × cache_read + output × output` and converted to CNY at the **rate in effect when the row was written** (`USD_CNY_RATE`, default `6.70`; CNY primary, USD secondary). Costs are **fixed at write time** (`usage_events.cost_usd` / `cost_cny`), **never recomputed**; a model with no models.dev match, or a request with no input tokens, contributes nothing and aggregates render `—` (never a fake `0`), so the figure is a **lower bound**. The price table is refreshed by the background `PRICE_CATALOG_MINUTES` task (default daily, floor 60 min), snapshotted to `data/model_prices.json`, replayed on startup with zero upstream requests (a fresh deploy fetches once in the background). Stats never store prompts, completions, headers, tokens, or tool arguments: 90-day detail, permanent hourly rollups, charts by upstream and model (Top N trends, official brand logos).

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

Point any OpenAI-compatible client at `http://127.0.0.1:8000/v1`. The model list is **credential-gated** (see Features): a cold start lists only `zen` / `kilo` (their virtual credentials are seeded); connecting CodeBuddy / TRAE adds their models on the next list request, and pausing a channel or losing its credentials hides its models until it recovers. `model@provider` pins an upstream (`glm-5.2@trae`, `mimo-v2.5-free@zen`); bare model names route automatically.

> `scripts/create_user.py` writes directly to SQLite (`--db`, or `DATA_DIR`; default
> `data/coding2api.sqlite3`). On startup, an existing `secrets/users.txt` is imported
> once (existing usernames are never overwritten), and `ADMIN_USERNAMES` promotes the
> named users to `admin`. Both are **bootstrap-only** afterwards — day-to-day user and
> role management happens on the *Users* page. See [`README.md`](README.md) (Chinese)
> for the role matrix, activation flow, and the audit log.

### OpenCode Zen free tier

The `zen` channel is a credential-less free tier (see Features). Notable:

- **No credentials**: the `OpenCode Zen` pool row is a virtual placeholder (scheduling, cooldown, and stats work as usual); its quota column reads "free tier (no quota API)". It **re-seeds on restart after deletion** — *pause* it to disable permanently; pausing also hides zen's models until you resume.
- **Free-tier gate**: the upstream wants to believe it is talking to the official client (UA version, session header, `stream:true`, `tools` containing `bash`/`read`). The gateway satisfies this; the injected `bash`/`read` are empty shells, and if the model calls them those calls are filtered out, so you never see functions you did not declare. If *you* declare `bash`/`read`, they pass through untouched.
- **The upstream changes**: both the gate threshold and the free list may move. Tune the UA version with `ZEN_OPENCODE_VERSION`; change the endpoint with `ZEN_API_ENDPOINT` (must be inside `ZEN_ALLOWED_ENDPOINTS`).
- **No quota API**: health stays "no probe" (no quota API, so probing is pointless) — distinct from "not probed" (a paid channel whose probe failed). The admin UI offers no *Probe* button (unknown ≠ exhausted).

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

`POST /v1/messages` serves an Anthropic Messages subset for clients that only speak the Anthropic protocol, such as [Claude Code](https://docs.anthropic.com/en/docs/claude-code). It shares `/v1/chat/completions`'s credential selection, cooldown, rotation, accounting, and session affinity; only inbound mapping and outbound translation differ (see [`TECHNICAL.md` §3.18](TECHNICAL.md), in Chinese):

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

**Restart the process after changing anything under `src/`.** The backend loads routes and assembly at startup only; the process manager restarts on *exit* — a keep-alive policy is not a hot reload. The frontend differs: the backend serves `web/dist` via `FileResponse`, reading from disk per request, so a rebuild only needs a browser refresh. Updating them independently yields a **new frontend against an old backend**: the page loads (static files current) but new endpoints fail — the old process lacks the route, unmatched `/api/*` returns a JSON `404`, and the client collapses that into a generic error with an unrelated message. This happened on the B5 rollout: creating a user reported "username may already exist, or the role is invalid" when the real cause was `/api/users` being a `404`, the running process predating the migration (old DB `PRAGMA user_version` = 13, no `users` table).

```bash
docker compose up -d --force-recreate                # Docker / compose
sudo systemctl restart coding2api                    # systemd

# Confirm the upgrade took effect (check the version first, then the routes)
sqlite3 data/coding2api.sqlite3 "PRAGMA user_version;"                     # expect 18
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/users   # expect 401; 404 = old backend
```

Schema upgrades are additive — `users` / `audit_events` are new tables and existing rows (credentials, usage) are preserved. The restart performs the migration and bootstrap in one step. Details in [`TECHNICAL.md` §6.4](TECHNICAL.md) and [`README.md`](README.md) (Chinese).

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