#!/usr/bin/env python3
"""
fetch_idx_data.py — pull real IDX market data into the research terminal.

    pip install yfinance feedparser
    python fetch_idx_data.py

This does two things:
  1. Writes idx_data.json (the raw data file).
  2. If idx-research-terminal.html sits in the same folder, it also builds
     idx-research-terminal-LIVE.html with the data baked directly into the
     file. Double-click that one — no Load button, no server, real data.

Run it again any time to refresh, or keep it refreshing on its own:

    python fetch_idx_data.py --loop 15      # re-fetch every 15 minutes

If you instead serve the folder over HTTP (python -m http.server) and open
the normal terminal file, it auto-loads idx_data.json and re-checks it every
15 minutes, so a running --loop keeps the open page current too.

ABOUT TIMING
------------
When you run this during trading hours (09:00–16:00 WIB), the final price
point is Yahoo's intraday quote for IDX, which is delayed — treat it as at
least 15 minutes behind the market, since Yahoo does not publish the exact
lag. Run after the close and you get the official closing price. Either way
this is a periodic snapshot, not a streaming feed; the --loop mode is what
approximates a delayed live terminal.

WHAT THIS DOES AND DOES NOT DO
------------------------------
Prices come from Yahoo Finance, which carries IDX tickers with a ".JK"
suffix. This is END-OF-DAY data and it is NOT real time. Intraday quotes
from IDX require a licensed feed; a fifteen-minute delay is the normal
tier for anyone without one. The terminal labels everything accordingly
and you should never claim otherwise.

Yahoo is a convenience source, not an official one, and its terms permit
personal use only. For anything you publish or sell, license the data
properly — IDX sells real-time, delayed and end-of-day products directly,
and several Indonesian vendors resell it with a documented API.

Fundamentals here are best-effort. Yahoo's coverage of IDX financials is
patchy and occasionally wrong. Every field this script cannot verify is
left alone so the terminal keeps its own value rather than showing a
confident number that happens to be false. Check anything that matters
against the company's own filings before you put your name on it.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

WIB = timezone(timedelta(hours=7))

# The universe the terminal ships with. Keep in sync with the U object.
TICKERS = [
    "BBCA", "BBRI", "BMRI", "BBNI", "BRIS", "ARTO",
    "TLKM", "EXCL", "ISAT", "JSMR", "TOWR",
    "ASII", "UNTR",
    "ANTM", "MDKA", "INCO", "INTP", "SMGR", "BRPT",
    "ADRO", "PTBA", "PGAS", "MEDC", "AADI",
    "ICBP", "INDF", "UNVR", "MYOR", "AMRT", "CPIN",
    "KLBF", "MIKA", "SIDO",
    "GOTO", "BUKA", "EMTK",
    "MAPI", "ACES", "ERAA",
    "CTRA", "BSDE", "PWON",
]

# Indonesian market news via public RSS. Add or replace freely.
# Indonesian market news. Fetched with a browser user-agent, because most of
# these publishers reject the default one a Python library sends — which is
# exactly why the first version of this script came back with zero headlines
# from every Indonesian source and only stale Yahoo items.
NEWS_FEEDS = [
    ("https://www.cnbcindonesia.com/market/rss", "CNBC Indonesia"),
    ("https://www.cnbcindonesia.com/rss", "CNBC Indonesia"),
    ("https://finance.detik.com/rss", "detikFinance"),
    ("https://rss.detik.com/index.php/finance", "detikFinance"),
    ("https://www.antaranews.com/rss/ekonomi.xml", "Antara"),
    ("https://www.cnnindonesia.com/ekonomi/rss", "CNN Indonesia"),
    ("https://investasi.kontan.co.id/rss", "Kontan"),
    ("https://www.kontan.co.id/rss", "Kontan"),
    ("https://bisnis.tempo.co/rss", "Tempo Bisnis"),
    ("https://www.liputan6.com/rss/bisnis", "Liputan6 Bisnis"),
]

# Google News RSS is a reliable fallback that rarely blocks scripts. These
# queries are scoped to Indonesian market coverage.
GOOGLE_NEWS = [
    ("IHSG saham", "Market"),
    ("Bursa Efek Indonesia emiten", "Market"),
    ("Bank Indonesia suku bunga", "Macro"),
]
GOOGLE_NEWS_TICKERS = 8      # per-ticker Google News searches for top names

# A research terminal should show market news, not everything a publisher
# prints. Headlines must mention a covered ticker or one of these terms;
# the rest (lifestyle, gadgets, general crime) are dropped.
NEWS_KEYWORDS = [
    "IHSG", "BEI", "BURSA", "SAHAM", "EMITEN", "IPO", "RUPS", "DIVIDEN",
    "OBLIGASI", "SUKU BUNGA", "BANK INDONESIA", " BI ", "RUPIAH", "INFLASI",
    "OJK", "INVESTOR", "ASING", "REKSA DANA", "LQ45", "KURS", "EKONOMI",
    "PERTUMBUHAN", "APBN", "EKSPOR", "IMPOR", "KOMODITAS", "BATU BARA",
    "NIKEL", "EMAS", "MIGAS", "LABA", "KINERJA", "AKUISISI", "MERGER",
]

YAHOO_NEWS_TICKERS = 12      # per-ticker Yahoo headlines for the N most-traded names

LOOKBACK_DAYS = 760          # ~2 trading years
MAX_HEADLINES = 60
ADV_WINDOW = 63              # days for average traded value
FF_SESSIONS = 30             # foreign-flow sessions to attempt from IDX

# Rough mapping from Yahoo sector names to the terminal's IDX-style sectors,
# used for stocks you add via my_stocks.txt.
SECTOR_MAP = {
    "Financial Services": "Financials", "Basic Materials": "Basic Materials",
    "Energy": "Energy", "Consumer Defensive": "Consumer Non-Cyc",
    "Consumer Cyclical": "Consumer Cyclicals", "Healthcare": "Healthcare",
    "Technology": "Technology", "Communication Services": "Infrastructures",
    "Industrials": "Industrials", "Real Estate": "Properties",
    "Utilities": "Infrastructures",
}


def read_my_stocks():
    """Extra tickers queued from the terminal's Watchlist tab."""
    extra = []
    if os.path.exists("my_stocks.txt"):
        for line in open("my_stocks.txt", encoding="utf-8"):
            t = line.strip().upper().replace(".JK", "")
            if t and t.isalpha() and 2 < len(t) < 6 and t not in TICKERS and t not in extra:
                extra.append(t)
        if extra:
            log(f"my_stocks.txt: adding {', '.join(extra)}")
    return extra


