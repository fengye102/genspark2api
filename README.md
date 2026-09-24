# genspark2api

OpenAI-compatible API bridge for the Genspark web session — **single-file Windows executable**,
stream support, and an automated registration toolkit.

The bridge reuses a web session **you exported yourself**, so it works on the free tier
where the official API key path is blocked. The browser is only used once, to export
cookies; the proxy itself is pure HTTP.

---

## 🚀 Quick link

[📥 Download v1.1.0 Windows exe](https://github.com/fengye102/genspark2api/releases/tag/v1.1.0)

[中文快速指南](README_zh-CN.md) — 双击即用，一键获取 Cookie

---

## How it works
cookies (from the admin panel's one-click flow, or `gs_login.py`); after that the bridge
talks HTTP directly.

---

## Quick start

### Option A — prebuilt exe (easiest)

Download / build `dist\genspark2api.exe`, double-click it, then open
`http://127.0.0.1:8899/`. Log in (default password `admin123`), go to
**账号管理 → 添加账号 → 获取 Cookie（自动登录）** — a dedicated browser window opens, log
into Genspark there, and the cookies are captured and written back automatically.

Then go to **API 密钥** to get the key your clients must send (see below).

### Option B — from source

#### 1. Requirements

```bash
pip install fastapi uvicorn curl_cffi cloakbrowser
```

#### 2. Add an account

**Easiest: use the admin panel** (step 4 → open `http://127.0.0.1:8899/` → 账号管理 →
添加账号 → 获取 Cookie). No manual cookie hunting.

Or export from the command line through a dedicated browser profile (never your system
Chrome profile):

```bash
python gs_login.py            # opens a window; log in, then create a .proceed file
python gs_login.py --auto     # or export immediately if already logged in
```

This writes `cookies1.json` containing the session cookies. Then register it in the pool:

#### 3. Configure the account pool (command-line path)

```bash
cp accounts.example.json accounts.json
```

Fill in one entry per account. Only `cookie_file` is strictly required; `proxy` is optional
but recommended for per-account egress isolation.

```json
{
  "accounts": [
    { "seq": 1, "email": "you@example.com", "cookie_file": "cookies1.json",
      "proxy": "", "status": "active" }
  ]
}
```

#### 4. Run

```bash
python genspark2api.py
# serving on :8899
```

#### 5. Get an API key, then call it

All `/v1/*` endpoints require an API key. One is auto-generated on first run; manage keys
in the admin panel under **API 密钥** (or set `GS_API_KEY` before first run).

```bash
curl http://127.0.0.1:8899/v1/chat/completions \
  -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-6-luna","messages":[{"role":"user","content":"hi"}]}'
```

### Endpoints

| Endpoint | Auth | Description |
|---|---|---|
| `POST /v1/chat/completions` | API key | OpenAI-compatible; supports `stream: true` |
| `GET /v1/models` | API key | Model list |
| `GET /health` | — | Per-account status: `ready`, `cooldown_left_s`, success/failure counters |

---

## Web admin panel

The bridge serves a browser-based admin panel at the root URL:

```
http://127.0.0.1:8899/
```

Default password is `admin123`. Override it before exposing the port beyond localhost:

```bash
# PowerShell
$env:GS_ADMIN_PASSWORD = "a-strong-password"

# bash
export GS_ADMIN_PASSWORD="a-strong-password"
```

You can also change the password from the panel itself: **设置 → 修改管理员密码**.
The new password is persisted to `config.json` and takes precedence over the default
(the `GS_ADMIN_PASSWORD` env var, when set, still wins over `config.json`).

Pages:

- **仪表盘** — per-account stats (success / failure counters, cooldown state), total
  requests, success rate, a 24 h traffic chart, and the most recent requests
- **账号管理** — add an account (one-click **获取 Cookie（自动登录）** opens a browser,
  captures the session after you log in, and pre-fills the form; or paste a cookie string
  manually), delete, force-cooldown, or reset an account; export / import the whole
  account pool as JSON
- **API 密钥** — generate named keys, enable / disable them with a switch, rename, or
  delete. A newly generated key is shown **once**; copy it immediately. At least one key
  must exist. Each key tracks its last-used time.
- **日志** — request logs (filter by model / status / keyword, paginated, persisted to
  `logs/requests.jsonl`) with 24 h KPIs (requests, success rate, avg / P95 latency),
  plus runtime logs with level filtering and auto-refresh
- **模型测试** — send a test chat upstream (optionally pinning one account) to verify
  model and account availability
- **访问信息** — copy-ready endpoints (Base URL / chat / models / health), data file
  locations, and client config examples for Cherry Studio / NextChat / LobeChat
- **设置** — runtime parameters (default model, upstream timeout, retry count, cooldown
  durations; persisted to `config.json`) and the admin password
- **关于** — version, uptime, model count

### API keys

Calls to `/v1/*` are authenticated with a Bearer API key:

- Keys are `sk-…` strings, stored in `config.json` under `api_keys`.
- One key is **auto-generated on first run**, so fresh installs work out of the box —
  just open the API 密钥 page to see it (and generate your own).
- Set the `GS_API_KEY` env var to pin a specific key (overrides `config.json`).
- Generate additional keys for different clients; deleting a key revokes it immediately.

Account changes are written back to `accounts.json` atomically, so the panel and the
request path always share one source of truth. Admin endpoints are protected by a bearer
token obtained from `POST /api/admin/login` (24 h expiry, in-memory only).

---

## Standalone exe (Windows)

The bridge bundles into a single-file exe with PyInstaller:

```bash
pip install pyinstaller
python -m PyInstaller --onefile --console --name genspark2api ^
  --add-data "accounts.example.json;." --add-data "static;static" ^
  --hidden-import cloakbrowser --hidden-import playwright ^
  --collect-all cloakbrowser --collect-all playwright genspark2api.py
```

The cloakbrowser/playwright flags are required so the panel's **获取 Cookie（自动登录）**
button works inside the exe.

Run `dist\genspark2api.exe` next to your `accounts.json` (and `cookies*.json` files).
Runtime state lives next to the exe: `accounts.json`, `cookies*.json`, `config.json`
(admin password + API keys), and the `gs_login_profile/` browser profile. It listens on
`127.0.0.1:8899` and serves the admin panel at `/`.

---

## Configuration reference

All settings are optional; defaults work for local use.

| Source | Name | Purpose |
|---|---|---|
| env | `GS_PORT` | Listen port (default `8899`) |
| env | `GS_ACCOUNTS` | Path to the account-pool JSON (default `./accounts.json`) |
| env | `GS_PROXY` | Fallback egress proxy for accounts without their own `proxy` |
| env | `GS_ADMIN_PASSWORD` | Admin password (overrides `config.json`, default `admin123`) |
| env | `GS_API_KEY` | Pin a single API key (overrides `config.json` `api_keys`) |
| `config.json` | `admin_password` | Persisted admin password (set via 设置 → 修改管理员密码) |
| `config.json` | `api_keys` | API keys for `/v1/*` (managed from the API 密钥 page) |

`config.json` is created automatically on first run / first change.

---

## Supported models

Tested 2026-09-23 — **50 of 53 reachable** on a free-tier account.
Grouped by upstream family; the model IDs below are the ones you pass in `"model"`.

### OpenAI

| Model ID | Notes |
|---|---|
| `gpt-6-luna` | Current default; verified against upstream fingerprint |
| `gpt-6-sol` | |
| `gpt-5.6-luna` / `gpt-5.6-sol` / `gpt-5.6-terra` | |
| `gpt-5.5` / `gpt-5.5-pro` | |
| `gpt-5.4` / `gpt-5.4-mini` / `gpt-5.4-nano` / `gpt-5.4-pro` | |
| `gpt-5.2` / `gpt-5.1-high` / `gpt-5-pro` / `gpt-5` | |

### Anthropic

`claude-opus-5-5`, `claude-opus-5`, `claude-opus-4-8`, `claude-opus-4-7`,
`claude-opus-4-6`, `claude-sonnet-5`, `claude-sonnet-4-6`, `claude-sonnet-4-5`,
`claude-sonnet-4`, `claude-4-5-haiku`

### Google

`gemini-3.8-flash`, `gemini-3.7-flash`, `gemini-3.6-flash`,
`gemini-3.1-pro-preview`, `gemini-3.1-flash-lite-preview`, `gemini-2.5-flash`

### Other

`grok-4.7`, `grok-4.6`, `grok-4.5`, `kimi-k3`, `GLM-5.3`, `glm-5p3`,
`deep-seek-v4.1-flash`, `deep-seek-v4-flash`, `minimax-m3`, `nemotron-3-ultra`

**Not reachable:** `claude-opus-4-1`, `kimi-k2-instruct` (upstream returns an error),
`claude-opus-4-5` (transient network error during testing).

> Model availability changes upstream without notice. The list above is a snapshot.

---

## Free-tier limits (measured)

| Limit | Value |
|---|---|
| Credits per request | **1** |
| Daily grant | **100** credits, expires in 24 h (does not accumulate) |
| Rate limit | **6 requests/minute, 60/hour** per account |
| Concurrent | **3+ → HTTP 429** |
| 429 recovery | ~30 s (no `Retry-After` header) |

Because the daily grant does not roll over, per-account daily throughput is roughly
**100 requests**. With a pool of N accounts, throughput scales accordingly — but the
per-account rate limit binds earlier than the credit budget.

---

## Registration toolkit (optional)

`signup_e2e.py` automates the whole signup pipeline, including the image CAPTCHA:

```bash
export TWOCAPTCHA_KEY=<your-2captcha-key>
export UM_DIR=<dir containing your mail CLI>        # optional, for code retrieval
python signup_e2e.py --email you@example.com --seq 1
```

It will: configure and launch the browser driver, navigate to the form, fill the email,
solve the image CAPTCHA, poll for the email verification code, submit it, fill the password
twice, create the account, export cookies, and append the account to `accounts.json`.

**With `TWOCAPTCHA_KEY` set, no human interaction is required.** Measured end to end:
**~116 s per account, zero human steps.**

Without a solver key the driver still exposes a manual path: capture the image with
`capimg`, write the answer to a file, and continue.

### Automatic CAPTCHA solving

`two_captcha.py` submits the CAPTCHA to 2captcha and returns the answer. Measured on the
live signup flow (2026-09-23):

| Metric | Value |
|---|---|
| Success rate | 2/2 signups passed on the first image |
| Solve time | 6–20 s |
| Cost | **$0.001 per solve** (measured: two single-solve balance deltas, billed with a ~30 s lag) |
| Stability | 3/3 identical answers for the same image |

**One implementation detail matters a lot:** read the image from `img.src` (a
`data:image/jpeg;base64,...` URL). Do **not** screenshot the element by coordinates — the
screenshot includes the surrounding background, and the solver then misreads the distorted
glyphs. This single difference was the gap between a wrong answer and a passing one.

The driver exposes an `autocap` command that does the whole loop: read image → solve →
fill → submit → verify, retrying with a fresh image on failure.

### Notes

- **Without a solver key, the image CAPTCHA needs a human.** Vision models refuse the
  request outright, and when asked neutrally they misread the distorted glyphs (they
  confuse strokes with characters).
- **Email verification code retrieval:** if you use an MCP-based mail tool, beware that it
  may return a **cached** code. Poll the mailbox directly to get the newest one; a stale
  code produces `We are having trouble verifying your email address`.
- **Order matters:** email → CAPTCHA → *Send verification code* → verification code →
  *Verify code* → password ×2 → *Create*. The password fields stay `disabled` until the
  verification step succeeds; filling them earlier times out rather than failing loudly.

---

## Session lifetime

| Cookie | Role | Lifetime |
|---|---|---|
| `session_id` | **session identity** | ~20 days |
| auth tokens | request signing | ~24 h, auto-renewed |
| bot-management cookie | anti-bot | ~30 min, auto-renewed per request |

**Re-login is fully automatic.** The login form has **no image CAPTCHA** (unlike signup),
so a plain email + password login works headlessly — re-run `gs_login.py` when
`session_id` expires.

---

## Architecture notes

### Required headers

The upstream web endpoint rejects requests that are missing a `User-Agent` with a
`400` whose body reads `bad request cf` — which looks like an edge/CDN block but is
actually an application-layer check. A browser-like `User-Agent` is mandatory.

### Authentication

A single `session_id` cookie is sufficient. Sending the full cookie jar also works; sending
only the auxiliary auth cookies does not.

### Egress isolation

Per-account `proxy` values are supported and recommended. Accounts sharing one egress IP
are more likely to be rate-limited or restricted together. The bridge attaches a separate
HTTP session per account, so each account can use its own egress.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the gateway integration pattern and
the per-account egress isolation design.

---

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — gateway integration (config shape,
  alias/priority semantics, verification ladder) and per-account egress isolation design

---

## Project layout

```
genspark2api.py          # the proxy (multi-account round-robin, streaming)
static/admin.html        # web admin panel (login, dashboard, accounts, API keys, settings)
gs_login.py              # CLI one-time login + cookie export (panel has a one-click flow)
signup_e2e.py            # end-to-end signup: register -> solve CAPTCHA -> export -> pool
gs_reg_driver.py         # browser driver used by signup_e2e.py
two_captcha.py           # automatic CAPTCHA solving (optional)
gs_export.py             # cookie export from a browser profile
accounts.example.json    # account-pool template (copy to accounts.json)
config.json              # runtime state: admin password + API keys (auto-created)
docs/ARCHITECTURE.md     # gateway integration + egress isolation design
DISCLAIMER.md            # full terms — read this
LICENSE                  # MIT
```

---

## Disclaimer

This project is unofficial and unaffiliated with the upstream service. It automates a
browser session **you control**, using credentials **you exported**, and it does **not**
bypass authentication or grant access to any account but your own. You are responsible for
complying with the upstream Terms of Service, and your account may be rate-limited or
suspended at your own risk.

See [DISCLAIMER.md](DISCLAIMER.md) for the full terms.

---

## License

MIT — see [LICENSE](LICENSE).
