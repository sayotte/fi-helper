#!/usr/bin/env python3
"""
Unify Goodbudget history with Costco and Amazon item-level detail into a
single analysis-ready transactions table.

For Costco/Amazon transactions that match receipts/orders, the single
Goodbudget row is replaced by one row per item. Non-matched transactions
(and those without matching data) pass through as-is.

Goodbudget split transactions (Details column) are also exploded into
separate rows.

Output: unified.csv
"""

import csv
import os
import re
import sys
from datetime import datetime, timedelta


def parse_amount(s: str) -> float:
    """Parse Goodbudget amount string like '-2,673.75' or '17,000.00'."""
    return float(s.replace(",", "").replace('"', ""))


def parse_date(s: str) -> datetime:
    """Parse MM/DD/YYYY date string."""
    return datetime.strptime(s.strip(), "%m/%d/%Y")


def load_goodbudget(path: str) -> list:
    """Load and parse history.csv into a list of dicts."""
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                amt = parse_amount(r["Amount"])
            except (ValueError, KeyError):
                continue
            rows.append({
                "date": r["Date"].strip(),
                "envelope": r["Envelope"].strip(),
                "account": r["Account"].strip(),
                "name": r["Name"].strip(),
                "notes": r.get("Notes", "").strip(),
                "amount": amt,
                "status": r.get("Status", "").strip(),
                "details": r.get("Details", "").strip(),
            })
    return rows


def load_costco_items(path: str) -> dict:
    """Load costco_items.csv and group by (receipt_date, receipt_total)."""
    by_receipt = {}
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            key = (r["receipt_date"], float(r["receipt_total"]))
            if key not in by_receipt:
                by_receipt[key] = []
            by_receipt[key].append(r)
    return by_receipt


def parse_goodbudget_details(details: str) -> list:
    """
    Parse Goodbudget Details column into splits.
    Format: "Envelope1|Amount1||Envelope2|Amount2||..."
    Also handles: "Principal|-598.47||Interest|-700.98||Fees|-779.46"
    Returns list of (envelope, amount) tuples.
    """
    if not details or details.startswith("[Available]"):
        return []

    splits = []
    parts = details.split("||")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Split on last | to separate envelope from amount
        pipe_idx = part.rfind("|")
        if pipe_idx == -1:
            continue
        envelope = part[:pipe_idx].strip()
        try:
            amount = parse_amount(part[pipe_idx + 1:])
            splits.append((envelope, amount))
        except ValueError:
            continue

    return splits


def match_costco_receipt(gb_row: dict, costco_by_receipt: dict) -> list | None:
    """
    Try to match a Goodbudget Costco transaction to a Costco receipt.
    Returns the list of item rows if matched, None otherwise.
    """
    gb_date = parse_date(gb_row["date"])
    gb_amount = gb_row["amount"]

    # Match by absolute value (Goodbudget stores expenses as negative,
    # Costco receipts as positive; refunds are the reverse)
    gb_abs = abs(gb_amount)

    # Try exact date match first, then +/- 1-2 days
    for day_offset in [0, -1, 1, -2, 2]:
        check_date = gb_date + timedelta(days=day_offset)
        check_date_str = check_date.strftime("%m/%d/%Y")
        # Try both positive and negative receipt totals
        for sign in [1, -1]:
            key = (check_date_str, round(sign * gb_abs, 2))
            if key in costco_by_receipt:
                return costco_by_receipt[key]

    return None


def is_costco_transaction(row: dict) -> bool:
    """Check if a Goodbudget row is a Costco purchase (not food court)."""
    name = row["name"].lower()
    if "costco" not in name:
        return False
    # Small Costco charges are food court, not warehouse purchases
    if abs(row["amount"]) < 20:
        return False
    return True