def log(msg):
    print(f"  {msg}", flush=True)


def fetch_prices(tickers):
    """Daily closes for the universe plus the IHSG index."""
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("yfinance is not installed.  Run:  pip install yfinance feedparser")

    symbols = [f"{t}.JK" for t in tickers] + ["^JKSE"]
    start = (datetime.now(WIB) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    log(f"downloading {len(symbols)} symbols from {start} …")
    raw = yf.download(
        symbols, start=start, interval="1d",
        auto_adjust=False, progress=False, group_by="ticker", threads=True,
    )
    if raw is None or raw.empty:
        sys.exit("No price data returned. Check your internet connection and try again.")

    # Build a common trading calendar from the index itself.
    try:
        idx_close = raw["^JKSE"]["Close"].dropna()
    except Exception:
        sys.exit("Could not read the IHSG series (^JKSE).")

    dates = [d.strftime("%Y-%m-%d") for d in idx_close.index]
    ihsg = [round(float(v), 2) for v in idx_close.values]

    prices, adv, skipped = {}, {}, []
    for t in tickers:
        try:
            s = raw[f"{t}.JK"]["Close"].reindex(idx_close.index).ffill().dropna()
        except Exception:
            skipped.append(t)
            continue
        # Only keep names covering the full window; partial series would
        # silently distort every return and multiple downstream.
        if len(s) < len(idx_close) * 0.9:
            skipped.append(t)
            continue
        filled = s.reindex(idx_close.index).ffill()
        prices[t] = [round(float(v)) for v in filled.values]
        # Real average daily traded value (close × volume), IDR bn — this is
        # what orders the ticker tape and feeds the liquidity score.
        try:
            vol = raw[f"{t}.JK"]["Volume"].reindex(idx_close.index)
            val = (filled * vol).dropna().tail(ADV_WINDOW)
            if len(val) > 10:
                adv[t] = round(float(val.mean()) / 1e9, 1)
        except Exception:
            pass

    log(f"got {len(prices)} price series over {len(dates)} trading days")
    if skipped:
        log(f"skipped (insufficient history): {', '.join(skipped)}")
    return dates, ihsg, prices, adv


def fetch_fundamentals(tickers, extras):
    """Best-effort fundamentals. Missing fields are omitted, never guessed.
    For user-added tickers we also need a name, sector and share count so the
    terminal can create a universe entry."""
    import yfinance as yf

    out = {}
    log("fetching fundamentals (this is the slow part) …")
    for i, t in enumerate(tickers, 1):
        try:
            info = yf.Ticker(f"{t}.JK").info or {}
        except Exception:
            continue

        rec = {}
        # Shares outstanding, in millions — the terminal's unit.
        so = info.get("sharesOutstanding")
        if so and so > 0:
            rec["sh"] = round(so / 1e6)

        pe = info.get("trailingPE")
        if pe and 0 < pe < 300:
            rec["pe"] = round(float(pe), 2)

        pb = info.get("priceToBook")
        if pb and 0 < pb < 60:
            rec["pb"] = round(float(pb), 2)

        dy = info.get("dividendYield")
        if dy is not None:
            # Yahoo has returned this both as a fraction and as a percent.
            dyv = float(dy) * 100 if float(dy) < 1 else float(dy)
            if 0 <= dyv < 40:
                rec["dy"] = round(dyv, 2)

        roe = info.get("returnOnEquity")
        if roe is not None and -1 < float(roe) < 2:
            rec["roe"] = round(float(roe) * 100, 2)

        beta = info.get("beta")
        if beta and 0.1 < float(beta) < 3:
            rec["beta"] = round(float(beta), 2)

        # Free cash flow, converted to IDR billion.
        fcf = info.get("freeCashflow")
        if fcf:
            rec["fcf"] = round(float(fcf) / 1e9)

        pm = info.get("profitMargins")
        if pm is not None and -2 < float(pm) < 1:
            rec["nm"] = round(float(pm) * 100, 2)

        rg = info.get("revenueGrowth")
        if rg is not None and -1 < float(rg) < 5:
            rec["rg"] = round(float(rg) * 100, 2)

        eg = info.get("earningsGrowth")
        if eg is not None and -5 < float(eg) < 10:
            rec["eg"] = round(float(eg) * 100, 2)

        if t in extras:
            name = info.get("longName") or info.get("shortName")
            if name:
                rec["n"] = str(name).replace(" Tbk", "").replace(" PT ", " ").strip()
            ys = info.get("sector")
            rec["s"] = SECTOR_MAP.get(ys, ys or "Other")
            rec["sub"] = (info.get("industry") or "—")[:24]
            de = info.get("debtToEquity")
            if de is not None and 0 <= float(de) < 1000:
                rec["de"] = round(float(de) / 100, 2)
            po = info.get("payoutRatio")
            if po is not None and 0 <= float(po) < 2:
                rec["payout"] = round(float(po) * 100)
            if "n" not in rec or "sh" not in rec:
                log(f"  {t}: Yahoo has no name/share count — cannot add it, skipping")
                out.pop(t, None)
                continue
        if rec:
            out[t] = rec
        if i % 10 == 0:
            log(f"  … {i}/{len(tickers)}")

    log(f"fundamentals for {len(out)} names (fields Yahoo could not confirm are left to the terminal's own values)")
    return out


def fetch_yahoo_news(tickers, adv):
    """Per-ticker headlines from Yahoo Finance for the most-traded names."""
    import yfinance as yf
    ranked = sorted([t for t in tickers if t in adv], key=lambda t: -adv[t])[:YAHOO_NEWS_TICKERS]
    items = []
    for t in ranked:
        try:
            for e in (yf.Ticker(f"{t}.JK").news or [])[:3]:
                c = e.get("content") or e
                title = (c.get("title") or "").strip()
                if not title:
                    continue
                when = c.get("pubDate") or ""
                date = when[:10] if len(when) >= 10 else datetime.now(WIB).strftime("%Y-%m-%d")
                url = ((c.get("clickThroughUrl") or {}).get("url")
                       or (c.get("canonicalUrl") or {}).get("url") or e.get("link", ""))
                items.append({"d": date, "t": title, "src": "Yahoo Finance",
                              "tk": [t], "cat": "Company", "url": url})
        except Exception:
            continue
    log(f"Yahoo Finance: {len(items)} company headlines across {len(ranked)} names")
    return items


def fetch_foreign_flow(dates):
    """BEST-EFFORT net foreign flow from IDX's own public daily trading
    summary. This endpoint sits behind protection that sometimes blocks
    scripted access; when that happens this returns nothing and the terminal
    honestly shows the flow panel as unavailable. When it does work, verify
    a day or two against the figures IDX publishes before trusting the units
    — the summary reports foreign buy/sell per stock and this sums the net
    across the market."""
    try:
        import requests
    except ImportError:
        log("foreign flow: requests not available — skipped")
        return None
    ses = requests.Session()
    ses.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Referer": "https://www.idx.co.id/",
        "Accept": "application/json",
    })
    got = {}
    for d in dates[-FF_SESSIONS:]:
        url = ("https://www.idx.co.id/primary/TradingSummary/GetStockSummary"
               f"?length=9999&start=0&date={d.replace('-', '')}")
        try:
            r = ses.get(url, timeout=10)
            if r.status_code != 200:
                continue
            rows = (r.json() or {}).get("data") or []
            net, seen = 0.0, False
            for row in rows:
                fb = row.get("ForeignBuy") or row.get("foreignBuy") or 0
                fs = row.get("ForeignSell") or row.get("foreignSell") or 0
                if fb or fs:
                    seen = True
                net += float(fb) - float(fs)
            if seen:
                got[d] = net
        except Exception:
            continue
        time.sleep(0.4)
    if len(got) < 5:
        log("foreign flow: IDX endpoint blocked or empty — the panel will say so honestly")
        return None
    log(f"foreign flow: {len(got)} sessions from the IDX daily summary "
        "(spot-check the units against IDX publications before relying on it)")
    return [got.get(d) for d in dates]


