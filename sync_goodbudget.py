#!/usr/bin/env python3
"""sync_goodbudget.py — Push envelope assignments from classified.csv to Goodbudget."""

import argparse
import base64
import csv
import datetime
import getpass
import http.client
import http.cookiejar
import json
import math
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager


BASE_URL = "https://goodbudget.com"
NEEDS_ENVELOPE_UUID = "7b0e8896-963e-40f3-942f-324d8e71e8e8"
NTC_CACHE = os.path.join(os.path.dirname(__file__), "ntc_cache.json")
ALL_TXN_CACHE = os.path.join(os.path.dirname(__file__), "all_txn_cache.json")
CLASSIFIED_CSV = os.path.join(os.path.dirname(__file__), "classified.csv")
SKIP_ENVELOPES = {"[Needs Envelope]", "[Skip]"}
AMAZON_COSTCO_RECEIVERS = {
    "Amazon", "Amazon Prime", "Amazon Kindle",
    "Amazon Prime Video", "Costco",
    "COSTCO WHSE #0188 ATLANTA GA",
}


def make_opener():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.jar = jar
    opener.addheaders = [
        ("User-Agent", "Mozilla/5.0"),
        ("X-Requested-With", "XMLHttpRequest"),
        ("Referer", f"{BASE_URL}/home"),
    ]
    return opener


RETRYABLE_ERRORS = (
    socket.timeout,
    urllib.error.URLError,
    http.client.RemoteDisconnected,
    ConnectionResetError,
    ConnectionAbortedError,
)


def api_request(opener, url_or_req, timeout=30, max_retries=3, label=""):
    """Make an HTTP request with timeout and exponential backoff retry.

    Returns the decoded response body as bytes.
    Retries on timeout, connection reset, and server errors (5xx).
    Raises immediately on client errors (4xx).
    """
    for attempt in range(max_retries + 1):
        try:
            with opener.open(url_or_req, timeout=timeout) as resp:
                return resp.read()
        except RETRYABLE_ERRORS as e:
            if attempt == max_retries:
                raise
            wait = 2 * (2 ** attempt)  # 2s, 4s, 8s
            desc = label or str(url_or_req)[:60]
            print(f"  Retry {attempt + 1}/{max_retries}: {desc} after {wait}s ({e})")
            time.sleep(wait)
        except urllib.error.HTTPError as e:
            if e.code >= 500 and attempt < max_retries:
                wait = 2 * (2 ** attempt)
                desc = label or str(url_or_req)[:60]
                print(f"  Retry {attempt + 1}/{max_retries}: {desc} after {wait}s (HTTP {e.code})")
                time.sleep(wait)
            else:
                raise


SECRETS_FILE = os.path.join(os.path.dirname(__file__), "secrets.sh")


def _load_secrets_file():
    """Parse KEY="value" or KEY=value lines from secrets.sh."""
    secrets = {}
    try:
        with open(SECRETS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r'^([A-Z_]+)=["\']?([^"\']*)["\']?$', line)
                if m:
                    secrets[m.group(1)] = m.group(2)
    except FileNotFoundError:
        pass
    return secrets


def get_credentials():
    file_secrets = _load_secrets_file()
    user = (os.environ.get("GOODBUDGET_USER")
            or file_secrets.get("GOODBUDGET_USER")
            or input("Email: "))
    passwd = (os.environ.get("GOODBUDGET_PASS")
              or file_secrets.get("GOODBUDGET_PASS")
              or getpass.getpass("Password: "))
    return user, passwd


@contextmanager
def session():
    opener = make_opener()
    user, passwd = get_credentials()
    login(opener, user, passwd)
    try:
        yield opener
    finally:
        logout(opener)


def login(opener, user, passwd):
    url = f"{BASE_URL}/login_check"
    body = urllib.parse.urlencode({"_username": user, "_password": passwd}).encode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest",
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    # urllib follows the 302 redirect automatically; GBSESS cookie lands in the jar
    api_request(opener, req, label="login")


def get_household_id(opener):
    url = f"{BASE_URL}/home"
    html = api_request(opener, url, label="get household_id").decode("utf-8", errors="replace")
    # Find "householdData" then grab the first Uuid after it
    idx = html.find("householdData")
    if idx == -1:
        raise RuntimeError("householdData not found in /home response — login may have failed")
    snippet = html[idx:]
    m = re.search(r'"Uuid"\s*:\s*"([0-9a-f-]{36})"', snippet)
    if not m:
        raise RuntimeError("Could not find Uuid in householdData block")
    return m.group(1)


