# Ops floor (P3-OPS): metering, rate limits, alerts

`ops/` is a standalone package: COGS metering into `usage_events`, per-account
rate limits over the same table, and threshold alerting. It imports nothing
from `canon/`, `extract/`, `ingest/`, or `ask/`; the only `ui/` import is
`ui.auth.get_current_user`, lazily, inside `ops.middleware`. The table it
reads and writes is `usage_events` from
`supabase/migrations/20260703060000_identity_and_access.sql` (P3-IDENTITY).

## Wiring

**LLM call sites (extract/llm.py and the report engine)** — one-line change,
owned by those streams, not by ops:

```python
from ops import meter

# before:  response = client.messages.create(**request)
response = meter("extraction", cursor=cur, user_id=uid, world_id=wid)(
    client.messages.create)(**request)
```

`meter` reads `response.usage.input_tokens` / `output_tokens` and
`response.model` (pass `model=` as a fallback), prices them, and inserts a
`usage_events` row. A failed insert alerts (`metering_error`) and still
returns the response — metering never breaks a paid-for call. Context-manager
form: `with meter(...) as m: ... m.record(response)`.

**FastAPI routes (ui/app.py, wired by P3-SURFACE)**:

```python
from fastapi import Depends
from ops.middleware import rate_limited
from ops import record_action

ask_guard = rate_limited("ask", cursor_factory=my_cursor_factory)

@app.get("/ask")
def ask(user=Depends(ask_guard)):          # 401 unauthenticated, 429 over limit
    result = run_ask(...)
    record_action(cur, user_id=user.id, action="ask", world_id=wid)  # on success
    return result
```

The guard authenticates via `ui.auth.get_current_user`, checks the limit, and
returns the user. Denials are `429` with a `Retry-After` header (seconds).
Counting contract: an action consumes quota only when the route logs it with
`record_action` **after success** — denied or failed attempts are free. Keep
action names (`ask`, `report_run`, `script_ingest`, `world_create`) distinct
from metering kinds (`extraction`, `report_f1`, ...); both live in
`usage_events.kind`.

`tier_resolver=` maps a user to `"free"` / `"paid"` (everyone is free until
P3-BILLING). Rollups: `per_world_cogs(cur, world_id)`,
`per_user_cogs(cur, user_id, since=...)`.

## Default limits (docs/readers-report.md free-tier guardrails)

| Action | Free | Paid (abuse guard) | Window |
|---|---|---|---|
| `world_create` | 1 | 25 | lifetime |
| `script_ingest` | 1 | 100 | lifetime |
| `report_run` | 3 | 50 | 24 h sliding |
| `ask` | 20 | 500 | 1 h sliding |
| script size | 130 pages | 600 pages | static (`check_script_pages`) |

**Fail-open:** if the `usage_events` query errors, `check_limit` allows the
request and fires `ratelimit_db_error`. Availability beats enforcement; the
alert is how we notice we're flying unmetered.

## Environment variables

| Var | Default | Meaning |
|---|---|---|
| `ALERT_WEBHOOK_URL` | unset | POST JSON alerts here; unset = log-only no-op |
| `CANON_LIMIT_<TIER>_<ACTION>` | table above | max events, e.g. `CANON_LIMIT_FREE_ASK=30` |
| `CANON_FREE_MAX_PAGES` / `CANON_PAID_MAX_PAGES` | 130 / 600 | script page cap |
| `CANON_REPORT_COGS_ALERT_USD` | 3.00 | per-report COGS alert threshold |
| `CANON_DAILY_SPEND_CAP_USD` | 50.00 | trailing-24h total spend alert |
| `CANON_DENIAL_SPIKE_THRESHOLD` | 20 | 429s in window before spike alert |
| `CANON_DENIAL_SPIKE_WINDOW_SECONDS` | 300 | denial-spike window |

Env is read at call time (`get_policy(tier)`), so overrides apply without a
restart for new requests.

## What fires alerts

| Event | Fired by | Trigger |
|---|---|---|
| `report_cogs_exceeded` | `alerts.check_report_cogs` (call after each report run) | one report > $3 — the readers-report "revisit the free tier" trigger |
| `daily_spend_cap_exceeded` | `alerts.check_daily_spend` (call from a cron/loop) | trailing-24h `SUM(cost_usd)` > cap |
| `rate_limit_denial_spike` | automatic, on denials | ≥ threshold 429s in the window (fires once, then resets) |
| `ratelimit_db_error` | automatic | rate-limit store unreachable → failing open |
| `metering_error` | automatic | usage insert failed after a successful LLM call |
| `eval_failure` | `alerts.eval_failed` — stub, wire from CI | eval gate goes red |

Every alert is logged under the `canon.ops` logger regardless of webhook
config.

## Updating the pricing table

`ops/metering.py::PRICING` maps model id → `(input, output)` USD per Mtok.
When a model's price changes or a new model ships: (1) update the dict from
the published Anthropic pricing page, (2) update the cost expectations in
`tests/test_ops.py`, (3) sanity-check the per-script estimate in
docs/readers-report.md ("Unit economics") still holds. Unknown models are
never guessed: they record cost 0 with the kind suffixed `:unpriced`, so
`SELECT ... WHERE kind LIKE '%:unpriced'` is the audit query for missing
prices.
