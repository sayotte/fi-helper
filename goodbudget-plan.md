# sync_goodbudget.py — Implementation Plan

## Purpose

Push envelope assignments from `classified.csv` back to Goodbudget via its private API.
Build and verify one piece at a time, never touching live data until the final step.

---

## Full API Reference

### Authentication

**Login**

```
POST https://goodbudget.com/login_check
Content-Type: application/x-www-form-urlencoded

_username=<email>&_password=<password>
```

Response: HTTP 302 redirect. Sets cookie `GBSESS=<token>`.
All subsequent requests must include the `GBSESS` cookie.

**Get household_id**

After login, fetch `https://goodbudget.com/home` (GET). Parse household_id from the HTML:
```
householdData: {"Uuid":"bd45c196-67e1-4fb3-bc8b-4b62169357b2",...}
```
Regex: `r'"Uuid"\s*:\s*"([0-9a-f-]{36})"'` on the first match after `householdData`.
Known value: `bd45c196-67e1-4fb3-bc8b-4b62169357b2`

**Logout**

```
GET https://goodbudget.com/logout
```

Deletes `GBSESS` cookie (server sets it to `deleted`).

---

### Get Envelopes

```
GET https://goodbudget.com/api/envelopes
X-Requested-With: XMLHttpRequest
```

Returns a nested JSON tree. Walk it recursively to extract `{FullName: Uuid}` pairs.
Only leaf nodes (non-group, non-header, non-totals) have both `FullName` and `Uuid` as plain strings.
Group nodes have `Uuid` prefixed with `g_` and lack `FullName`.

Example leaf:
```json
{
  "Id": 34145747,
  "Uuid": "18319ff3-ced1-408f-96b5-3eb0edb4ab13",
  "Name": "Shopping",
  "FullName": "Discretionary:Shopping",
  "EnvelopeType": "ENV_REG"
}
```

Special envelope:
- `[Needs Envelope]`: `EnvelopeType == "ENV_NO_ENV"`, `Uuid = "7b0e8896-963e-40f3-942f-324d8e71e8e8"`
- `[Available]`: `EnvelopeType == "ENV_INC"`

To walk the tree: iterate `data[0]["nodes"]`, recurse into each node's `"nodes"` list.
A node is a leaf if `"Uuid"` does NOT start with `"g_"`.

---

### Get Accounts

```
GET https://goodbudget.com/api/accounts
X-Requested-With: XMLHttpRequest
```

Nested structure similar to envelopes. Leaf accounts have `Uuid`, `Name`, `AccountType`.
Build `{Name: Uuid}` map for non-debt, non-header nodes.

Key accounts (known UUIDs):
- `Wells Fargo:Checking:Primary` → `17958583-2944-4814-8a2c-4ebf92f1190d`
- `Wells Fargo Visa` → `1075fd0d-0d73-4708-8054-f9ab1d0e15bc`

---

### Get NTC Transactions (paginated)

"NTC" = Not To Clear (uncleared). Returns 120 items per page.

```
GET https://goodbudget.com/api/ntc_transactions?page=N&_=<epoch_ms>
X-Requested-With: XMLHttpRequest
```

Response:
```json
{
  "count": 274,
  "currPage": 1,
  "numPages": 0,
  "items": [...]
}
```

**WARNING**: `numPages` is always 0 (bug). Calculate page count as `ceil(count / 120)`.
Paginate from page=1 until items_received >= count.
Use `int(time.time() * 1000)` for the `_` cache-buster.

Each item:
```json
{
  "uuid": "019c5d57-30f8-8094-85b7-d96b905c26f0",
  "envelope_uuid": "7b0e8896-963e-40f3-942f-324d8e71e8e8",
  "account_uuid": "17958583-2944-4814-8a2c-4ebf92f1190d",
  "receiver": "ONLINE TRANSFER REF #...",
  "amount": "10190.44",
  "trans_type": "DEB",
  "created": "2026-02-13 00:00:00",
  "status": "",
  "check_num": "",
  "description": ""
}
```

