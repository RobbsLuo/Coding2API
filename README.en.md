# Coding2API

Unified OpenAI-compatible gateway for the **CodeBuddy** (CN site + international site),
**TRAE SOLO**, **OpenCode Zen**, **Kilo Gateway**, **Qoder**, **CodeArts**, and **officeAce**
coding-agent upstreams, with a shared credential pool, unified scheduling, and per-user usage stats.

> [!WARNING]
> This project bridges reverse-engineered third-party endpoints; study and research use
> only. Not security-audited. Do not expose it to the public internet without a reverse
> proxy, authentication, and an IP allowlist.

## Features

- **OpenAI-compatible surface**: `/v1/chat/completions` (streaming + non-streaming), `/v1/responses` (for the Codex CLI), `/v1/messages` (Anthropic Messages / Claude Code), `/v1/models` plus `/v1/models/{id}` (single-model lookup), `/v1/user/balance` (DeepSeek-compatible).
- **Unified scheduling**: bare model names auto-route by health, `model@provider` pins an upstream. When free channels are candidates, **free wins**; then **least in-flight first** (concurrent requests spread across idle credentials), then expiring credits, then three-state health with tiered cooldowns, ties broken by **largest balance first**. A model-scoped limit sidelines only that model (the same account's other models stay available); a model held by a single cooling channel returns "model temporarily unavailable" instead of fanning out. A multi-turn conversation keeps one credential and rotates only on error.
- **Expiry-aware burn**: credits expiring within the main window (default 36h) burn largest-first (near-expiry quota not wasted), ties fall to a 7-day window. Units unify to credits.
- **Credential-gated model list**: only channels with a currently usable credential are fetched and shown, so a never-connected channel never shows phantom models; pausing or losing credentials hides a channel's models until it recovers.
- **Shared credential pool**: admins maintain credentials, everyone shares them, encrypted at rest (`APP_SECRET`); device-code login, account switching, quota probing, daily check-in, token pre-refresh.
- **Three-role accounts**: `admin` / `operator` / `viewer` in SQLite; one-time activation links (no shared initial secret), forced password change on first login, changing a role or disabling an account revokes its sessions immediately; logins and writes are audited.
- **Growth center** (CodeBuddy only): travel gifts, Buddy dispatch, task rewards, streak repair, streak redemption, blind boxes; irreversible steps off via `GROWTH_IRREVERSIBLE_ACTIONS=false`; runnable from the admin UI.
- **Privacy-preserving stats**: no conversation content stored; 90-day request detail, permanent hourly rollups; per user / channel / model / credential views; cost estimated from OpenRouter list prices (CNY primary); a **Live** toggle in the request-detail panel streams just-committed requests over SSE.
- **Sidebar to-dos**: count badges on sidebar entries for things you would otherwise only notice by visiting the page (credentials needing re-login or out of quota, unresolved alerts, failed background tasks, abnormal upstream sources, pending activations, your own expiring API keys, stale model catalog). One aggregate endpoint, role-filtered, counts only outstanding items.
- **Admin security**: login rate limiting, CSRF checks, body limits, Host allowlist.
- **Responsive tables**: wide tables (credentials, request detail, model list, …) turn into one card per row below 768px — column names collapse into a row of tappable sort chips and each cell shows its field name inline. The DOM is unchanged, so screen-reader semantics match the wide layout.

Protocol-level implementation (private envelopes, signing, gate spoofing) is in [`TECHNICAL.md`](TECHNICAL.md) (Chinese).

## Quick start

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js 24+ and pnpm 10+ (frontend build only).

```bash
uv sync
# Create the first admin (add the rest from the "Users" page later)
uv run python scripts/create_user.py admin --role admin
cd web && pnpm install && pnpm build && cd ..
APP_SECRET="pick-a-random-string" \
  uv run python -m uvicorn src.main:build_app --factory --port 8000
```

Open <http://127.0.0.1:8000> and sign in.

> `scripts/create_user.py` writes directly to SQLite (`--db`, or `DATA_DIR`; default `data/coding2api.sqlite3`). On startup, an existing `secrets/users.txt` is imported once (existing usernames are never overwritten).

### Docker

```bash
cat > .env <<'EOF'
APP_SECRET=pick-a-random-string
PUBLIC_BASE_URL=http://127.0.0.1:8000
EOF
docker compose build
docker compose up -d
docker compose exec coding2api python scripts/create_user.py admin --role admin
```

Pull the GHCR image instead of building:

```bash
docker compose pull
# or pin a version: docker pull ghcr.io/robbsluo/coding2api:v0.9.0
```

Pushing a `v*` tag to main triggers the publish workflow (`.github/workflows/publish.yml`), tagging both `<tag>` and `:latest` on GHCR.

## Users and roles

Accounts live in SQLite's `users` table; the *Users* page (admins only) handles day-to-day management.

| Role | Can do |
|---|---|
| `admin` | User management, tasks & settings, credential read/write, all stats, audit log, server logs, upstream status, model list |
| `operator` | Credential read/write (import / delete / pause / check-in / growth / account switch), all stats, upstream status, model list |
| `viewer` | Read-only: dashboard, credential list, stats, Playground |

**Adding users**: an admin sets a username + role and gets a **one-time activation link** (valid 24h, shown once). The user opens `/activate` and sets their own password — no "admin knows an initial password" secret exists. "Reset password" issues a new link.

**Role change / disable**: effective immediately — all issued session cookies for that account are invalidated (re-login required). Disabling is the default way to "remove" someone (reversible, usage still attributed to the username); **hard delete is CLI-only** (it would leave orphan `usage_events` rows).

**Self-service password change**: user menu → *Change password*, requires the current password.

**Lockout guard**: you cannot disable or demote the **last active admin**, nor demote or disable yourself. Add another `admin` first.

**CLI fallback**:

```bash
python3 scripts/create_user.py alice --role admin   # interactive password
python3 scripts/create_user.py --list
python3 scripts/create_user.py alice --delete --force   # hard delete, irreversible
echo "$PASSWORD" | python3 scripts/create_user.py alice --role admin --password-stdin  # password off argv / shell history
```

`ADMIN_USERNAMES` and `secrets/users.txt` are **bootstrap-only**: on startup, `users.txt` is imported into the DB and the named users are promoted to `admin`; afterwards roles come from the DB alone.

## Audit log

The *Audit log* page (admins only) records logins and writes, filterable by actor/action and paginated: **logins** (success and failure), **account changes** (create, activate, role change, enable/disable, password change/reset, hard delete), **credential writes** (import, delete, pause, pin, revive, account switch) and **manual alert resolution**. Each row has time, actor, action, target, detail and source IP. **No passwords or token plaintext** (the one-time activation token appears once in the create response; only its SHA-256 digest is stored). Retention matches request detail (90 days), trimmed by the background cleanup task.

## Server logs

The *Server logs* page (`/logs`, admins only) is a **read-only** live view of this process's own logging: `GET /api/logs?limit=` returns recent lines, `GET /api/logs/stream` is SSE (buffer replay, then live). The page offers level filtering, keyword search, follow-latest, pause and clear — **no write or execute entry point**, subscribers can only read.

- **This process only**: it attaches a `logging.Handler`, it never tails a file. Under multiple processes or containers each one sees its own slice; that is deliberate, since log rotation and permissions make a file view lie.
- **Redaction is a hard precondition**: every line passes an allow-list before leaving the process. Anything matching `token` / `secret` / `authorization` / `cookie` / `password` / `api_key` (including `X-Api-Key:`, `client_secret=` and JSON key forms), an `sk-…` shape or `Bearer <value>` is **dropped whole**. Losing a log line beats pushing a credential into a browser.
- **Bounded buffer**: size is `LOG_BUFFER_LINES` (default 500, hot-reloadable), oldest overwritten first; lowering it trims immediately. `0` disables the page (both endpoints 404 and the handler is detached), matching how `/metrics` turns off.
- Each line is capped at 4000 chars and carries `seq` (replay/live dedupe), timestamp, level, logger and `request_id` when available. Subscriber limit is 4; beyond that 429 + `Retry-After`.

## Usage

1. **Credentials** page — add credentials:
   - **CodeBuddy** (incl. international): device-code login, or paste `{"token":"..."}`.
   - **TRAE**: paste the credential JSON (`accessToken`/`uid`/`refreshToken`) or the callback URL (`apiHost` must be an official address).
   - **Qoder / CodeArts**: device-code / OAuth2 authorization-code login (CodeArts: paste the unreachable local callback URL back into the page).
   - **officeAce**: click *Log in to officeAce* for the Huawei Cloud authorization (the callback is handled in the cloud — no URL to paste, no local port).
   - **OpenCode Zen / Kilo Gateway**: nothing to add — a virtual credential is seeded at startup (re-added from the same panel if deleted).
2. **API keys** — create an `sk-...` (plaintext shown once).
3. Call it:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-your-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"hello"}]}'
```

`model@provider` pins an upstream (`glm-5.2@trae`, `mimo-v2.5-free@zen`); bare model names route automatically. Any OpenAI-compatible client can connect (Base URL `http://127.0.0.1:8000/v1`, key `sk-...`, model names per `GET /v1/models`); the *Playground* page tests with the session login, no API key.

### Three model-name fields

Every model on the eight channels is unified into three fields:

