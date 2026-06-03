import argparse
import re
from pathlib import Path

import pandas as pd

from app import (
    STATEMENT_TYPES,
    calculate_custom_metrics,
    fetch_and_parse_financial_statement_single_html,
    fetch_employee_count_data,
    get_company_slug_from_yfinance,
    get_macrotrends_financial_url,
    make_request_with_retry,
    numeric_statement_series,
    find_shares_outstanding_metric_name,
    yf,
)


def parse_tickers(raw: str) -> list[str]:
    return [ticker.strip().upper() for ticker in re.split(r"[\s,;]+", raw) if ticker.strip()]


def fetch_snapshot(tickers: list[str], delay: float, max_retries: int) -> tuple[dict, list[str]]:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Referer": "https://www.macrotrends.net/",
        "Accept-Language": "en-US,en;q=0.9",
    }
    import time

    logs = []
    out = {}
    for idx, ticker in enumerate(tickers, start=1):
        print(f"[{idx}/{len(tickers)}] Fetching {ticker}")
        data = {}
        stock = None
        if yf is not None:
            try:
                stock = yf.Ticker(ticker.replace(".", "-"))
                info = stock.info
                data["_yfinance_market_cap_"] = info.get("marketCap")
                data["_yfinance_total_debt_"] = info.get("totalDebt")
                data["_yfinance_cash_"] = info.get("totalCash") or info.get("cash")
                data["_yfinance_shares_outstanding_"] = info.get("sharesOutstanding")
                data["_yfinance_sector_"] = info.get("sector")
                data["_yfinance_industry_"] = info.get("industry")
                data["_yfinance_current_price_"] = info.get("regularMarketPrice") or info.get("currentPrice")
                hist = stock.history(period="max", auto_adjust=True)
                if not hist.empty:
                    hist.index = hist.index.tz_localize(None)
                    data["_yfinance_price_history_"] = hist
            except Exception as exc:
                logs.append(f"{ticker}: yfinance metadata failed: {exc}")

        slug = get_company_slug_from_yfinance(ticker) or ticker.lower()
        logs.append(f"{ticker}: company slug {slug}")
        for title, statement_slug in STATEMENT_TYPES.items():
            url = get_macrotrends_financial_url(ticker, slug, statement_slug)
            log_start = len(logs)
            html = make_request_with_retry(url, headers, log=logs, max_retries=max_retries)
            if html:
                df = fetch_and_parse_financial_statement_single_html(html)
                if df is not None and not df.empty:
                    data[title] = df
                    logs.append(f"{ticker}: parsed {title} ({df.shape[0]} x {df.shape[1]})")
            if any("repeated 403 block detected" in line for line in logs[log_start:]):
                logs.append(f"{ticker}: stopping statement fetch early due to Macrotrends 403 block")
                break
            if title != list(STATEMENT_TYPES)[-1]:
                sleep_for = max(0.0, delay)
                logs.append(f"{ticker}: sleeping {sleep_for:.1f}s before next statement")
                time.sleep(sleep_for)

        has_statement = any(isinstance(data.get(statement), pd.DataFrame) and not data[statement].empty for statement in STATEMENT_TYPES)
        if has_statement:
            emp_df = fetch_employee_count_data(ticker, slug, headers, log=logs, max_retries=max_retries)
            if emp_df is not None and not emp_df.empty:
                data["Employee Count"] = emp_df
        else:
            logs.append(f"{ticker}: skipped employee count because no statements parsed")

        is_df = data.get("Income Statement")
        if isinstance(is_df, pd.DataFrame):
            shares_name = find_shares_outstanding_metric_name(is_df)
            if shares_name:
                data["_shares_outstanding_"] = numeric_statement_series(is_df, shares_name)
        calculate_custom_metrics(ticker, data, logs)

        if has_statement:
            out[ticker] = data
        else:
            logs.append(f"{ticker}: no Macrotrends statement data parsed; omitted")
            if any("403 for https://www.macrotrends.net" in line for line in logs):
                logs.append("Stopping remaining tickers because Macrotrends appears blocked from this host")
                break
    return out, logs


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Macrotrends-only statement data and save a Streamlit snapshot.")
    parser.add_argument("tickers", help="Comma, space, or semicolon separated tickers.")
    parser.add_argument("--output", default="financial_dashboard_snapshot.pkl", help="Output snapshot path.")
    parser.add_argument("--delay", type=float, default=8.0, help="Delay between Macrotrends statement requests.")
    parser.add_argument("--max-retries", type=int, default=2, help="Max retries per Macrotrends page.")
    args = parser.parse_args()

    data, logs = fetch_snapshot(parse_tickers(args.tickers), args.delay, args.max_retries)
    output = Path(args.output)
    pd.to_pickle(data, output)
    output.with_suffix(".log.txt").write_text("\n".join(logs), encoding="utf-8")
    print(f"Saved {len(data)} tickers to {output}")
    print(f"Saved logs to {output.with_suffix('.log.txt')}")


if __name__ == "__main__":
    main()