def _walk_nodes(nodes, result, name_field, filter_fn=None, prune_fn=None):
    """Recursively collect {name: uuid} from a Goodbudget API node tree.

    prune_fn(node) → True  : skip this node AND its children entirely (e.g. totals sections)
    filter_fn(node) → True : add this node to result (default: add all)
    """
    for node in nodes:
        if prune_fn and prune_fn(node):
            continue
        uuid = node.get("Uuid", "")
        if not uuid.startswith("g_") and name_field in node:
            if filter_fn is None or filter_fn(node):
                result[node[name_field].strip()] = uuid
        if "nodes" in node:
            _walk_nodes(node["nodes"], result, name_field, filter_fn, prune_fn)


def get_envelope_map(opener):
    url = f"{BASE_URL}/api/envelopes"
    data = json.loads(api_request(opener, url, label="get envelopes"))
    result = {}
    _walk_nodes(data[0]["nodes"], result, "FullName")
    return result


def get_account_map(opener):
    url = f"{BASE_URL}/api/accounts"
    data = json.loads(api_request(opener, url, label="get accounts"))
    result = {}
    _walk_nodes(data[0]["nodes"], result, "Name",
                filter_fn=lambda n: n.get("AccountType") != "DEBT",
                prune_fn=lambda n: n.get("totals") or n.get("total") or n.get("subtotal"))
    return result


def logout(opener):
    url = f"{BASE_URL}/logout"
    api_request(opener, url, label="logout")


def cmd_auth(opener):
    hid = get_household_id(opener)
    print(f"Logged in. household_id={hid}")


def cmd_envelopes(opener):
    env_map = get_envelope_map(opener)
    print(f"{len(env_map)} envelopes found.")
    for name in sorted(env_map):
        print(f"  {name} → {env_map[name]}")


def cmd_accounts(opener):
    acct_map = get_account_map(opener)
    print(f"{len(acct_map)} accounts found.")
    for name in sorted(acct_map):
        print(f"  {name} → {acct_map[name]}")


def get_ntc_transactions(opener):
    all_items = []
    page = 1
    total = None
    while True:
        ts = int(time.time() * 1000)
        url = f"{BASE_URL}/api/ntc_transactions?page={page}&_={ts}"
        data = json.loads(api_request(opener, url, label=f"ntc page {page}"))
        if total is None:
            total = data["count"]
            num_pages = math.ceil(total / 120)
        all_items.extend(data["items"])
        if len(all_items) >= total or page >= num_pages:
            break
        page += 1
    return all_items


def cmd_ntc(opener):
    items = get_ntc_transactions(opener)
    num_pages = math.ceil(len(items) / 120) if items else 1
    needs = sum(1 for i in items if i.get("envelope_uuid") == NEEDS_ENVELOPE_UUID)
    other = len(items) - needs
    print(f"Fetched {len(items)} NTC transactions ({num_pages} pages).")
    print(f"  [Needs Envelope]: {needs}")
    print(f"  Already categorized: {other}")
    with open(NTC_CACHE, "w") as f:
        json.dump(items, f, indent=2)
    print(f"{NTC_CACHE} written.")


def get_all_transactions(opener):
    all_items = []
    page = 1
    total = None
    while True:
        ts = int(time.time() * 1000)
        url = f"{BASE_URL}/api/transactions?page={page}&_={ts}"
        data = json.loads(api_request(opener, url, label=f"all txn page {page}"))
        page_items = data["items"]
        all_items.extend(page_items)
        if len(page_items) < 120:
            break
        page += 1
    return all_items


def cmd_fetch_all(opener):
    items = get_all_transactions(opener)
    with open(ALL_TXN_CACHE, "w") as f:
        json.dump(items, f, indent=2)
    ntc = sum(1 for i in items if i.get("envelope_uuid") == NEEDS_ENVELOPE_UUID)
    clr_amz = sum(1 for i in items
                  if i.get("envelope_uuid") != NEEDS_ENVELOPE_UUID
                  and i["receiver"] in AMAZON_COSTCO_RECEIVERS)
    print(f"Fetched {len(items)} transactions total.")
    print(f"  NTC ([Needs Envelope]): {ntc}")
    print(f"  CLR Amazon/Costco:      {clr_amz}")
    print(f"{ALL_TXN_CACHE} written.")