`amount` is always a positive string. `trans_type` is `"DEB"` (expense) or `"CRE"` (income/refund).

Filter for `[Needs Envelope]` with: `item["envelope_uuid"] == "7b0e8896-963e-40f3-942f-324d8e71e8e8"`

---

### Get Single Transaction (to fetch nonce)

```
GET https://goodbudget.com/api/transactions/get/<uuid>
X-Requested-With: XMLHttpRequest
```

Response:
```json
{
  "uuid": "f7dc82bf-eaef-4824-be45-79b4932ff303",
  "receiver": "TESTPAYEE",
  "created": "2026-02-21 20:29:15",
  "amount": "100.00",
  "note": "...",
  "check_num": "123",
  "type": "DEB",
  "nonce": "8a51dedf99acc4622551afd1aed1a62a09772dff",
  "status": "",
  "envelope": "18319ff3-ced1-408f-96b5-3eb0edb4ab13",
  "account": "17958583-2944-4814-8a2c-4ebf92f1190d"
}
```

The `nonce` is required for update/delete. It changes on each write. **Fetch it immediately before each update.**

---

### Update Transaction Envelope

```
POST https://goodbudget.com/api/transactions/save?cltVersion=web
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
X-Requested-With: XMLHttpRequest
Origin: https://goodbudget.com
Referer: https://goodbudget.com/home

id=<txn-uuid>&household_id=<household-uuid>&n=<nonce>&o=transaction&d=<base64-json>
```

The `d` parameter is `base64.b64encode(json.dumps(payload).encode()).decode()` where `payload` is:

```json
{
  "created": "2026-02-21 20:29:15",
  "uuid": "<txn-uuid>",
  "receiver": "<payee-name>",
  "status": "",
  "note": "<note-text>",
  "envelope": "<new-envelope-uuid>",
  "account": "<account-uuid>",
  "amount": "100.00",
  "nonce": "<nonce>",
  "type": "DEB",
  "check_num": "<check-num>"
}
```

### Create multi-envelope transaction
Request
```
curl 'https://goodbudget.com/api/transactions/save?cltVersion=web' \
  -H 'accept: */*' \
  -H 'accept-language: en-US,en;q=0.9' \
  -H 'cache-control: no-cache' \
  -H 'content-type: application/x-www-form-urlencoded; charset=UTF-8' \
  -b 'optimizelyEndUserId=oeu1771795475443r0.7103482980355496; optimizelySegments=%7B%7D; optimizelyBuckets=%7B%7D; GBSESS=1g4uoubgcifmq3c0l0s2bjajpi; _vwo_uuid_v2=D7E44467276FB4C769BE697FFA9B780D6|896aa8329de328a68137f2f2ea469aad' \
  -H 'origin: https://goodbudget.com' \
  -H 'pragma: no-cache' \
  -H 'priority: u=1, i' \
  -H 'referer: https://goodbudget.com/home' \
  -H 'sec-ch-ua: "Brave";v="143", "Chromium";v="143", "Not A(Brand";v="24"' \
  -H 'sec-ch-ua-mobile: ?0' \
  -H 'sec-ch-ua-platform: "macOS"' \
  -H 'sec-fetch-dest: empty' \
  -H 'sec-fetch-mode: cors' \
  -H 'sec-fetch-site: same-origin' \
  -H 'sec-gpc: 1' \
  -H 'user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36' \
  -H 'x-requested-with: XMLHttpRequest' \
  --data-raw 'id=eb791074-c6f9-4e5d-a63b-5b3e1d958e1a&household_id=bd45c196-67e1-4fb3-bc8b-4b62169357b2&n=&o=transaction&d=eyJjcmVhdGVkIjoiMjAyNi0wMi0yMiAxNjoyNDo0MiIsInV1aWQiOiJlYjc5MTA3NC1jNmY5LTRlNWQtYTYzYi01YjNlMWQ5NThlMWEiLCJyZWNlaXZlciI6InRlc3QiLCJub3RlIjoiIiwiZW52ZWxvcGUiOm51bGwsImFjY291bnQiOiIxNzk1ODU4My0yOTQ0LTQ4MTQtOGEyYy00ZWJmOTJmMTE5MGQiLCJhbW91bnQiOiIxMjMuNDUiLCJ0eXBlIjoiU1BMIiwiY2hlY2tfbnVtIjoiIiwiY2hpbGRyZW4iOlt7InV1aWQiOiIyZjQyNGJhYS1hN2YyLTRkNzUtODU2Ny1lNzM2ZGJhOGExNjAiLCJlbnZlbG9wZSI6IjU5ZjQ0ZjA1LWM4YzEtNGY3Mi04NTRkLTA3YWViN2E5NjBhMSIsImFtb3VudCI6IjkzLjQ1In0seyJ1dWlkIjoiODlmNDRmNTItOTgxNy00MWY5LWJkZmUtZDljNTRhM2Y3ZTY4IiwiZW52ZWxvcGUiOiIwYmIyOWQwYi04YzYzLTQxOWEtOTcyZC0wMThiMzcxNmRjMWIiLCJhbW91bnQiOiIzMCJ9XX0%3D'
```