1. **Raw id** (`raw_id`): the key actually sent upstream, **never changed**. Raw ids differ per channel (Qoder `kmodel_latest` = TRAE `kimi-k3-1`) and may carry a namespace prefix (Kilo's `kilo-auto/free`).
2. **Canonical key**: free-tier markers (`-free` / path segment `free`) and namespace prefixes stripped, then slugified — `kilo-auto`, `longcat-2.5-preview`. The list merges entries by it, so Zen's free tier and TRAE's paid tier collapse into one row.
3. **Display name** (`name`): cleaned readable text, from upstream when given (`Qwen3.8-Max` → `Qwen3.8 Max`), otherwise derived; brand casing normalized (`DeepSeek` / `MiMo` / `GLM` / `GPT`).

Every `id` in `GET /v1/models` is the canonical key; requests are translated back to the channel's raw id (`by_provider.{channel}.raw_id`). **All three spellings are directly requestable** (`kmodel_latest`, `Kimi K3`, `kimi-k3` all resolve to the same model). **`benefit` marker**: CodeArts models that draw on the daily free token pool get no rate from upstream, so their entries carry `benefit: true` (top level for single-channel, `by_provider.{channel}` for multi-channel); the Playground uses it to show a「福」badge on the channel pill, distinguishing them from the credit-billed built-in models.

**`GET /v1/models/{id}` single-model lookup** (same shape as OpenAI): all three spellings resolve, case-insensitively. The route uses `:path`, so namespaced raw ids (`dots-studio/dots-3-note-preview:free`) work too. A model that does not exist **or is outside the calling key's model allowlist** returns the same 404 with an OpenAI error body (`code=model_not_found`) — allowlisted-out and missing look identical, so a client cannot probe "what else is in the pool". Benchmarks and refresh timing come from the same merged snapshot as the list, so a model shown in the list never 404s on lookup.

### Streaming usage and reasoning tokens

- **Streaming usage frame**: after the finish chunk and before `data: [DONE]`, a `choices: []` usage frame (standard OpenAI shape) lets stream-reading clients show tok/s, context occupancy and session totals; nothing is sent when the upstream reported no usage (never a fake `0`), and unreported fields are `null`.
- **Reasoning tokens need `reasoning_effort`**: CodeBuddy defaults to `medium` when the client omits it (the upstream would otherwise write the whole reasoning pass into the visible answer); explicit values pass through untouched.

### Playground advanced parameters

The Playground request form carries optional `temperature` / `max_tokens` / `reasoning_effort` inputs; **leaving one empty omits the key entirely** (each channel then applies its own default). Client-side validation matches `compat/openai/request.py` (`temperature` up to 1000, `max_tokens` a positive integer up to 4,000,000) so nothing passes the UI only to be rejected as a 400. The `reasoning_effort` control appears only when the model metadata reports `supports_reasoning` or a `default_effort`, offers the standard levels (`low`/`medium`/`high`, plus the upstream-declared default if it is outside that set), and starts at "not sent" — the upstream decides whether a given level is acceptable.

## Channels

> CodeBuddy (incl. international) / TRAE / Qoder / CodeArts / officeAce need credentials; **OpenCode Zen and Kilo Gateway are credential-less free tiers** — the pool holds one virtual placeholder row (so scheduling / cooldown / stats work as usual), the quota column reads "free tier (no quota API)", and health stays "no probe" (unknown ≠ exhausted). The virtual row **re-seeds on restart after deletion**; use *pause* to disable permanently.

### OpenCode Zen free tier (`zen`)

Free models at [opencode.ai/zen](https://opencode.ai), no account or login. The upstream list mixes unmarked paid models, so the gateway narrows by the `-free` suffix and probes each candidate, exposing **only free models that answer anonymously** (liveness cached 30 min, no static allowlist; free models marked **x0**). The upstream list has no display names, so names are derived from the id.

**Free-tier gate**: the upstream wants to believe it is talking to the official client (UA version, session header, `stream:true`, `tools` with `bash`/`read`). The gateway satisfies this; the injected `bash`/`read` are empty shells and their calls are filtered out, so you never see functions you did not declare. UA version via `ZEN_OPENCODE_VERSION`, endpoint via `ZEN_API_ENDPOINT` (must be inside `ZEN_ALLOWED_ENDPOINTS`). Paid models return "unavailable" even when pinned.

### Kilo Gateway free tier (`kilo`)

Free models at [Kilo Gateway](https://kilo.ai) (`api.kilo.ai/api/gateway`), no account or login. Standard OpenAI protocol, no private envelope or gate spoofing; the upstream `/models` carries an authoritative `isFree` flag, so the gateway filters on it directly and **does not probe** (probing burns the tiny quota). Free models marked **x0**.

**Quota and limits**: the free tier is small (gateway-wide ~200 requests/hour/IP) and is a forward of OpenRouter's free pool; upstream **429 and 502/503/504 are model-scoped** — only the named model cools, and the gateway adds no circuit breaker. Endpoint via `KILO_API_ENDPOINT` (must be inside `KILO_ALLOWED_ENDPOINTS`).

### Qoder (`qoder`, real account)

[Qoder](https://qoder.com), **real account** required: click *Log in to Qoder*, complete device-code authorization in the browser (the backend polls; the token never touches the browser). The upstream speaks a private COSY protocol (custom Base64 + envelope SSE), signed and unwrapped by the gateway; the surface stays standard OpenAI.

**Quota and check-in**: quota probing (`GET /api/v2/quota/usage`) and campaign-based daily check-in. Check-in uses campaigns (`/sash/api/v1/me/campaigns`, needs `Cosy-ClientType`) with the old `daily-check-in` as fallback; claims run automatically and can be triggered from the admin UI. Campaigns **refresh daily at 10:00 (UTC+8)** (valid 30 days after claiming) and the task seals per that window, so an "already claimed" just after midnight seals only until 09:59 and the new campaign is claimed at 10:00. No streak count is shown. **The international site has no check-in** (404), reported as "no such endpoint in this region", not an error.

**Endpoints and pacing**: CN site by default (`openapi.qoder.com.cn` + `gateway.qoder.com.cn`); international via `QODER_API_ENDPOINT=https://openapi.qoder.sh` with the whitelist extended (signing carries full `cosy-*` headers, so the whitelist prevents misdelivery). Default `QODER_CHAT_MIN_INTERVAL=5` (own pacer), hot-updatable.

**Upstream node failures**: Qoder sometimes wraps its own inference-node failures in a 400 (`[FAIL]node:… msg:Execution failed`). The gateway treats these as a **model-scoped transient fault** — only that model cools, and an exhausted candidate set returns a "model temporarily unavailable" 503 (`no_healthy_credential`), not a missing-model error.

### CodeArts (`codearts`, real account)

[Huawei Cloud CodeArts](https://codearts.huaweicloud.com), **real account** required: *Log in to CodeArts*, complete OAuth2 authorization, then paste the **unreachable local URL** (`http://127.0.0.1:12800/oauth/callback?code=…`) back into the page; the server exchanges it for STS credentials (AK/SK + security_token + refresh_token + DPoP key) and stores them encrypted.

**Token refresh is rigid**: refresh_token, `client_id=codearts-agent` and the DPoP key are bound together and **single-use** — the new refresh_token must be written back or the account breaks; the pre-refresh task does this automatically (DPoP ES256/P-256). The pre-refresh window is capped at 45 minutes and must be **wider** than the polling interval (`REFRESH_INTERVAL_MINUTES`, default 30 min). A revoked renewal marks the credential "re-login required" and stops automatic retries until restored.

**Daily check-in**: a "1000 credits/day" campaign (`GET /v1/ops/delivery` → `POST /v1/ops/claim` → `POST /v1/ops/confirm`). The task claims automatically and can be triggered from the admin UI; `claim` alone is **not** enough (`CLAIMED` is a "claimed but unconfirmed" state), so both steps always run. This credit ledger is **separate from** the daily token pool: the pool feeds benefit models (`deepseek-v4.1-flash`), the credits feed built-in models (GLM-5.2 / OpenPangu) — both ledgers are shown on the admin UI (see below).

**Burn first**: the daily pool does not roll over, so the day's remainder is registered as midnight-expiring quota and scheduled **before** other channels. The upstream meters tokens, normalized to **credits** on the admin UI (1 credit = 10,000 tokens; a full daily pool = 1000 credits); pre-upgrade history is converted by `scripts/convert_codearts_credit_unit.py`. **This "credit" is a synthetic unit for cross-channel ordering, not the upstream check-in/plan credits.**

**Two ledgers shown**: CodeArts is split into two groups on the credentials page, and **each group tells one ledger only** — **内置** shows the **credit ledger** (`GET {snap}/snap-manager/v1/statistics/plugin`, built-in models GLM-5.2 / OpenPangu), **福利** shows the **daily pool** (token-derived, benefit models). They are two independent upstream endpoints that never talk to each other and differ by an order of magnitude, so they live in separate groups with their own hover notes instead of one merged number. The credit sub-packages (`基础积分` / `按需积分` / `福利积分`) go into the "套餐 N 个" detail popover, and the plan's expiry date (`end_date`, verified to differ per account) is registered in the expiry ladder — unused credits are voided too, so "burn near-expiry first" also burns credits first. The **credit ledger does not feed the scheduler's health** (`health` still reads the daily pool only, so routing is unchanged), and a failing credit endpoint only logs, leaving the balance probe result intact. The 内置 group's **health badge** is derived from the credit ledger instead (`quota_credits_health`), so it always agrees with that group's quota column rather than showing "99% credits left, 0% health". **Change events are split per ledger** (`credit_events.ledger`): the 内置 group expands the credit-ledger history, other channels and the 福利 group expand the daily-pool history — mixing them would read as consecutive samples of one ledger.

**The credentials page splits CodeArts into two channels**: **CodeArts 内置** (writable: all actions and the credit ledger) and **CodeArts 福利** (read-only: daily token pool only), each listing the same accounts once. This is a **front-end-only display split — the backend still has one `codearts` channel**: `refresh_token` is single-use and bound to `client_id` + the DPoP key, so two credential rows would each refresh once and the second would fail with `the refresh token has been used`, burning the ticket. The ledger split axis is the **model** (`is_benefit_model()` decides per request), not the channel, so this split only affects display and action entry points. **Expiring credits are split per ledger too**: the backend keeps the daily-pool rung and the credit-package rung apart (`quota_credits_expiry_ladder`), so the 福利 group shows the daily pool's expiring credits and the 内置 group shows the credit ledger's own.

**Signing / cumulative SSE / pacing**: the upstream requires Huawei `SDK-HMAC-SHA256` (AK/SK + `X-Security-Token`), and `CODEARTS_ALLOWED_ENDPOINTS` must include the snap engine, STS, benefit gateway and portal hosts. Streaming `text` is **cumulative (replace semantics)**, reduced to incremental events for clients. Default `CODEARTS_CHAT_MIN_INTERVAL=5`; the upstream hard-limits **3 concurrent sessions per account**, so an in-flight cap (`CODEARTS_MAX_CONCURRENCY`) and a sliding window (`CODEARTS_REQUEST_WINDOW_SECONDS`, 3 starts per 60s) keep bursts ahead of `400 TM.00001041`. All hot-updatable.

### officeAce (`officeace`, real account)

[Huawei Cloud officeAce](https://www.huaweicloud.com/product/agentarts.html) (果办 / AgentArts office agents), **real account** required: click *Log in to officeAce* and finish the Huawei Cloud authorization in the browser — **the callback is handled in the cloud** (`/v1/claw/auth/callback`), so this service listens on no port and the user never pastes a URL; the backend polls `/v1/claw/auth/code` every 2.5s for the authorization code (the same mechanism the official desktop client uses).

**Provisioning prerequisite**: the account must first sign in to the OfficeAce client and redeem an invitation code. An unprovisioned account completes the login but gets no model key (`subscription: null` from the cloud); the service reports "this account has not activated officeAce" instead of a vague login failure.

**Two credential sets with very different lifetimes** (the most common mistake on this channel):

| Credential | Lifetime | Used for |
|---|---|---|
| `Basic model_app_key:model_app_secret` | **never expires** | chat forwarding (`{gateway}/v2/chat/completions`) |
| temporary AK/SK + `security_token` + `project_id` | **~2 hours** | quota lookup, check-in (`/v1/subscription*`) |

Mixing them yields `APIG.0303 app not found`. The failure is confusing either way: chat keeps working (the Basic key never expires) while the quota column goes blank and check-in keeps failing (the temporary credential expired). The pre-refresh task renews the temporary half automatically — `refresh_token` is single-use and bound to `client_id=pdp5_for_agentarts` plus the DPoP key, and **is not interchangeable with the CodeArts ticket** (`STS5.1806 invalid client id`).

**The upstream is plain OpenAI protocol**, so no private envelope parsing is needed; three upstream quirks do need handling:

- **Rate limits arrive as fake 200s**: HTTP 200 + `data:{"error":…}`, and "first frame fine, second frame is the error" has been observed. Error frames flow to the engine as usual; `81111`/`81114` cool down only that credential × model.
- **Model-level rejection must not kill the credential**: `81004` (account lacks the model) / `81009` (unknown name) would otherwise be read from the status code as a dead credential. Business codes are checked before status codes and classified as a model-level negative cache (cleared on first success).
- **`max_tokens` is a combined "reasoning + answer" budget**: 9 of the 11 usable models are reasoning models, and a small budget leaves the answer empty. Values `≥16` get 1024 extra headroom clamped to the model ceiling (`glm-5*` 131072 / `qwen3*` 65536 / `deepseek-v4.1-flash` 393216); `<16` passes through untouched (probes / exact tests); absent stays absent.

**Credits are the primary ledger**: the unit is `officeace_points` (one-off new-user grant + a daily check-in grant). Unlike CodeArts there is no parallel token pool — `remaining`/`total` are simply credits. Each grant **expires independently**, hence the quota label reads "per-grant expiry"; scheduling still burns near-expiry first.

**Daily check-in**: `POST /v1/subscription/bonus/claim` (**a write — it really grants credits**; the body must be {} with a JSON `Content-Type`, an empty body returns 500 `OfficeAce.11020001`). Re-claiming on the same day is idempotent upstream, so no local "already claimed" flag is needed. The gain is computed from the **remaining-quantity delta** of `bonus_skus` (`points` is the campaign's cumulative total, contributed to by several channels, so using it as "what I just earned" would overstate). The seal runs to the next Beijing midnight: the next day's campaign triggers an automatic catch-up.

**No active model probing**: only 11 of the 34 catalog entries actually work, but probing sends real inference and burns credits, which are scarce here. Model-level rejection is handled by the `BLOCKED` negative cache from real requests instead. **No credit estimation either**: upstream exposes `cost_coefficient` and `tokens_per_point`, but their multiplication is undocumented, and a guess would desynchronize the whole cross-channel quota ordering.

**Signing and endpoints**: the control plane (`/v1/claw/*`) uses Huawei `SDK-HMAC-SHA256`; the model gateway (`/v1/subscription*`, `/v1/model-token-rates`) accepts only **`V11-HMAC-SHA256`** (an extra region scope plus key derivation) and answers `APIG.0624` or 401 otherwise. `OFFICEACE_ALLOWED_ENDPOINTS` only needs the control-plane host — the model gateway differs per account and is issued by `client-permission-validate` into the credential, so it is not something this service can misconfigure. `OFFICEACE_CHAT_MIN_INTERVAL` defaults to `0` (no CodeArts-style hard concurrency limit) and is hot-updatable.

**One account, one user**: the official plan page states the personal edition is "limited to a single user per account", so this channel is not shared between people — each user adds their own credential.

**Switching accounts**: the login flow supports `force_login`, which first hits `/authui/logout?service=…` to clear the Huawei Cloud browser session. The *Log in to officeAce* button does not pass it — use a private window or another browser for a second account, or sign out of Huawei Cloud first. The server-side capability is in place; wiring it into the UI is follow-up work.

### CodeBuddy international site (`codebuddy_intl`, real account)

The international site [www.codebuddy.ai](https://www.codebuddy.ai), **same protocol** as the CN site (same `/v2/chat/completions`, `/v3/config`, `/v2/plugin/auth/*`, `/v2/billing/*` routes, same Keycloak realm) but a **separate account system** (separate signup, billing) — hence a **separate channel**. Login is identical to the CN site (device code, token never in the browser).

**Not a site switch**: `CODEBUDDY_API_ENDPOINT` is process-wide, so flipping it would send every CN-site credential to the international site (quota mismatch / 403). Both sites can hold credentials, each with its own endpoint, pacer and login flow.

**Whitelists deliberately exclude each other**: `CODEBUDDY_INTL_ALLOWED_ENDPOINTS` holds only `https://www.codebuddy.ai`; the two sites' tokens are issued by different gateways, so a cross-site send is credential leakage and pointing the international endpoint at the CN host fails **at startup**.

**Feature split**: chat, model list, quota probe, token refresh and **activity reporting** are enabled; **daily check-in and the growth center are off** — check-in simply **doesn't exist on that site** (`checkin-activity-status` returns `active: false`), and the growth center **cannot be verified** (the international gateway puts the whole `/v2/activity/*` prefix behind its auth plugin, answering 401 rather than 404) while performing irreversible actions. Both menu entries are hidden for this channel. Activity reporting posts to `/v2/report`, a different path, verified working, so it stays enabled.

**No channel suffix on shared model names**: models on both sites are listed **once** with both providers attached; genuinely different upstream models (`-x`, `-sg`) still get their own entry.

**Pacing**: `CODEBUDDY_INTL_CHAT_MIN_INTERVAL` defaults to 5s on its **own** pacer (sharing one would make the two sites queue behind each other), hot-updatable.

### Per-channel outbound proxy (optional)

`PROVIDER_PROXIES` gives each channel its own egress proxy:

```
PROVIDER_PROXIES="codebuddy=http://127.0.0.1:7890;qoder=socks5://127.0.0.1:1080"
```

Format `channel=url;channel2=url2`, schemes `http/https/socks5/socks5h`, empty = all direct. It covers **all** of that channel's outbound requests (chat streaming, quota/model fetches, background tasks and OAuth login). **Startup-only** (restart required). Parsing is strict: an unknown channel, unsupported scheme or missing `=` fails startup — "thought it used a proxy but went direct" is worse than a startup error. Environment proxies (`HTTP_PROXY`, …) are **never honoured** (`trust_env=False`), so a deployment-wide proxy cannot silently hijack token-bearing requests.

## Outbound protocols

### Responses API (Codex CLI)

`POST /v1/responses` serves a Responses subset for clients that only speak the Responses API, such as the [Codex CLI](https://github.com/openai/codex). It shares the same credential selection, cooldown, rotation, accounting and session affinity as `/v1/chat/completions`; only the inbound mapping and outbound translation differ:

```bash
export CODING2API_KEY=sk-your-key
codex -c "model_providers.coding2api={ name='coding2api', base_url='http://127.0.0.1:8000/v1', wire_api='responses', env_key='CODING2API_KEY' }" \
      -c model_provider=coding2api \
      -c model='glm-5.2' \
      'your task'
```

Supports streaming text, reasoning summaries, function tool calls and `finish_reason=length` → `response.incomplete`. `text.format` maps to chat's `response_format` (`text` / `json_object` pass through; `json_schema` is expanded). Content parts map as `input_text`/`text`/`output_text` → chat text and `input_image` → chat `image_url` (the URL is passed through as-is). **Not supported**: `store=true`, `previous_response_id` (stateless server, not faked), `input_file` (needs server-side file storage), Responses-only tools — explicit 400, never silently degraded. `include=["reasoning.encrypted_content"]` is accepted and ignored.

### chat `response_format` validation

`POST /v1/chat/completions` no longer forwards `response_format` verbatim: only the three OpenAI-defined shapes pass, and malformed or unknown shapes are an **explicit 400** (never silently degraded, and never sent upstream where a 4xx would cool the credential):

- `{"type": "text"}`
- `{"type": "json_object"}`
- `{"type": "json_schema", "json_schema": {"name": <non-empty string>, "schema": <any JSON Schema>, "description"?: <string>, "strict"?: <bool>}}` — unknown keys inside `json_schema` are dropped (not part of the chat spec, not honored upstream); a missing `name`, a missing `schema` and an unknown `type` each get their own 400 message.

The Responses `text.format` → `response_format` mapping shares **the same validator** (`normalize_response_format` in `src/compat/openai/request.py`), so the two inbound protocols cannot drift; the `prefix` argument only changes the field path in error messages (Responses passes `text.format`).

### Anthropic Messages API (Claude Code)

`POST /v1/messages` serves an Anthropic Messages subset for [Claude Code](https://docs.anthropic.com/en/docs/claude-code):

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_AUTH_TOKEN=sk-your-key     # or ANTHROPIC_API_KEY (sent via x-api-key)
claude
```

**Auth**: both `x-api-key` and `Authorization: Bearer` are accepted. **Streaming**: `message_start` → `content_block_start/delta/stop` → `message_delta` (with `stop_reason` and usage) → `message_stop`; no `[DONE]` sentinel, and thinking blocks get a `signature_delta` before `content_block_stop`. **Cache hit**: usage is split Anthropic-style — `input_tokens` counts only misses, hits are reported separately as `cache_read_input_tokens` (the internal `input_tokens` is OpenAI's `prompt_tokens`, which includes hits), so Claude Code shows cached tokens instead of always 0. **`count_tokens`**: `POST /v1/messages/count_tokens` estimates input tokens locally. **Images**: `image` blocks (`source.type=base64` becomes a `data:` URL, `source.type=url` passes through) are normalized to chat `image_url` parts, and images inside `tool_result.content` are kept too — pure-text content is still folded into a string, so image-free requests map byte-for-byte as before. **Not supported**: `document` blocks (PDF, no chat equivalent), `source.type=file` and Anthropic-only server tools — explicit 400.

**`cache_control` pass-through** (always on, no flag): inbound cache markers are forwarded verbatim, at the same places Anthropic accepts them — `system` content blocks, user/assistant content blocks, `tool_result` (attached to the **tool message top level**, avoiding a synthetic empty text block), `tool_calls` elements, and `tools` elements. Only **structure** is validated (must be an object with a non-empty string `type`; `ttl`, if present, must be a string); values are not interpreted (an unknown `type` still passes through — a gateway should not gatekeep cache types upstream may add later). Malformed markers are an explicit 400 rather than a silent drop. **Without markers the mapping is byte-for-byte unchanged** (pure text still folds into a string), so session-affinity fingerprints and upstream prefix caching are unaffected. Six of the eight channels accept the markers in testing (codebuddy / codebuddy_intl / trae / qoder / codearts / kilo); **zen and officeAce are unverified** (its upstream returns `FreeTierError` 403 even with `--plain`, unrelated to the markers). Probe script: `scripts/probe_cache_control.py`.

### CC Switch one-click import

The API Keys page can generate [CC Switch](https://github.com/farion1231/cc-switch) `ccswitch://` import deep links (one button each for Claude Code / Codex CLI / OpenCode) that write the endpoint, key and model into the desktop client after a confirmation dialog. There are two entry points: the **dialog shown right after a key is created** (using the freshly generated once-only cleartext key; closing the dialog drops it) and the **"Import to CC Switch" button on each key-list row** (the list only stores a masked `preview`, so you paste the full key you saved at creation time — it stays in dialog memory only). Both offer a "default model" dropdown sourced from the same list as Playground (`/api/playground/models`), grouped by channel. A deep link **only fires on a user click** (browsers block automatic redirects) and **carries the key in cleartext**, so it is generated only when the user opens the dialog and supplies a key. Protocol and `app` / `apiFormat` values: see CC Switch's "Deep Link Protocol" manual; requires CC Switch v4.0.10+. Note the **endpoint form depends on `apiFormat`**: the Anthropic format (Claude Code) takes the bare origin **without `/v1`**, the two OpenAI formats take `/v1`. This gateway has no Gemini egress, so no `app=gemini` entry is offered.

### Balance query

`GET /v1/user/balance` is DeepSeek-shaped, Bearer `sk-...`, for clients like Cherry Studio / cc-switch. Balances come from the credential pool's quota-probe cache (`QUOTA_PROBE_MINUTES`), aggregated over usable credentials, in upstream credits. `balance_known` is `false` when no credential has ever probed successfully (unknown, not 0).

### Pool health

`GET /health` is a pure liveness probe. `GET /healthz` adds credential-pool counts (`total` / `ready` / `cooling` / `paused` / `disabled`), so monitors can alert when the pool is exhausted (`ready=0`: alive but unusable). Both are unauthenticated; buckets are mutually exclusive and sum to `total`.

### Prometheus metrics (`GET /metrics`)

`GET /metrics` serves the Prometheus text format (`text/plain; version=0.0.4`) for direct scraping. On by default, **unauthenticated** (same stance as `/healthz`); `METRICS_ENABLED=false` (hot-updatable) makes the endpoint return 404.

Five metric families:

| Metric | Type | Labels | Source |
| --- | --- | --- | --- |
| `coding2api_requests_total` | counter | `provider` / `model` / `status`(ok\|error) | `usage_hourly` (permanent, monotonic across restarts) |
| `coding2api_tokens_total` | counter | `provider` / `type`(input\|output\|reasoning\|cached) | `usage_hourly` |
| `coding2api_credentials` | gauge | `state`(ready\|cooling\|paused\|disabled) | credential pool (same stance as `/healthz`) |
| `coding2api_upstream_errors_total` | counter | `provider` / `kind` | in-process counter (resets on restart) |
| `coding2api_task_failures_total` | counter | `task` | in-process task state (resets on restart) |

**No high-cardinality labels**: no `credential_id` / `api_key_id` / `username`. `requests_total` / `tokens_total` read the **permanent** hourly rollup (not the 90-day detail table), so scraping never scans details and counts stay monotonic; the in-process counters reset on restart, which Prometheus handles as a counter reset.

## API key policy

A key can be restricted by channel, model, source IP and expiry:

- **Channel binding**: pins the key to one channel; a model owned by another channel gets a 400 naming the real owner (no silent reroute). Empty = automatic. A `model@channel` conflicting with the binding is also a 400.
- **Source IP allowlist**: comma-separated IP/CIDR (empty = unrestricted), normalized on write; an off-list source gets 403. `X-Forwarded-For` is **ignored by default**; only with `TRUST_PROXY=true` is the *last* entry used (exactly one trusted reverse proxy).
- **Model allowlist**: comma-separated names or fnmatch globs (e.g. `glm-*,kimi-k3`), case-insensitive, `@channel` suffix excluded; out-of-list models get a 400 and `/v1/models` lists only allowlisted models.
- **Expiry**: epoch seconds; empty = never; an expired key gets a 401 (same copy as "key not found", so nothing is enumerable).

## Credential pause

The per-row *Pause* action removes a credential from **chat traffic only**: quota probing, token pre-refresh, daily check-in, growth center and activity reporting keep running (they honour only the system hard-disable `disabled`). Use it to "stop chatting but keep earning". *Resume* returns it immediately. Different from *Disabled* (the upstream rejected the session; needs re-login + *Restore*). Same toggle at `POST /api/credentials/{id}/toggle`.

> Zen / Kilo are credential-less: *pause* is the only way to disable them permanently (deletion re-seeds on restart). Pausing also hides the channel's models.

## Per-credential in-flight cap

`MAX_INFLIGHT_PER_CREDENTIAL` (default `0` = unlimited) stops a credential from being selected once its concurrent request count reaches the cap, keeping a single account from being hammered into upstream rate limits or bans; it is global across channels (orthogonal to the per-channel `CODEARTS_MAX_CONCURRENCY`). The in-flight count doubles as the **load key**: the executor injects it into `Candidate.inflight` and the scheduler sorts by **least in-flight first** (after the free tier, before expiring credits), so concurrent requests spread across idle credentials; with the cap at `0` leases are still registered, so the load key stays live even when concurrency is unlimited.

- The counter is **in-process** (single-instance semantics, like the `/api/tasks` state).
- Leases release when the request ends (success, rotation, error, client disconnect), with a **600s TTL** reclaiming any that leak—better to occasionally admit one extra request than to silently retire a credential.
- When every candidate is at its cap, the response is the same "no healthy credential" 503 as the limits guard.
- Hot-updatable on the *Tasks & settings* page; the next request picks it up (no snapshot cache).

## Credential pool: filtering and grouping

Once the pool grows, the page splits "find that one account" into three layers, while the first screen shows only the channel-level overview:

- **Filter bar**: search (nickname / channel / id, case-insensitive) × channel × state × tag. The four are **AND**-combined and purely client-side — the backend still returns the full set ordered by `sort` / `order`. When any filter is active it shows "showing N / total" plus a *Clear* action; no match yields an empty state rather than a blank table. Counts in the channel / state / tag dropdowns come from the full set, so narrowing one filter never re-narrows the others. The **tag dropdown renders only when some credential actually carries a tag** (no permanent "all tags" control otherwise), and tags are a structured dimension that **does not participate in the search term** — otherwise finding a nickname that happens to be "prod" would be impossible.
- **Tags**: each credential can carry any number of ops tags (comma-separated, up to 16 tags of ≤ 24 chars; half-width and full-width commas and semicolons all separate), edited via the row's *More → Set tags*. Tags are **display/filter only** — they never affect selection and never change request behaviour, since using them as policy would make a rename silently change traffic. A row shows at most two tags, collapsing the rest into `+N` with the full list in the hover title.
- **Group sort**: a separate control reorders only the channel *groups* (never the rows inside them, which still follow the column header / backend `sort`). Default is **quota high→low**, ranked by the group's summed remaining quota so the channels that can still serve the most surface first; switchable to **channel order** (fixed, so 30s polling cannot make headers jump). Channels with no quota API (free tier) sort last rather than being treated as `0`, and ties fall back to channel order.
- **Grouped by channel**: rows aggregate per channel; each group header is a compact **channel dashboard** — brand icon + channel name + account count, plus a **state breakdown** (icon + count, listing only states that occur, using the same `credentialState` as the state badge), a **total quota** figure (a share ring plus the number; the ring is a pure gauge and the exact percentage sits on the "total quota / total credits" label line; sums the group's non-null values, and the free tier zen / kilo shows `—` rather than `0` with no ring), and an **expiring credits** figure (one block for the primary 1.5-day window; the secondary 7-day window appears only when it holds **more** than the primary — the 7-day window is a superset, so an equal number means nothing expires beyond 1.5 days). Clicking a header collapses / expands that channel — **collapsed by default** (see structure first, expand to inspect accounts); an active filter auto-expands matching channels, and clearing it returns to the collapsed overview. Group order is "quota high→low" by default and switchable from the toolbar to the fixed site-wide channel order (CodeBuddy, TRAE first); it does not follow the sort key. The channel name appears only in the header, never repeated per row. **The total reads the group's own ledger**: the CodeArts 内置 group totals the credit ledger (labelled "total credits"), every other group totals the daily pool.
- **Column headers follow the expanded region**: there is no global table header; the column row (with sort buttons and column hints) renders inside each expanded channel group and sticks while the container scrolls. When collapsed, the group header *is* the overview and the column names have nothing to label; only an expanded group needs the "what is this column" anchor. Several groups can be expanded at once, and every group's header shares one sort state (clicking any of them writes the same backend `sort` parameter). The **"growth center" column appears only for channels that support it** (CodeBuddy); other channels get an empty placeholder cell — the column count stays constant, otherwise auto layout recomputes the widths and groups expanded below misalign.

- **CodeArts splits into two channel groups**: **CodeArts 内置** (writable: all actions, the credit-history entry, the credit ledger and the package detail) and **CodeArts 福利** (read-only: daily token pool only), each listing the same accounts once, header labels suffixed "内置 / 福利" with 内置 first. **Each group tells one ledger**: the 内置 group's quota column is the credit ledger (`积分 x / y`, sort key `quota_credits_remaining`), the 福利 group is the daily token pool; the change-event entry follows the ledger too (内置 reads `ledger=credits`; 福利 renders no entry). **Expiring credits follow the ledger as well** — the backend splits the merged ladder per ledger (`quota_expiring_credits` = daily pool, `quota_credits_expiring` = credit ledger), so the 福利 group shows the daily pool's near-expiry quota and the 内置 group shows the credit ledger's own. **Health follows the ledger too**: the 内置 group's health badge and state read the derived `quota_credits_health` (still display-only — routing keeps using the daily-pool `health`), while the 福利 group reads the daily pool. The 福利 group drops probe / check-in / more-actions, the credit-history entry and the package detail, but the **column count stays constant**: the group-header and row checkboxes still render, only **disabled** — an empty placeholder cell is narrower than a real checkbox, so under auto table layout the row's column starts drift from the 内置 group; a same-size disabled box is what actually aligns them. This is a **front-end-only display split; the backend still has one `codearts` channel** (`refresh_token` is single-use, so two rows would burn each other's ticket), and the ledger split axis is the model, not the channel.
- **Scrollable long list**: the table is capped at 70vh, and the group's sticky column header and row actions stay visible deep in the list.

The quota number is dotted-underlined: hovering reveals **that row's cycle semantics** (this-cycle remainder / account balance / daily pool / free tier with no quota API); the per-row caption lives in that hover hint.

## Model list loading

`GET /v1/models` and the Playground dropdown merge only channels with a **currently usable credential** (at least one not-paused, not-dead row). The check runs before the cache, so a never-connected channel never shows phantom models; pausing or losing credentials hides a channel's models until it recovers; a cold start lists only `zen` / `kilo` (their virtual credentials are seeded). A cooling credential still counts — it is only temporarily limited, so the list does not flicker.

**Ordering**: CodeBuddy, TRAE, Qoder and CodeArts models come first, dictionary-ordered within a group; multi-channel models take their highest-priority channel's slot. Display-only — it does not change scheduling.

**Routing honours this cache**: when the catalog proves which channels own a model, those are the only candidates — if all are unavailable (cooling / paused) the request gets a 503 instead of fanning out (each upstream would only answer 11102 / 4001 / 401 and write hours-long "no such model" negative caches). A model absent from the catalog still routes to all candidates; pinned `model@channel` and key channel binding are unaffected.

### Catalog snapshot and startup

Besides the in-process TTL cache (300s), every successful fetch is snapshotted to `DATA_DIR/model_catalog.json` and **read back synchronously at startup**, so the model→channel table is ready in the first second (flat model names no longer fan out while zen's 10+ second probes run). A corrupt or >7-day-old snapshot is just dropped. `MODEL_CATALOG_MINUTES` provides a background refresh so the catalog stays fresh even when nobody calls `/v1/models`.

Playground and `GET /v1/models` use **stale-while-revalidate**: with a cache they return the old list immediately and refresh in the background; only a channel with **no cache at all** is awaited synchronously. The list lags by at most one TTL.

### Admin *Model list* page

The admin *Model list* page (`GET /api/model-catalog`, visible to admins and operators) is a read-only view of the public OpenRouter catalog: prices (input / output / cache read / cache write, USD per million tokens, switchable to CNY), context/output limits, input→output modalities, capabilities (reasoning / tool calling / attachments / structured output), knowledge cutoff, and the **Artificial Analysis indices** (intelligence / coding / agentic, via the public OpenRouter API — **not measured by this service**, never sorted). A missing catalog shows an empty state and does not affect chat or cost.

OpenRouter lists hundreds of models and most are not connected here. Each row carries an `available` boolean (whether the model is **currently served by a channel with a usable credential**, same rule as `/v1/models`), and the page offers an **off-by-default** "only current channels" toggle that narrows the table locally to models fetched from current channels (no extra request).

The 能力分 (capability score) header is sortable by the **intelligence index** (智); rows without a score sort last (never treated as 0). Only this one sub-score has a sort entry — one header column maps to one entry, and sorting all three would crowd the header into a row of buttons.

### Token expiry visibility

A "token remaining" column, red below `TOKEN_EXPIRY_WARNING_SECONDS` (default 1h). Expiry uses explicit `expires_at`, falling back to the JWT `exp`; both missing → `—`, **never guessed**.

## Stats

### Scope

The time range defaults to the **last 24 hours** (also 7 days / 30 days / all): the first question when opening the stats page is "how is the pool doing right now", and 7 days flattens out the day's variation.

### Credit, cost and cache hit rate

The **Credit** column: CodeBuddy is the real upstream value; TRAE's `token_usage` frame carries only tokens, so its credit is **estimated** from official unit prices (`≈`); CodeArts benefit models are estimated 1:1 against the daily pool (`≈`). Tables live in `src/provider/trae/pricing.py` and `src/provider/codearts/units.py`; unmatched models show `—`.

**Cost (est.)** estimates tokens × public list prices, not the real charge. Prices come from the **OpenRouter public catalog** (USD per million tokens), computed as `(input − cached) × input + cached × cache_read + output × output`, converted to CNY at the **rate in effect when the row was written** (`USD_CNY_RATE`). Costs are **fixed at write time** (`usage_events.cost_usd` / `cost_cny`) and **never recomputed**; pre-upgrade rows are `NULL` and can be backfilled with `scripts/backfill_cost.py --apply`. Matching compares canonical keys for equality and **drops ambiguous hits**; unmatched models or requests without input tokens contribute nothing and aggregates render `—` (never a fake `0`), so the figure is a **lower bound**.

**Cache hit rate**: the *Token usage* card adds cached ÷ input (1 decimal), from the upstream `cached_tokens`; `—` when never reported or input is 0. The **Grouped stats** table (after *cached*) and the **Request detail** table (after *hit*) each add a display-only **hit rate** column on the same formula.

### Credit record

The drawer under a credential's quota number lists the **net change between consecutive quota probes**, attributing nothing to check-in / growth / chat (the upstream logs nothing), so the UI says "net change". The first probe records only a baseline; unchanged balances are skipped; a balance going unknown still records a row. 90-day retention.

**Split per ledger**: CodeArts has two ledgers (daily token pool / credit ledger), so the table carries a `ledger` column (`pool` / `credits`) and each probe writes one row per ledger. The 内置 group expands the credit-ledger history (`ledger=credits`), other channels and the 福利 group expand the daily-pool history (`ledger=pool`); `GET /api/credentials/{id}/credit-events` takes a `ledger` query parameter (default `pool`, unknown values fall back to `pool`). **They must be read separately**: the two ledgers differ by an order of magnitude, and mixing them reads as consecutive samples of one ledger. On upgrading an old database every historical row lands in `pool` — those rows were produced by the daily-pool diff.

### Grouping and re-rollup

The **Grouped stats** panel tabs four dimensions (channel / model / user / credential) over one table (requests / ok / success rate / input / output / cached / hit rate / credit / cost), endpoints `GET /api/stats/by-provider|by-model|by-user|by-credential`. Default sort is requests descending; **user and credential tabs are admin/operator only**. **Scope differs**: channel / model / user read the **hourly rollup** (permanent; the last 5 minutes are not yet counted); the **credential** dimension reads request detail (the rollup key has no `credential_id`), covering the **last 90 days**.

**Click-to-drill**: clicking any group row drills the **Request detail** panel below into that dimension (channel / model / credential filter by the same-named column; the user dimension goes through the scope parameter). Conditions stack across dimensions, the active filters show as chips above the panel, and one click clears them. `GET /api/stats/events` therefore accepts `provider` / `model` / `credential_id` filters (AND semantics).

**Merged columns**: the Request detail table merges same-kind columns into one, showing a readable default and the full breakdown on hover: **Token** (defaults to "input / output · hit rate", hover adds cached tokens) and **Latency** (defaults to "duration / TTFB"). Column count drops from 14 to about 10, and the narrow-screen cards carry half as many fields. Sorting is preserved: clicking a merged header opens a dropdown to pick the sub-column (Token → input / output / cached / hit rate; Latency → duration / TTFB), and selecting one sorts by it. The **hit rate** sub-column uses backend `sort=cache_rate` (cached ÷ input; `NULL` when input is 0 or never reported, matching the sort semantics of other nullable columns).

**CSV export**: the **Export CSV** button (admin/operator only) exports request detail for the **current time range and drill filters**, via `GET /api/stats/events?format=csv`. The file carries a UTF-8 BOM (so Excel detects the encoding), `\r\n` line endings, ISO 8601 (UTC) timestamps, and prefixes a leading apostrophe on **text** cells starting with `= + - @` (or tab/CR) to block spreadsheet formula injection (numbers, negatives included, are left as-is). Export is capped at 10000 rows; beyond that it truncates and flags `X-Export-Truncated: 1` in the response headers, and the page tells the user to narrow the conditions.

**Live stream (SSE)**: the **Live** toggle in the request-detail panel subscribes with `EventSource` to `GET /api/stats/events/stream` and shows requests **just committed** within the current filter range. Live rows render in their own panel above the paginated history, so the table you are paging through never jumps. Events are broadcast after the `StatsCollector.record` transaction commits via an in-process `LiveEventHub` (no DB polling); the client keeps the newest 50, newest first, and closing the toggle closes the connection.

- **Scope and filters**: identical to `/api/stats/events` — admin/operator may pass `username` (omitted = everything), viewers are pinned to themselves; `provider` / `model` / `credential_id` stack. Changing a filter reopens the connection (`EventSource` cannot change its query string).
- **Credential names**: the broadcast payload carries no nickname (same redacted field set as the detail endpoint, plus `rowid`); only admin/operator connections get `credential_name` filled in per event **server-side**, so viewers never see pool nicknames.
- **Backpressure and limits**: each subscriber gets a **bounded** buffer (200 events); when full the **oldest** is dropped (a live view cares about now). At most 8 concurrent subscribers; beyond that the endpoint answers 429 with `Retry-After` instead of queueing or quietly degrading the service.
- **Keep-alive and caching**: a comment frame (`:`) every 15s keeps proxies from dropping an idle connection; the response sets `Cache-Control: no-store` and `X-Accel-Buffering: no` so no middle layer caches or fully buffers the stream. Browsers auto-reconnect; the page reports a disconnect once the browser gives up (401 / 429).

If history looks light (pre-upgrade detail not covered, or detail backfilled without re-rolling), run `python3 scripts/rollup_hourly.py --apply` (preview by default, backs up before writing, idempotent). It reports gaps and warns about intervals whose detail has passed the 90-day retention and cannot be restored.

### List sorting

Admin lists are **sortable by clicking column headers**: the first click uses the column's natural direction (numbers descending, text ascending), subsequent clicks toggle. Sorting is done by the **backend** — every `GET` list endpoint accepts `sort` (whitelist key) and `order` (`asc` / `desc`). An unknown `sort` key or invalid `order` falls back to the endpoint's default (no error); `sort` is a whitelist key, never a SQL column, so the injection surface is zero.

## Background tasks

Nine tasks — quota probe, token pre-refresh, daily check-in, growth center, activity reporting, detail cleanup, channel catalog refresh, OpenRouter catalog refresh, and operations alerts — are scheduled by `TaskRunner`, failing independently. Each task's last run, latest result and error appear on the *Tasks & settings* page (30s refresh); task state is **in-process only** and resets on restart.

**Channel catalog refresh** (`MODEL_CATALOG_MINUTES`, default 30, floor 5) keeps the catalog fresh when nobody visits `/v1/models`; 30 minutes aligns with zen's 30-minute liveness cache, so going denser would only hit other channels' `/models` for nothing.

**Growth center** is CodeBuddy-only; Buddy travels return every 1–4 hours, so the default cycle is 60 minutes (`GROWTH_INTERVAL_MINUTES`). Lottery / streak redemption / blind boxes / streak-repair cards are **irreversible** and skipped when `GROWTH_IRREVERSIBLE_ACTIONS=false`.

### Operations alerts

A periodic task evaluates four risks and persists hits to `alert_events`, reviewed on the *Operations alerts* page (admins only):

| Rule | Fires when |
|---|---|
| `pool_empty` | The pool has credentials but fewer than `ALERT_POOL_READY_MIN` are usable |
| `task_failed` | A background task fails `ALERT_TASK_FAILURES` times in a row (a success resets it) |
| `token_expiring` | A credential's token has less than `ALERT_TOKEN_EXPIRY_HOURS` left |
| `error_rate` | The window's upstream failure ratio reaches `ALERT_ERROR_RATE_THRESHOLD` with enough samples |

- **Silence window** (`ALERT_SILENCE_MINUTES`): the same `(rule, target)` is recorded / pushed only once per window.
- **Hysteresis** (`ALERT_FOR_TICKS` / `ALERT_CLEAR_TICKS`): consecutive hits before firing, consecutive misses before confirming recovery. On recovery every **still-unresolved** row for that `(rule, target)` gets `resolved_at` (the reminder rows of the same incident close together), and a `status="resolved"` webhook is emitted.
- **Restart self-healing**: hysteresis **counters** live in-process and restart from zero, but "which alerts are still open" is a persisted fact — `AlertTask` rebuilds it from the unresolved rows at startup, so an alert recorded before a restart whose condition has since cleared is still marked resolved.
- **Manual resolution**: each unresolved row has a *Mark resolved* button (`POST /api/alerts/{id}/resolve`, admin + CSRF + audit) for cases the state machine cannot judge (rule retired, false positive). It touches that row only.
- **Retention**: same as request detail (90 days), trimmed by the cleanup task.

### Activity reporting (optional, off by default)

The growth center's streak counts daily client chat events; an account driven only by the gateway would break the streak. With `ACTIVITY_REPORT_ENABLED=true`, a task posts **one** `chat_request_send` event per account per day at `ACTIVITY_REPORT_HOUR` (default 10:00) to keep the streak alive — unrelated to credits or scheduling, and failures do not affect chat. Applies to CodeBuddy and CodeBuddy International.

> ⚠️ **Risk**: the official terms forbid scripted tampering with activity data (disqualification + gift clawback). Off by default; evaluate the account risk first. Event names and request shapes depend on the upstream and **break without notice** — not a reliability feature. Measured (2026-09-21): without `userId` the upstream returns HTTP 200 `{"code":0}` and **silently drops** the event; when a credential's `user_id`/`account_uid` is empty, the gateway falls back to the bearer JWT `sub`, and skips if still missing (never fabricated).

## Configuration

Key variables below; the full list is in `src/config.py` (authoritative), and **every one is plumbed through `docker-compose.yml`**. In containers use `HOST`/`PORT` to change the listen address (`PORT` also sets the host mapping).

| Variable | Default | Notes |
|---|---|---|
| `APP_SECRET` | **required** | Credential encryption key, ≥16 chars; loss = all credentials void, no rotation. Low-entropy values are rejected at startup |
| `ADMIN_USERNAMES` | empty | **Bootstrap-only**: users promoted to `admin` on first start; inert once the DB has roles |
| `USERS_FILE` | `secrets/users.txt` | **Bootstrap-only**: legacy user file imported once (idempotent) |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | Browser-reachable address; TRAE login callback depends on it |
| `DATA_DIR` | `./data` | SQLite and runtime data directory |
| `DEFAULT_MODEL` | `deepseek-v4.1-flash` | Model when empty/`auto` |
| `QUOTA_PROBE_MINUTES` | `60` | Quota probe interval (floor 1) |
| `REFRESH_INTERVAL_MINUTES` | `30` | Token pre-refresh polling interval (floor 5); short-lived channels cap their window wider than this |
| `MODEL_CATALOG_MINUTES` | `30` | Channel catalog fallback refresh (floor 5) |
| `OPENROUTER_CATALOG_MINUTES` | `1440` | Catalog refresh (floor 60): one fetch from the OpenRouter public API covers prices, metadata and the Artificial Analysis capability indices |
| `OPENROUTER_MODELS_URL` | `https://openrouter.ai/api/v1/models` | Catalog source (anonymous, no key); restart required |
| `USD_CNY_RATE` | `6.70` | Cost-estimate FX rate; hot-updatable, affects only later writes |
| `GROWTH_INTERVAL_MINUTES` | `60` | Growth-center cycle (CodeBuddy only; floor 5) |
| `GROWTH_IRREVERSIBLE_ACTIONS` | `true` | Allow lottery, streak redemption, blind boxes, repair cards |
| `ACTIVITY_REPORT_ENABLED` | `false` | Post one chat event per account per day. **Off by default** — terms forbid tampering |
| `ACTIVITY_REPORT_HOUR` | `10` | Activity hour (local Beijing time) |
| `CHECKIN_TIMES` | `""` | Daily check-in times: local `HH:MM`, comma separated (e.g. `09:37,21:10`). A round runs only at those times; **empty = every 10 minutes all day** (the old behaviour, unchanged after upgrade). Bad segments are ignored with a warning; all-bad is treated as empty. Check-in is idempotent upstream, so extra times only add retry opportunities. Once times are set the loop wakes **every 60 seconds** — otherwise a zero-jitter window lasts one minute and the day is silently missed. Hot-reloadable |
| `CHECKIN_JITTER_MINUTES` | `0` | Check-in jitter (minutes): run a random `0..N` minutes **before** the configured time so multiple deployments do not hit upstream in the same minute. Deduplication keys on *date + time*, so jitter never makes the same slot run twice. `0` = off. Hot-reloadable |
| `LOG_BUFFER_LINES` | `500` | In-process ring-buffer size for the admin *Server logs* page (collects this process's `logging` records only, never tails a file). `0` disables the page (both `GET /api/logs` and `/api/logs/stream` return 404). Every line passes a redaction allow-list first: anything matching `token` / `secret` / `authorization` / `cookie` / `password` / `api_key`, an `sk-…` shape or `Bearer <value>` is **dropped entirely**. Hot-reloadable |
| `ALERT_ENABLED` | `true` | Operations-alert evaluation (pool exhausted / repeated task failures / token near expiry / error-rate spike), persisted to `alert_events` |
| `ALERT_WEBHOOK_URL` | empty | Webhook POST (JSON, body carries `status`: `firing` / `resolved`), comma-separated (≤16, `http(s)` only, private/loopback/link-local blocked against SSRF, re-checked against DNS resolution right before delivery and skipped if it fails to resolve), concurrent delivery, all must succeed (15s budget); requests always carry `X-Coding2API-Signature: sha256=<HMAC-SHA256(APP_SECRET, body)>`; empty = in-app records only |
| `ALERT_INTERVAL_MINUTES` | `5` | Alert evaluation cycle (floor 1) |
| `ALERT_SILENCE_MINUTES` | `30` | Silence window per `(rule, target)`; `0` disables |
| `ALERT_FOR_TICKS` | `1` | Enter-hysteresis: consecutive hits before actually alerting; `1` = fire immediately |
| `ALERT_CLEAR_TICKS` | `2` | Clear-hysteresis: consecutive misses before confirming recovery (writes `resolved_at`, pushes a `status="resolved"` notice) |
| `ALERT_POOL_READY_MIN` | `1` | Usable-credential threshold; `0` disables the rule |
| `ALERT_TASK_FAILURES` | `3` | Consecutive task failures before alerting; `0` disables |
| `ALERT_TOKEN_EXPIRY_HOURS` | `24` | Alert when a token's remaining lifetime is below this; `0` disables |
| `ALERT_ERROR_RATE_THRESHOLD` | `0.5` | Upstream error-rate threshold; `0` disables |
| `ALERT_ERROR_RATE_MIN_REQUESTS` | `20` | Minimum sample size |
| `ALERT_ERROR_RATE_WINDOW_MINUTES` | `15` | Error-rate window (from `usage_events` detail) |
| `QUOTA_EXPIRY_WINDOW_SECONDS` | `129600` | Main expiry window: credits expiring within it burn account-first; `≤0` disables expiry sorting |
| `QUOTA_EXPIRY_SECONDARY_WINDOW_SECONDS` | `604800` | Tie-breaking window; `≤0` disables |
| `CONVERSATION_STICKY_SECONDS` | `3600` | Session-affinity TTL. Precedence: **explicit request-body session id** (`conversation_id` / `prompt_cache_key`, `metadata` first) > **session header** (`x-session-id` / `x-opencode-session` / `x-session-affinity` / `conversation-id`) > **message-prefix fingerprint** fallback; a manual pin beats all. Header and fingerprint values are salted with `username` before hashing, so two users sending the same value never share an account; headers must match `[A-Za-z0-9._:-]{1,128}` or they are dropped. `user_id` still suppresses the prefix fallback (headers keep working); `≤0` disables |
| `MODEL_BLOCKLIST` | `custom_model_*,*sub*agent*,summary,browser_use_*,file_search_agent,default,seed-code-pro-*,sagitta,aquila,glm-4.*,glm-5,glm-5-turbo,glm-5.0*,glm-5.1,glm-5v-*,kimi-k2.5,kimi-k2.6,kimi-k2-thinking,minimax-m2.5,deepseek-v3-*,hunyuan-*,hy3*,deepseek-v4-*,qwen3.7-*,auto,nemotron-3-*,glm-5.2*,nemotron-3*,lfm2*,north-mini-code,openrouter-models-router,fast,balanced,deep,primary,hy4-preview,inkling-small,minimax-m2.7,kimi-k2.*,minimax-m3,gpt-5.4,gpt-5.5,laguna*,qwen3-vl-235b,seed-2.1*,xing4.0-29b-a4,glyph-cluster,qwen3.8-flash,seed-evolving` | Model-list blocklist (fnmatch, display only), matched on the outward spellings |
| `MODEL_FALLBACK_GROUPS` | `""` | Cross-channel fallback groups: `name=m1,m2;name2=m3`; members may carry `@channel`; switches only **before any frame is emitted** |
| `ALLOWED_HOSTS` | empty | Host allowlist (DNS rebinding) |
| `ALLOW_TEST_HOST` | `false` | Also allow the TestClient host `testserver`. **Tests only** |
| `TRUST_PROXY` | `false` | Honour `X-Forwarded-For` (last entry); exactly one trusted proxy only |
| `ENABLE_DOCS` | `false` | Expose `/docs` and `/openapi.json` |
| `DUMP_REQUEST_BODIES` | `false` | Diagnostic: dump `/v1` bodies to `data/dumps/` (**contains conversation content**) |
| `LOG_LEVEL` | `INFO` | Audit log is INFO — switching to `WARNING` turns it off |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Listen address/port (compose defaults to `0.0.0.0`) |
| `CODEBUDDY_API_ENDPOINT` | `https://copilot.tencent.com` | CodeBuddy upstream; add it to `CODEBUDDY_ALLOWED_ENDPOINTS` when changed |
| `CODEBUDDY_ALLOWED_ENDPOINTS` | both official sites (see compose) | Token only goes to allowlisted hosts |
| `CODEBUDDY_INTL_API_ENDPOINT` | `https://www.codebuddy.ai` | International upstream; add it to its own allowlist |
| `CODEBUDDY_INTL_ALLOWED_ENDPOINTS` | `https://www.codebuddy.ai` | **Deliberately excludes** the CN host (cross-site = leakage; misconfig fails startup) |
| `ZEN_API_ENDPOINT` / `ZEN_ALLOWED_ENDPOINTS` | `https://opencode.ai` | Zen endpoint + allowlist |
| `ZEN_OPENCODE_VERSION` | `1.18.0` | Spoofed `opencode/<version>` UA (below threshold → 426) |
| `KILO_API_ENDPOINT` / `KILO_ALLOWED_ENDPOINTS` | `https://api.kilo.ai/api/gateway` | Kilo endpoint + allowlist |
| `QODER_API_ENDPOINT` / `QODER_GATEWAY_ENDPOINT` | `https://openapi.qoder.com.cn` / `https://gateway.qoder.com.cn` | Qoder openapi (quota/login) + gateway (chat); international: `openapi.qoder.sh` / `api1.qoder.sh` |
| `QODER_ALLOWED_ENDPOINTS` | CN + international (see compose) | COSY-signed requests only to allowlisted hosts |
| `CODEARTS_API_ENDPOINT` | `https://snap-access.cn-north-4.myhuaweicloud.com` | CodeArts snap engine; add it to its allowlist |
| `OFFICEACE_API_ENDPOINT` | `https://agentarts.cn-southwest-2.myhuaweicloud.com` | officeAce control plane (OAuth + provisioning); add it to its allowlist |
| `OFFICEACE_ALLOWED_ENDPOINTS` | the control-plane host only | The model gateway differs per account and is issued into the credential, so it is **not** allowlisted here |
| `CODEARTS_ALLOWED_ENDPOINTS` | snap + STS + benefit gateway + portal (see compose) | AK/SK-signed requests only to allowlisted hosts |
| `PROVIDER_PROXIES` | `""` | Per-channel egress proxy (startup-only); empty = all direct |
| `CODEBUDDY_CHAT_MIN_INTERVAL` | `5` | CB pacer (seconds), per-credential bucket; `0` disables |
| `CODEBUDDY_INTL_CHAT_MIN_INTERVAL` | `5` | CB international pacer, **independent**; `0` disables |
| `ZEN_CHAT_MIN_INTERVAL` / `KILO_CHAT_MIN_INTERVAL` | `0` | Zen / Kilo pacers, independent, off by default |
| `QODER_CHAT_MIN_INTERVAL` / `CODEARTS_CHAT_MIN_INTERVAL` | `5` | Qoder / CodeArts pacers; `0` disables |
| `OFFICEACE_CHAT_MIN_INTERVAL` | `0` | officeAce pacer, independent; `0` disables |
| `CODEARTS_MAX_CONCURRENCY` | `3` | CodeArts in-flight cap per account; `0` disables |
| `CODEARTS_REQUEST_WINDOW_SECONDS` | `60` | CodeArts sliding window (3 starts / 60s with the cap above); `0` disables |
| `CODEBUDDY_SANITIZE_CHANNEL_MARKERS` | `true` | Neutralize official-client fingerprints in outbound `system`/`assistant` text (upstream 11128 content filter); outbound copy only |
| `REFRESH_SKEW_HOURS` | `24` | Pre-refresh window; short-lived channels cap it (CodeArts 45 min) |
| `TOKEN_EXPIRY_WARNING_SECONDS` | `3600` | Admin warning threshold; `≤0` disables (display only) |
| `PACER_MIN_SECONDS` / `PACER_MAX_SECONDS` | `5` / `20` | Global pacer random wait (seconds). When the bounds are equal (the `*_CHAT_MIN_INTERVAL` shape) the wait is additionally jittered **upwards** by ±25% so instances do not fire in lockstep; the lower bound is never breached |
| `AUTO_CONTINUE_MAX` | `10` | Auto-continue attempts on `finish_reason=length`; `0` disables |
| `CONTEXT_COMPRESS_ENABLED` | `true` | Trim over-long conversations to the catalog's input limit; models without a known limit are untouched |
| `CONTEXT_COMPRESS_RESERVE_TOKENS` | `4096` | Output tokens reserved by the compression budget |
| `CONTEXT_COMPRESS_MIN_KEEP_MESSAGES` | `4` | Recent messages always kept |
| `CONTEXT_COMPRESS_SAFETY_RATIO` | `0.95` | Budget as a fraction of the model limit |
| `UPSTREAM_COMPLETE_TIMEOUT_SECONDS` | `600` | Non-streaming aggregate timeout (retried as transient); `≤0` disables |
| `MAX_INFLIGHT_PER_CREDENTIAL` | `0` | Per-credential in-flight cap (global across channels): a credential at its cap stops being selected; `0` = unlimited. The count also serves as the scheduler's least-in-flight load key. Leases release when the request ends, with a 600s TTL guarding leaked ones |
| `METRICS_ENABLED` | `true` | Expose `GET /metrics` (Prometheus text format, **unauthenticated**, same stance as `/healthz`); `false` disables it (404). Hot-updatable |
| `REQUEST_LOG_ENABLED` | `true` | Request-level structured log (logfmt): one line per business request (`/api/*`, `/v1/*`, `/authorize`) — `event=request request_id=… method=… route=… status=… duration_ms=…`. The route is the template (`/api/credentials/{credential_id}`), never the real ID; **no query string, no body, no username** — correlate via `request_id` against `usage_events`. 5xx at WARNING, the rest at INFO; static assets and probes are not logged. `false` falls back to uvicorn's plain access log. Hot-updatable |
| `FORCE_DISABLE_THINKING` | `false` | Strip reasoning from outbound responses: no more `reasoning_content` / Anthropic `thinking` blocks / Responses reasoning items. **Response-side only — the request body is untouched** (upstream still reasons). Applies to all three exits, streaming and non-streaming. Hot-updatable |
| `REASONING_EFFORT_MAP` | `""` | Outbound `reasoning_effort` remap: `channel=from:to,from:to;*=from:to` (channel is a `KNOWN_PROVIDERS` id, `*` is the fallback); a target of `none` deletes the key and lets the channel default apply. Only rewrites values the client **explicitly** sent — a channel's own `setdefault` is untouched. Empty = pass through. Hot-updatable |
| `ANTHROPIC_THINKING_EFFORTS` | `""` | Anthropic inbound `thinking.budget_tokens` → upstream `reasoning_effort`: `budget:effort,budget:effort,disabled:effort` (`disabled` matches `thinking.type=="disabled"`; otherwise the largest threshold ≤ `budget_tokens` wins, and budgets below the smallest threshold are not mapped). Empty = no mapping at all (`thinking` stays ignored). Hot-updatable |

### Runtime settings (*Tasks & settings* page)

**58 settings** apply immediately without editing `.env` or restarting: default model, blocklist, model aliases, context-compression knobs, fallback groups, both expiry windows, sticky TTL, stream timeouts, short-cooldown wait, the per-credential in-flight cap, growth toggles/interval, refresh/probe/catalog intervals, `USD_CNY_RATE`, every per-channel chat interval, CodeArts concurrency/window, pacer bounds, activity toggle/hour, check-in times/jitter, all alerting rules, `METRICS_ENABLED`, `REQUEST_LOG_ENABLED`, `LOG_BUFFER_LINES` and the three reasoning knobs (`FORCE_DISABLE_THINKING` / `REASONING_EFFORT_MAP` / `ANTHROPIC_THINKING_EFFORTS`).

**DB overrides beat `.env`**: once changed, `.env` no longer applies to that key; the row is marked "DB override" and *Reset* removes the override. Values live in the `runtime_settings` table (plain key/value), so adding a hot-updatable key needs no migration; unknown keys and invalid/out-of-range values are rejected on write. Startup-only knobs (`APP_SECRET`, `PORT`, `DATA_DIR`, `USERS_FILE`, `OPENROUTER_MODELS_URL`, endpoint allowlists) are **excluded**. API: `GET /api/settings`, `PUT /api/settings` (admin + CSRF; `null` resets). The page also shows the 10 tasks' state (`GET /api/tasks`, admin, 30s refresh), in-process only. Each task card has a **Run now** button (`POST /api/tasks/{key}/run`, admin + CSRF): it bypasses the due-time window but **not** the enable switch — a disabled task honestly returns `ran=false` instead of pretending it ran; if the same task is already running it returns `busy=true` immediately (no queueing, no concurrent upstream calls); the response is the finished round.

## Deployment notes

- **Mount ownership**: the container runs as uid 1001 (`appuser`); `./data` and `./secrets` must be writable/readable or SQLite cannot open:

  ```bash
  mkdir -p data secrets && sudo chown -R 1001:1001 data secrets
  ```

- **Timezone**: the image defaults to `TZ=Asia/Shanghai`.

### Restart the backend after upgrading

**Changing anything under `src/` requires a process restart, or you get a "new frontend against an old backend" mismatch.** Typical symptom: the admin UI loads (frontend assets are static) but new endpoints fail — the old process lacks the route, unmatched `/api/*` returns a JSON `404`, and the client collapses that into an unrelated error. The frontend never needs a restart (the backend reads `web/dist` per request), but the **backend does** — a process manager restarts on *exit*, not on source change.

```bash
docker compose up -d --force-recreate   # Docker / compose
sudo systemctl restart coding2api       # systemd
```

Confirm the upgrade took effect (version first, then routes):

```bash
sqlite3 data/coding2api.sqlite3 "PRAGMA user_version; SELECT username, role, enabled FROM users;"   # expect user_version 22
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/users   # expect 401; 404 = old backend
```

### Frontend assets on the same port

The backend serves the frontend build at `/` (reading from disk per request), so a frontend-only change just needs a rebuild and a browser refresh; `localhost:5173` is the Vite dev server. `scripts/build-web.sh` builds only when stale, or unconditionally with `--force`.

> Deployment specifics for the development machine (process management, paths, tool versions) live in `docs/local-environment.md`, which is gitignored and absent from clones.

## Public entry point (VPS + frp reverse tunnel)

Home broadband behind CGNAT has no inbound address, so the public entry point must be built by **dialing out** from the private side. The free Cloudflare Tunnel tier routes mainland traffic to the LAX edge (measured 1.1–2.4s end-to-end, ~60% packet loss); replacing it with an overseas VPS entry plus an frp tunnel drops that to 80–150ms. **Only the inbound path changes**: outbound calls to upstreams still use the local `PROVIDER_PROXIES`.

```
client ──443/TLS──> VPS (nginx) ──frp tunnel──> home Mac:8000
```

Artifacts: `deploy/frp/` (frps/frpc configs), `deploy/nginx/` (reverse proxy + TLS), `deploy/systemd/frps.service` (VPS side), `deploy/launchd/com.coding2api.frpc.plist` (Mac side). Config format targets frp v0.71.1 TOML.

### VPS side (Linux + systemd)

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
# grab the linux_amd64 archive from https://github.com/fatedier/frp/releases, extract frps
sudo install -m 755 frps /usr/local/bin/frps

sudo useradd --system --no-create-home --shell /usr/sbin/nologin frps
sudo install -d -m 755 /etc/frp
sudo install -m 644 deploy/frp/frps.toml /etc/frp/frps.toml
# shared token (use the same value when installing the client)
openssl rand -hex 32 | sudo tee /etc/frp/frps-token >/dev/null
sudo chmod 600 /etc/frp/frps-token && sudo chown frps:frps /etc/frp/frps-token

sudo install -m 644 deploy/systemd/frps.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now frps

# nginx: replace __TUNNEL_DOMAIN__ with your domain
sed 's/__TUNNEL_DOMAIN__/ai-api.example.com/g' deploy/nginx/coding2api.conf \
    | sudo tee /etc/nginx/sites-available/coding2api.conf >/dev/null
sudo ln -sf /etc/nginx/sites-available/coding2api.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d ai-api.example.com     # issues the cert and sets up renewal
```

Point the domain's A record at the VPS public IP. An overseas VPS needs no ICP filing.

### Home Mac side (launchd)

```bash
brew install frp                                # or download it and set FRPC_BIN
./scripts/install-frpc.sh --server <VPS address> --token <same token as the VPS>
launchctl print gui/$(id -u)/com.coding2api.frpc | grep -E 'state|pid'
./scripts/install-frpc.sh --uninstall           # uninstall
```

`deploy/frp/frpc.toml` reads values through frp's native `{{ .Envs.FRP_SERVER_ADDR }}` templating, so the repo holds nothing machine-specific; the address and token file path are injected by the plist, and the token lives in `secrets/frpc-token` (mode 600, gitignored) rather than in the plist — `launchctl print` dumps the plist verbatim.

### Three `.env` values to update

| Variable | Value | What breaks if missed |
|---|---|---|
| `ALLOWED_HOSTS` | append the tunnel domain | every tunnel request gets 400 `invalid host header` |
| `PUBLIC_BASE_URL` | `https://<tunnel domain>` | TRAE login callback base is wrong; no HSTS over https |
| `HOST` | keep `127.0.0.1` | the tunnel loops back locally; there is **no need** to bind `0.0.0.0` |

`TRUST_PROXY`: there is exactly one nginx hop on the VPS, so `true` is fine and lets an API key's `allowed_ips` see the real client IP (nginx forwards `X-Forwarded-For`). If you still want a direct LAN entry point, keep it `false` — otherwise peers on the same subnet can spoof XFF and bypass the allowlist.

> Restart the service after changing `.env` (see "Deployment notes" above).
> Both frpc and frps log to stdout → `logs/frpc.*.log` on the Mac (rotated by
> newsyslog; `deploy/newsyslog/coding2api.conf` already has the two rules) and
> journald on the VPS.

## Logs

The app writes **stdout / stderr** only, with no file logging or self-rotation, so the logs never stop growing on their own — configure rotation once per deployment shape.

| Deployment | Where | Rotation | Action |
|---|---|---|---|
| **Docker / compose** | `docker logs coding2api` | json-file (`10m × 5`, configured) | **none** |
| **Linux (systemd)** | `journalctl -u coding2api` | journald | **none** (template in `deploy/systemd/`) |
| **Linux (non-systemd)** | `/var/log/coding2api/*.log` | `logrotate` | `sudo cp deploy/logrotate/coding2api /etc/logrotate.d/` |
| **Bare** | terminal stderr | none | redirect and rotate yourself |

Level via `LOG_LEVEL` (default `INFO`); the audit log is INFO, so `WARNING` turns it off.

### Request-level structured log

Besides uvicorn's plain access log, every business request (`/api/*`, `/v1/*`, `/authorize`) emits one logfmt line:

```
event=request request_id=9f2c… method=POST route=/v1/chat/completions status=200 duration_ms=1234
```

- **`route` is the template** (`/api/credentials/{credential_id}`), not the real path — resource IDs never enter the log and the log doesn't fan out per ID. Unmatched routes (404, rejected by the security middleware) fall back to a sanitized raw path (whitelisted characters, 120-char cap).
- **No query string, body, headers or username.** Query strings may carry client-supplied tokens; identity is correlated through `request_id` against `usage_events.request_id` (which has the `username` column) — logs are kept longer and read by more people, so less is better.
- `request_id` matches the `X-Request-ID` response header.
- **Levels**: `5xx` at WARNING (our own fault, worth catching), everything else at INFO (otherwise 401 probes and typo'd paths drown the alert log).
- **Static assets and probes are not logged** (`assets/*.js`, `/health`, `/healthz`, `/metrics`) — uvicorn already covers them, and duplicating only pushes business lines out of view.
- Toggle `REQUEST_LOG_ENABLED` (default `true`, hot-updatable); turning it off falls back to uvicorn's plain access log.

## Development

```bash
# Backend: lint + tests (100% line/branch coverage gate)
uv run ruff check src tests scripts
uv run pytest -q --cov=src --cov-report=term --cov-fail-under=100

# Frontend
cd web
pnpm exec tsc --noEmit
pnpm exec vitest run
pnpm build
```

## Documentation

Detailed documentation is written in Chinese:

| Document | Content |
|---|---|
| [`PROPOSAL.md`](PROPOSAL.md) | Scope, upstream feasibility findings, architecture, data model, security boundary |
| [`TECHNICAL.md`](TECHNICAL.md) | Stack, module specs, provider protocol, request flow, testing |
| [`diagrams/coding2api-architecture.html`](diagrams/coding2api-architecture.html) | Architecture diagram |
| [`diagrams/coding2api-request-sequence.html`](diagrams/coding2api-request-sequence.html) | Request main-chain sequence diagram |
| [`diagrams/coding2api-credential-lifecycle.html`](diagrams/coding2api-credential-lifecycle.html) | Credential scheduling lifecycle |

## License

MIT — see [LICENSE](LICENSE). Attribution for the upstream projects this work learned from is in [NOTICE](NOTICE).
