#!/usr/bin/env python3
"""
Phase 2a: Profile merchant→envelope mappings from unified.csv.

Produces:
  - Deterministic mapping table (merchant → single envelope)
  - Ambiguous mapping report (merchants → multiple envelopes)
  - Coverage analysis: how many uncategorized rows a simple lookup would fix
  - Uncategorized merchants not in the lookup table
  - Monthly spend by envelope
  - merchant_mappings.csv for reuse in Phase 2b
"""

import csv
import os
import re
from collections import defaultdict
from datetime import datetime



def load_unified(path: str) -> list:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def is_categorized(envelope: str) -> bool:
    return envelope != "" and envelope != "[Needs Envelope]"


def is_costco_line_item(row: dict) -> bool:
    """Costco receipt line items aren't standalone merchants."""
    return row.get("source") == "costco_receipt"


def is_transaction_description(name: str) -> bool:
    """Detect merchant names that are really one-off transaction descriptions
    (contain dates, ref numbers, account numbers) and won't generalize."""
    # REF #, date patterns, long account numbers
    if re.search(r"REF\s*#", name, re.IGNORECASE):
        return True
    if re.search(r"\d{2}/\d{2}", name):  # embedded dates
        return True
    if re.search(r"XXXXXX\d+", name):  # masked account numbers
        return True
    if re.search(r"\bON\s+\d{2}/\d{2}\b", name):  # "ON 02/13"
        return True
    if re.search(r"SEQUENCE:\s*\d+", name):
        return True
    # Long wire transfer descriptions with routing info
    if re.search(r"WT FED#\S+.{40,}", name):
        return True
    if re.search(r"ONLINE TRANSFER REF", name):
        return True
    return False



