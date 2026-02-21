# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

Personal financial analysis project: ingest transaction data from multiple sources (Goodbudget, Costco receipts, Amazon orders), unify into a single table, classify uncategorized transactions, and produce spending analysis for budget planning.

## Pipeline

```bash
# Step 1a: Parse Costco receipt PDFs into costco_items.csv (requires poppler/pdftotext)
python3 parse_costco.py

# Step 1b: Parse Amazon order data into amazon_items.csv
python3 parse_amazon.py

# Step 2: Merge Goodbudget + Costco items + Amazon items + split transactions into unified.csv
python3 unify.py

# Step 3: Profile merchant→envelope mappings, produce merchant_mappings.csv
python3 profile.py

# Step 4: Classify uncategorized transactions, produce classified.csv
python3 classify.py
```

Re-run in order. Re-run from step 1a if Costco PDFs change; from step 1b if Amazon data changes; from step 2 if `history.csv` changes; step 3 if unified.csv changes; step 4 if classification_rules.csv changes.

## Architecture

**Stdlib-only Python** (csv, re, subprocess, datetime). No external dependencies.

- `history.csv` — Goodbudget export, source of truth. Schema: Date, Envelope, Account, Name, Notes, Check #, Amount, Status, Details
- `parse_costco.py` — Extracts item-level data from Costco receipt PDFs using `pdftotext -layout`. Two-pass parser: classifies lines (price/discount/description), then groups descriptions around price lines. Multi-line descriptions wrap AROUND the price line (desc before + price + desc after).
- `parse_amazon.py` — Parses Amazon Order History.csv and Digital Content Orders.csv into `amazon_items.csv`. Builds charge candidates at 4 levels (shipment, digital, order-sum-shipments, order-sum-items) for matching.
- `unify.py` — Produces `unified.csv` by: (1) matching Costco receipts to Goodbudget rows by date±2 days and amount, (2) matching Amazon orders via multi-level charge matching (±5 days, ±$0.15), (3) exploding Goodbudget split transactions (Details column format: `Envelope|Amount||Envelope|Amount`), (4) passing through everything else. Filters out reconciliation rows.
- `profile.py` — Profiles merchant→envelope mappings from `unified.csv`. Filters out Costco line items and transaction-description merchants. Produces report + `merchant_mappings.csv` with confidence levels.
- `classify.py` — Classifies uncategorized transactions using user rules (`classification_rules.csv`), exact merchant lookup (`merchant_mappings.csv`), and fuzzy name matching. Produces `classified.csv` + console report.
- `classification_rules.csv` — User-editable rules file. Schema: `pattern,match_type,envelope,notes`. match_type: exact/contains/regex. envelope: category name or "skip" for internal transfers.
- `costco_items.csv` / `amazon_items.csv` / `unified.csv` / `merchant_mappings.csv` / `classified.csv` — Generated outputs, never edit manually (except `classification_rules.csv` which IS user-edited).

## Data Conventions

- **Sign convention**: expenses are negative, income/refunds are positive (Goodbudget convention). All unified data follows this.
- **`[Needs Envelope]`** = uncategorized transaction
- `source` column in unified.csv: `"goodbudget"`, `"costco_receipt"`, `"amazon_order"`, or `"goodbudget_split"`
- Costco tax flag `3` = food (GA non-taxable), `Y` = taxable (non-food)
- Supplements (VITALPROTEIN, whey) → `Food:Groceries` despite GA taxing as non-food
- `Household:Cleaning` = maid fees only, NOT cleaning products (those → `Household:Maintenance`)
- `Household:Office and Network` = electronics, IT equipment

## Known Data Issues

- Wells Fargo Visa stopped syncing to Goodbudget ~Oct 20, 2025. ~4 months of CC data missing.
- 6 Costco Goodbudget transactions have no matching receipt PDF.
- 9 Amazon Goodbudget transactions have no matching Amazon order (likely Subscribe & Save adjustments or timing mismatches).
