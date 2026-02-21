#!/usr/bin/env python3
"""
Phase 3: Spending analysis and budget baseline.

Reads classified.csv and produces a spending report with:
  - Monthly spending by category
  - Budget baseline (averages over analysis period)
  - Fixed vs variable breakdown
  - Runway scenarios

Usage:
  python3 analyze.py                  # defaults: Jun-Oct 2025
  python3 analyze.py --from 2025-01   # custom start month
  python3 analyze.py --to 2025-08     # custom end month
  python3 analyze.py --from 2024-12 --to 2025-10  # full range
"""

import argparse
import csv
import os
from collections import defaultdict

SKIP_ENVELOPES = {"[Skip]", "[Needs Envelope]", "", "[Available]"}

# Hardcoded fallbacks if analysis_config.csv is missing
_DEFAULT_CONFIG = {
    "oneoff": {
        "Finance:Taxes", "Health:Coaching", "Household:Improvements",
        "Other Spending:Unplanned!", "Divorce:Legal and Mediation Fees",
    },
    "terminated": {
        "Finance:Car Loan", "Divorce:Anna Transitional Support",
        "Income:Disability Insurance", "Income:Salary",
    },
    "default_from": "2025-01",
    "default_to": "2025-10",
}


def load_analysis_config(path):
    """Load analysis configuration from CSV. Falls back to defaults if missing."""
    if not os.path.exists(path):
        return dict(_DEFAULT_CONFIG)
    config = {"oneoff": set(), "terminated": set(), "default_from": None, "default_to": None}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            key = r["key"].strip()
            value = r["value"].strip()
            if key == "oneoff":
                config["oneoff"].add(value)
            elif key == "terminated":
                config["terminated"].add(value)
            elif key == "default_from":
                config["default_from"] = value
            elif key == "default_to":
                config["default_to"] = value
    # Fill in any missing defaults
    for k, v in _DEFAULT_CONFIG.items():
        if not config[k]:
            config[k] = v if isinstance(v, set) else _DEFAULT_CONFIG[k]
    return config


def parse_date(date_str):
    """Parse MM/DD/YYYY to (year, month) tuple and YYYY-MM string."""
    parts = date_str.split("/")
    return f"{parts[2]}-{parts[0].zfill(2)}"


