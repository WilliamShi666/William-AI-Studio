#!/usr/bin/env python3
"""
fetch_data.py - yfinance-first financial data fetcher with multi-API fallback.

Usage:
    python fetch_data.py AAPL
    python fetch_data.py AAPL --source alphavantage
    python fetch_data.py AAPL --source polygon
    python fetch_data.py AAPL --source fmp

Sources (in priority order):
    1. yfinance       - free, no key required
    2. alphavantage   - requires ALPHA_VANTAGE_API_KEY env var
    3. polygon        - requires POLYGON_API_KEY env var
    4. fmp            - requires FMP_API_KEY env var
"""

import argparse
import json
import os
import sys
import time
from typing import Optional


def _safe_millions(val) -> Optional[float]:
    """Convert a raw value to millions, returning None if unavailable."""
    try:
        if val is None:
            return None
        return float(val) / 1e6
    except (TypeError, ValueError):
        return None


def fetch_yfinance(ticker: str) -> dict:
    """Fetch financials using yfinance (free, no API key required)."""
    import yf_cache

    # ── Layer 1 & 2 cache check ──────────────────────────────────
    cached = yf_cache.get(ticker)
    if cached:
        return {**cached, "source": "yfinance (cached)"}

    try:
        import yfinance as yf
    except ImportError:
        raise ImportError("yfinance not installed. Run: pip install yfinance")

    # ── Fetch with retry (backoff: 1s → 2s → give up) ───────────
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            t = yf.Ticker(ticker)
            info = t.info or {}

            # Income statement
            try:
                income = t.financials
                revenue = _safe_millions(income.loc["Total Revenue"].iloc[0]) if "Total Revenue" in income.index else None
                ebitda = _safe_millions(income.loc["EBITDA"].iloc[0]) if "EBITDA" in income.index else None
                net_income = _safe_millions(income.loc["Net Income"].iloc[0]) if "Net Income" in income.index else None
            except Exception:
                revenue = ebitda = net_income = None

            # Cash flow
            try:
                cf = t.cashflow
                fcf = _safe_millions(cf.loc["Free Cash Flow"].iloc[0]) if "Free Cash Flow" in cf.index else None
            except Exception:
                fcf = None

            # Balance sheet
            try:
                bs = t.balance_sheet
                debt = _safe_millions(bs.loc["Total Debt"].iloc[0]) if "Total Debt" in bs.index else None
                cash = _safe_millions(bs.loc["Cash And Cash Equivalents"].iloc[0]) if "Cash And Cash Equivalents" in bs.index else None
            except Exception:
                debt = cash = None

            result = {
                "ticker": ticker.upper(),
                "revenue": revenue,
                "ebitda": ebitda,
                "net_income": net_income,
                "fcf": fcf,
                "shares": _safe_millions(info.get("sharesOutstanding")),
                "price": info.get("currentPrice") or info.get("regularMarketPrice"),
                "beta": info.get("beta"),
                "debt": debt,
                "cash": cash,
                "currency": info.get("currency", "USD"),
            }

            yf_cache.put(ticker, result)
            return {**result, "source": "yfinance"}

        except Exception as e:
            last_exc = e
            if attempt < 2:
                wait = 2 ** attempt   # 1s, 2s
                print(f"[yf_cache] yfinance attempt {attempt + 1} failed ({e}), retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)

    raise RuntimeError(f"yfinance failed after 3 attempts: {last_exc}")


def fetch_alphavantage(ticker: str) -> dict:
    """Fetch financials using Alpha Vantage API."""
    import requests

    api_key = os.environ.get("ALPHA_VANTAGE_API_KEY")
    if not api_key:
        raise ValueError("ALPHA_VANTAGE_API_KEY not set")

    base = "https://www.alphavantage.co/query"

    def get(func, **kwargs):
        r = requests.get(base, params={"function": func, "symbol": ticker, "apikey": api_key, **kwargs}, timeout=15)
        r.raise_for_status()
        return r.json()

    income = get("INCOME_STATEMENT")
    cf_data = get("CASH_FLOW")
    bs_data = get("BALANCE_SHEET")
    quote = get("GLOBAL_QUOTE")
    overview = get("OVERVIEW")

    def latest(data, key):
        try:
            return float(data["annualReports"][0].get(key) or 0) / 1e6 or None
        except Exception:
            return None

    return {
        "ticker": ticker.upper(),
        "revenue": latest(income, "totalRevenue"),
        "ebitda": latest(income, "ebitda"),
        "net_income": latest(income, "netIncome"),
        "fcf": latest(cf_data, "operatingCashflow"),
        "shares": latest(bs_data, "commonStockSharesOutstanding"),
        "price": float(quote.get("Global Quote", {}).get("05. price", 0) or 0) or None,
        "beta": float(overview.get("Beta", 0) or 0) or None,
        "debt": latest(bs_data, "longTermDebt"),
        "cash": latest(bs_data, "cashAndCashEquivalentsAtCarryingValue"),
        "currency": "USD",
        "source": "alphavantage",
    }


def fetch_polygon(ticker: str) -> dict:
    """Fetch financials using Polygon.io API."""
    import requests

    api_key = os.environ.get("POLYGON_API_KEY")
    if not api_key:
        raise ValueError("POLYGON_API_KEY not set")

    headers = {"Authorization": f"Bearer {api_key}"}

    def get(url, **params):
        r = requests.get(url, headers=headers, params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    fin = get(f"https://api.polygon.io/vX/reference/financials", ticker=ticker, limit=1, timeframe="annual")
    results = fin.get("results", [{}])
    r = results[0] if results else {}
    ic = r.get("financials", {}).get("income_statement", {})
    bs = r.get("financials", {}).get("balance_sheet", {})
    cf = r.get("financials", {}).get("cash_flow_statement", {})

    def val(d, key):
        v = d.get(key, {}).get("value")
        return float(v) / 1e6 if v is not None else None

    snap = get(f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker.upper()}")
    price = snap.get("ticker", {}).get("day", {}).get("c")

    return {
        "ticker": ticker.upper(),
        "revenue": val(ic, "revenues"),
        "ebitda": val(ic, "operating_income"),
        "net_income": val(ic, "net_income_loss"),
        "fcf": val(cf, "net_cash_flow_from_operating_activities"),
        "shares": val(bs, "equity"),
        "price": price,
        "beta": None,
        "debt": val(bs, "long_term_debt"),
        "cash": val(bs, "cash_and_equivalents"),
        "currency": "USD",
        "source": "polygon",
    }


def fetch_fmp(ticker: str) -> dict:
    """Fetch financials using Financial Modeling Prep API."""
    import requests

    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        raise ValueError("FMP_API_KEY not set")

    base = "https://financialmodelingprep.com/api/v3"

    def get(path):
        r = requests.get(f"{base}/{path}?apikey={api_key}", timeout=15)
        r.raise_for_status()
        data = r.json()
        return data[0] if isinstance(data, list) and data else data

    income = get(f"income-statement/{ticker}?limit=1")
    bs = get(f"balance-sheet-statement/{ticker}?limit=1")
    cf = get(f"cash-flow-statement/{ticker}?limit=1")
    profile = get(f"profile/{ticker}")

    def m(d, key):
        v = d.get(key)
        return float(v) / 1e6 if v is not None else None

    return {
        "ticker": ticker.upper(),
        "revenue": m(income, "revenue"),
        "ebitda": m(income, "ebitda"),
        "net_income": m(income, "netIncome"),
        "fcf": m(cf, "freeCashFlow"),
        "shares": m(bs, "commonStock"),
        "price": profile.get("price"),
        "beta": profile.get("beta"),
        "debt": m(bs, "totalDebt"),
        "cash": m(bs, "cashAndCashEquivalents"),
        "currency": profile.get("currency", "USD"),
        "source": "fmp",
    }


def fetch_financials(ticker: str, source: Optional[str] = None) -> dict:
    """
    Fetch financial data for a ticker using the best available source.

    Priority order (unless source is forced):
    1. yfinance (free, no key)
    2. Alpha Vantage (ALPHA_VANTAGE_API_KEY)
    3. Polygon.io (POLYGON_API_KEY)
    4. Financial Modeling Prep (FMP_API_KEY)

    Args:
        ticker: Stock ticker symbol (e.g. 'AAPL')
        source: Force a specific source ('yfinance', 'alphavantage', 'polygon', 'fmp')

    Returns:
        Normalized dict with keys: ticker, revenue, ebitda, net_income, fcf,
        shares, price, beta, debt, cash, currency, source
        Values in millions (except price, beta, currency, source, ticker).
    """
    fetchers = {
        "yfinance": fetch_yfinance,
        "alphavantage": fetch_alphavantage,
        "polygon": fetch_polygon,
        "fmp": fetch_fmp,
    }

    if source:
        if source not in fetchers:
            raise ValueError(f"Unknown source '{source}'. Choose from: {list(fetchers)}")
        return fetchers[source](ticker)

    # Auto-detect: yfinance first, then API-key-based sources
    order = ["yfinance"]
    if os.environ.get("ALPHA_VANTAGE_API_KEY"):
        order.append("alphavantage")
    if os.environ.get("POLYGON_API_KEY"):
        order.append("polygon")
    if os.environ.get("FMP_API_KEY"):
        order.append("fmp")

    errors = []
    for src in order:
        try:
            return fetchers[src](ticker)
        except Exception as e:
            errors.append(f"  {src}: {e}")

    print("ERROR: Could not fetch data from any source.", file=sys.stderr)
    print("Tried:", file=sys.stderr)
    for e in errors:
        print(e, file=sys.stderr)
    print("\nTo fix:", file=sys.stderr)
    print("  - yfinance is free: pip install yfinance", file=sys.stderr)
    print("  - Or set ALPHA_VANTAGE_API_KEY, POLYGON_API_KEY, or FMP_API_KEY", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Fetch financial data for a ticker")
    parser.add_argument("ticker", help="Stock ticker symbol (e.g. AAPL)")
    parser.add_argument("--source", choices=["yfinance", "alphavantage", "polygon", "fmp"],
                        help="Force a specific data source")
    args = parser.parse_args()

    data = fetch_financials(args.ticker, source=args.source)
    print(json.dumps(data, indent=2, default=str))


if __name__ == "__main__":
    main()
