# sync_goodbudget.py — Reference

## Purpose

Push envelope assignments from `classified.csv` back to Goodbudget via its private API.

---

## API Reference

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

**Logout**

```
GET https://goodbudget.com/logout
```

---

### Get Envelopes

```
GET https://goodbudget.com/api/envelopes
X-Requested-With: XMLHttpRequest
```

Returns a nested JSON tree. Walk it recursively to extract `{FullName: Uuid}` pairs.
Only leaf nodes (non-group, non-header, non-totals) have both `FullName` and `Uuid` as plain strings.
Group nodes have `Uuid` prefixed with `g_` and lack `FullName`.

Special envelopes:
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

Each item has: `uuid`, `envelope_uuid`, `account_uuid`, `receiver`, `amount` (positive string),
`trans_type` (`"DEB"` or `"CRE"`), `created` (`"YYYY-MM-DD HH:MM:SS"`), `status`, `check_num`.

Filter for `[Needs Envelope]` with: `item["envelope_uuid"] == "7b0e8896-963e-40f3-942f-324d8e71e8e8"`

---

### Get All Transactions (paginated)

```
GET https://goodbudget.com/api/transactions?page=N&_=<epoch_ms>
X-Requested-With: XMLHttpRequest
```

Same pagination pattern as NTC. Stop when a page returns < 120 items (no `count` field).

Split parent items have `trans_type == "SPL"` and `envelope_uuid == null`.
Split child items have `parentUuid` set to the parent's uuid.

---

### Get Single Transaction (to fetch nonce)

```
GET https://goodbudget.com/api/transactions/get/<uuid>
X-Requested-With: XMLHttpRequest
```

Returns full transaction detail including `nonce`. **Fetch nonce immediately before each update** — it changes on each write.

See `goodbudget-test-fixtures.json` → `txn_detail_example` for response structure.

---

