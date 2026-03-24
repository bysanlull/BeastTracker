"""
beast_tracker/dashboard.py
Local web dashboard for beast price + listing history.
Opens at http://localhost:5000

Usage:
    pip install flask
    python dashboard.py
"""

import sqlite3
import os
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, render_template_string, request

from poller import LISTING_DROP_PCT, PRICE_RISE_PCT, migrate_beast_snapshots_schema

DB_PATH = os.path.join(os.path.dirname(__file__), "beast_history.db")
# Display / conversion (league economy; adjust if needed)
CHAOS_PER_DIVINE = 265
# Softer tier for “storage” sellers — momentum building before full alert
WATCH_LISTING_DROP_PCT = 10
WATCH_PRICE_RISE_PCT = 3

app = Flask(__name__)


def compute_sell_momentum(prices: list, listings: list, seeded: list) -> dict:
    """
    Compare last two live (non-seeded) snapshots — same idea as poller buyout detection.
    Returns sell_signal: 'strong' | 'watch' | None, plus last-step % changes.
    """
    idx = [i for i, s in enumerate(seeded) if not s]
    if len(idx) < 2:
        return {
            "sell_signal": None,
            "listing_drop_pct": None,
            "price_rise_pct": None,
        }
    i0, i1 = idx[-2], idx[-1]
    p0, p1 = prices[i0], prices[i1]
    l0, l1 = listings[i0], listings[i1]
    if l0 <= 0 or p0 <= 0:
        return {
            "sell_signal": None,
            "listing_drop_pct": None,
            "price_rise_pct": None,
        }
    listing_drop = (l0 - l1) / l0 * 100
    price_rise = (p1 - p0) / p0 * 100
    ld = round(listing_drop, 1)
    pr = round(price_rise, 1)

    sig = None
    if listing_drop >= LISTING_DROP_PCT and price_rise >= PRICE_RISE_PCT:
        sig = "strong"
    elif listing_drop >= WATCH_LISTING_DROP_PCT and price_rise >= WATCH_PRICE_RISE_PCT:
        sig = "watch"

    return {
        "sell_signal": sig,
        "listing_drop_pct": ld,
        "price_rise_pct": pr,
    }

HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Beast Tracker</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Source+Sans+3:ital,wght@0,400;0,500;0,600;0,700;1,400&display=swap" rel="stylesheet">
  <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/chartjs-plugin-annotation/3.0.1/chartjs-plugin-annotation.min.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #12151c;
      color: #e4e6eb;
      font-family: "Source Sans 3", system-ui, "Segoe UI", Roboto, sans-serif;
      font-size: 15px;
      line-height: 1.5;
      font-weight: 400;
      -webkit-font-smoothing: antialiased;
    }

    header {
      padding: 14px 20px 16px;
      background: #1a1f28;
      border-bottom: 1px solid #2a3140;
      display: flex; align-items: center; gap: 14px 20px; flex-wrap: wrap;
    }
    header h1 {
      font-size: 17px; font-weight: 600;
      color: #f0f2f5;
      letter-spacing: -0.02em;
    }
    .header-rate {
      font-size: 12px;
      color: #9aa3b5;
      padding: 4px 10px;
      background: #222833;
      border: 1px solid #343c4d;
      border-radius: 6px;
    }
    .header-rate strong { color: #c8d0e0; font-weight: 600; }
    header select, header button {
      background: #222833;
      color: #e4e6eb;
      border: 1px solid #3d4659;
      border-radius: 6px;
      padding: 6px 11px;
      font: inherit;
      cursor: pointer;
    }
    header button:hover { background: #2a3140; border-color: #4a5568; }
    header label { font-size: 13px; color: #9aa3b5; }

    #alerts-section { margin: 14px 20px 0; display: none; }
    #alerts-section h2 {
      font-size: 13px; font-weight: 600;
      color: #e8a598;
      margin-bottom: 8px;
      display: flex; align-items: center; gap: 8px;
    }
    #alerts-section .hint {
      font-size: 11px; font-weight: normal; color: #7d8699;
    }
    .alert-dot {
      width: 8px; height: 8px; border-radius: 50%; background: #e07a65;
      display: inline-block; animation: pulse 1.2s ease-in-out infinite;
    }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.35} }

    .alert-table { width: 100%; border-collapse: collapse; font-size: 12px; }
    .alert-table th {
      text-align: left; padding: 6px 10px;
      color: #7d8699; border-bottom: 1px solid #2a3140;
      font-weight: 600;
    }
    .alert-table td { padding: 6px 10px; border-bottom: 1px solid #1e232e; }
    .alert-table tr:hover td { background: #1e232e; }
    .alert-name { color: #7eb8d6; font-weight: 600; }
    .alert-up   { color: #8fd49a; }
    .alert-down { color: #e8a598; }
    .alert-time { color: #6b7385; }

    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
      grid-auto-rows: 300px;
      gap: 14px; padding: 18px 20px 24px;
    }
    .card {
      background: #1a1f28;
      border: 1px solid #2a3140;
      border-radius: 10px; padding: 14px 14px 12px;
      height: 100%; display: flex; flex-direction: column;
    }
    .card.has-alert { border-color: #c75a45; box-shadow: 0 0 0 1px rgba(199, 90, 69, 0.35); }
    .card.sell-strong { border-color: #d4a43a; box-shadow: 0 0 0 1px rgba(212, 164, 58, 0.4); }
    .card.sell-watch { border-color: #5a8f7a; }
    .card-header {
      display: flex; justify-content: space-between;
      align-items: flex-start; gap: 8px; margin-bottom: 8px;
    }
    .card-name { font-size: 14px; font-weight: 600; color: #eef1f5; }
    .card-meta { font-size: 11px; color: #6b7385; white-space: nowrap; }
    .sell-badge {
      font-size: 11px; font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      padding: 4px 8px;
      border-radius: 5px;
      margin-bottom: 8px;
      line-height: 1.3;
    }
    .sell-badge.strong {
      background: linear-gradient(135deg, #3d3420 0%, #2a2518 100%);
      color: #f0d78c;
      border: 1px solid #a88432;
    }
    .sell-badge.watch {
      background: #1e2a24;
      color: #9cd4b0;
      border: 1px solid #3d5c4f;
    }
    .sell-badge .detail { font-weight: 500; text-transform: none; letter-spacing: 0; color: #b8c5d6; font-size: 10px; display: block; margin-top: 3px; }
    .stat-row {
      display: flex; justify-content: space-between;
      font-size: 12px; color: #9aa3b5; margin-bottom: 4px;
    }
    .stat-val           { font-weight: 600; color: #e4e6eb; font-variant-numeric: tabular-nums; }
    .stat-val.up        { color: #8fd49a; }
    .stat-val.down      { color: #e8a598; }
    .stat-val.flat      { color: #c9b87a; }
    .stat-sub { font-size: 11px; color: #6b7385; font-weight: 500; }
    .stat-hint {
      font-size: 10px; font-weight: 500; color: #6b7385;
      cursor: help;
      border-bottom: 1px dotted #4a5568;
    }
    .help-bar {
      margin: 0 20px 0;
      padding: 8px 12px;
      font-size: 12px;
      color: #8b95a8;
      background: #181c24;
      border: 1px solid #2a3140;
      border-radius: 6px;
      line-height: 1.4;
    }
    .seeded-note        { font-size: 10px; color: #5c6578; margin-top: 4px; }
    canvas              { margin-top: 8px; flex: 1; min-height: 0; }
    .empty              { grid-column:1/-1; text-align:center; padding:60px; color:#6b7385; font-size:14px; }
    .refresh-note       { text-align:center; padding:8px; font-size:11px; color:#5c6578; }
  </style>
</head>
<body>
<header>
  <h1>Beast Tracker</h1>
  <span class="header-rate"><strong>{{ chaos_per_divine }} chaos</strong> = 1 divine · farm prices in chaos</span>
  <label>Window:
    <select id="windowSel" onchange="loadAll()">
      <option value="3">3 hours</option>
      <option value="6">6 hours</option>
      <option value="12">12 hours</option>
      <option value="24" selected>24 hours</option>
      <option value="48">48 hours</option>
      <option value="168">7 days</option>
    </select>
  </label>
  <label>Chart:
    <select id="chartSel" onchange="loadAll()">
      <option value="price">Price (chaos)</option>
      <option value="listings">Listings</option>
    </select>
  </label>
  <button onclick="loadAll()">Refresh</button>
</header>

<p class="help-bar" title="How percentages work">
  <strong style="color:#c8d0e0;">Δ%</strong> is the change from the snapshot at or just before <strong>30 minutes ago</strong> to the <strong>latest</strong> point — good for spotting fast moves. Hover chart points for time, chaos, listings, and data source.
</p>

<div id="alerts-section">
  <h2><span class="alert-dot"></span> Sell alerts <span class="hint">listings fell fast + price rose — good time to list stock</span></h2>
  <table class="alert-table">
    <thead><tr><th>Time</th><th>Beast</th><th>Price</th><th>Listings</th></tr></thead>
    <tbody id="alert-rows"></tbody>
  </table>
</div>

<div class="grid" id="grid"><div class="empty">Loading...</div></div>
<div class="refresh-note">Auto-refreshes every 2 minutes · hover the chart line to inspect each snapshot</div>

<script>
const DIVINE_RATE = {{ chaos_per_divine }};
const DELTA_WINDOW_MS = 30 * 60 * 1000;

const C = {
  price:     '#5eb0e8',
  listings:  '#7dcea4',
  priceFill: 'rgba(94,176,232,0.14)',
  listFill:  'rgba(125,206,164,0.12)',
  seeded:    'rgba(100,110,128,0.75)',
  alert:     '#e07a65',
};

/** % change from snapshot at/before (latest − 30 min) → latest; catches rapid moves. */
function pctChange30m(timestamps, values) {
  const n = timestamps.length;
  if (n < 2) return { str: '—', cls: 'flat' };
  const lastMs = new Date(timestamps[n - 1] + 'Z').getTime();
  const anchor = lastMs - DELTA_WINDOW_MS;
  let refIdx = -1;
  for (let i = 0; i < n; i++) {
    const t = new Date(timestamps[i] + 'Z').getTime();
    if (t <= anchor) refIdx = i;
    else break;
  }
  if (refIdx < 0) refIdx = 0;
  const i1 = n - 1;
  if (refIdx === i1) return { str: '—', cls: 'flat' };
  const v0 = values[refIdx], v1 = values[i1];
  if (v0 == null || v1 == null || !isFinite(v0) || !isFinite(v1)) return { str: '—', cls: 'flat' };
  const p = ((v1 - v0) / (Math.abs(v0) || 1)) * 100;
  const str = (p >= 0 ? '+' : '') + p.toFixed(1) + '%';
  const cls = p > 0.5 ? 'up' : p < -0.5 ? 'down' : 'flat';
  return { str, cls };
}

function timeLabel(ts) {
  const d = new Date(ts + 'Z');
  const diffH = (Date.now() - d) / 3600000;
  return diffH < 20
    ? d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})
    : d.toLocaleDateString([], {month:'short', day:'numeric'});
}

async function loadAll() {
  const hours    = document.getElementById('windowSel').value;
  const chartKey = document.getElementById('chartSel').value;
  const [hRes, aRes] = await Promise.all([
    fetch(`/api/history?hours=${hours}`),
    fetch(`/api/alerts?hours=${hours}`)
  ]);
  const hData = await hRes.json();
  const aData = await aRes.json();
  renderAlerts(aData.alerts || []);
  renderCards(hData, aData.alerts || [], chartKey);
}

function renderAlerts(alerts) {
  const sec = document.getElementById('alerts-section');
  if (!alerts.length) { sec.style.display = 'none'; return; }
  sec.style.display = 'block';
  document.getElementById('alert-rows').innerHTML = alerts.map(a => `
    <tr>
      <td class="alert-time">${timeLabel(a.ts)}</td>
      <td class="alert-name">${a.beast_name}</td>
      <td><span class="alert-up">${a.prev_price}c &rarr; ${a.new_price}c (+${a.price_change_pct.toFixed(1)}%)</span></td>
      <td><span class="alert-down">${a.prev_listings.toLocaleString()} &rarr; ${a.new_listings.toLocaleString()} (&minus;${a.listing_drop_pct.toFixed(1)}%)</span></td>
    </tr>`).join('');
}

function tsIndex(timestamps, ts) {
  const j = timestamps.indexOf(ts);
  return j;
}

function renderCards(data, alerts, chartKey) {
  const grid = document.getElementById('grid');
  if (!data.beasts || !data.beasts.length) {
    grid.innerHTML = '<div class="empty">No data yet — wait for the poller to collect snapshots.</div>';
    return;
  }

  const alertedBeasts = new Set(alerts.map(a => a.beast_name));
  function sellRank(b) {
    const strong = alertedBeasts.has(b.name) || b.sell_signal === 'strong';
    const watch = !strong && b.sell_signal === 'watch';
    if (strong) return 0;
    if (watch) return 1;
    return 2;
  }
  data.beasts.sort((a, b) => {
    const ra = sellRank(a), rb = sellRank(b);
    if (ra !== rb) return ra - rb;
    return (b.latest_price || 0) - (a.latest_price || 0);
  });

  // Destroy all existing Chart instances
  Object.values(Chart.instances).forEach(c => c.destroy());
  grid.innerHTML = '';

  data.beasts.forEach(beast => {
    const labels   = beast.timestamps.map(timeLabel);
    const prices   = beast.prices;
    const listings = beast.listings;
    const seeded   = beast.seeded;
    const yData    = chartKey === 'price' ? prices : listings;
    const lineColor = chartKey === 'price' ? C.price : C.listings;
    const fillColor = chartKey === 'price' ? C.priceFill : C.listFill;
    const hasAlert  = alertedBeasts.has(beast.name);
    const hasSeed   = seeded.some(Boolean);

    const sig = beast.sell_signal;
    const strongSell = hasAlert || sig === 'strong';
    const watchSell = !strongSell && sig === 'watch';

    const latestPrice   = [...prices].reverse().find(v => v != null) ?? '—';
    const latestListing = [...listings].reverse().find(v => v != null) ?? '—';
    const divineStr = (typeof latestPrice === 'number' && beast.latest_divine != null)
      ? `≈ ${beast.latest_divine} div`
      : '';

    const dPrice = pctChange30m(beast.timestamps, prices);
    const dList  = pctChange30m(beast.timestamps, listings);

    let badgeHtml = '';
    if (strongSell) {
      const det = (beast.listing_drop_pct != null && beast.price_rise_pct != null)
        ? `Listings −${beast.listing_drop_pct}% · price +${beast.price_rise_pct}% (last live poll step)`
        : 'Sharp listings drop + price jump vs prior snapshot';
      badgeHtml = `<div class="sell-badge strong">Consider listing — sell pressure${hasAlert ? ' (logged)' : ''}<span class="detail">${det}</span></div>`;
    } else if (watchSell) {
      badgeHtml = `<div class="sell-badge watch">Watch — momentum building<span class="detail">Listings −${beast.listing_drop_pct}% · price +${beast.price_rise_pct}% (last step)</span></div>`;
    }

    const card = document.createElement('div');
    card.className = 'card'
      + (hasAlert ? ' has-alert' : '')
      + (strongSell ? ' sell-strong' : '')
      + (watchSell ? ' sell-watch' : '');
    card.innerHTML = `
      ${badgeHtml}
      <div class="card-header">
        <span class="card-name">${beast.name}</span>
        <span class="card-meta">${beast.snapshots} pts</span>
      </div>
      <div class="stat-row">
        <span>Price <span class="stat-hint" title="Change from the snapshot at or just before 30 minutes ago to the latest point (same clock as the chart).">Δ30m</span></span>
        <span class="stat-val ${dPrice.cls}">${typeof latestPrice === 'number' ? latestPrice + 'c' : latestPrice} <span class="stat-sub">${divineStr}</span> &nbsp;${dPrice.str}</span>
      </div>
      <div class="stat-row">
        <span>Listings <span class="stat-hint" title="Change in listing count over the same ~30 minute window as price.">Δ30m</span></span>
        <span class="stat-val ${dList.cls}">${typeof latestListing === 'number' ? latestListing.toLocaleString() : latestListing} &nbsp;${dList.str}</span>
      </div>
      ${hasSeed ? '<div class="seeded-note">Dashed line = seeded history (approx daily; poe.ninja sparkline)</div>' : ''}
      <canvas id="c-${beast.name.replace(/[^a-z0-9]/gi,'_')}"></canvas>
    `;
    grid.appendChild(card);

    const annotations = {};
    beast.alert_ts.forEach((ts, i) => {
      const idx = tsIndex(beast.timestamps, ts);
      if (idx < 0) return;
      annotations['a' + i] = {
        type: 'line',
        xMin: idx, xMax: idx,
        borderColor: C.alert,
        borderWidth: 2,
        borderDash: [4, 3],
        label: {
          display: true, content: 'sell alert',
          color: '#fde8e4', backgroundColor: 'rgba(40,30,28,0.92)',
          font: { size: 10, weight: '600' }, position: 'start',
        }
      };
    });

    const validY = yData.filter(v => v != null && isFinite(v) && v > 0);
    const yMin   = validY.length ? Math.min(...validY) : 0;
    const yMax   = validY.length ? Math.max(...validY) : 1;
    const yPad   = (yMax - yMin) * 0.1 || yMax * 0.05 || 1;

    const ctx = card.querySelector(`#c-${beast.name.replace(/[^a-z0-9]/gi,'_')}`).getContext('2d');
    new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          data: yData,
          borderColor: lineColor,
          backgroundColor: fillColor,
          segment: {
            borderColor: ctx => seeded[ctx.p0DataIndex] ? C.seeded : lineColor,
            borderDash:  ctx => seeded[ctx.p0DataIndex] ? [4, 3] : [],
          },
          borderWidth: 1.5,
          pointRadius: 0,
          pointHoverRadius: 6,
          pointHoverBorderWidth: 2,
          pointHoverBorderColor: lineColor,
          pointHoverBackgroundColor: '#1a1f28',
          hitRadius: 22,
          fill: true, tension: 0.3, spanGaps: true,
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: 'nearest', intersect: false, axis: 'x' },
        plugins: {
          legend: { display: false },
          annotation: { annotations },
          tooltip: {
            enabled: true,
            backgroundColor: 'rgba(22,26,34,0.96)',
            titleColor: '#eef1f5',
            bodyColor: '#9aa3b5',
            borderColor: '#3d4659',
            borderWidth: 1,
            padding: 12,
            displayColors: false,
            titleFont: { size: 13, weight: '600' },
            bodyFont: { size: 12 },
            callbacks: {
              title(items) {
                const i = items[0].dataIndex;
                const ts = beast.timestamps[i];
                return new Date(ts + 'Z').toLocaleString(undefined, {
                  weekday: 'short', month: 'short', day: 'numeric',
                  hour: '2-digit', minute: '2-digit', second: '2-digit'
                });
              },
              label(item) {
                const i = item.dataIndex;
                const pc = prices[i], lc = listings[i], sd = seeded[i];
                const div = (pc / DIVINE_RATE).toFixed(2);
                return [
                  'Chaos: ' + pc + 'c   (~' + div + ' div)',
                  'Listings: ' + (typeof lc === 'number' ? lc.toLocaleString() : lc),
                  sd ? 'Source: seeded (poe.ninja sparkline, approx daily)' : 'Source: live poll (poe.ninja)',
                ];
              }
            }
          }
        },
        scales: {
          x: { ticks: { color:'#6b7385', maxTicksLimit:6, font:{ family: "'Source Sans 3', sans-serif", size:10 } }, grid:{color:'#2a3140'} },
          y: {
            min: Math.floor(yMin - yPad),
            max: Math.ceil(yMax + yPad),
            ticks: { color:'#6b7385', font:{ family: "'Source Sans 3', sans-serif", size:10 }, maxTicksLimit:4 },
            grid: { color:'#2a3140' }
          }
        }
      }
    });
  });
}

loadAll();
setInterval(loadAll, 120_000);
</script>
</body>
</html>
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    migrate_beast_snapshots_schema(conn)
    conn.commit()
    return conn


@app.route("/")
def index():
    return render_template_string(HTML, chaos_per_divine=CHAOS_PER_DIVINE)


@app.route("/api/alerts")
def api_alerts():
    hours = int(request.args.get("hours", 24))
    since = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    ).isoformat(timespec="seconds")
    try:
        conn = get_conn()
    except Exception:
        return jsonify({"alerts": []}), 500
    rows = conn.execute("""
        SELECT ts, beast_name, prev_price, new_price, price_change_pct,
               prev_listings, new_listings, listing_drop_pct
        FROM buyout_alerts WHERE ts >= ? ORDER BY ts DESC
    """, (since,)).fetchall()
    return jsonify({"alerts": [dict(r) for r in rows]})


@app.route("/api/history")
def api_history():
    hours = int(request.args.get("hours", 24))
    since = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    ).isoformat(timespec="seconds")
    try:
        conn = get_conn()
    except Exception:
        return jsonify({"error": "DB not found — run poller.py first"}), 500

    rows = conn.execute("""
        SELECT beast_name, ts, chaos_value, listing_count, seeded
        FROM beast_snapshots WHERE ts >= ? ORDER BY beast_name, ts
    """, (since,)).fetchall()

    alert_rows = conn.execute("""
        SELECT beast_name, ts FROM buyout_alerts WHERE ts >= ? ORDER BY ts
    """, (since,)).fetchall()

    alert_map = {}
    for row in alert_rows:
        alert_map.setdefault(row["beast_name"], []).append(row["ts"])

    beasts = {}
    for row in rows:
        name = row["beast_name"]
        if name not in beasts:
            beasts[name] = {"timestamps": [], "prices": [], "listings": [], "seeded": []}
        beasts[name]["timestamps"].append(row["ts"])
        beasts[name]["prices"].append(round(row["chaos_value"], 1))
        beasts[name]["listings"].append(row["listing_count"])
        beasts[name]["seeded"].append(bool(row["seeded"]))

    result = []
    for name, d in beasts.items():
        mom = compute_sell_momentum(d["prices"], d["listings"], d["seeded"])
        latest = d["prices"][-1] if d["prices"] else None
        result.append({
            "name":         name,
            "snapshots":    len(d["timestamps"]),
            "latest_price": latest,
            "latest_divine": round(latest / CHAOS_PER_DIVINE, 2) if latest is not None else None,
            "timestamps":   d["timestamps"],
            "prices":       d["prices"],
            "listings":     d["listings"],
            "seeded":       d["seeded"],
            "alert_ts":     alert_map.get(name, []),
            **mom,
        })

    return jsonify({
        "beasts": result,
        "chaos_per_divine": CHAOS_PER_DIVINE,
    })


if __name__ == "__main__":
    print("Dashboard running at http://localhost:5000")
    app.run(debug=False, port=5000)