def load_amazon_items(path: str) -> list:
    """Load amazon_items.csv and build charge candidates for matching.

    Returns a list of charge dicts, each with:
      date, charge_amount, charge_level, order_id, items[]

    Charge candidates are deduplicated: multiple item rows with the same
    (order_id, charge_amount, charge_level) are grouped into one charge.
    """
    charges_map = {}  # (order_id, charge_amount, charge_level) -> charge dict
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            key = (r["order_id"], r["charge_amount"], r["charge_level"])
            if key not in charges_map:
                charges_map[key] = {
                    "date": r["order_date"],
                    "charge_amount": float(r["charge_amount"]),
                    "charge_level": r["charge_level"],
                    "order_id": r["order_id"],
                    "items": [],
                }
            charges_map[key]["items"].append({
                "product_name": r["product_name"],
                "unit_price": float(r["unit_price"]),
                "unit_tax": float(r["unit_tax"]),
                "item_total": float(r["item_total"]),
                "is_digital": r["is_digital"] == "True",
            })
    return list(charges_map.values())


def is_amazon_transaction(row: dict) -> bool:
    """Check if a Goodbudget row is an Amazon purchase."""
    name = row["name"].lower()
    return "amazon" in name


def match_amazon_charge(gb_row: dict, amazon_charges: list,
                        used: set) -> dict | None:
    """
    Match a Goodbudget Amazon transaction to an Amazon charge candidate.

    Tries multiple charge levels with priority:
      shipment > digital > order_sum_shipments > order_sum_items

    Returns the matched charge dict, or None.
    """
    LEVEL_PRIORITY = {
        "shipment": 0,
        "digital": 1,
        "order_sum_shipments": 2,
        "order_sum_items": 3,
    }

    gb_date = parse_date(gb_row["date"])
    gb_amt = abs(gb_row["amount"])

    best = None
    best_score = (999, 999)  # (level_priority, day_diff)

    for i, charge in enumerate(amazon_charges):
        if i in used:
            continue
        charge_date = datetime.strptime(charge["date"], "%Y-%m-%d")
        day_diff = abs((gb_date - charge_date).days)
        if day_diff > 5:
            continue
        if abs(charge["charge_amount"] - gb_amt) > 0.15:
            continue
        level_pri = LEVEL_PRIORITY.get(charge["charge_level"], 99)
        score = (level_pri, day_diff)
        if score < best_score:
            best = i
            best_score = score

    if best is not None:
        used.add(best)
        return amazon_charges[best]
    return None


def load_split_collapse_rules(config_path: str) -> dict:
    """Load split-collapse rules from analysis_config.csv.
    Returns dict mapping source_envelope → target_envelope."""
    rules = {}
    if not os.path.exists(config_path):
        return rules
    with open(config_path, newline="") as f:
        for r in csv.DictReader(f):
            if r["key"] == "split_collapse":
                source_env, target_env = r["value"].split("|", 1)
                rules[source_env] = target_env
    return rules


def load_categorization_rules(path: str, source_filter: str) -> list:
    """Load classification_rules.csv filtered to a specific source.
    Returns list of {pattern, match_type, envelope} dicts."""
    rules = []
    if not os.path.exists(path):
        return rules
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("source", "").strip() == source_filter:
                rules.append({
                    "pattern": r["pattern"],
                    "match_type": r.get("match_type", "contains").strip().lower(),
                    "envelope": r["envelope"].strip(),
                })
    return rules


def match_categorization_rule(name: str, rules: list) -> str | None:
    """Match a name against categorization rules. Returns envelope or None."""
    for rule in rules:
        pat = rule["pattern"]
        mt = rule["match_type"]
        if mt == "exact":
            if name == pat:
                return rule["envelope"]
        elif mt == "contains":
            if pat.lower() in name.lower():
                return rule["envelope"]
        elif mt == "regex":
            if re.search(pat, name, re.IGNORECASE):
                return rule["envelope"]
    return None


