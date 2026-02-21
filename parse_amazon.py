#!/usr/bin/env python3
"""
Phase 1b: Parse Amazon order data into a normalized items table.

Reads:
  - amazon_data/Your Amazon Orders/Order History.csv (physical orders)
  - amazon_data/Your Amazon Orders/Digital Content Orders.csv (digital content)

Produces: amazon_items.csv

Each row represents one item from an Amazon order, with the CC charge amount
it belongs to (for matching to Goodbudget transactions in unify.py).

Amazon CC charges appear at different levels:
  1. Shipment charges (most common) — Total Amount per item row
  2. Order-level charges — sum of unique shipment totals per order
  3. Item-sum charges — sum of (Unit Price + Tax) per order (when != shipment sum)
  4. Digital charges — sum of Transaction Amounts per digital Order ID
"""

import csv
import os
from collections import defaultdict


AMAZON_DATA_DIR = "amazon_data/Your Amazon Orders"

AMAZON_ITEMS_FIELDS = [
    "order_date", "order_id", "charge_amount", "charge_level",
    "product_name", "unit_price", "unit_tax", "item_total",
    "is_digital", "source_file",
]


def safe_float(s: str) -> float:
    """Parse a numeric string, handling commas and quoted negatives."""
    try:
        return float(str(s).replace(",", "").strip("'"))
    except (ValueError, AttributeError):
        return 0.0


def load_physical_orders(path: str, date_start: str, date_end: str) -> list:
    """Load Order History.csv and filter to date range."""
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if date_start <= r["Order Date"] <= date_end:
                rows.append(r)
    return rows


def load_digital_orders(path: str, date_start: str, date_end: str) -> list:
    """Load Digital Content Orders.csv and filter to date range."""
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if date_start <= r["Order Date"] <= date_end:
                rows.append(r)
    return rows


def build_physical_charges(phys_rows: list) -> list:
    """
    Build charge candidates from physical orders at multiple levels.

    Returns list of dicts with: date, charge_amount, charge_level, items, order_id
    Each 'items' entry has: product_name, unit_price, unit_tax, item_total
    """
    # Group by order ID
    orders = defaultdict(list)
    for r in phys_rows:
        orders[r["Order ID"]].append(r)

    charges = []

    for oid, items in orders.items():
        date = items[0]["Order Date"][:10]

        # Level 1: shipment charges (group by Total Amount)
        shipments = defaultdict(list)
        for item in items:
            shipments[item["Total Amount"]].append(item)

        shipment_totals = set()
        for total_str, ship_items in shipments.items():
            total = safe_float(total_str)
            if total <= 0:
                continue
            shipment_totals.add(total)
            parsed_items = []
            for i in ship_items:
                up = safe_float(i["Unit Price"])
                ut = safe_float(i["Unit Price Tax"])
                parsed_items.append({
                    "product_name": i["Product Name"],
                    "unit_price": up,
                    "unit_tax": ut,
                    "item_total": round(up + ut, 2),
                })
            charges.append({
                "date": date,
                "charge_amount": total,
                "charge_level": "shipment",
                "items": parsed_items,
                "order_id": oid,
            })

        # Level 2: order-level sum of unique shipment totals
        if len(shipment_totals) > 1:
            order_sum = round(sum(shipment_totals), 2)
            all_parsed = []
            for item in items:
                up = safe_float(item["Unit Price"])
                ut = safe_float(item["Unit Price Tax"])
                all_parsed.append({
                    "product_name": item["Product Name"],
                    "unit_price": up,
                    "unit_tax": ut,
                    "item_total": round(up + ut, 2),
                })
            charges.append({
                "date": date,
                "charge_amount": order_sum,
                "charge_level": "order_sum_shipments",
                "items": all_parsed,
                "order_id": oid,
            })

        # Level 3: order-level sum of item prices (when different from shipment sum)
        item_sum = round(sum(
            safe_float(i["Unit Price"]) + safe_float(i["Unit Price Tax"])
            for i in items
        ), 2)
        shipment_sum = round(sum(shipment_totals), 2)
        if len(items) > 1 and abs(item_sum - shipment_sum) > 0.10:
            all_parsed = []
            for item in items:
                up = safe_float(item["Unit Price"])
                ut = safe_float(item["Unit Price Tax"])
                all_parsed.append({
                    "product_name": item["Product Name"],
                    "unit_price": up,
                    "unit_tax": ut,
                    "item_total": round(up + ut, 2),
                })
            charges.append({
                "date": date,
                "charge_amount": item_sum,
                "charge_level": "order_sum_items",
                "items": all_parsed,
                "order_id": oid,
            })

    return charges