### Update Transaction Envelope (single-envelope)

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
  "created": "YYYY-MM-DD HH:MM:SS",
  "uuid": "<txn-uuid>",
  "receiver": "<payee>",
  "status": "CLR",
  "note": "",
  "envelope": "<new-envelope-uuid>",
  "account": "<account-uuid>",
  "amount": "100.00",
  "nonce": "<nonce>",
  "type": "DEB",
  "check_num": ""
}
```

Response: `{"status":202,"reason":"Record Created"}` (202 for both create and update)

---

### Update Transaction Envelope (income)

Income transactions (`classified.csv` envelope starts with `Income:`) require a different
structure. Detection: `clf_row["envelope"].startswith("Income:")`.

Same endpoint and POST format. Key differences from single-envelope:

- `"type": "INC"`, `"envelope": null`
- `"amount"` is a **negative float** (not a positive string) — money flowing into the account
- Add `"children"` array with one entry: `"type": "ADJ"`, `"envelope": "<available-uuid>"`,
  same negative amount, plus `receiver`/`created`/`nonce`/`check_num` from parent and a fresh UUID
- Child `"status"` is `"NTC"` even when parent `"status"` is `"CLR"`
- `Income:*` envelope names (e.g. `Income:Interest`) don't exist in Goodbudget; they all map to
  `[Available]` via `resolve_envelope_uuid`

Example payload (from `income-example.sh`):
```json
{
  "created": "2026-01-31 00:00:00",
  "uuid": "<txn-uuid>",
  "receiver": "INTEREST",
  "status": "CLR",
  "note": "",
  "envelope": null,
  "account": "<account-uuid>",
  "amount": -0.36,
  "nonce": "<nonce>",
  "type": "INC",
  "check_num": "",
  "children": [{
    "amount": -0.36,
    "check_num": "",
    "created": "2026-01-31 00:00:00",
    "envelope": "<available-uuid>",
    "nonce": "<nonce>",
    "receiver": "INTEREST",
    "status": "NTC",
    "type": "ADJ",
    "uuid": "<new-uuid>"
  }]
}
```

---

### Update Transaction Envelope (split / multi-envelope)

Same endpoint and POST format. Key differences:

- `"type": "SPL"`, `"envelope": null`
- Add `"children"` array: `[{"uuid": "<new-uuid>", "envelope": "<env-uuid>", "amount": "N.NN"}, ...]`
- For **re-allocating** an existing txn: include `"status"` and `"nonce"` in payload, set `n=<nonce>`
- For **creating new**: omit `"status"` and `"nonce"` from payload, set `n=""`

See `goodbudget-test-fixtures.json` → `split_payload_create` / `split_payload_realloc` for full examples.

### Key rules:
- `n` (POST body param) and `"nonce"` (in JSON) must be the **same** value fetched from GET
- `"amount"` is always a **positive** string (e.g. `"2673.75"`, not `"-2673.75"`)
- `"type"` is `"DEB"` for expenses, `"CRE"` for credit/refunds, `"INC"` (with children) for income (envelope starts with `Income:`)
- `"status"` is `""` for normal update, `"DEL"` for delete, `"CLR"` to confirm

---

## Data Sources

### classified.csv columns
`date, envelope, account, name, notes, amount, status, source, parent_name, parent_amount, is_food, is_taxable, costco_item_id`

- `date`: `MM/DD/YYYY`
- `amount`: negative string for expenses (`"-2673.75"`), positive for income/refunds
- `source`: `goodbudget` | `amazon_order` | `costco_receipt`
- `envelope`: `"Category:Subcategory"` format (matches `FullName` in Goodbudget API)
- `status`: `NTC` (uncleared) | `CLR` (cleared)

Filter for NTC single-envelope sync: `source == 'goodbudget'` AND `envelope not in ('[Needs Envelope]', '[Skip]')`

---

## Matching Logic

Match `classified.csv` rows to Goodbudget NTC transactions:

1. **Key**: `(date_obj, abs_amount, receiver_str)` — exact string match on receiver
2. Build index on NTC side; look up each classified row
3. If no exact match, try ±1 day (same amount + receiver)
4. Unique match → update. No match → skip with warning
5. Consume-from-pool: each NTC item can only be claimed once (handles duplicate transactions)

Split matching: group `amazon_order` / `costco_receipt` rows by `(date, abs(parent_amount), parent_name)`, then match that key against NTC items.

---

## CLI Flags

| Flag | Effect |
|------|--------|
| `--step auth` | Login, print household_id, logout |
| `--step envelopes` | Print envelope map |
| `--step accounts` | Print account map |
| `--step ntc` | Fetch NTC, print summary, write ntc_cache.json |
| `--step fetch-all` | Fetch all transactions, write all_txn_cache.json |
| `--step classified` | Print classified row summary |
| `--step match` | Match offline using ntc_cache.json (single + split) |
| `--step show-splits` | Print matched split groups with line items (offline) |
| `--dry-run` | Full pipeline, no writes |
| `--limit N` | Write only first N matched transactions |
| `--splits-only` | Write only split (Amazon/Costco) transactions |
| `--historical` | Also process CLR (already-cleared) Amazon/Costco splits |
| `--historical-single` | Reclassify CLR non-split transactions (skip-if-already-correct) |
| *(none)* | Full pipeline: single-envelope NTC + split NTC |

---

## Implementation Notes

- **Stdlib only**: `urllib.request`, `urllib.parse`, `http.cookiejar`, `base64`, `json`,
  `csv`, `re`, `datetime`, `uuid`, `getpass`, `argparse`, `time`, `math`, `socket`
- Use `http.cookiejar.CookieJar` + `urllib.request.build_opener(HTTPCookieProcessor(jar))` for session cookies
- Set `User-Agent`, `X-Requested-With: XMLHttpRequest`, `Referer: https://goodbudget.com/home` on all API calls
- Credentials: check `GOODBUDGET_USER` / `GOODBUDGET_PASS` env vars or `secrets.sh`; else prompt
- `ntc_cache.json` / `all_txn_cache.json` in project root; add to `.gitignore`
- `Income:*` envelopes map to `[Available]` (no named envelope exists in Goodbudget)
- Retry with exponential backoff via `api_request()`: 2s/4s/8s on timeout/connection errors; raises immediately on 4xx

---

## Backlog

### Progress printing

Add status output for slow operations:
- `print("Logging in...")` before `login()`
- In pagination loops: print `Fetching page X/Y...`
- In the update loop: print `Updated X/Y...` (overwrite line with `\r`, or just print each)

### New classification rule: Breda Pest Management

Add to `classification_rules.csv`:
```
Breda Pest Management,contains,Household:Pest Control,
```

**Prerequisite**: create envelope `Household:Pest Control` in the Goodbudget UI first.

### Configurable confirmation behavior

Add `--no-confirm` flag (default: confirm / CLR). For manually-entered transactions, Goodbudget
needs to match them to a bank import before CLR'ing — sync without confirming in that case.
