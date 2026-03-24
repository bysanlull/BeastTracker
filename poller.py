"""
beast_tracker/poller.py
Polls poe.ninja every N minutes, stores beast price + listing count history in SQLite,
seeds 7-day daily history from sparkline on first run, and detects buyout events.

Usage:
    pip install requests
    python poller.py
    python poller.py --league Mirage --interval 3 --beasts "Vivid Vulture,Wild Hellion Alpha"
"""

import sqlite3
import requests
import time
import argparse
import os
from datetime import datetime, timedelta, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "beast_history.db")


def utc_now_naive() -> datetime:
    """UTC now as naive datetime (same string shape as legacy utcnow() for DB + JS)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
NINJA_URL = "https://poe.ninja/api/data/itemoverview?league={league}&type=Beast"


def migrate_beast_snapshots_schema(conn):
    """Add `seeded` column to DBs created before that column existed."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(beast_snapshots)").fetchall()}
    if cols and "seeded" not in cols:
        conn.execute(
            "ALTER TABLE beast_snapshots ADD COLUMN seeded INTEGER NOT NULL DEFAULT 0"
        )

# Buyout detection thresholds
LISTING_DROP_PCT  = 20   # listings must fall by at least this % vs previous snapshot
PRICE_RISE_PCT    =  5   # price must rise by at least this % vs previous snapshot
# (both must trigger together to flag a buyout)

DEFAULT_BEASTS = [
    "Vivid Vulture",
    "Wild Hellion Alpha",
    "Wild Bristle Matron",
    "Wild Brambleback",
    "Craicic Chimeral",
    "Black Mórrigan",
    "Fenumal Plagued Arachnid",
    "Primal Crushclaw",
]


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS beast_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT    NOT NULL,
            beast_name    TEXT    NOT NULL,
            chaos_value   REAL    NOT NULL,
            listing_count INTEGER NOT NULL,
            divine_value  REAL,
            seeded        INTEGER NOT NULL DEFAULT 0  -- 1 = from sparkline, 0 = live poll
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS buyout_alerts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT NOT NULL,
            beast_name      TEXT NOT NULL,
            prev_price      REAL NOT NULL,
            new_price       REAL NOT NULL,
            price_change_pct REAL NOT NULL,
            prev_listings   INTEGER NOT NULL,
            new_listings    INTEGER NOT NULL,
            listing_drop_pct REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_name_ts ON beast_snapshots(beast_name, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_ts ON buyout_alerts(ts)")
    migrate_beast_snapshots_schema(conn)
    conn.commit()


def fetch_ninja(league: str) -> list[dict]:
    url = NINJA_URL.format(league=league)
    try:
        r = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "beast-tracker/1.0"},
            trust_env=False,
        )
    except requests.exceptions.ProxyError as e:
        raise RuntimeError(
            "Outbound HTTPS failed (proxy). PythonAnywhere free accounts only allow "
            "requests to whitelisted sites; poe.ninja is not available on the free tier. "
            "Use a paid PythonAnywhere plan, or run the poller on your PC / Render / a VPS. "
            "https://help.pythonanywhere.com/pages/RestrictedInternet/"
        ) from e
    r.raise_for_status()
    return r.json().get("lines", [])


def seed_sparkline(conn, lines: list[dict], tracked: set[str]):
    """
    On first run, back-fill 7 daily data points from poe.ninja's sparkline.
    sparkline.data is a list of up to 7 floats representing % change from
    the first day — we reconstruct approximate absolute prices from them
    and insert them as daily-spaced seeded rows.
    We can't reconstruct listing counts from sparkline, so we use 0 as placeholder.
    Skips seeding if any data already exists for a beast.
    """
    now = utc_now_naive()
    seeded_count = 0

    for line in lines:
        name = line.get("name", "")
        if name not in tracked:
            continue

        existing = conn.execute(
            "SELECT COUNT(*) FROM beast_snapshots WHERE beast_name = ?", (name,)
        ).fetchone()[0]
        if existing > 0:
            continue  # already have data, skip

        current_price = line.get("chaosValue", 0.0)
        spark_data = line.get("lowConfidenceSparkLine", {}).get("data", [])
        # spark_data[i] = % change relative to spark_data[0] (which is 0)
        # so index 0 = oldest day, last = most recent (= ~current price)
        if not spark_data or len(spark_data) < 2:
            continue

        # Reconstruct absolute prices: last point ≈ current_price
        # spark_data values are cumulative % changes from day 0
        # day0_price * (1 + spark[-1]/100) ≈ current_price
        last_pct = spark_data[-1] if spark_data[-1] is not None else 0
        try:
            day0_price = current_price / (1 + last_pct / 100)
        except ZeroDivisionError:
            day0_price = current_price

        rows = []
        n = len(spark_data)
        for i, pct_change in enumerate(spark_data):
            if pct_change is None:
                continue
            days_ago = n - 1 - i
            ts = (now - timedelta(days=days_ago)).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat(timespec="seconds")
            approx_price = round(day0_price * (1 + pct_change / 100), 1)
            rows.append((ts, name, approx_price, 0, None, 1))

        if rows:
            conn.executemany(
                "INSERT INTO beast_snapshots "
                "(ts, beast_name, chaos_value, listing_count, divine_value, seeded) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows
            )
            seeded_count += 1

    conn.commit()
    if seeded_count:
        print(f"[seed] Back-filled sparkline data for {seeded_count} beasts (daily, approx prices)")


