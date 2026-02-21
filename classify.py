#!/usr/bin/env python3
"""
Phase 2b: Classify uncategorized transactions.

Reads unified.csv + merchant_mappings.csv + classification_rules.csv.
Produces classified.csv (same schema, envelopes filled where possible)
and a console report.

Classification tiers (applied in order):
  1. Already categorized — pass through
  2. User rules (classification_rules.csv) — exact/contains/regex
  3. Exact merchant lookup (merchant_mappings.csv)
  4. Fuzzy merchant lookup — token-based Jaccard similarity
  5. Unclassified remainder — left as [Needs Envelope]
"""

import csv
import os
import re
from collections import defaultdict


UNIFIED_FIELDS = [
    "date", "envelope", "account", "name", "notes", "amount", "status",
    "source", "parent_name", "parent_amount", "is_food", "is_taxable",
    "costco_item_id",
]


def load_csv(path: str) -> list:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def load_rules(path: str) -> list:
    """Load classification_rules.csv. Each rule has pattern, match_type,
    envelope, notes."""
    if not os.path.exists(path):
        return []
    rules = load_csv(path)
    # Compile regex patterns once
    for rule in rules:
        mt = rule.get("match_type", "exact").strip().lower()
        pat = rule["pattern"]
        if mt == "regex":
            rule["_compiled"] = re.compile(pat, re.IGNORECASE)
        rule["match_type"] = mt
    return rules


def load_mappings(path: str) -> dict:
    """Load merchant_mappings.csv into {merchant: envelope} dict.
    Only use high-confidence and manual mappings for exact lookup."""
    if not os.path.exists(path):
        return {}
    mappings = {}
    for row in load_csv(path):
        conf = row.get("confidence", "").strip().lower()
        if conf in ("high", "manual"):
            mappings[row["merchant"]] = row["envelope"]
    return mappings


def is_uncategorized(envelope: str) -> bool:
    return envelope == "" or envelope == "[Needs Envelope]"


def match_rule(name: str, rule: dict) -> bool:
    """Check if a merchant name matches a classification rule."""
    mt = rule["match_type"]
    pat = rule["pattern"]
    if mt == "exact":
        return name == pat
    elif mt == "contains":
        return pat.lower() in name.lower()
    elif mt == "regex":
        return bool(rule["_compiled"].search(name))
    return False


def normalize(name: str) -> str:
    """Normalize merchant name for fuzzy matching."""
    s = name.lower()
    s = re.sub(r"[^\w\s]", " ", s)  # strip punctuation
    s = re.sub(r"\s+", " ", s).strip()  # collapse whitespace
    return s


def tokenize(name: str) -> set:
    """Split normalized name into word tokens."""
    return set(normalize(name).split())


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_fuzzy_match(name: str, mappings: dict, threshold: float = 0.6):
    """Find best fuzzy match for name in mappings.
    Returns (matched_merchant, envelope, score) or None."""
    name_tokens = tokenize(name)
    if not name_tokens:
        return None

    best = None
    best_score = 0.0
    for merchant, envelope in mappings.items():
        merchant_tokens = tokenize(merchant)
        score = jaccard(name_tokens, merchant_tokens)
        if score > best_score:
            best_score = score
            best = (merchant, envelope, score)

    if best and best_score >= threshold:
        return best
    return None


def classify(rows: list, rules: list, mappings: dict):
    """Classify rows. Returns (classified_rows, stats) where stats tracks
    what happened to each row."""
    stats = {
        "already_categorized": 0,
        "rule_classified": 0,
        "rule_skipped": 0,
        "exact_lookup": 0,
        "fuzzy_lookup": 0,
        "unclassified": 0,
    }
    fuzzy_matches = []  # (name, matched_merchant, envelope, score)
    classified = []

    for row in rows:
        out = dict(row)
        envelope = row["envelope"]
        name = row["name"]

        # Tier 1: already categorized
        if not is_uncategorized(envelope):
            stats["already_categorized"] += 1
            classified.append(out)
            continue

        # Tier 2: user rules
        matched_rule = None
        for rule in rules:
            if match_rule(name, rule):
                matched_rule = rule
                break

        if matched_rule:
            env = matched_rule["envelope"].strip()
            if env.lower() == "skip":
                out["envelope"] = "[Skip]"
                stats["rule_skipped"] += 1
            else:
                out["envelope"] = env
                stats["rule_classified"] += 1
            classified.append(out)
            continue

        # Tier 3: exact merchant lookup
        if name in mappings:
            out["envelope"] = mappings[name]
            stats["exact_lookup"] += 1
            classified.append(out)
            continue

        # Tier 4: fuzzy merchant lookup
        match = find_fuzzy_match(name, mappings)
        if match:
            matched_merchant, env, score = match
            out["envelope"] = env
            stats["fuzzy_lookup"] += 1
            fuzzy_matches.append((name, matched_merchant, env, score))
            classified.append(out)
            continue

        # Tier 5: unclassified
        stats["unclassified"] += 1
        classified.append(out)

    return classified, stats, fuzzy_matches