def _browser_get(url, timeout=12):
    """GET with a browser user-agent. Publishers commonly reject the default
    agent a Python HTTP library sends, so identifying as a browser is the
    difference between a full feed and an empty one."""
    try:
        import requests
    except ImportError:
        return None
    try:
        r = requests.get(url, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
            "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
        })
        return r.content if r.status_code == 200 else None
    except Exception:
        return None


def _parse_feed(url):
    """Parse a feed, preferring the browser-agent fetch."""
    try:
        import feedparser
    except ImportError:
        return []
    raw = _browser_get(url)
    try:
        feed = feedparser.parse(raw) if raw else feedparser.parse(url)
        return feed.entries or []
    except Exception:
        return []


def _is_relevant(title, tagged):
    """Keep only market-relevant headlines."""
    if tagged:
        return True
    u = " " + title.upper() + " "
    return any(k in u for k in NEWS_KEYWORDS)


def _entry_to_item(entry, source, category, tickers):
    title = (entry.get("title") or "").strip()
    if not title:
        return None
    when = entry.get("published_parsed") or entry.get("updated_parsed")
    date = (datetime(*when[:6]).strftime("%Y-%m-%d") if when
            else datetime.now(WIB).strftime("%Y-%m-%d"))
    upper = title.upper()
    tagged = [t for t in tickers if t in upper]
    if not _is_relevant(title, tagged):
        return None
    return {"d": date, "t": title, "src": source, "tk": tagged,
            "cat": category, "url": entry.get("link", "")}