def build_digital_charges(dig_rows: list) -> list:
    """
    Build charge candidates from digital content orders.

    Each digital order has "Price Amount" and "Tax" component rows.
    The CC charge = sum of all Transaction Amounts for that Order ID.
    """
    # Group by Order ID
    orders = defaultdict(list)
    for r in dig_rows:
        orders[r["Order ID"]].append(r)

    charges = []
    for oid, rows in orders.items():
        total = round(sum(safe_float(r["Transaction Amount"]) for r in rows), 2)
        if abs(total) < 0.01:
            continue

        date = rows[0]["Order Date"][:10]
        price_rows = [r for r in rows if r["Component Type"] == "Price Amount"]
        items = []
        for r in price_rows:
            amt = safe_float(r["Transaction Amount"])
            items.append({
                "product_name": r["Product Name"],
                "unit_price": amt,
                "unit_tax": 0.0,
                "item_total": amt,
            })

        charges.append({
            "date": date,
            "charge_amount": total,
            "charge_level": "digital",
            "items": items,
            "order_id": oid,
        })

    return charges


def write_amazon_items(charges: list, output_path: str):
    """Write all charge candidates to amazon_items.csv."""
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=AMAZON_ITEMS_FIELDS)
        writer.writeheader()
        for charge in charges:
            is_digital = charge["charge_level"] == "digital"
            source = ("Digital Content Orders.csv" if is_digital
                      else "Order History.csv")
            for item in charge["items"]:
                writer.writerow({
                    "order_date": charge["date"],
                    "order_id": charge["order_id"],
                    "charge_amount": charge["charge_amount"],
                    "charge_level": charge["charge_level"],
                    "product_name": item["product_name"],
                    "unit_price": item["unit_price"],
                    "unit_tax": item["unit_tax"],
                    "item_total": item["item_total"],
                    "is_digital": is_digital,
                    "source_file": source,
                })


def main():
    directory = os.path.dirname(os.path.abspath(__file__))
    phys_path = os.path.join(directory, AMAZON_DATA_DIR, "Order History.csv")
    dig_path = os.path.join(directory, AMAZON_DATA_DIR,
                            "Digital Content Orders.csv")
    output_path = os.path.join(directory, "amazon_items.csv")

    # Date range matching our Goodbudget data
    date_start = "2024-12"
    date_end = "2026-03"

    if not os.path.exists(phys_path):
        print(f"Missing: {phys_path}")
        print("Extract Amazon data first: unzip 'Your Orders.zip' -d amazon_data")
        return

    phys_rows = load_physical_orders(phys_path, date_start, date_end)
    print(f"Physical order items loaded: {len(phys_rows)}")

    dig_rows = []
    if os.path.exists(dig_path):
        dig_rows = load_digital_orders(dig_path, date_start, date_end)
        print(f"Digital content items loaded: {len(dig_rows)}")

    phys_charges = build_physical_charges(phys_rows)
    dig_charges = build_digital_charges(dig_rows)
    all_charges = phys_charges + dig_charges

    print(f"\nCharge candidates built:")
    print(f"  Shipment-level:    {sum(1 for c in all_charges if c['charge_level'] == 'shipment')}")
    print(f"  Order sum shipments: {sum(1 for c in all_charges if c['charge_level'] == 'order_sum_shipments')}")
    print(f"  Order sum items:   {sum(1 for c in all_charges if c['charge_level'] == 'order_sum_items')}")
    print(f"  Digital:           {sum(1 for c in all_charges if c['charge_level'] == 'digital')}")
    print(f"  Total:             {len(all_charges)}")

    write_amazon_items(all_charges, output_path)

    # Count unique items
    total_items = sum(len(c["items"]) for c in all_charges)
    print(f"\nTotal item rows written: {total_items}")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    main()
