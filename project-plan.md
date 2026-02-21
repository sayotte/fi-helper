# Financial Analysis Project Plan

## Data Overview
- **Source**: Goodbudget export (`history.csv`), ~1116 transactions, Dec 2024 – Feb 2026
- **Accounts**: Wells Fargo Checking, Wells Fargo Visa (CC), Bask Bank Savings, Schwab Checking, Optum HSA, Mr. Cooper mortgage, Truist, E-Trade
- **Existing categories**: ~40 envelope categories (Food:Groceries, Automotive:Gas, Health:Out Of Pocket, etc.)
- **Known data gap**: Wells Fargo Visa stopped syncing to Goodbudget on/after Oct 20, 2025. ~4 months of CC transactions missing. Will be re-synced later; working with incomplete data for now.
- **Schema preference**: Keep data in Goodbudget schema (Date, Envelope, Account, Name, Notes, Check #, Amount, Status, Details) so it can be pushed back into Goodbudget.
- **Sign convention**: Expenses are negative, income/refunds are positive (Goodbudget convention). All unified data follows this.

## Envelope Categories (from Goodbudget)
Household:Cleaning = maid fees only (NOT cleaning products).
Household:Maintenance = cleaning products, batteries, towels, bags, misc household supplies.
Household:Office and Network = electronics, IT equipment.
Household:Furniture and Decorations, Household:Improvements, Household:Pet Supplies.
Food:Groceries includes supplements (VITALPROTEIN, whey protein, etc.) even though GA taxes them as non-food.

## Custody Schedule (5-5-2-2)
- Didi is with Stephen **every Mon/Tue night** (2 nights)
- Didi is with Stephen on **alternating weekends** (Fri/Sat/Sun nights, 3 nights)
- On "Didi weekends", the block is continuous: Fri–Tue (5 nights)
- On "non-Didi weekends": only Mon–Tue (2 nights)
- Schedule repeats on a **2-week cycle**
- There are **holiday exceptions** and **one-off swaps** that will need to be encoded manually or via calendar import

### Modeling the schedule for analysis
- Primary source: Google Calendar events (parse .ics export)
- Fallback: generate from 5-5-2-2 rule for dates not covered by calendar
- Build a day-level boolean series: `didi_with_me[date] -> bool`
- For spending analysis, aggregate to weekly or biweekly periods and compute `didi_days_in_period` as a continuous feature (0–7 or 0–14) rather than binary, since partial weeks at period boundaries are common
- Calendar may have gaps where swaps weren't recorded; user will backfill over time

---

## Phase 1: Data Ingestion & Unification

### 1a. Ingest history.csv — DONE
- `history.csv` parsed and loaded in `unify.py`
- Details column (split transactions) parsed and exploded into separate rows
- Amount formatting handled (quoted negatives with commas)

### 1b. Ingest Amazon order history — DONE
- **`parse_amazon.py`**: Parses `Order History.csv` (physical) and `Digital Content Orders.csv` (digital) from Amazon data export
  - Builds charge candidates at 4 levels: shipment, digital, order-sum-shipments, order-sum-items
  - Handles complex Amazon charging: split shipments, Subscribe & Save, multi-item orders
  - Produces `amazon_items.csv` (466 item rows from 318 charge candidates)
- **`unify.py` Amazon matching**: Multi-level matching by date ±5 days and amount ±$0.15
  - Priority: shipment > digital > order_sum_shipments > order_sum_items
  - 116 of 125 Amazon transactions matched (93%), 9 unmatched
  - Matched orders exploded into 167 item rows
  - Auto-categorization: Prime → memberships, streaming → streaming media, Kindle → shopping
  - Physical items left as `[Needs Envelope]` for user classification

### 1c. Ingest Costco purchase history — DONE
- **`parse_costco.py`**: Extracts item-level data from Costco receipt PDFs (Chrome print-to-PDF from Costco.com purchase history)
  - Uses `pdftotext -layout` (poppler) for text extraction
  - Two-pass parser: first classifies lines (price, discount, description), then groups descriptions around price lines
  - Key insight: multi-line descriptions wrap AROUND the price line (desc before + price + desc after). Only no-inline-description price lines consume adjacent desc lines.
  - Tax flag `3` = food (non-taxable in GA), `Y` = taxable (non-food). Used as category signal but has known exceptions (supplements taxed as non-food).
  - Handles refunds (negative totals, `APPROVED - REFUND`), instant savings/coupon discount lines, duplicate PDF detection
  - **16 unique receipts parsed, all matching subtotals exactly**
- **`costco_items.csv`**: Output with columns: receipt_date, receipt_total, transaction_id, item_id, description, price, is_taxable, is_food, discount_amount, is_refund, source_file, items_sold_count

### 1c-unify. Costco ↔ Goodbudget matching — DONE
- Implemented in `unify.py`
- Matches by date (+/- 2 days) and absolute amount value
- 6 of 12 Costco transactions matched (other 6 have no receipt PDF)
- 2 receipts (01/16/2026, 12/29/2025) will match once CC data re-syncs
- Auto-categorization via `categorize_costco_item()`: food flag → Food:Groceries; heuristics for clothing, electronics, cleaning products, liquor, household supplies
- Known overrides: VITALPROTEIN, WHEY → Food:Groceries (taxed as non-food in GA but are groceries)

### 1d. Ingest calendar/custody data — NOT STARTED
- **Input**: Google Calendar .ics export
- Build `didi_with_me` daily series from 5-5-2-2 rule + exceptions
- Extract trip dates, holidays, other relevant events
- Produce a date-level context table: `{date, didi_with_me, is_trip, is_holiday, trip_name, ...}`

### 1e. Ingest Mr. Cooper mortgage statements — CANCELLED
- Originally planned to split mortgage into Principal/Interest/Fees from Mr. Cooper statements
- **Decision**: User doesn't want this granularity; single `Finance:Mortgage` line per payment is preferred
- Mortgage splits in Goodbudget (forced by Goodbudget) are now collapsed back into single rows by `unify.py`
- Truist car loan splits similarly collapsed into `Finance:Car Loan`

### Unified output — DONE
- **`unify.py`**: Produces `unified.csv` — one row per classifiable spending unit
  - Non-Costco/non-Amazon transactions pass through from Goodbudget
  - Costco transactions with matching receipts exploded into item-level rows
  - Mortgage splits (Finance:Mortgage) and car loan splits (Debt Payment: Truist → Finance:Car Loan) collapsed into single rows
  - Other Goodbudget split transactions still exploded
  - Internal transfers, balance adjustments, fills filtered out
  - 1206 unified rows from 1116 Goodbudget rows (after Amazon + Costco explosion)
- Schema: date, envelope, account, name, notes, amount, status, source, parent_name, parent_amount, is_food, is_taxable, costco_item_id
- `source` column tracks provenance: "goodbudget", "costco_receipt", "amazon_order", "goodbudget_split"

---

## Phase 2: Classify Uncategorized Transactions — DONE

### 2a. Analyze existing categorizations — DONE
- **`profile.py`**: Reads `unified.csv`, produces report + `merchant_mappings.csv`
- Deterministic and ambiguous merchant mappings identified
- `merchant_mappings.csv` used as lookup in classifier

### 2b. Build classifier — DONE
- **`classify.py`**: Pipeline step 5, reads `unified.csv` + `merchant_mappings.csv` + `classification_rules.csv`
- **Classification tiers** (applied in order):
  1. User rules (`classification_rules.csv`) — exact/contains/regex pattern matching
  2. Exact merchant lookup from `merchant_mappings.csv`
  3. Fuzzy merchant lookup (token-based Jaccard similarity, threshold 0.6)
  4. Unclassified remainder listed in console report
- **`classification_rules.csv`**: User-editable rules file. `envelope=skip` for internal transfers.
- **Final status**: 1206 total rows, all classified, **0 uncategorized (100%)**
- 175+ rules in classification_rules.csv
- **Workflow**: Run classify.py → review unclassified list → add rules → re-run

### Key classification rules updates
- `Mr. Cooper` → `Finance:Mortgage` (was skip for Phase 1e)
- Balance-setup rows (Set/Updated Mr. Cooper, Set/Updated Truist) → skip
- `HARTFORD` → `Income:Disability Insurance` (was Income:Other)

---

## Phase 3: Descriptive Statistics & Spending Baseline — UPDATED

### 3a. Monthly spending by category — DONE
- **`analyze.py`**: Reads `classified.csv`, produces spending report
  - `--from YYYY-MM --to YYYY-MM` for custom date ranges
  - Grid view (≤6 months) or summary view (>6 months) with Avg/Min/Max
  - Budget baseline with recurring/one-off/terminated breakdown
  - Income summary with recurring/terminated breakdown
  - Net monthly cashflow computation

### analyze.py features
- **One-off categories** (`[one-off]` tag): Finance:Taxes, Health:Coaching, Household:Improvements, Other Spending:Unplanned!, Divorce:Legal and Mediation Fees — excluded from recurring baseline
- **Terminated categories** (`[terminated]` tag): Finance:Car Loan, Divorce:Anna Transitional Support, Income:Disability Insurance, Income:Salary — excluded from recurring baseline (expenses that have ceased or income that has stopped)
- **Refund netting**: Non-Income positive amounts net against their spending category (e.g., insurance reimbursements reduce Health:Out Of Pocket, product returns reduce Discretionary:Shopping)
- Min column shows $0 for months with no activity (not filtered to nonzero)

### Current budget snapshot (Jan–Oct 2025, 10 months)
- **Recurring monthly spending**: ~$9,305/mo
- **Recurring monthly income**: ~$1,312/mo (interest + other)
- **Terminated spending**: Car loan ($7K/mo avg), Anna support ($7.5K/mo avg)
- **Terminated income**: Disability insurance ($10.9K/mo), Salary ($4K/mo)
- Key recurring items: Mortgage ~$2,469, COBRA $2,515, Therapy ~$880, Groceries, Eating Out, etc.

### 3b. Identify and handle outliers — PARTIALLY DONE
- One-off items identified and separated from baseline
- Renovation ($37.5K), taxes ($19.5K), coaching ($12K), Lexus payoff (~$70K) excluded via tags
- Per-category IQR analysis not yet done

### 3c. Fixed vs. variable expenses — NEEDS UPDATE
- Previous breakdown was on Jun–Oct 2025 data; needs refresh with current analyze.py improvements

---

## Phase 4: Pattern Analysis — NOT STARTED

### 4a. Custody-spending correlation
- Aggregate spending by period (weekly or biweekly)
- Correlate `didi_days_in_period` with spending in relevant categories (food, eating out, entertainment, etc.)
- Test statistical significance

### 4b. Category co-variation
- Pivot to weekly spending-by-category matrix
- Correlation heatmap across categories
- PCA on this matrix to find dominant spending patterns

### 4c. Temporal patterns
- Day-of-week and day-of-month spending patterns
- Seasonal trends (if enough data)
- Spending trajectory over time (are costs trending up/down?)

---

## Phase 5: Budget Conversation Prep — NOT STARTED

### 5a. Summary document
- Compile findings from Phases 3-4 into a structured summary
- Present "conservative baseline" monthly budget
- Highlight categories with high variability and what drives it

### 5b. Interactive analysis support
- Prepare data and summaries for what-if scenarios

---

## Open Items / Questions
- [ ] Calendar data — user to provide Google Calendar .ics export
- [ ] Confirm which months have complete CC data (looks like Dec 2024 through mid-Oct 2025)
- [ ] Missing CC data: user will re-sync to Goodbudget and re-export when available
- [ ] Propagate parent envelope to Costco discount lines? (currently empty)
- [ ] Double mortgage payment in Jun 2025 (missed May?) — inflates avg if window excludes the gap month
- [ ] Health:Coaching has a $12K refund+recharge pair on same day (Beata Lewis) — nets to zero but looks odd

## Notes for Next Session
- **Pipeline**: `parse_costco.py` → `parse_amazon.py` → `unify.py` → `profile.py` → `classify.py`
- **Analysis**: `python3 analyze.py --from 2025-01 --to 2025-10` for best coverage (avoids CC data gap)
- **Classification is 100% complete** — 1206 rows, 0 unclassified
- **analyze.py improvements this session**:
  - Mortgage splits collapsed (not exploded) — single Finance:Mortgage row per payment
  - Car loan splits collapsed into Finance:Car Loan
  - Terminated category tag for ceased expenses/income
  - Refunds net against spending categories (not shown as income)
  - Min/Max columns fixed (were reversed; min now includes $0 months)
  - Totals row removed from spending grid (neither sum-of-min nor min-of-sum was useful)
  - Hartford reclassified: Income:Other → Income:Disability Insurance
- `classified.csv` is the working analysis table (downstream of `unified.csv`)