def load_data(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def monthly_summary(rows, month_from, month_to):
    """Aggregate spending and income by month and category."""
    spending = defaultdict(lambda: defaultdict(float))
    income = defaultdict(lambda: defaultdict(float))
    all_months = set()

    for r in rows:
        env = r["envelope"]
        if env in SKIP_ENVELOPES:
            continue

        month_key = parse_date(r["date"])
        if month_key < month_from or month_key > month_to:
            continue

        all_months.add(month_key)
        amount = float(r["amount"])

        if amount < 0:
            spending[month_key][env] += amount
        elif env.startswith("Income:"):
            income[month_key][env] += amount
        else:
            # Refunds/reimbursements net against spending
            spending[month_key][env] += amount

    return spending, income, sorted(all_months)


def print_monthly_grid(spending, months):
    """Print monthly spending by category grid."""
    all_envs = set()
    for m in spending.values():
        all_envs.update(m.keys())

    n = len(months)
    # If too many months for terminal, show just averages
    show_grid = n <= 6

    print("=" * 80)
    print("MONTHLY SPENDING BY CATEGORY")
    print("=" * 80)

    if show_grid:
        header = f"{'Category':<40}"
        for m in months:
            header += f"{m:>10}"
        header += f"{'Avg/Mo':>10}"
        print(header)
        print("-" * (40 + 10 * (n + 1)))
    else:
        print(f"{'Category':<50} {'Avg/Mo':>10}  {'Min':>10}  {'Max':>10}")
        print("-" * 85)

    grand_totals = defaultdict(float)

    for cat in sorted(all_envs):
        vals = [spending[m].get(cat, 0) for m in months]
        total = sum(vals)
        avg = total / n
        for i, m in enumerate(months):
            grand_totals[m] += vals[i]

        if show_grid:
            line = f"{cat:<40}"
            for v in vals:
                line += f"{v:>10.0f}" if v != 0 else f"{'':>10}"
            line += f"{avg:>10.0f}"
            print(line)
        else:
            mn = min(vals)
            mx = max(vals)
            print(f"  {cat:<48} ${abs(avg):>8.0f}  ${abs(mx):>8.0f}  ${abs(mn):>8.0f}")

    if show_grid:
        print("-" * (40 + 10 * (n + 1)))
        line = f"{'TOTAL':<40}"
        gt = 0
        for m in months:
            line += f"{grand_totals[m]:>10.0f}"
            gt += grand_totals[m]
        line += f"{gt / n:>10.0f}"
        print(line)
    else:
        pass


def print_budget_baseline(spending, income, months, oneoff_cats, terminated_cats):
    """Print budget baseline with fixed/variable breakdown."""
    n = len(months)

    # Compute category averages
    all_envs = set()
    for m in spending.values():
        all_envs.update(m.keys())

    cat_avgs = {}
    for cat in all_envs:
        total = sum(spending[m].get(cat, 0) for m in months)
        cat_avgs[cat] = total / n

    # Group by top-level
    groups = defaultdict(list)
    for env in sorted(all_envs):
        top = env.split(":")[0] if ":" in env else env
        groups[top].append(env)

    print()
    print("=" * 80)
    print("BUDGET BASELINE (average monthly spending)")
    print("=" * 80)

    recurring_total = 0
    oneoff_total = 0
    terminated_total = 0

    for group_name in sorted(groups.keys()):
        print(f"\n  {group_name}")
        for env in sorted(groups[group_name]):
            avg = cat_avgs[env]
            is_oneoff = env in oneoff_cats
            is_terminated = env in terminated_cats
            if is_oneoff:
                marker = " [one-off]"
            elif is_terminated:
                marker = " [terminated]"
            else:
                marker = ""
            print(f"    {env:<45} ${abs(avg):>8.0f}/mo{marker}")
            if is_oneoff:
                oneoff_total += avg
            elif is_terminated:
                terminated_total += avg
            else:
                recurring_total += avg

    print()
    print("-" * 80)
    print(f"  {'RECURRING MONTHLY TOTAL':<48} ${abs(recurring_total):>8.0f}/mo")
    if oneoff_total:
        print(f"  {'One-off monthly avg':<48} ${abs(oneoff_total):>8.0f}/mo")
    if terminated_total:
        print(f"  {'Terminated monthly avg':<48} ${abs(terminated_total):>8.0f}/mo")
    print()

    # Income summary
    all_income_envs = set()
    for m in income.values():
        all_income_envs.update(m.keys())

    income_total = 0
    terminated_income_total = 0
    if all_income_envs:
        print("  INCOME")
        for env in sorted(all_income_envs):
            total = sum(income[m].get(env, 0) for m in months)
            avg = total / n
            if abs(avg) >= 1:
                is_terminated = env in terminated_cats
                marker = " [terminated]" if is_terminated else ""
                print(f"    {env:<45} ${avg:>8.0f}/mo{marker}")
                if is_terminated:
                    terminated_income_total += avg
                else:
                    income_total += avg
        print(f"    {'RECURRING INCOME':<45} ${income_total:>8.0f}/mo")
        if terminated_income_total:
            print(f"    {'Terminated income avg':<45} ${terminated_income_total:>8.0f}/mo")

    print()
    net = income_total + recurring_total  # recurring_total is negative
    print(f"  {'NET MONTHLY (recurring only)':<48} ${net:>+8.0f}/mo")
    print(f"  {'Annual burn (if negative)':<48} ${net * 12:>+8.0f}/yr")


def print_top_expenses(spending, months):
    """Print the largest individual expense items across the period."""
    # Not implemented as individual-transaction view;
    # the category breakdown covers this.
    pass


def main():
    directory = os.path.dirname(os.path.abspath(__file__))
    classified_path = os.path.join(directory, "classified.csv")
    config_path = os.path.join(directory, "analysis_config.csv")

    if not os.path.exists(classified_path):
        print("Run classify.py first to generate classified.csv")
        return

    config = load_analysis_config(config_path)

    parser = argparse.ArgumentParser(description="Spending analysis and budget baseline")
    parser.add_argument("--from", dest="month_from", default=config["default_from"],
                        help="Start month YYYY-MM (default: %(default)s)")
    parser.add_argument("--to", dest="month_to", default=config["default_to"],
                        help="End month YYYY-MM (default: %(default)s)")
    args = parser.parse_args()

    rows = load_data(classified_path)
    spending, income, months = monthly_summary(rows, args.month_from, args.month_to)

    if not months:
        print(f"No data found for {args.month_from} to {args.month_to}")
        return

    print(f"\nAnalysis period: {months[0]} to {months[-1]} ({len(months)} months)")
    print_monthly_grid(spending, months)
    print_budget_baseline(spending, income, months, config["oneoff"], config["terminated"])


if __name__ == "__main__":
    main()