def get_previous_snapshot(conn, beast_name: str) -> dict | None:
    """Return the most recent live (non-seeded) snapshot for a beast."""
    row = conn.execute("""
        SELECT chaos_value, listing_count FROM beast_snapshots
        WHERE beast_name = ? AND seeded = 0
        ORDER BY ts DESC LIMIT 1
    """, (beast_name,)).fetchone()
    if row:
        return {"price": row[0], "listings": row[1]}
    return None


def detect_buyout(conn, ts: str, name: str, new_price: float, new_listings: int):
    """Compare against previous snapshot and insert alert if thresholds are met."""
    prev = get_previous_snapshot(conn, name)
    if not prev or prev["listings"] == 0:
        return

    listing_drop = (prev["listings"] - new_listings) / prev["listings"] * 100
    price_rise   = (new_price - prev["price"]) / prev["price"] * 100 if prev["price"] > 0 else 0

    if listing_drop >= LISTING_DROP_PCT and price_rise >= PRICE_RISE_PCT:
        conn.execute("""
            INSERT INTO buyout_alerts
            (ts, beast_name, prev_price, new_price, price_change_pct,
             prev_listings, new_listings, listing_drop_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (ts, name, prev["price"], new_price, round(price_rise, 1),
              prev["listings"], new_listings, round(listing_drop, 1)))
        print(f"  *** BUYOUT ALERT: {name} | "
              f"listings {prev['listings']} -> {new_listings} ({listing_drop:.0f}% drop) | "
              f"price {prev['price']}c -> {new_price}c (+{price_rise:.0f}%)")


def store_snapshot(conn, ts: str, lines: list[dict], tracked: set[str]):
    rows = []
    for line in lines:
        name = line.get("name", "")
        if name not in tracked:
            continue
        chaos    = line.get("chaosValue", 0.0)
        count    = line.get("listingCount", 0)
        divine   = line.get("divineValue", None)

        detect_buyout(conn, ts, name, chaos, count)
        rows.append((ts, name, chaos, count, divine, 0))

    if rows:
        conn.executemany(
            "INSERT INTO beast_snapshots "
            "(ts, beast_name, chaos_value, listing_count, divine_value, seeded) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows
        )
        conn.commit()
    return rows


def poll_loop(league: str, interval_minutes: int, tracked: set[str]):
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    print(f"[beast_tracker] League: {league}")
    print(f"[beast_tracker] Polling every {interval_minutes} min")
    print(f"[beast_tracker] Tracking: {', '.join(sorted(tracked))}")
    print(f"[beast_tracker] DB: {DB_PATH}")
    print(f"[beast_tracker] Buyout thresholds: "
          f"listings drop >={LISTING_DROP_PCT}% AND price rise >={PRICE_RISE_PCT}%")
    print("[beast_tracker] Press Ctrl+C to stop.\n")

    # First fetch — seed sparkline history then store live snapshot
    first_run = True

    while True:
        try:
            ts = utc_now_naive().isoformat(timespec="seconds")
            lines = fetch_ninja(league)

            if first_run:
                seed_sparkline(conn, lines, tracked)
                first_run = False

            rows = store_snapshot(conn, ts, lines, tracked)
            print(f"[{ts}] Stored {len(rows)} snapshots")
            for _, name, chaos, count, _, _ in rows:
                print(f"  {name:35s}  {chaos:7.1f}c  {count:5d} listings")

        except requests.RequestException as e:
            print(f"[{utc_now_naive().isoformat()}] Fetch error: {e}")
        except Exception as e:
            print(f"[{utc_now_naive().isoformat()}] Unexpected error: {e}")

        time.sleep(interval_minutes * 60)


def poll_once(league: str, tracked: set[str]) -> int:
    """
    Single fetch + optional sparkline seed + store. Use with --once on hosts
    that only allow scheduled jobs (e.g. PythonAnywhere), not a long-running loop.
    """
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    try:
        ts = utc_now_naive().isoformat(timespec="seconds")
        lines = fetch_ninja(league)
        seed_sparkline(conn, lines, tracked)
        rows = store_snapshot(conn, ts, lines, tracked)
        print(f"[{ts}] Stored {len(rows)} snapshots")
        for _, name, chaos, count, _, _ in rows:
            print(f"  {name:35s}  {chaos:7.1f}c  {count:5d} listings")
        return len(rows)
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="poe.ninja beast tracker")
    parser.add_argument("--league", default=os.environ.get("LEAGUE", "Mirage"))
    parser.add_argument(
        "--interval",
        default=int(os.environ.get("POLL_INTERVAL", "3")),
        type=int,
        help="Poll interval in minutes (env POLL_INTERVAL)",
    )
    parser.add_argument("--once", action="store_true",
                        help="Run one poll then exit (for cron / PythonAnywhere scheduled tasks)")
    parser.add_argument("--beasts",   default=None,
                        help="Comma-separated beast names (default: built-in list)")
    args = parser.parse_args()

    tracked_beasts = (
        {b.strip() for b in args.beasts.split(",")} if args.beasts
        else set(DEFAULT_BEASTS)
    )
    if args.once:
        poll_once(args.league, tracked_beasts)
    else:
        poll_loop(args.league, args.interval, tracked_beasts)
