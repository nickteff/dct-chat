"""Build the small synthetic demo database (workspace/data/examples.duckdb).

Deterministic: the same seed always produces the same rows, so the committed file can be
regenerated at will. Nothing here is real data.

    uv run python scripts/build_demo_db.py
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import duckdb

OUT = Path(__file__).resolve().parent.parent / "workspace" / "data" / "examples.duckdb"
rng = random.Random(42)

REGIONS = {"North": 1.0, "South": 0.8, "East": 1.15, "West": 0.95}
PRODUCTS = {  # product -> (category, typical unit price)
    "Widget A": ("Electronics", 29),
    "Widget B": ("Electronics", 49),
    "Widget C": ("Electronics", 19),
    "Gadget X": ("Accessories", 24),
    "Gadget Y": ("Accessories", 35),
    "Gadget Z": ("Accessories", 15),
    "Tool A": ("Tools", 39),
    "Tool Z": ("Tools", 59),
}
START, DAYS = date(2024, 1, 1), 182  # January to June 2024


def orders() -> list[tuple]:
    rows = []
    for _ in range(600):
        day = START + timedelta(days=rng.randrange(DAYS))
        region = rng.choices(list(REGIONS), weights=REGIONS.values())[0]
        product = rng.choice(list(PRODUCTS))
        category, price = PRODUCTS[product]
        growth = 1 + (day - START).days / DAYS * 0.35  # sales climb through the half-year
        units = max(1, round(rng.gauss(30, 12) * growth))
        rows.append((day, region, product, category, round(units * price * rng.uniform(0.9, 1.1)), units))
    return sorted(rows)


def signups() -> list[tuple]:
    rows = []
    for month in range(1, 7):
        for channel, base in (("Organic", 220), ("Paid", 150), ("Referral", 90), ("Partner", 60)):
            n = round(base * (1 + month * 0.06) * rng.uniform(0.85, 1.15))
            rows.append((date(2024, month, 1), channel, n, round(n * rng.uniform(0.08, 0.2))))
    return rows


def tickets() -> list[tuple]:
    rows = []
    for i in range(300):
        created = START + timedelta(days=rng.randrange(DAYS))
        priority = rng.choices(["Low", "Medium", "High"], weights=[5, 3, 1.5])[0]
        hours = round(rng.expovariate(1 / {"Low": 30, "Medium": 14, "High": 5}[priority]), 1)
        rows.append((i + 1, created, priority, rng.choice(["Billing", "Bug", "How-to", "Account"]), hours, rng.random() > 0.15))
    return rows


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.unlink(missing_ok=True)
    con = duckdb.connect(str(OUT))
    con.execute("CREATE TABLE ecommerce_orders (date DATE, region VARCHAR, product VARCHAR, category VARCHAR, revenue BIGINT, units_sold BIGINT)")
    con.executemany("INSERT INTO ecommerce_orders VALUES (?,?,?,?,?,?)", orders())
    con.execute("CREATE TABLE signups (month DATE, channel VARCHAR, signups BIGINT, conversions BIGINT)")
    con.executemany("INSERT INTO signups VALUES (?,?,?,?)", signups())
    con.execute("CREATE TABLE support_tickets (ticket_id BIGINT, created_at DATE, priority VARCHAR, topic VARCHAR, hours_to_resolve DOUBLE, resolved BOOLEAN)")
    con.executemany("INSERT INTO support_tickets VALUES (?,?,?,?,?,?)", tickets())
    for table in ("ecommerce_orders", "signups", "support_tickets"):
        print(table, con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], "rows")
    con.execute("CHECKPOINT")
    con.close()
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