Response
```
{"status":202,"reason":"Record Created"}
```

### Re-allocate existing transaction to multi-envelope
Request
```
curl 'https://goodbudget.com/api/transactions/save?cltVersion=web' \
  -H 'accept: */*' \
  -H 'accept-language: en-US,en;q=0.9' \
  -H 'cache-control: no-cache' \
  -H 'content-type: application/x-www-form-urlencoded; charset=UTF-8' \
  -b 'optimizelyEndUserId=oeu1771795475443r0.7103482980355496; optimizelySegments=%7B%7D; optimizelyBuckets=%7B%7D; GBSESS=1g4uoubgcifmq3c0l0s2bjajpi; _vwo_uuid_v2=D7E44467276FB4C769BE697FFA9B780D6|896aa8329de328a68137f2f2ea469aad' \
  -H 'origin: https://goodbudget.com' \
  -H 'pragma: no-cache' \
  -H 'priority: u=1, i' \
  -H 'referer: https://goodbudget.com/home' \
  -H 'sec-ch-ua: "Brave";v="143", "Chromium";v="143", "Not A(Brand";v="24"' \
  -H 'sec-ch-ua-mobile: ?0' \
  -H 'sec-ch-ua-platform: "macOS"' \
  -H 'sec-fetch-dest: empty' \
  -H 'sec-fetch-mode: cors' \
  -H 'sec-fetch-site: same-origin' \
  -H 'sec-gpc: 1' \
  -H 'user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36' \
  -H 'x-requested-with: XMLHttpRequest' \
  --data-raw 'id=eb791074-c6f9-4e5d-a63b-5b3e1d958e1a&household_id=bd45c196-67e1-4fb3-bc8b-4b62169357b2&n=a5e1f0b6e1e20acd198a4257ba1f93aecc000943&o=transaction&d=eyJjcmVhdGVkIjoiMjAyNi0wMi0yMiAxNjoyNDo0MiIsInV1aWQiOiJlYjc5MTA3NC1jNmY5LTRlNWQtYTYzYi01YjNlMWQ5NThlMWEiLCJyZWNlaXZlciI6InRlc3QiLCJzdGF0dXMiOiIiLCJub3RlIjoiIiwiZW52ZWxvcGUiOm51bGwsImFjY291bnQiOiIxNzk1ODU4My0yOTQ0LTQ4MTQtOGEyYy00ZWJmOTJmMTE5MGQiLCJhbW91bnQiOiIxMjMuNDUiLCJub25jZSI6ImE1ZTFmMGI2ZTFlMjBhY2QxOThhNDI1N2JhMWY5M2FlY2MwMDA5NDMiLCJ0eXBlIjoiU1BMIiwiY2hlY2tfbnVtIjoiIiwiY2hpbGRyZW4iOlt7InV1aWQiOiJmODhjMjg4NS1jMDA1LTQ2MjEtYmUxOC1jZDU4NjUwNzU2YzEiLCJlbnZlbG9wZSI6IjU5ZjQ0ZjA1LWM4YzEtNGY3Mi04NTRkLTA3YWViN2E5NjBhMSIsImFtb3VudCI6IjkzLjQ1In0seyJ1dWlkIjoiNGI5NTE5NzgtZTFkNS00NTdmLWExY2EtZjJjNjgzNTQzY2Q3IiwiZW52ZWxvcGUiOiIwYmIyOWQwYi04YzYzLTQxOWEtOTcyZC0wMThiMzcxNmRjMWIiLCJhbW91bnQiOiIzMC4wMCJ9XX0%3D'
```