def categorize_amazon_item(item: dict, rules: list) -> str:
    """
    Suggest a Goodbudget envelope for an Amazon item.
    Only auto-categorizes digital content; physical items → [Needs Envelope].
    """
    if not item["is_digital"]:
        return "[Needs Envelope]"

    name = item["product_name"]
    matched = match_categorization_rule(name, rules)
    if matched:
        return matched

    # Default for unmatched digital content
    return "Discretionary:Shopping"


def categorize_costco_item(item: dict, rules: list) -> str:
    """
    Suggest a Goodbudget envelope for a Costco item based on its attributes.
    Uses is_food flag, then rules, then taxable default.
    """
    is_food = item["is_food"] == "True"
    is_taxable = item["is_taxable"] == "True"
    is_discount = float(item.get("discount_amount", 0)) != 0

    if is_discount:
        return ""  # discounts inherit parent category

    # Food items (tax flag '3' = food in GA)
    if is_food:
        return "Food:Groceries"

    # Check rules against description
    desc = item["description"]
    matched = match_categorization_rule(desc, rules)
    if matched:
        return matched

    # Default for taxable non-food
    if is_taxable:
        return "Household:Maintenance"

    return ""


def make_unified_row(date, envelope, account, name, amount, status, source,
                     notes="", parent_name="", parent_amount="",
                     is_food="", is_taxable="", costco_item_id=""):
    """Create a unified row dict with consistent field ordering and defaults."""
    return {
        "date": date,
        "envelope": envelope,
        "account": account,
        "name": name,
        "notes": notes,
        "amount": amount,
        "status": status,
        "source": source,
        "parent_name": parent_name,
        "parent_amount": parent_amount,
        "is_food": is_food,
        "is_taxable": is_taxable,
        "costco_item_id": costco_item_id,
    }


