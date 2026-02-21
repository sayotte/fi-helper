#!/usr/bin/env python3
"""Parse Costco receipt PDFs (printed from Costco website via Chrome) into structured CSV."""

import csv
import glob
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field


@dataclass
class LineItem:
    item_id: str
    description: str
    price: float
    tax_flag: str  # 'Y' (taxable), '3' (food/non-taxable), '' (discount/unknown)
    is_discount: bool = False
    parent_item_id: str = ""  # for discount lines, the item they apply to


@dataclass
class Receipt:
    store_number: str = ""
    date: str = ""  # MM/DD/YYYY
    time: str = ""
    member_number: str = ""
    card_last4: str = ""
    card_type: str = ""
    is_refund: bool = False
    subtotal: float = 0.0
    tax: float = 0.0
    total: float = 0.0
    items_sold_count: int = 0
    instant_savings: float = 0.0
    transaction_id: str = ""
    items: list = field(default_factory=list)
    source_file: str = ""


def extract_text(pdf_path: str) -> str:
    """Extract text from PDF using pdftotext with layout preservation."""
    result = subprocess.run(
        ["pdftotext", "-layout", pdf_path, "-"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"Warning: pdftotext failed for {pdf_path}: {result.stderr}", file=sys.stderr)
        return ""
    return result.stdout


def parse_receipt(text: str, source_file: str) -> Receipt:
    """Parse extracted text into a Receipt object."""
    receipt = Receipt(source_file=source_file)
    lines = text.split("\n")

    # Extract store number
    store_match = re.search(r"(?:PERIMETER|COSTCO)\s*#(\d+)", text)
    if store_match:
        receipt.store_number = store_match.group(1)

    # Extract member number
    member_match = re.search(r"Member\s+(\d+)", text)
    if member_match:
        receipt.member_number = member_match.group(1)

    # Extract card info
    card_match = re.search(r"X{5,}(\d{4})", text)
    if card_match:
        receipt.card_last4 = card_match.group(1)

    # Detect refund
    receipt.is_refund = "APPROVED - REFUND" in text

    # Extract transaction date/time and ID
    txn_match = re.search(
        r"AMOUNT:.*?\n\s*(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2})\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)",
        text, re.DOTALL
    )
    if txn_match:
        receipt.date = txn_match.group(1)
        receipt.time = txn_match.group(2)
        wh, trm, trn, opt = txn_match.group(3), txn_match.group(4), txn_match.group(5), txn_match.group(6)
        receipt.transaction_id = f"{receipt.date}-{wh}-{trm}-{trn}-{opt}"

    # Extract card type
    visa_match = re.search(r"\n\s*(VISA|MASTERCARD|AMEX|DISCOVER)\s", text)
    if visa_match:
        receipt.card_type = visa_match.group(1)

    # Extract totals
    subtotal_match = re.search(r"SUBTOTAL\s+([\d,.]+)-?", text)
    if subtotal_match:
        val = float(subtotal_match.group(1).replace(",", ""))
        receipt.subtotal = -val if receipt.is_refund else val

    tax_total_match = re.search(r"(?<!\w)TAX\s+([\d,.]+)-?", text)
    if tax_total_match:
        val = float(tax_total_match.group(1).replace(",", ""))
        receipt.tax = -val if receipt.is_refund else val

    total_match = re.search(r"\*{4}\s+TOTAL\s+([\d,.]+)-?", text)
    if total_match:
        val = float(total_match.group(1).replace(",", ""))
        receipt.total = -val if receipt.is_refund else val

    # Extract items sold count
    items_sold_match = re.search(r"ITEMS SOLD\s*=\s*(-?\d+)", text)
    if items_sold_match:
        receipt.items_sold_count = abs(int(items_sold_match.group(1)))

    # Extract instant savings
    savings_match = re.search(r"INSTANT SAVINGS\s+\$?([\d,.]+)", text)
    if savings_match:
        receipt.instant_savings = float(savings_match.group(1).replace(",", ""))

    # Find item block: between member number line and SUBTOTAL line
    item_start = None
    item_end = None
    found_member = False
    for i, line in enumerate(lines):
        if "Member" in line and not found_member:
            found_member = True
            # Skip the member number on the next line(s)
            for j in range(i + 1, min(i + 4, len(lines))):
                if re.match(r"\s*\d{9,}", lines[j].strip()):
                    item_start = j + 1
                    break
            if item_start is None:
                item_start = i + 1
        if "SUBTOTAL" in line:
            item_end = i
            break

    if item_start is not None and item_end is not None:
        _parse_item_block(lines[item_start:item_end], receipt)

    return receipt


def _parse_item_block(lines: list, receipt: Receipt):
    """
    Parse the item block using a two-pass approach:
    1. Identify lines with prices (these are the item/discount anchors)
    2. Attach description text from adjacent non-price lines
    """
    # A price line has a decimal number (possibly followed by - and/or Y/3) in the latter portion.
    # Pattern: stuff... price[-] [Y|3]  at the end of the line
    price_line_re = re.compile(r"^(.*?)\s+([\d,]+\.\d{2})(-?)\s*([Y3])?\s*$")
    # Discount line: coupon_id / item_id  price[-]
    # On regular receipts the discount has trailing - (e.g. "4.00-")
    # On refund receipts the coupon reversal is positive (e.g. "4.00")
    discount_re = re.compile(r"(\d+)\s*/\s*(\d+)\s+([\d,]+\.\d{2})(-?)")

    # First pass: classify each line
    classified = []  # list of (type, data, line_index)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped == "E":
            classified.append(("empty", None, i))
            continue

        # Check for discount pattern
        dm = discount_re.search(stripped)
        if dm:
            classified.append(("discount", dm, i))
            continue

        # Check for price pattern
        pm = price_line_re.match(stripped)
        if pm:
            classified.append(("price_line", pm, i))
            continue

        # It's a description-only line
        # Remove leading E if present
        cleaned = re.sub(r"^E\s+", "", stripped).strip()
        if cleaned:
            classified.append(("desc", cleaned, i))
        else:
            classified.append(("empty", None, i))

    # Second pass: build items by grouping descriptions around price lines
    #
    # Key observation from Costco receipt layout:
    # When a product name wraps, it splits AROUND the price line:
    #   desc_before (e.g. "MATEOS")
    #   item_id  price tax_flag  (e.g. "559608  41.94 3")
    #   desc_after (e.g. "SALSA")
    # So the full name is "MATEOS SALSA".
    #
    # When a product fits on one line:
    #   item_id DESC price tax_flag (e.g. "1625149 DURACELL AA 19.99 Y")
    #
    # Rule: a price line with NO inline description owns desc lines above and below.
    # A price line WITH an inline description does NOT consume adjacent desc lines.
    # A desc line between two price lines that both have inline descriptions is orphaned
    # (shouldn't happen in practice).

    items_raw = []
    j = 0
    while j < len(classified):
        ctype, cdata, cidx = classified[j]

        if ctype == "discount":
            dm = cdata
            coupon_id = dm.group(1)
            parent_id = dm.group(2)
            price = float(dm.group(3).replace(",", ""))
            if dm.group(4) == "-":
                price = -price
            items_raw.append({
                "item_id": coupon_id,
                "description": f"INSTANT SAVINGS on {parent_id}",
                "price": price,
                "tax_flag": "",
                "is_discount": True,
                "parent_item_id": parent_id,
            })
            j += 1
            continue

        if ctype == "price_line":
            pm = cdata
            prefix_text = pm.group(1).strip()
            price = float(pm.group(2).replace(",", ""))
            if pm.group(3) == "-":
                price = -price
            tax_flag = pm.group(4) or ""

            # Parse prefix_text: may contain [E] [item_id] [description]
            prefix_text = re.sub(r"^E\s+", "", prefix_text).strip()

            item_id = ""
            inline_desc = ""
            id_match = re.match(r"(\d{3,})\s*(.*)", prefix_text)
            if id_match:
                item_id = id_match.group(1)
                inline_desc = id_match.group(2).strip()
            else:
                inline_desc = prefix_text

            has_inline_desc = bool(inline_desc)

            if has_inline_desc:
                # This item's description is already on the price line.
                # Do NOT consume adjacent desc lines.
                description = inline_desc
            else:
                # No inline description — collect from adjacent desc lines.
                pre_descs = []
                k = j - 1
                while k >= 0 and classified[k][0] == "desc":
                    pre_descs.insert(0, classified[k][1])
                    k -= 1

                post_descs = []
                k = j + 1
                while k < len(classified) and classified[k][0] == "desc":
                    post_descs.append(classified[k][1])
                    k += 1

                # Mark consumed
                for p in range(j - len(pre_descs), j):
                    classified[p] = ("consumed", None, classified[p][2])
                for p in range(j + 1, j + 1 + len(post_descs)):
                    classified[p] = ("consumed", None, classified[p][2])

                description = " ".join(pre_descs + post_descs)

            items_raw.append({
                "item_id": item_id,
                "description": description,
                "price": price,
                "tax_flag": tax_flag,
                "is_discount": False,
                "parent_item_id": "",
            })
            j += 1
            continue

        j += 1

    # Apply refund sign if needed
    for item in items_raw:
        if receipt.is_refund and not item["is_discount"]:
            item["price"] = -abs(item["price"])
        elif receipt.is_refund and item["is_discount"]:
            item["price"] = abs(item["price"])  # discount on refund reverses

        receipt.items.append(LineItem(**item))


def parse_all_pdfs(directory: str) -> list:
    """Parse all Costco PDF files in the given directory."""
    pdf_files = sorted(glob.glob(os.path.join(directory, "*Costco*.pdf")))
    print(f"Found {len(pdf_files)} Costco PDF files")

    all_receipts = []
    seen_txn_ids = set()

    for pdf_path in pdf_files:
        filename = os.path.basename(pdf_path)
        text = extract_text(pdf_path)
        if not text:
            print(f"  SKIP {filename}: no text extracted")
            continue

        receipt = parse_receipt(text, filename)

        # Deduplicate by transaction ID
        if receipt.transaction_id and receipt.transaction_id in seen_txn_ids:
            print(f"  SKIP {filename}: duplicate transaction {receipt.transaction_id}")
            continue
        if receipt.transaction_id:
            seen_txn_ids.add(receipt.transaction_id)

        all_receipts.append(receipt)
        item_total = sum(it.price for it in receipt.items if not it.is_discount)
        discount_total = sum(it.price for it in receipt.items if it.is_discount)
        net = item_total + discount_total
        status = "REFUND" if receipt.is_refund else "OK"
        match_flag = "" if abs(net - receipt.subtotal) < 0.02 else " *** MISMATCH"
        print(f"  {filename}: {receipt.date} ${receipt.total:,.2f} "
              f"({len(receipt.items)} line items, net=${net:,.2f} vs subtotal=${receipt.subtotal:,.2f}) "
              f"[{status}]{match_flag}")

    return all_receipts


def write_csv(receipts: list, output_path: str):
    """Write parsed receipts to CSV."""
    fieldnames = [
        "receipt_date", "receipt_total", "transaction_id", "item_id",
        "description", "price", "is_taxable", "is_food", "discount_amount",
        "is_refund", "source_file", "items_sold_count",
    ]

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for receipt in receipts:
            for item in receipt.items:
                writer.writerow({
                    "receipt_date": receipt.date,
                    "receipt_total": receipt.total,
                    "transaction_id": receipt.transaction_id,
                    "item_id": item.item_id,
                    "description": item.description,
                    "price": item.price,
                    "is_taxable": item.tax_flag == "Y",
                    "is_food": item.tax_flag == "3",
                    "discount_amount": item.price if item.is_discount else 0,
                    "is_refund": receipt.is_refund,
                    "source_file": receipt.source_file,
                    "items_sold_count": receipt.items_sold_count,
                })


def print_receipt_detail(receipt: Receipt):
    """Print a human-readable summary of a receipt for verification."""
    print(f"\n{'='*70}")
    print(f"Receipt: {receipt.date} {receipt.time}, Costco #{receipt.store_number}")
    print(f"Card: {receipt.card_type} ending {receipt.card_last4}")
    print(f"Transaction ID: {receipt.transaction_id}")
    refund_label = " (REFUND)" if receipt.is_refund else ""
    print(f"{'='*70}")
    print(f"{'Item ID':<10} {'Description':<30} {'Price':>8} {'Tax':>5}")
    print(f"{'-'*10} {'-'*30} {'-'*8} {'-'*5}")
    for item in receipt.items:
        tax_label = "food" if item.tax_flag == "3" else ("yes" if item.tax_flag == "Y" else "")
        if item.is_discount:
            print(f"{'':10} {'  ' + item.description:<30} {item.price:>8.2f}")
        else:
            print(f"{item.item_id:<10} {item.description:<30} {item.price:>8.2f} {tax_label:>5}")
    print(f"{'-'*10} {'-'*30} {'-'*8} {'-'*5}")
    print(f"{'':10} {'Subtotal':<30} {receipt.subtotal:>8.2f}")
    print(f"{'':10} {'Tax':<30} {receipt.tax:>8.2f}")
    print(f"{'':10} {'TOTAL' + refund_label:<30} {receipt.total:>8.2f}")
    if receipt.instant_savings:
        print(f"{'':10} {'Instant Savings':<30} {receipt.instant_savings:>8.2f}")
    print(f"Items sold (units): {receipt.items_sold_count}")
    item_sum = sum(it.price for it in receipt.items)
    print(f"Sum of parsed line items: ${item_sum:.2f}")
    if abs(item_sum - receipt.subtotal) > 0.02:
        print(f"  *** WARNING: item sum ({item_sum:.2f}) != subtotal ({receipt.subtotal:.2f}), "
              f"diff: ${item_sum - receipt.subtotal:.2f}")


def main():
    directory = os.path.dirname(os.path.abspath(__file__))
    output_path = os.path.join(directory, "costco_items.csv")

    receipts = parse_all_pdfs(directory)
    print(f"\nParsed {len(receipts)} unique receipts")

    write_csv(receipts, output_path)
    print(f"Wrote {output_path}")

    # Print detailed view of all receipts for verification
    for receipt in receipts:
        print_receipt_detail(receipt)


if __name__ == "__main__":
    main()
