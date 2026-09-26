# Integration & Architecture Notes

Design notes for wiring this bridge into an LLM gateway and isolating per-account egress.
**No deployment-specific values here** — no real hostnames, IPs, keys, or account data.

---

## 1. Gateway integration (OpenAI-compatible gateway)

This bridge speaks the OpenAI Chat Completions protocol, so it plugs into any gateway that
supports `openai-compatibility`-style upstreams. The gateway sits in front and gives you a
single endpoint, model aliases, priority/failover, and usage accounting.

```
client ──► gateway ──► this bridge ──► upstream web session
             │              │
             │              └── round-robin over the account pool
             └── alias mapping + priority + failover
```

### Layering

| Layer | Responsibility |
|---|---|
| **Client** | Sends OpenAI-shaped requests, names models by the gateway's alias |
| **Gateway** | Auth, alias→model mapping, priority tiers, retry, usage stats |
| **This bridge** | Account selection, cookie/proxy attachment, upstream protocol translation |
| **Upstream** | The web session endpoint (SSE) |

### Config shape (placeholders)

The gateway entry looks like this — note that a gateway key is *not* the same thing as an
upstream account credential; the gateway key only authenticates the client to the gateway.

```yaml
- name: "<channel-name>"
  base-url: "http://127.0.0.1:<bridge-port>/v1"
  priority: <int>            # higher wins; lower tier acts as fallback
  api-key-entries:
    - api-key: "<gateway-local-key>"    # must be one of the bridge's own API keys (issued in the panel)
  models:
    - name: "<upstream-model-id>"       # the id the bridge accepts
      alias: "<prefix>/<friendly-name>" # what the client asks for
```

### Rules worth knowing

- **Alias prefix**: use a channel-distinctive prefix so aliases from different channels
  never collide in the gateway's flat namespace.
- **`priority` semantics**: the gateway picks the highest-priority bucket that has an
  available upstream; lower buckets serve as fallback. Useful when the same model name
  exists on a paid channel and a free channel — the free one can lead.
- **The bridge authenticates its callers itself**: every `/v1/*` call requires a Bearer
  API key issued in the panel, and the admin API uses a separate session token. Loopback
  plus a fronting gateway is still the recommended topology — if you do expose the port,
  set a strong admin password first.
- **One model per channel at first.** Register a single model, verify end-to-end, then add
  the rest. Registering 50 models before proving the path works makes failures ambiguous.

### Verifying an integration

```
1. bridge alone     POST /v1/chat/completions   → a real completion
2. through gateway  POST /v1/chat/completions   → same, with the gateway alias
3. streaming        stream: true                → incremental deltas, then [DONE]
```

Test 1 isolates the bridge; test 2 proves alias mapping; test 3 proves the gateway does not
buffer the stream.

---

## 2. Per-account egress isolation

### Why it matters

Accounts that share one egress IP are correlated by the upstream. Rate limits, risk
scoring, and account restrictions tend to apply **per IP**, so a single noisy account can
degrade every account behind the same address. Isolating egress turns "all accounts share
one fate" into "one account degrades at a time".

This is not a theoretical concern — on similar bridges, accounts with fully isolated
device identifiers and distinct email addresses were still restricted together, and the
only shared attribute was the egress IP.

### Design

```
        ┌──────────────────────────────────┐
        │  egress pool definition          │  single source of truth:
        │  slot N → local listener :PORT_N │  account index → egress index
        └───────────────┬──────────────────┘
                        │  all channels read the same mapping
        ┌───────────────┴──────────────────┐
        │  proxy engine                    │
        │  one listener per slot           │
        └───────────────┬──────────────────┘
                        │
   ┌────────────┬───────┴───────┬────────────┐
 account 1    account 2      account N   (other channels)
 → :PORT_1    → :PORT_2       → :PORT_N   → same slot index
```

**Core rule: account index N → egress slot N.** Using the index (not a name) means
"which egress does this account use?" is answered by arithmetic, with no lookup table to
drift out of sync.

### Design decisions and their rationale

| Decision | Rationale |
|---|---|
| **Pool decoupled from the bridge** | Adding a channel does not rebuild the pool; egress resources stay centrally managed |
| **Cross-channel slot sharing is intentional** | The isolation requirement is *per-channel* (account N of a channel ↔ slot N), not global exclusivity |
| **One listener per slot** (not one config per account) | The bridge only needs `proxy: http://127.0.0.1:<port>`; it never learns node details |
| **Bind listeners to loopback** | Otherwise the proxy is an open relay reachable from the local network |
| **Fail closed on a bad proxy URL** | Silently falling back to a direct connection leaks the real IP — worse than an error |

### Implementation in this bridge

- Each account entry carries an optional `proxy`. Empty means direct.
- A **separate HTTP session per account**, so connection pools and cookie jars never mix.
- A per-account session is cached, not rebuilt per request (avoids connection churn).
- The proxy is attached to **every** upstream call, including any refresh path, so no
  request escapes over the default route.

```json
{
  "accounts": [
    { "seq": 1, "cookie_file": "cookies1.json", "proxy": "http://127.0.0.1:<port1>" },
    { "seq": 2, "cookie_file": "cookies2.json", "proxy": "http://127.0.0.1:<port2>" }
  ]
}
```

### Operational notes

- **Single-node slots are a single point of failure.** A slot whose node dies is simply
  unavailable; the bridge should be restarted or the slot reassigned.
- **Slot health drifts.** Different probe targets report different "down" sets at the same
  moment — a slot that fails an IP-echo probe may still reach the actual upstream. Always
  probe **the upstream you care about**, not a generic echo service.
- **Verify the proxy is actually used**, don't assume. A field that is stored but never
  applied looks configured while traffic goes direct.

---

## 3. Failure semantics

| Condition | Behavior |
|---|---|
| Upstream says not logged in | Cool the account down (long), try the next one |
| Upstream rate-limits | Cool the account down (long), try the next one |
| Network error | Short cool-down, try the next one |
| All accounts cooling | Return `429` with an explicit "no account available" error |
| No cookie for an account | Skip it at load time rather than failing at request time |

**Distinguish "rate-limited" from "model unavailable".** A rate-limit message is a
throttle signal, not a model failure — misclassifying it produces a model list full of
false negatives. When probing availability, go serial with a generous interval, and treat
a throttle response as "unknown", not "broken".