def print_report(stats: dict, fuzzy_matches: list, classified: list):
    """Print classification report to console."""
    total = sum(stats.values())
    uncategorized_input = total - stats["already_categorized"]

    print("=" * 70)
    print("PHASE 2b: CLASSIFICATION REPORT")
    print("=" * 70)
    print(f"\nTotal rows:              {total}")
    print(f"Already categorized:     {stats['already_categorized']}")
    print(f"Uncategorized input:     {uncategorized_input}")
    print()
    print(f"  Classified by rules:   {stats['rule_classified']}")
    print(f"  Skipped by rules:      {stats['rule_skipped']}")
    print(f"  Exact merchant lookup: {stats['exact_lookup']}")
    print(f"  Fuzzy merchant lookup: {stats['fuzzy_lookup']}")
    print(f"  Unclassified:          {stats['unclassified']}")

    resolved = (stats["rule_classified"] + stats["rule_skipped"]
                + stats["exact_lookup"] + stats["fuzzy_lookup"])
    if uncategorized_input > 0:
        pct = 100 * resolved / uncategorized_input
        print(f"\n  Resolution rate:       {resolved}/{uncategorized_input} "
              f"({pct:.1f}%)")

    # Fuzzy matches for review
    if fuzzy_matches:
        print(f"\n{'=' * 70}")
        print("FUZZY MATCHES (review for correctness)")
        print(f"{'=' * 70}")
        # Deduplicate by (name, matched_merchant)
        seen = set()
        for name, matched, env, score in sorted(fuzzy_matches,
                                                  key=lambda x: -x[3]):
            key = (name, matched)
            if key in seen:
                continue
            seen.add(key)
            print(f"  {name:<40} → {matched:<35} ({env}, {score:.2f})")

    # Unclassified remainder
    unclassified = [r for r in classified
                    if r["envelope"] == "[Needs Envelope]"
                    or r["envelope"] == ""]
    if unclassified:
        print(f"\n{'=' * 70}")
        print("UNCLASSIFIED (needs manual rules or review)")
        print(f"{'=' * 70}")
        name_groups = defaultdict(list)
        for r in unclassified:
            name_groups[r["name"]].append(r)
        print(f"{'Merchant':<55} {'N':>4} {'Total':>10}")
        print(f"{'-' * 55} {'-' * 4} {'-' * 10}")
        for name, rows in sorted(name_groups.items(),
                                  key=lambda x: len(x[1]), reverse=True):
            spend = sum(float(r["amount"]) for r in rows)
            print(f"{name[:55]:<55} {len(rows):>4} {spend:>10.2f}")


def main():
    directory = os.path.dirname(os.path.abspath(__file__))
    unified_path = os.path.join(directory, "unified.csv")
    mappings_path = os.path.join(directory, "merchant_mappings.csv")
    rules_path = os.path.join(directory, "classification_rules.csv")
    output_path = os.path.join(directory, "classified.csv")

    if not os.path.exists(unified_path):
        print("Run unify.py first to generate unified.csv")
        return

    rows = load_csv(unified_path)
    rules = load_rules(rules_path)
    mappings = load_mappings(mappings_path)

    classified, stats, fuzzy_matches = classify(rows, rules, mappings)

    # Write output
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=UNIFIED_FIELDS,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(classified)

    print_report(stats, fuzzy_matches, classified)
    print(f"\nWrote {len(classified)} rows to {output_path}")


if __name__ == "__main__":
    main()