def unify(goodbudget_path: str, costco_items_path: str,
         amazon_items_path: str, output_path: str, rules_path: str,
         config_path: str = "analysis_config.csv"):
    """Build unified transactions table."""
    gb_rows = load_goodbudget(goodbudget_path)
    collapse_rules = load_split_collapse_rules(config_path)
    costco_by_receipt = load_costco_items(costco_items_path)

    # Load source-specific categorization rules
    costco_rules = load_categorization_rules(rules_path, "costco")
    amazon_rules = load_categorization_rules(rules_path, "amazon")

    # Load Amazon charges if available
    amazon_charges = []
    if os.path.exists(amazon_items_path):
        amazon_charges = load_amazon_items(amazon_items_path)
    amazon_used = set()

    unified = []
    costco_matched = 0
    costco_unmatched = 0
    amazon_matched = 0
    amazon_unmatched = 0
    amazon_items_total = 0
    splits_expanded = 0

    for row in gb_rows:
        # Skip internal transfers, balance adjustments, fills
        if row["name"] in ("Reconciliation Transaction", "Reconciliation Adjustment"):
            continue
        if row["name"].endswith("fill"):
            continue

        # Check for Costco match
        if is_costco_transaction(row):
            items = match_costco_receipt(row, costco_by_receipt)
            if items:
                costco_matched += 1
                for item in items:
                    discount_amt = float(item.get("discount_amount", 0))
                    is_discount = discount_amt != 0
                    envelope = categorize_costco_item(item, costco_rules)

                    # Flip sign to match Goodbudget convention:
                    # purchases are negative, refunds are positive
                    price = float(item["price"])
                    gb_sign = -1 if row["amount"] < 0 else 1
                    receipt_sign = 1 if price >= 0 else -1
                    amount = gb_sign * abs(price)

                    unified.append(make_unified_row(
                        date=row["date"], envelope=envelope,
                        account=row["account"], name=item["description"],
                        notes=f"Costco #{item['item_id']}", amount=amount,
                        status=row["status"], source="costco_receipt",
                        parent_name=row["name"], parent_amount=row["amount"],
                        is_food=item["is_food"] == "True",
                        is_taxable=item["is_taxable"] == "True",
                        costco_item_id=item["item_id"],
                    ))
                continue
            else:
                costco_unmatched += 1
                # Fall through to normal processing

        # Check for Amazon match
        if is_amazon_transaction(row) and amazon_charges:
            charge = match_amazon_charge(row, amazon_charges, amazon_used)
            if charge:
                amazon_matched += 1
                items = charge["items"]
                # Calculate total of all items for proportional allocation
                items_sum = sum(i["item_total"] for i in items)

                for item in items:
                    # Proportionally allocate the CC charge to each item
                    if items_sum != 0:
                        share = item["item_total"] / items_sum
                    else:
                        share = 1.0 / len(items)
                    allocated_amount = round(
                        -abs(charge["charge_amount"]) * share, 2
                    )
                    # Flip sign for refunds (positive GB amount)
                    if row["amount"] > 0:
                        allocated_amount = abs(allocated_amount)

                    envelope = categorize_amazon_item(item, amazon_rules)
                    amazon_items_total += 1

                    unified.append(make_unified_row(
                        date=row["date"], envelope=envelope,
                        account=row["account"], name=item["product_name"],
                        notes=f"Amazon #{charge['order_id']}",
                        amount=allocated_amount, status=row["status"],
                        source="amazon_order", parent_name=row["name"],
                        parent_amount=row["amount"],
                    ))
                continue
            else:
                amazon_unmatched += 1
                # Fall through to normal processing

        # Check for Goodbudget split transactions
        splits = parse_goodbudget_details(row["details"])
        if splits:
            # Check if this split should collapse into a single row
            collapse_envelope = collapse_rules.get(row["envelope"])
            if collapse_envelope:
                unified.append(make_unified_row(
                    date=row["date"], envelope=collapse_envelope,
                    account=row["account"], name=row["name"],
                    notes=row["notes"], amount=row["amount"],
                    status=row["status"], source="goodbudget",
                ))
            else:
                # All other splits: explode as before
                splits_expanded += 1
                for envelope, amount in splits:
                    unified.append(make_unified_row(
                        date=row["date"], envelope=envelope,
                        account=row["account"], name=row["name"],
                        notes=row["notes"], amount=amount,
                        status=row["status"], source="goodbudget_split",
                        parent_name=row["name"], parent_amount=row["amount"],
                    ))
            continue

        # Normal transaction — pass through
        unified.append(make_unified_row(
            date=row["date"], envelope=row["envelope"],
            account=row["account"], name=row["name"],
            notes=row["notes"], amount=row["amount"],
            status=row["status"], source="goodbudget",
        ))

    # Write output
    fieldnames = [
        "date", "envelope", "account", "name", "notes", "amount",
        "status", "source", "parent_name", "parent_amount",
        "is_food", "is_taxable", "costco_item_id",
    ]
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in unified:
            writer.writerow(row)

    print(f"Goodbudget rows loaded: {len(gb_rows)}")
    print(f"Costco transactions matched to receipts: {costco_matched}")
    print(f"Costco transactions without receipt match: {costco_unmatched}")
    print(f"Amazon transactions matched to orders: {amazon_matched}"
          f" ({amazon_items_total} item rows)")
    print(f"Amazon transactions without order match: {amazon_unmatched}")
    print(f"Goodbudget split transactions expanded: {splits_expanded}")
    print(f"Unified rows written: {len(unified)}")
    print(f"Output: {output_path}")


def main():
    directory = os.path.dirname(os.path.abspath(__file__))
    goodbudget_path = os.path.join(directory, "history.csv")
    costco_items_path = os.path.join(directory, "costco_items.csv")
    amazon_items_path = os.path.join(directory, "amazon_items.csv")
    rules_path = os.path.join(directory, "classification_rules.csv")
    output_path = os.path.join(directory, "unified.csv")

    if not os.path.exists(costco_items_path):
        print("Run parse_costco.py first to generate costco_items.csv")
        sys.exit(1)

    unify(goodbudget_path, costco_items_path, amazon_items_path, output_path, rules_path)


if __name__ == "__main__":
    main()