def load_classified_rows(source=None, sources=None, status=None, exclude_envelopes=None):
    """Load rows from classified.csv with optional filters.

    source: single source string to match
    sources: set/list of source strings to match (alternative to source)
    status: status string to match (e.g. "NTC", "CLR")
    exclude_envelopes: set of envelope names to exclude
    """
    rows = []
    match_sources = {source} if source else (set(sources) if sources else None)
    with open(CLASSIFIED_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if match_sources and row["source"] not in match_sources:
                continue
            if status and row["status"] != status:
                continue
            if exclude_envelopes and row["envelope"] in exclude_envelopes:
                continue
            rows.append(row)
    return rows


def build_amazon_costco_index(all_txn_items):
    """Index CLR Amazon/Costco transactions. Skips NTC items (already handled by split matching)."""
    index = {}
    for item in all_txn_items:
        if item["receiver"] not in AMAZON_COSTCO_RECEIVERS:
            continue
        if item.get("envelope_uuid") == NEEDS_ENVELOPE_UUID:
            continue  # NTC — already handled by existing split matching
        date = datetime.date.fromisoformat(item["created"][:10])
        amt = round(float(item["amount"]), 2)
        key = (date, amt, item["receiver"])
        index.setdefault(key, []).append(item)
    return index


def build_clr_txn_index(all_txn_items):
    """Index CLR non-split, non-Amazon/Costco transactions for historical reclassification."""
    index = {}
    for item in all_txn_items:
        if item.get("envelope_uuid") == NEEDS_ENVELOPE_UUID:
            continue  # NTC — handled elsewhere
        if item["receiver"] in AMAZON_COSTCO_RECEIVERS:
            continue  # handled by split path
        if item.get("parentUuid"):
            continue  # split child — not independently addressable
        if item.get("trans_type") == "SPL":
            continue  # split parent — envelope is null; children hold envelopes
        date = datetime.date.fromisoformat(item["created"][:10])
        amt = round(float(item["amount"]), 2)
        key = (date, amt, item["receiver"])
        index.setdefault(key, []).append(item)
    return index


def build_split_index(split_rows):
    """Group sub-item rows by parent transaction key (date, abs_parent_amount, parent_name)."""
    index = {}
    for row in split_rows:
        date = datetime.datetime.strptime(row["date"], "%m/%d/%Y").date()
        amt = round(abs(float(row["parent_amount"])), 2)
        key = (date, amt, row["parent_name"])
        index.setdefault(key, []).append(row)
    return index


def match_splits_to_ntc(split_index, ntc_index):
    """Match split groups to NTC items using the same consume-from-pool pattern."""
    available = {key: list(items) for key, items in ntc_index.items()}
    matched = []  # list of (ntc_item, [clf_rows])
    for key, clf_rows in split_index.items():
        date, amt, name = key
        ntc_item = None
        for delta in (0, -1, 1):
            alt_key = (date + datetime.timedelta(days=delta), amt, name)
            pool = available.get(alt_key)
            if pool:
                ntc_item = pool.pop(0)
                break
        if ntc_item:
            matched.append((ntc_item, clf_rows))
    all_needs = [item for lst in ntc_index.values() for item in lst]
    matched_uuids = {ntc["uuid"] for ntc, _ in matched}
    no_match = [item for item in all_needs
                if item["uuid"] not in matched_uuids
                and item["receiver"] in AMAZON_COSTCO_RECEIVERS]
    return matched, no_match


def update_split_envelope(opener, ntc_item, clf_rows, env_map, household_id,
                          dry_run=False, prefix="SPLIT"):
    """Update an NTC transaction with split (or single) envelope assignment."""
    resolved = []
    for row in clf_rows:
        env_uuid = resolve_envelope_uuid(row["envelope"], env_map)
        if not env_uuid:
            print(f"  SKIP (unknown envelope {row['envelope']!r}): {ntc_item['receiver']}")
            return False
        resolved.append((row, env_uuid))

    unique_envs = {env_uuid for _, env_uuid in resolved}
    date = ntc_item["created"][:10]
    total = ntc_item["amount"]

    if dry_run:
        was = f"  (was: {ntc_item.get('name', '?')})" if "HIST" in prefix else ""
        if len(unique_envs) == 1:
            env_name = clf_rows[0]["envelope"]
            print(f"[DRY RUN {prefix}-1] {ntc_item['receiver'][:45]:<45}  {date}  ${total:>10}{was}  →  {env_name}")
        else:
            print(f"[DRY RUN {prefix}]   {ntc_item['receiver'][:45]:<45}  {date}  ${total:>10}{was}")
            for row, env_uuid in resolved:
                print(f"    {abs(float(row['amount'])):>8.2f}  {row['envelope']}")
        return

    # Single-envelope shortcut: reuse existing update_envelope
    if len(unique_envs) == 1:
        return update_envelope(opener, ntc_item, clf_rows[0], env_map, household_id)

    # Multi-envelope split: GET nonce, POST SPL payload with children
    url = f"{BASE_URL}/api/transactions/get/{ntc_item['uuid']}"
    txn_detail = json.loads(api_request(opener, url, label=f"get nonce (split) {ntc_item['receiver'][:30]}"))
    nonce = txn_detail["nonce"]

    children = [
        {"uuid": str(uuid.uuid4()), "envelope": env_uuid,
         "amount": f"{abs(float(row['amount'])):.2f}"}
        for row, env_uuid in resolved
    ]
    payload = {
        "created":   txn_detail["created"],
        "uuid":      txn_detail["uuid"],
        "receiver":  txn_detail["receiver"],
        "status":    "CLR",
        "note":      txn_detail.get("note", ""),
        "envelope":  None,
        "account":   txn_detail["account"],
        "amount":    txn_detail["amount"],
        "nonce":     nonce,
        "type":      "SPL",
        "check_num": txn_detail.get("check_num", ""),
        "children":  children,
    }
    d = base64.b64encode(json.dumps(payload).encode()).decode()
    body = urllib.parse.urlencode({
        "id": txn_detail["uuid"],
        "household_id": household_id,
        "n": nonce,
        "o": "transaction",
        "d": d,
    }).encode()
    save_url = f"{BASE_URL}/api/transactions/save?cltVersion=web"
    req = urllib.request.Request(save_url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
    req.add_header("Origin", BASE_URL)
    resp_data = json.loads(api_request(opener, req, label=f"save split {txn_detail['receiver'][:30]}"))
    if resp_data.get("status") != 202:
        raise RuntimeError(
            f"Unexpected split response for {txn_detail['uuid']} "
            f"{txn_detail['receiver']!r}: {resp_data}"
        )
    time.sleep(0.5)
    return True


def cmd_classified(opener=None):
    rows = load_classified_rows(source="goodbudget", exclude_envelopes=SKIP_ENVELOPES)
    print(f"{len(rows)} goodbudget rows ready to sync.")
    if rows:
        r = rows[0]
        print(f"Sample: {r['date']} | {r['name']} | {r['amount']} | {r['envelope']}")


def build_ntc_index(ntc_items):
    index = {}
    for item in ntc_items:
        if item["envelope_uuid"] != NEEDS_ENVELOPE_UUID:
            continue
        date = datetime.date.fromisoformat(item["created"][:10])
        amt = round(float(item["amount"]), 2)
        key = (date, amt, item["receiver"])
        index.setdefault(key, []).append(item)
    return index


def match_classified_to_ntc(classified_rows, ntc_index):
    # Mutable pools so duplicate transactions consume one NTC item each
    available = {key: list(items) for key, items in ntc_index.items()}
    claimed = {}  # ntc_uuid → (clf_row, ntc_item)
    for row in classified_rows:
        date = datetime.datetime.strptime(row["date"], "%m/%d/%Y").date()
        amt = round(abs(float(row["amount"])), 2)
        ntc_item = None
        for delta in (0, -1, 1):
            key = (date + datetime.timedelta(days=delta), amt, row["name"])
            pool = available.get(key)
            if pool:
                ntc_item = pool.pop(0)
                break
        if ntc_item:
            claimed[ntc_item["uuid"]] = (row, ntc_item)
    matched = list(claimed.values())
    all_needs = [item for lst in ntc_index.values() for item in lst]
    no_match = [item for item in all_needs if item["uuid"] not in claimed]
    return matched, no_match, []


def cmd_match(opener=None, historical=False, historical_single=False):
    with open(NTC_CACHE) as f:
        ntc_items = json.load(f)
    ntc_index = build_ntc_index(ntc_items)
    total_needs = sum(len(v) for v in ntc_index.values())
    classified_rows = load_classified_rows(source="goodbudget", exclude_envelopes=SKIP_ENVELOPES)
    matched, no_match, ambiguous = match_classified_to_ntc(classified_rows, ntc_index)
    print(f"Match results (against {total_needs} [Needs Envelope] NTC transactions):")
    print(f"  Unique match:  {len(matched)}")
    print(f"  No match:      {len(no_match)}")
    print(f"  Ambiguous:     {len(ambiguous)}")
    if no_match:
        print(f"\nNo match ({len(no_match)} items):")
        for item in no_match:
            print(f"  {item['created'][:10]} | {item['receiver'][:50]} | ${item['amount']}")

    split_rows = load_classified_rows(sources=("amazon_order", "costco_receipt"), status="NTC")
    split_index = build_split_index(split_rows)
    split_matched, split_no_match = match_splits_to_ntc(split_index, ntc_index)
    print(f"\nSplit match results ({len(split_index)} Amazon/Costco groups):")
    print(f"  Matched:   {len(split_matched)}")
    print(f"  No match:  {len(split_no_match)}")
    if split_no_match:
        for item in split_no_match:
            print(f"  {item['created'][:10]} | {item['receiver'][:50]} | ${item['amount']}")

    if historical:
        with open(ALL_TXN_CACHE) as f:
            all_items = json.load(f)
        hist_index = build_amazon_costco_index(all_items)
        hist_rows = load_classified_rows(sources=("amazon_order", "costco_receipt"), status="CLR")
        hist_split_index = build_split_index(hist_rows)
        hist_matched, hist_no_match = match_splits_to_ntc(hist_split_index, hist_index)
        print(f"\nHistorical CLR match results ({len(hist_split_index)} Amazon/Costco groups):")
        print(f"  Matched:   {len(hist_matched)}")
        print(f"  No match:  {len(hist_no_match)}")
        if hist_no_match:
            for item in hist_no_match:
                print(f"  {item['created'][:10]} | {item['receiver'][:50]} | ${item['amount']}")

    if historical_single:
        with open(ALL_TXN_CACHE) as f:
            all_items = json.load(f)
        clr_index = build_clr_txn_index(all_items)
        hist_rows = load_classified_rows(source="goodbudget", status="CLR", exclude_envelopes=SKIP_ENVELOPES)
        matched, no_match, _ = match_classified_to_ntc(hist_rows, clr_index)
        env_map = get_envelope_map(opener)
        already_correct = sum(
            1 for clf_row, txn_item in matched
            if txn_item["envelope_uuid"] == resolve_envelope_uuid(clf_row["envelope"], env_map)
        )
        print(f"\nHistorical single match ({len(hist_rows)} CLR rows):")
        print(f"  Matched:         {len(matched)}")
        print(f"  Already correct: {already_correct}")
        print(f"  Would update:    {len(matched) - already_correct}")
        print(f"  No match:        {len(no_match)}")


def resolve_envelope_uuid(env_name, env_map):
    """Return the Goodbudget UUID for a classified envelope name.

    Income:* envelopes don't exist as named envelopes in Goodbudget; they map to [Available].
    Returns None if the name cannot be resolved.
    """
    if env_name in env_map:
        return env_map[env_name]
    if env_name.startswith("Income:"):
        return env_map.get("[Available]")
    return None


def update_envelope(opener, ntc_item, clf_row, env_map, household_id,
                    dry_run=False, prefix=""):
    """GET nonce, then POST to assign a single envelope to an NTC transaction.

    Returns True on success, False if the envelope name cannot be resolved (skip).
    Raises RuntimeError on any unexpected API response.
    """
    env_uuid = resolve_envelope_uuid(clf_row["envelope"], env_map)
    if not env_uuid:
        print(f"  SKIP (unknown envelope {clf_row['envelope']!r}): {ntc_item['receiver']}")
        return False

    if dry_run:
        env_name = clf_row["envelope"]
        date = ntc_item["created"][:10]
        tag = f"[DRY RUN {prefix}]" if prefix else "[DRY RUN]"
        if env_name.startswith("Income:"):
            print(f"{tag} {ntc_item['receiver'][:45]:<45}  {date}  "
                  f"${ntc_item['amount']:>10}  →  [Available] ({env_name})")
        else:
            print(f"{tag} {ntc_item['receiver'][:45]:<45}  {date}  "
                  f"${ntc_item['amount']:>10}  →  {env_name}")
        return True

    # Fetch current transaction detail to get nonce and canonical field values
    url = f"{BASE_URL}/api/transactions/get/{ntc_item['uuid']}"
    txn_detail = json.loads(api_request(opener, url, label=f"get nonce {ntc_item['receiver'][:30]}"))
    nonce = txn_detail["nonce"]

    if clf_row["envelope"].startswith("Income:"):
        # Income transactions require type=INC with a children array (ADJ child carries the envelope).
        # Amount is a negative float (money flowing into the account) per the Goodbudget API.
        neg_amount = -abs(float(txn_detail["amount"]))
        child = {
            "amount":    neg_amount,
            "check_num": txn_detail.get("check_num", ""),
            "created":   txn_detail["created"],
            "envelope":  env_uuid,
            "nonce":     nonce,
            "receiver":  txn_detail["receiver"],
            "status":    "NTC",
            "type":      "ADJ",
            "uuid":      str(uuid.uuid4()),
        }
        payload = {
            "created":   txn_detail["created"],
            "uuid":      txn_detail["uuid"],
            "receiver":  txn_detail["receiver"],
            "status":    "CLR",
            "note":      txn_detail.get("note", ""),
            "envelope":  None,
            "account":   txn_detail["account"],
            "amount":    neg_amount,
            "nonce":     nonce,
            "type":      "INC",
            "check_num": txn_detail.get("check_num", ""),
            "children":  [child],
        }
    else:
        payload = {
            "created":   txn_detail["created"],
            "uuid":      txn_detail["uuid"],
            "receiver":  txn_detail["receiver"],
            "status":    "CLR",
            "note":      txn_detail.get("note", ""),
            "envelope":  env_uuid,
            "account":   txn_detail["account"],
            "amount":    txn_detail["amount"],
            "nonce":     nonce,
            "type":      txn_detail["type"],
            "check_num": txn_detail.get("check_num", ""),
        }
    d = base64.b64encode(json.dumps(payload).encode()).decode()
    body = urllib.parse.urlencode({
        "id": txn_detail["uuid"],
        "household_id": household_id,
        "n": nonce,
        "o": "transaction",
        "d": d,
    }).encode()

    save_url = f"{BASE_URL}/api/transactions/save?cltVersion=web"
    req = urllib.request.Request(save_url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
    req.add_header("Origin", BASE_URL)
    resp_data = json.loads(api_request(opener, req, label=f"save {txn_detail['receiver'][:30]}"))

    if resp_data.get("status") != 202:
        raise RuntimeError(
            f"Unexpected response for {txn_detail['uuid']} {txn_detail['receiver']!r}: {resp_data}"
        )

    time.sleep(0.5)
    return True


def cmd_show_splits(opener=None):
    with open(NTC_CACHE) as f:
        ntc_items = json.load(f)
    ntc_index = build_ntc_index(ntc_items)
    split_rows = load_classified_rows(sources=("amazon_order", "costco_receipt"), status="NTC")
    split_index = build_split_index(split_rows)
    matched, no_match = match_splits_to_ntc(split_index, ntc_index)

    print(f"{len(matched)} matched split groups:\n")
    for ntc_item, clf_rows in matched:
        date = ntc_item["created"][:10]
        total = ntc_item["amount"]
        print(f"  {ntc_item['receiver']:<30}  {date}  ${total}")
        for row in clf_rows:
            amt = abs(float(row["amount"]))
            print(f"      ${amt:>8.2f}  {row['envelope']:<35}  {row['name']}")

    if no_match:
        print(f"\n{len(no_match)} unmatched NTC Amazon/Costco transactions:")
        for item in no_match:
            print(f"  {item['created'][:10]}  {item['receiver']:<30}  ${item['amount']}")
    else:
        print("\nNo unmatched NTC Amazon/Costco transactions.")


def run_updates(items, update_fn, receiver_fn, label="", quiet=False):
    """Run update_fn(item) for each item. Returns (updated, skipped, aborted).

    update_fn(item) should return True on success, False to skip.
    receiver_fn(item) returns a display string for progress output.
    quiet: suppress per-item progress lines (e.g. for dry-run where update_fn prints its own output).
    Catches RuntimeError and aborts on first failure.
    """
    updated = 0
    skipped = 0
    try:
        for i, item in enumerate(items, 1):
            if not quiet:
                desc = receiver_fn(item)[:60]
                print(f"  [{label}] {i}/{len(items)}: {desc}" if label else
                      f"  Updating {i}/{len(items)}: {desc}")
            ok = update_fn(item)
            if ok:
                updated += 1
            else:
                skipped += 1
    except RuntimeError as e:
        print(f"ERROR: {e}")
        print("Aborting.")
        return updated, skipped, True
    return updated, skipped, False


def cmd_historical_single(opener, limit=None, dry_run=False):
    """Reclassify CLR single-envelope transactions where classified.csv disagrees."""
    with open(ALL_TXN_CACHE) as f:
        all_items = json.load(f)
    clr_index = build_clr_txn_index(all_items)
    hist_rows = load_classified_rows(source="goodbudget", status="CLR", exclude_envelopes=SKIP_ENVELOPES)
    env_map = get_envelope_map(opener)
    household_id = get_household_id(opener) if not dry_run else None

    matched, no_match, _ = match_classified_to_ntc(hist_rows, clr_index)

    already_correct = 0
    unknown_env = 0
    to_update = []
    for clf_row, txn_item in matched:
        desired_uuid = resolve_envelope_uuid(clf_row["envelope"], env_map)
        if not desired_uuid:
            unknown_env += 1
            continue
        if txn_item["envelope_uuid"] == desired_uuid:
            already_correct += 1
            continue
        to_update.append((clf_row, txn_item))

    if limit:
        to_update = to_update[:limit]

    print(f"Historical single-envelope: {len(matched)} matched, {len(no_match)} no-match.")
    print(f"  Already correct (skipped): {already_correct}")
    print(f"  Unknown envelope: {unknown_env}")
    print(f"  To update: {len(to_update)}")
    if limit and limit < (len(matched) - already_correct - unknown_env):
        print(f"  (applying --limit {limit})")

    updated, skipped, aborted = run_updates(
        to_update,
        lambda pair: update_envelope(opener, pair[1], pair[0], env_map, household_id,
                                     dry_run=dry_run, prefix="HIST-1"),
        lambda pair: pair[1]["receiver"],
        quiet=dry_run,
    )
    if aborted:
        return

    label = "to update" if dry_run else "updated"
    print(f"\n{updated} {label}. Already correct (skipped): {already_correct}. "
          f"Unknown envelope: {unknown_env + skipped}. No-match: {len(no_match)}.")


def cmd_sync(opener, limit=None, dry_run=False):
    """Assign envelopes to NTC single-envelope transactions."""
    with open(NTC_CACHE) as f:
        ntc_items = json.load(f)
    ntc_index = build_ntc_index(ntc_items)
    total_needs = sum(len(v) for v in ntc_index.values())

    classified_rows = load_classified_rows(source="goodbudget", exclude_envelopes=SKIP_ENVELOPES)
    env_map = get_envelope_map(opener)
    household_id = get_household_id(opener) if not dry_run else None

    matched, no_match, _ = match_classified_to_ntc(classified_rows, ntc_index)
    to_update = matched[:limit] if limit else matched

    print(f"Single-envelope: {len(matched)} matched, {len(no_match)} no-match "
          f"(of {total_needs} total NTC [Needs Envelope]).")
    if limit and limit < len(matched):
        print(f"  (applying --limit {limit}: updating first {limit} only)")

    updated, skipped, aborted = run_updates(
        to_update,
        lambda pair: update_envelope(opener, pair[1], pair[0], env_map, household_id,
                                     dry_run=dry_run),
        lambda pair: pair[1]["receiver"],
        quiet=dry_run,
    )
    if aborted:
        return

    print(f"\nSummary: {updated} {'to update' if dry_run else 'updated'}. "
          f"{skipped} skipped (unknown envelope). {len(no_match)} no-match.")


def cmd_sync_splits(opener, limit=None, historical=False, dry_run=False):
    """Assign split envelopes to NTC Amazon/Costco transactions."""
    with open(NTC_CACHE) as f:
        ntc_items = json.load(f)
    ntc_index = build_ntc_index(ntc_items)
    split_rows = load_classified_rows(sources=("amazon_order", "costco_receipt"), status="NTC")
    split_index = build_split_index(split_rows)
    split_matched, split_no_match = match_splits_to_ntc(split_index, ntc_index)
    env_map = get_envelope_map(opener)
    household_id = get_household_id(opener) if not dry_run else None

    to_update = split_matched[:limit] if limit else split_matched
    print(f"NTC splits: {len(split_matched)} matched, {len(split_no_match)} no-match.")
    if limit and limit < len(split_matched):
        print(f"  (applying --limit {limit})")

    updated, skipped, aborted = run_updates(
        to_update,
        lambda pair: update_split_envelope(opener, pair[0], pair[1], env_map, household_id,
                                           dry_run=dry_run),
        lambda pair: pair[0]["receiver"],
        quiet=dry_run,
    )
    if aborted:
        return

    print(f"\nSplits: {updated} {'to update' if dry_run else 'updated'}. "
          f"{skipped} skipped. {len(split_no_match)} no-match.")

    if historical:
        with open(ALL_TXN_CACHE) as f:
            all_items = json.load(f)
        hist_index = build_amazon_costco_index(all_items)
        hist_rows = load_classified_rows(sources=("amazon_order", "costco_receipt"), status="CLR")
        hist_split_index = build_split_index(hist_rows)
        hist_matched, hist_no_match = match_splits_to_ntc(hist_split_index, hist_index)
        hist_to_update = hist_matched[:limit] if limit else hist_matched
        print(f"\nHistorical CLR splits: {len(hist_matched)} matched, {len(hist_no_match)} no-match.")
        if limit and limit < len(hist_matched):
            print(f"  (applying --limit {limit})")
        hist_updated, hist_skipped, aborted = run_updates(
            hist_to_update,
            lambda pair: update_split_envelope(opener, pair[0], pair[1], env_map, household_id,
                                               dry_run=dry_run, prefix="HIST-SPLIT"),
            lambda pair: pair[0]["receiver"],
            label="HIST",
            quiet=dry_run,
        )
        if aborted:
            return
        print(f"Historical splits: {hist_updated} {'to update' if dry_run else 'updated'}. "
              f"{hist_skipped} skipped. {len(hist_no_match)} no-match.")


COMMANDS = {
    "auth":        cmd_auth,
    "envelopes":   cmd_envelopes,
    "accounts":    cmd_accounts,
    "ntc":         cmd_ntc,
    "fetch-all":   cmd_fetch_all,
    "classified":  cmd_classified,
    "match":       cmd_match,
    "show-splits": cmd_show_splits,
}


def main():
    parser = argparse.ArgumentParser(description="Goodbudget sync tool")
    parser.add_argument("--step", choices=list(COMMANDS))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--historical", action="store_true",
                        help="Also match/update CLR (already-cleared) Amazon/Costco transactions")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit number of live single-envelope updates (for testing)")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--splits-only", action="store_true",
                            help="Write only split (Amazon/Costco) transactions (Step 10)")
    mode_group.add_argument("--historical-single", action="store_true",
                            help="Reclassify CLR non-split transactions (Step 9.5)")
    args = parser.parse_args()

    # Steps that don't need a live session
    OFFLINE_STEPS = {"classified", "show-splits"}
    offline_match = (args.step == "match" and not args.historical_single)

    if args.step in OFFLINE_STEPS:
        COMMANDS[args.step]()
        return
    if offline_match:
        cmd_match(historical=args.historical)
        return

    with session() as opener:
        if args.step == "match":
            cmd_match(opener, historical=args.historical,
                      historical_single=args.historical_single)
        elif args.step:
            COMMANDS[args.step](opener)
        elif args.historical_single:
            cmd_historical_single(opener, limit=args.limit, dry_run=args.dry_run)
        elif args.splits_only:
            cmd_sync_splits(opener, limit=args.limit, historical=args.historical,
                            dry_run=args.dry_run)
        else:
            cmd_sync(opener, limit=args.limit, dry_run=args.dry_run)
            cmd_sync_splits(opener, limit=args.limit, historical=args.historical,
                            dry_run=args.dry_run)


if __name__ == "__main__":
    main()