Response
```
{"status":202,"reason":"Record Created"}
```


### Key rules:
- `n` (POST body param) and `"nonce"` (in JSON) must be the **same** value fetched from GET
- `"amount"` is always a **positive** string (e.g. `"2673.75"`, not `"-2673.75"`)
- `"type"` is `"DEB"` for expenses (negative in classified.csv), `"CRE"` for income/refunds
- `"status"` is `""` for normal update, `"DEL"` for delete
- For update (not create): `n` is non-empty; for create: `n=""` and no `"nonce"` or `"status"` in JSON

Response: `{"status":202,"reason":"Record Created"}` (202 for both create and update)

---

## Data Sources

### classified.csv columns
`date, envelope, account, name, notes, amount, status, source, parent_name, parent_amount, is_food, is_taxable, costco_item_id`

- `date`: `MM/DD/YYYY`
- `amount`: negative string for expenses (`"-2673.75"`), positive for income/refunds
- `source`: `goodbudget` | `amazon_order` | `costco_receipt`
- `envelope`: `"Category:Subcategory"` format (matches `FullName` in Goodbudget API)

Filter rows: `source == 'goodbudget'` AND `envelope not in ('[Needs Envelope]', '[Skip]')`
This yields ~913 rows ready to sync.

---

## Matching Logic

Match `classified.csv` rows to Goodbudget NTC ([Needs Envelope]) transactions:

1. **Key**: `(date_obj, abs_amount, receiver_str)` where:
   - `date_obj` = `datetime.datetime.strptime(classified_date, "%m/%d/%Y").date()`
   - `abs_amount` = `round(abs(float(classified_amount)), 2)`
   - `receiver_str` = `classified_name` (exact string, case-sensitive)

   Goodbudget side:
   - `date_obj` = `datetime.datetime.strptime(ntc_created[:10], "%Y-%m-%d").date()`
   - `abs_amount` = `round(float(ntc_amount), 2)`
   - `receiver_str` = `ntc_receiver`

2. Build index on NTC side; look up each classified row.
3. If no exact match, try ±1 day (keep same amount+receiver).
4. Unique match → update. No match or ambiguous → skip with warning.

---

## Incremental Build Steps

### Step 1 — Auth + household_id ✓ DONE

Implement `login()`, `get_household_id()`, `logout()`. Also: `session()` context manager,
`COMMANDS` dispatch dict — adding a new step is one function + one dict entry, `main()` never changes.

```bash
python3 sync_goodbudget.py --step auth
```

Output:
```
Logged in. household_id=bd45c196-67e1-4fb3-bc8b-4b62169357b2
Logged out.
```

---

### Step 2 — Fetch reference data ✓ DONE

Implement `get_envelope_map()` and `get_account_map()` via generic `_walk_nodes(nodes, result,
name_field, filter_fn=None, prune_fn=None)`. `prune_fn` skips a node AND its children (used to
exclude totals sections); `filter_fn` controls whether to add a node (used to exclude DEBT accounts).

```bash
python3 sync_goodbudget.py --step envelopes
python3 sync_goodbudget.py --step accounts
```

Verified output (envelopes):
```
52 envelopes found.
  [Needs Envelope] → 7b0e8896-963e-40f3-942f-324d8e71e8e8
  Automotive:Gas → 59f44f05-c8c1-4f72-854d-07aeb7a960a1
  Discretionary:Shopping → 18319ff3-ced1-408f-96b5-3eb0edb4ab13
  ...
```

