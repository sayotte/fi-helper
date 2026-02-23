#!/usr/bin/env python3
"""sync_goodbudget.py — Push envelope assignments from classified.csv to Goodbudget."""

import argparse
import base64
import csv
import datetime
import getpass
import http.cookiejar
import json
import math
import os
import re
import time
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
    with opener.open(req) as resp:
        resp.read()


def get_household_id(opener):
    url = f"{BASE_URL}/home"
    with opener.open(url) as resp:
        html = resp.read().decode("utf-8", errors="replace")
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
    with opener.open(url) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    result = {}
    _walk_nodes(data[0]["nodes"], result, "FullName")
    return result


def get_account_map(opener):
    url = f"{BASE_URL}/api/accounts"
    with opener.open(url) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    result = {}
    _walk_nodes(data[0]["nodes"], result, "Name",
                filter_fn=lambda n: n.get("AccountType") != "DEBT",
                prune_fn=lambda n: n.get("totals") or n.get("total") or n.get("subtotal"))
    return result


def logout(opener):
    url = f"{BASE_URL}/logout"
    with opener.open(url) as resp:
        resp.read()


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
        with opener.open(url) as resp:
            data = json.loads(resp.read().decode("utf-8"))
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
        with opener.open(url) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if total is None:
            total = data["count"]
            num_pages = math.ceil(total / 120)
        all_items.extend(data["items"])
        if len(all_items) >= total or page >= num_pages:
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


def load_classified():
    rows = []
    with open(CLASSIFIED_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["source"] == "goodbudget" and row["envelope"] not in SKIP_ENVELOPES:
                rows.append(row)
    return rows


def load_split_rows():
    rows = []
    with open(CLASSIFIED_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["source"] in ("amazon_order", "costco_receipt") and row["status"] == "NTC":
                rows.append(row)
    return rows


def load_historical_split_rows():
    rows = []
    with open(CLASSIFIED_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["source"] in ("amazon_order", "costco_receipt") and row["status"] == "CLR":
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
            return
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

    # Live path deferred to Step 10
    raise NotImplementedError("Live split updates implemented in Step 10")


def cmd_classified(opener):
    rows = load_classified()
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


def cmd_match(opener, historical=False):
    with open(NTC_CACHE) as f:
        ntc_items = json.load(f)
    ntc_index = build_ntc_index(ntc_items)
    total_needs = sum(len(v) for v in ntc_index.values())
    classified_rows = load_classified()
    matched, no_match, ambiguous = match_classified_to_ntc(classified_rows, ntc_index)
    print(f"Match results (against {total_needs} [Needs Envelope] NTC transactions):")
    print(f"  Unique match:  {len(matched)}")
    print(f"  No match:      {len(no_match)}")
    print(f"  Ambiguous:     {len(ambiguous)}")
    if no_match:
        print(f"\nNo match ({len(no_match)} items):")
        for item in no_match:
            print(f"  {item['created'][:10]} | {item['receiver'][:50]} | ${item['amount']}")

    split_rows = load_split_rows()
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
        hist_rows = load_historical_split_rows()
        hist_split_index = build_split_index(hist_rows)
        hist_matched, hist_no_match = match_splits_to_ntc(hist_split_index, hist_index)
        print(f"\nHistorical CLR match results ({len(hist_split_index)} Amazon/Costco groups):")
        print(f"  Matched:   {len(hist_matched)}")
        print(f"  No match:  {len(hist_no_match)}")
        if hist_no_match:
            for item in hist_no_match:
                print(f"  {item['created'][:10]} | {item['receiver'][:50]} | ${item['amount']}")


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


def cmd_dry_run(opener, historical=False):
    env_map = get_envelope_map(opener)
    with open(NTC_CACHE) as f:
        ntc_items = json.load(f)
    ntc_index = build_ntc_index(ntc_items)
    classified_rows = load_classified()
    matched, no_match, ambiguous = match_classified_to_ntc(classified_rows, ntc_index)
    unknown_env = []
    for clf_row, ntc_item in matched:
        env_name = clf_row["envelope"]
        env_uuid = resolve_envelope_uuid(env_name, env_map)
        if not env_uuid:
            unknown_env.append(env_name)
            continue
        date = ntc_item["created"][:10]
        display_name = "[Available]" if env_name.startswith("Income:") else env_name
        print(f"[DRY RUN] {ntc_item['receiver'][:45]:<45}  {date}  ${ntc_item['amount']:>10}  →  {display_name} ({env_name})" if env_name.startswith("Income:") else
              f"[DRY RUN] {ntc_item['receiver'][:45]:<45}  {date}  ${ntc_item['amount']:>10}  →  {env_name}")
    print(f"\nSummary: {len(matched)} to update, {len(no_match)} skipped (no match), "
          f"{len(ambiguous)} ambiguous, {len(unknown_env)} unknown envelopes.")
    if unknown_env:
        for e in unknown_env:
            print(f"  UNKNOWN ENVELOPE: {e!r}")

    split_rows = load_split_rows()
    split_index = build_split_index(split_rows)
    split_matched, _ = match_splits_to_ntc(split_index, ntc_index)
    split_unknown = 0
    for ntc_item, clf_rows in split_matched:
        bad = [r for r in clf_rows if not resolve_envelope_uuid(r["envelope"], env_map)]
        if bad:
            split_unknown += len(bad)
            continue
        update_split_envelope(None, ntc_item, clf_rows, env_map, None, dry_run=True)
    print(f"Split summary: {len(split_matched)} groups to update, {split_unknown} unknown envelopes.")

    if historical:
        with open(ALL_TXN_CACHE) as f:
            all_items = json.load(f)
        hist_index = build_amazon_costco_index(all_items)
        hist_rows = load_historical_split_rows()
        hist_split_index = build_split_index(hist_rows)
        hist_matched, _ = match_splits_to_ntc(hist_split_index, hist_index)
        hist_unknown = 0
        for txn_item, clf_rows in hist_matched:
            bad = [r for r in clf_rows if not resolve_envelope_uuid(r["envelope"], env_map)]
            if bad:
                hist_unknown += len(bad)
                continue
            update_split_envelope(None, txn_item, clf_rows, env_map, None,
                                  dry_run=True, prefix="HIST-SPLIT")
        print(f"Historical split summary: {len(hist_matched)} groups to update, "
              f"{hist_unknown} unknown envelopes.")


def cmd_show_splits(opener):
    with open(NTC_CACHE) as f:
        ntc_items = json.load(f)
    ntc_index = build_ntc_index(ntc_items)
    split_rows = load_split_rows()
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
    args = parser.parse_args()
    with session() as opener:
        if args.dry_run:
            cmd_dry_run(opener, historical=args.historical)
        elif args.step == "match":
            cmd_match(opener, historical=args.historical)
        elif args.step:
            COMMANDS[args.step](opener)
        else:
            parser.print_help()


if __name__ == "__main__":
    main()
