# Financial Analysis Pipeline

Personal financial analysis toolchain: ingest transaction data from Goodbudget, Costco receipts, and Amazon orders, unify into a single table, classify uncategorized transactions, and produce spending/budget reports.

**Pure Python stdlib** — no external packages. Only external dependency is `pdftotext` (poppler) for Costco PDF parsing.

## Pipeline

```bash
python3 parse_costco.py        # 1. Costco receipt PDFs → costco_items.csv
python3 parse_amazon.py        # 2. Amazon order exports → amazon_items.csv
python3 unify.py               # 3. Merge all sources → unified.csv
python3 profile.py             # 4. Profile merchant mappings → merchant_mappings.csv
python3 classify.py            # 5. Classify transactions → classified.csv
python3 analyze.py             # 6. Spending report (console output)
```

Re-run from the earliest changed step. Steps are idempotent — each overwrites its output.

| Re-run from | When |
|---|---|
| Step 1 | Costco PDFs or Amazon exports change |
| Step 3 | `history.csv` (Goodbudget export) changes |
| Step 4 | `unified.csv` changes |
| Step 5 | `classification_rules.csv` changes |
| Step 6 | Any time (read-only, no side effects) |

## Scripts

### parse_costco.py

Extracts item-level data from Costco receipt PDFs (Chrome print-to-PDF from Costco.com).

- **Input**: `*Costco*.pdf` files in project directory
- **Output**: `costco_items.csv`
- **Requires**: `pdftotext` — install via `brew install poppler` (macOS) or `apt install poppler-utils` (Linux)
- **No arguments**

### parse_amazon.py

Parses Amazon order history into normalized items with charge candidates at multiple levels (shipment, digital, order-sum-shipments, order-sum-items).

- **Input**: `amazon_data/Your Amazon Orders/Order History.csv`, optionally `Digital Content Orders.csv`
- **Output**: `amazon_items.csv`
- **No arguments**

### unify.py

Merges Goodbudget transactions with Costco/Amazon item-level data. Matches by date and amount, explodes matched transactions into item rows, handles split transactions.

- **Input**: `history.csv`, `costco_items.csv`, `amazon_items.csv`, `classification_rules.csv`, `analysis_config.csv`
- **Output**: `unified.csv` (one row per classifiable spending unit)
- **No arguments**

Key behaviors:
- Costco matching: date ±2 days, exact amount
- Amazon matching: date ±5 days, amount ±$0.15, priority by charge level
- Split-collapse rules (from `analysis_config.csv`): configurable envelope collapsing (e.g. mortgage splits → single row)
- Source-specific categorization rules (from `classification_rules.csv` with `source` column)

### profile.py

Profiles merchant → envelope mappings from categorized transactions. Produces a console report and a lookup table for the classifier.

- **Input**: `unified.csv`
- **Output**: `merchant_mappings.csv`, console report
- **No arguments**

### classify.py

Classifies uncategorized (`[Needs Envelope]`) transactions using a tiered approach:

1. **User rules** (`classification_rules.csv`) — exact, contains, or regex matching
2. **Exact merchant lookup** (`merchant_mappings.csv`, high-confidence only)
3. **Fuzzy merchant lookup** (token-based Jaccard similarity, threshold 0.6)

- **Input**: `unified.csv`, `merchant_mappings.csv`, `classification_rules.csv`
- **Output**: `classified.csv`, console report
- **No arguments**

### analyze.py

Produces spending analysis and budget baseline from classified transactions.

- **Input**: `classified.csv`, `analysis_config.csv`
- **Output**: console report (no files written)

```bash
python3 analyze.py                                # use defaults from analysis_config.csv
python3 analyze.py --from 2025-01 --to 2025-10    # custom date range
python3 analyze.py --help                          # show options
```

Report includes:
- Monthly spending by category (grid or summary view)
- Budget baseline with recurring / one-off / terminated breakdown
- Income summary with recurring / terminated breakdown
- Net monthly cashflow

## Configuration Files

### classification_rules.csv

User-editable rules for transaction classification. This is the primary way to teach the system new categorizations.

| Column | Values | Description |
|---|---|---|
| `pattern` | any string | Literal (for exact/contains) or regex pattern |
| `match_type` | `exact`, `contains`, `regex` | How pattern is matched against transaction name |
| `envelope` | category or `skip` | Target envelope; `skip` marks internal transfers |
| `notes` | free text | Human-readable notes |
| `source` | blank, `costco`, `amazon` | Limit rule to a specific source; blank = all |

### analysis_config.csv

Analysis settings and split-collapse rules. Key-value format with `key,value,notes` schema.

| Key | Value | Used by |
|---|---|---|
| `oneoff` | envelope name | `analyze.py` — tag category as non-recurring |
| `terminated` | envelope name | `analyze.py` — tag category as ended |
| `default_from` | `YYYY-MM` | `analyze.py` — default start month |
| `default_to` | `YYYY-MM` | `analyze.py` — default end month |
| `split_collapse` | `source_env\|target_env` | `unify.py` — collapse splits instead of exploding |

Multiple rows with the same key are supported (e.g. multiple `oneoff` entries).

## Data Conventions

- **Sign convention**: expenses negative, income/refunds positive (Goodbudget convention)
- **`[Needs Envelope]`**: uncategorized transaction
- **`[Skip]`**: internal transfer (excluded from analysis)
- **`source` column**: provenance tracking — `goodbudget`, `costco_receipt`, `amazon_order`, `goodbudget_split`

## Source Data

| File | Source | Notes |
|---|---|---|
| `history.csv` | Goodbudget export | Source of truth. Schema: Date, Envelope, Account, Name, Notes, Check #, Amount, Status, Details |
| `*Costco*.pdf` | Costco.com purchase history | Chrome print-to-PDF |
| `amazon_data/` | Amazon data export | Order History.csv + Digital Content Orders.csv |