def fetch_news(tickers, adv):
    """Indonesian market headlines from publisher RSS plus Google News."""
    items, seen = [], set()
    ok_sources = set()

    def add(entry, source, category):
        it = _entry_to_item(entry, source, category, tickers)
        if not it:
            return False
        key = it["t"].lower()[:80]
        if key in seen:
            return False
        seen.add(key)
        items.append(it)
        return True

    for url, source in NEWS_FEEDS:
        entries = _parse_feed(url)
        n = sum(1 for e in entries[:30] if add(e, source, "Market"))
        if n:
            ok_sources.add(source)
            log(f"{source}: {n} headlines")

    # Google News fallback — general market topics
    from urllib.parse import quote_plus
    for query, category in GOOGLE_NEWS:
        url = ("https://news.google.com/rss/search?q=" + quote_plus(query)
               + "&hl=id&gl=ID&ceid=ID:id")
        entries = _parse_feed(url)
        n = 0
        for e in entries[:12]:
            src_name = "Google News"
            # Google prefixes the publisher onto the title after a dash.
            if " - " in (e.get("title") or ""):
                src_name = e["title"].rsplit(" - ", 1)[-1][:24]
            if add(e, src_name, category):
                n += 1
        if n:
            log(f"Google News '{query}': {n} headlines")

    # Google News per-ticker for the most-traded names
    ranked = sorted([t for t in tickers if t in adv], key=lambda t: -adv[t])[:GOOGLE_NEWS_TICKERS]
    for t in ranked:
        url = ("https://news.google.com/rss/search?q=" + quote_plus(f"{t} saham")
               + "&hl=id&gl=ID&ceid=ID:id")
        for e in _parse_feed(url)[:3]:
            src_name = "Google News"
            if " - " in (e.get("title") or ""):
                src_name = e["title"].rsplit(" - ", 1)[-1][:24]
            it = _entry_to_item(e, src_name, "Company", tickers)
            if it:
                key = it["t"].lower()[:80]
                if key not in seen:
                    seen.add(key)
                    if t not in it["tk"]:
                        it["tk"].append(t)
                    items.append(it)

    if not ok_sources:
        log("WARNING: every publisher RSS feed came back empty — relying on "
            "Google News only. Feed URLs change; check them if this persists.")

    items.sort(key=lambda x: x["d"], reverse=True)
    log(f"news total: {len(items)} headlines, newest {items[0]['d'] if items else 'none'}")
    return items[:MAX_HEADLINES]