Verified output (accounts): 9 accounts; Wells Fargo Visa, WF Checking:Primary UUIDs confirmed;
debt accounts (Mr. Cooper, Truist) absent.

---

### Step 3 — Fetch NTC transactions + cache

Implement `get_ntc_transactions()` (paginated). Write result to `ntc_cache.json`.

```bash
python3 sync_goodbudget.py --step ntc
```

Expected:
```
Fetched 274 NTC transactions (3 pages).
  [Needs Envelope]: 232
  Already categorized: 42
ntc_cache.json written.
```

Verify: counts match history.csv (274 NTC status, 232 [Needs Envelope]).

---

### Step 4 — Load classified rows (offline)

Implement `load_classified()`.

```bash
python3 sync_goodbudget.py --step classified
```

Expected:
```
913 goodbudget rows ready to sync.
Sample: 02/13/2026 | Businessolver Be | -2673.75 | Health:Premiums
```

Verify: 913 rows, all with real envelope names.

---

### Step 5 — Match (offline, uses ntc_cache.json)

Implement `build_ntc_index()` + `match_classified_to_ntc()`.

```bash
python3 sync_goodbudget.py --step match
```

Expected:
```
Match results (against 232 [Needs Envelope] NTC transactions):
  Unique match:  N
  No match:      N
  Ambiguous:     N
```

Verify: total == 232. Review no-match list; should be explainable.

---

### Step 6 — Dry run (full pipeline, no writes)

Wire together all pieces. Implement `update_envelope(dry_run=True)`.

```bash
python3 sync_goodbudget.py --dry-run
```

Expected:
```
[DRY RUN] Businessolver Be  2026-02-13  $2673.75  →  Health:Premiums
...
Summary: N to update, N skipped (no match), N ambiguous
```

Verify: spot-check 5–10 rows against classified.csv manually.

---

### Step 7 — Capture split transaction API format  ⚠️ REQUIRES MANUAL ACTION

**Background**: 51 NTC [Needs Envelope] items are Amazon/Costco transactions that
`unify.py` exploded into item-level rows (`source = "amazon_order"` / `"costco_receipt"`),
each with a different envelope. These cannot be assigned a single envelope — they need the
Goodbudget split transaction API, which has not been documented yet.

We want to understand the full API before writing anything live.

**How to capture the split API format:**

1. Open Chrome DevTools → Network tab → filter by `save`
2. In Goodbudget UI, find one of the Amazon NTC transactions (receiver = "Amazon")
3. Manually split it into **2 envelopes** (any amounts/envelopes — we just need the format)
4. Click Save — capture the POST to `/api/transactions/save`
5. Copy as cURL (or copy the raw request body)
6. The `d` parameter is base64-encoded JSON — decode it to see the split payload structure

**Known single-envelope `d` payload for reference:**
```json
{
  "created": "YYYY-MM-DD HH:MM:SS",
  "uuid": "<txn-uuid>",
  "receiver": "<payee>",
  "status": "",
  "note": "",
  "envelope": "<envelope-uuid>",
  "account": "<account-uuid>",
  "amount": "100.00",
  "nonce": "<nonce>",
  "type": "DEB",
  "check_num": ""
}
```

Expected: `envelope` becomes an array or the structure gains a `splits` / `details` field.
Document the captured format in this file before proceeding to Step 8.

---

### Step 8 — Implement split transaction support

Once the split API format is captured, implement:
- `build_split_index()` — groups `amazon_order` / `costco_receipt` classified rows by
  their `parent_name` + `parent_amount` + date, yielding `{(date, amt, receiver): [rows]}`
- `update_split_envelope(opener, ntc_item, splits, ...)` — GET nonce, POST split payload
- `--dry-run` and `--step match` updated to show split matches alongside single-envelope matches

---

### Step 8.5 — Review split line items before any live writes  ⚠️ REQUIRED