def profile(rows: list, output_dir: str):
    # Filter out costco line items for merchant profiling
    merchant_rows = [r for r in rows if not is_costco_line_item(r)]

    # Group categorized rows by merchant → {envelope → [rows]}
    merchant_envelopes = defaultdict(lambda: defaultdict(list))
    uncategorized = []

    for r in merchant_rows:
        env = r["envelope"]
        name = r["name"]
        if is_categorized(env):
            merchant_envelopes[name][env].append(r)
        else:
            uncategorized.append(r)

    # Split into deterministic vs ambiguous
    deterministic = {}  # merchant → (envelope, count, total_spend)
    ambiguous = {}      # merchant → {envelope → (count, total_spend)}

    for merchant, env_map in sorted(merchant_envelopes.items()):
        if len(env_map) == 1:
            env = next(iter(env_map))
            rows_for = env_map[env]
            deterministic[merchant] = (
                env,
                len(rows_for),
                sum(float(r["amount"]) for r in rows_for),
            )
        else:
            ambiguous[merchant] = {}
            for env, rows_for in sorted(env_map.items()):
                ambiguous[merchant][env] = (
                    len(rows_for),
                    sum(float(r["amount"]) for r in rows_for),
                )

    # --- Report ---

    total_categorized = sum(
        sum(len(rows_for) for rows_for in env_map.values())
        for env_map in merchant_envelopes.values()
    )

    print("=" * 70)
    print("PHASE 2a: MERCHANT → ENVELOPE PROFILE")
    print("=" * 70)
    print(f"\nTotal rows: {len(rows)} ({len(merchant_rows)} excluding Costco line items)")
    print(f"Categorized: {total_categorized}")
    print(f"Uncategorized: {len(uncategorized)}")
    print(f"Deterministic merchants: {len(deterministic)}")
    print(f"Ambiguous merchants: {len(ambiguous)}")

    # 1. Deterministic mappings (sorted by frequency)
    print(f"\n{'=' * 70}")
    print("DETERMINISTIC MAPPINGS (merchant always → same envelope)")
    print(f"{'=' * 70}")
    print(f"{'Merchant':<45} {'Envelope':<35} {'N':>4} {'Total':>10}")
    print(f"{'-' * 45} {'-' * 35} {'-' * 4} {'-' * 10}")
    for merchant, (env, count, spend) in sorted(
        deterministic.items(), key=lambda x: x[1][1], reverse=True
    ):
        print(f"{merchant[:45]:<45} {env:<35} {count:>4} {spend:>10.2f}")

    # 2. Ambiguous mappings
    print(f"\n{'=' * 70}")
    print("AMBIGUOUS MAPPINGS (merchant → multiple envelopes)")
    print(f"{'=' * 70}")
    for merchant, env_map in sorted(ambiguous.items()):
        total_n = sum(c for c, _ in env_map.values())
        print(f"\n  {merchant} ({total_n} transactions):")
        for env, (count, spend) in sorted(
            env_map.items(), key=lambda x: x[1][0], reverse=True
        ):
            pct = 100 * count / total_n
            print(f"    {env:<40} {count:>4} ({pct:5.1f}%)  {spend:>10.2f}")

    # 3. Coverage analysis
    print(f"\n{'=' * 70}")
    print("COVERAGE: UNCATEGORIZED ROWS MATCHABLE BY DETERMINISTIC LOOKUP")
    print(f"{'=' * 70}")
    matchable = [r for r in uncategorized if r["name"] in deterministic]
    ambiguous_match = [r for r in uncategorized if r["name"] in ambiguous]
    no_match = [
        r for r in uncategorized
        if r["name"] not in deterministic and r["name"] not in ambiguous
    ]
    print(f"Uncategorized rows:              {len(uncategorized)}")
    print(f"  Exact match (deterministic):   {len(matchable)} "
          f"({100 * len(matchable) / len(uncategorized):.1f}%)")
    print(f"  Exact match (ambiguous):       {len(ambiguous_match)} "
          f"({100 * len(ambiguous_match) / len(uncategorized):.1f}%)")
    print(f"  No match:                      {len(no_match)} "
          f"({100 * len(no_match) / len(uncategorized):.1f}%)")

    if matchable:
        print(f"\n  Top deterministic matches:")
        match_counts = defaultdict(int)
        for r in matchable:
            match_counts[r["name"]] += 1
        for merchant, count in sorted(
            match_counts.items(), key=lambda x: x[1], reverse=True
        )[:20]:
            env = deterministic[merchant][0]
            print(f"    {merchant:<45} → {env:<30} ({count}x)")

    # 4. Uncategorized merchants with no lookup match
    print(f"\n{'=' * 70}")
    print("UNCATEGORIZED MERCHANTS NOT IN LOOKUP TABLE")
    print(f"{'=' * 70}")
    no_match_counts = defaultdict(list)
    for r in no_match:
        no_match_counts[r["name"]].append(r)
    print(f"{'Merchant':<55} {'N':>4} {'Total':>10}")
    print(f"{'-' * 55} {'-' * 4} {'-' * 10}")
    for merchant, mrs in sorted(
        no_match_counts.items(), key=lambda x: len(x[1]), reverse=True
    ):
        spend = sum(float(r["amount"]) for r in mrs)
        print(f"{merchant[:55]:<55} {len(mrs):>4} {spend:>10.2f}")

    # 5. Monthly spend by envelope
    print(f"\n{'=' * 70}")
    print("MONTHLY SPEND BY ENVELOPE (categorized rows only)")
    print(f"{'=' * 70}")
    monthly = defaultdict(lambda: defaultdict(float))
    months_seen = set()
    for r in rows:
        if not is_categorized(r["envelope"]):
            continue
        try:
            dt = datetime.strptime(r["date"].strip(), "%m/%d/%Y")
        except ValueError:
            continue
        month_key = dt.strftime("%Y-%m")
        months_seen.add(month_key)
        monthly[r["envelope"]][month_key] += float(r["amount"])

    months_sorted = sorted(months_seen)
    # Print header
    print(f"\n{'Envelope':<35}", end="")
    for m in months_sorted:
        print(f" {m:>9}", end="")
    print(f" {'TOTAL':>10}")
    print(f"{'-' * 35}", end="")
    for _ in months_sorted:
        print(f" {'-' * 9}", end="")
    print(f" {'-' * 10}")

    envelope_totals = {}
    for env in sorted(monthly.keys()):
        total = sum(monthly[env].values())
        envelope_totals[env] = total
        print(f"{env[:35]:<35}", end="")
        for m in months_sorted:
            val = monthly[env].get(m, 0)
            print(f" {val:>9.2f}", end="")
        print(f" {total:>10.2f}")

    # Grand total row
    print(f"{'-' * 35}", end="")
    for _ in months_sorted:
        print(f" {'-' * 9}", end="")
    print(f" {'-' * 10}")
    print(f"{'TOTAL':<35}", end="")
    for m in months_sorted:
        month_total = sum(monthly[env].get(m, 0) for env in monthly)
        print(f" {month_total:>9.2f}", end="")
    print(f" {sum(envelope_totals.values()):>10.2f}")

    # Write deterministic mappings CSV (filtered)
    mappings_path = os.path.join(output_dir, "merchant_mappings.csv")
    written = 0
    skipped_txn_desc = 0
    skipped_low_conf = 0
    with open(mappings_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "merchant", "envelope", "transaction_count", "total_spend",
            "confidence",
        ])
        writer.writeheader()

        for merchant, (env, count, spend) in sorted(
            deterministic.items(), key=lambda x: x[1][1], reverse=True
        ):
            # Skip transaction-description merchants
            if is_transaction_description(merchant):
                skipped_txn_desc += 1
                continue

            confidence = "high" if count >= 2 else "low"
            if count < 2:
                skipped_low_conf += 1

            writer.writerow({
                "merchant": merchant,
                "envelope": env,
                "transaction_count": count,
                "total_spend": round(spend, 2),
                "confidence": confidence,
            })
            written += 1

    print(f"\nWrote {written} mappings to {mappings_path}")
    print(f"  Skipped {skipped_txn_desc} transaction-description merchants")
    print(f"  Flagged {skipped_low_conf} low-confidence (count=1) mappings")


def main():
    directory = os.path.dirname(os.path.abspath(__file__))
    unified_path = os.path.join(directory, "unified.csv")
    if not os.path.exists(unified_path):
        print("Run unify.py first to generate unified.csv")
        return
    rows = load_unified(unified_path)
    profile(rows, directory)


if __name__ == "__main__":
    main()