def build_live_html(payload):
    """Bake the data into a standalone HTML file so it opens with real data
    directly from disk — no server, no manual load step."""
    import os
    src_html = "idx-research-terminal.html"
    if not os.path.exists(src_html):
        log("idx-research-terminal.html not found here — skipped the LIVE build")
        return False
    html = open(src_html, encoding="utf-8").read()
    inject = "<body>\n<script>window.__IDX_DATA__=" + json.dumps(
        payload, separators=(",", ":")) + ";</script>"
    if "<body>" not in html:
        log("could not find <body> in the terminal file — skipped the LIVE build")
        return False
    out = html.replace("<body>", inject, 1)
    with open("idx-research-terminal-LIVE.html", "w", encoding="utf-8") as f:
        f.write(out)
    return True


def load_previous():
    """Fundamentals from the last full run, so quick refreshes can reuse them
    instead of re-downloading a slow dataset that barely changes intraday."""
    try:
        with open("idx_data.json", encoding="utf-8") as f:
            return json.load(f).get("fundamentals") or {}
    except Exception:
        return {}


def run_once(quick=False):
    print("\nIDX Research Terminal — data fetch" + ("  [quick]" if quick else ""))
    print("=" * 52)

    extras = read_my_stocks()
    tickers = TICKERS + extras
    dates, ihsg, prices, adv = fetch_prices(tickers)
    if quick:
        fundamentals = load_previous()
        if fundamentals:
            log(f"quick mode: reusing stored fundamentals for {len(fundamentals)} names")
        else:
            log("quick mode: no stored fundamentals found — running the full fetch")
            fundamentals = fetch_fundamentals(tickers, set(extras))
    else:
        fundamentals = fetch_fundamentals(tickers, set(extras))
    for t, v in adv.items():
        fundamentals.setdefault(t, {})["adv"] = v
    news = fetch_news(tickers, adv) + fetch_yahoo_news(tickers, adv)
    news.sort(key=lambda x: x["d"], reverse=True)
    news = news[:60]
    ff = fetch_foreign_flow(dates)

    payload = {
        "asof": dates[-1],
        "generated": datetime.now(WIB).strftime("%Y-%m-%d %H:%M WIB"),
        "source": "Yahoo Finance (.JK) — delayed/end-of-day, not real time",
        "dates": dates,
        "ihsg": ihsg,
        "prices": prices,
        "fundamentals": fundamentals,
        "news": news,
    }
    if ff:
        payload["ff"] = ff

    with open("idx_data.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))

    live = build_live_html(payload)

    print("=" * 52)
    print("  Wrote idx_data.json")
    if live:
        print("  Built idx-research-terminal-LIVE.html  <-- open this one")
    print(f"  As of        {dates[-1]}   ({len(dates)} trading days)")
    print(f"  Prices       {len(prices)} tickers")
    print(f"  Fundamentals {len(fundamentals)} tickers "
          f"(real traded value for {sum(1 for x in fundamentals.values() if 'adv' in x)})")
    print(f"  Headlines    {len(news)}")
    if live:
        print("\n  Double-click idx-research-terminal-LIVE.html — it opens with real")
        print("  data already inside. Re-run this script any time to refresh it.")
    else:
        print("\n  Open the terminal, click 'Load data', and select idx_data.json.")
    print("\n  Reminder: prices are Yahoo's delayed/end-of-day figures, not a")
    print("  real-time feed, and the terminal labels them that way.\n")


def main():
    args = sys.argv[1:]
    quick = "--quick" in args
    loop = None
    if "--loop" in args:
        try:
            loop = float(args[args.index("--loop") + 1])
        except (IndexError, ValueError):
            sys.exit("Usage: python fetch_idx_data.py [--quick] [--loop MINUTES]")
    run_once(quick=quick)
    while loop:
        print(f"  … next refresh in {loop:g} minutes (Ctrl+C to stop)")
        time.sleep(loop * 60)
        run_once(quick=True)


if __name__ == "__main__":
    main()