Implement `--step show-splits` (offline, reads `ntc_cache.json` + `classified.csv`).
Prints each matched split group with child rows so the data can be reviewed before
touching anything live.

```bash
python3 sync_goodbudget.py --step show-splits
```

Output format:
```
Amazon  2026-01-15  $134.22
    $89.99  Discretionary:Shopping
    $44.23  Food:Groceries
Costco  2026-01-20  $312.45
    ...
```

**Do not proceed to Step 9 until every split group looks correct.**

---

### Step 9 — Live update, single-envelope transactions (batch of 5 first)

Implement `update_envelope(dry_run=False)`: GET nonce → POST save → 0.2s sleep.
**Error handling required**: check returned JSON `{"status": 202, ...}` after every POST;
raise with txn UUID + receiver on any unexpected value. Wrap the update loop in try/except
so a single failure prints a clear message and aborts rather than dumping a traceback
mid-run. Silent failures are not acceptable.

Covers the **172 single-envelope matched transactions** (non-Amazon, non-Costco NTC items).

```bash
python3 sync_goodbudget.py --limit 5
```

Manually verify in Goodbudget UI: those 5 transactions now have correct envelopes.

---

### Step 10 — Live update, split transactions (batch of 5 first)

```bash
python3 sync_goodbudget.py --splits-only --limit 5
```

Manually verify in Goodbudget UI: those 5 Amazon transactions now have correct split envelopes.

---

### Step 11 — Full run

```bash
python3 sync_goodbudget.py
```

Expected:
```
Updated 172 single-envelope. Updated ~51 split. Skipped ~9 (no match). 0 errors.
```

Verify: log into Goodbudget; [Needs Envelope] count drops from 232 to ~9.

---

## CLI Flags

| Flag | Effect |
|------|--------|
| `--step auth` | Login, print household_id, logout |
| `--step envelopes` | Print envelope map |
| `--step accounts` | Print account map |
| `--step ntc` | Fetch NTC, print summary, write ntc_cache.json |
| `--step classified` | Print classified row summary |
| `--step match` | Match offline using ntc_cache.json (single + split) |
| `--step show-splits` | Print matched split groups with line items (offline) |
| `--dry-run` | Full pipeline, no writes |
| `--limit N` | Write only first N matched transactions |
| `--splits-only` | Write only split (Amazon/Costco) transactions |
| *(none)* | Full pipeline, all writes |

---

## Implementation Notes

- **Stdlib only**: `urllib.request`, `urllib.parse`, `http.cookiejar`, `base64`, `json`,
  `csv`, `re`, `datetime`, `uuid`, `getpass`, `argparse`, `time`, `math`
- Use `http.cookiejar.CookieJar` + `urllib.request.build_opener(HTTPCookieProcessor(jar))` for session cookies
- Set `User-Agent`, `X-Requested-With: XMLHttpRequest`, `Referer: https://goodbudget.com/home`
  on all API calls (server may reject requests missing these)
- Credentials: check `GOODBUDGET_USER` / `GOODBUDGET_PASS` env vars; else `input()` / `getpass.getpass()`
- `ntc_cache.json` goes in project root; add to `.gitignore`
- `goodbudget-plan.md` — this file, keep in repo for reference

---

## Backlog

### Progress printing

Add status output for slow operations:
- `print("Logging in...")` before `login()`
- In pagination loops (`get_ntc_transactions`, `get_all_transactions`): print
  `Fetching page X/Y...` using the calculated `num_pages`
- In the update loop: print `Updated X/Y...` (overwrite line with `\r`, or just print each)
- Final summary line already planned; keep it

### New classification rule: Breda Pest Management

Add to `classification_rules.csv`:
```
Breda Pest Management,contains,Household:Pest Control,
```

**Prerequisite**: the envelope `Household:Pest Control` must be created manually in the
Goodbudget UI (under the `Household` group) before sync will assign it — `resolve_envelope_uuid`
returns `None` for unknown envelope names and skips the transaction with a warning.
