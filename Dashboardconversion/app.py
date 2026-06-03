import io
import json
import random
import re
import time
import traceback
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from matplotlib.ticker import FuncFormatter

try:
    import yfinance as yf
except ImportError:  # pragma: no cover - handled in UI
    yf = None


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.1 Safari/605.1.15",
]

STATEMENT_TYPES = {
    "Income Statement": "income-statement",
    "Balance Sheet": "balance-sheet",
    "Cash Flow Statement": "cash-flow-statement",
}

SHARES_METRIC_KEYWORDS = [
    "diluted weighted average shares outstanding",
    "weighted average shares outstanding diluted",
    "shares outstanding diluted",
    "basic weighted average shares outstanding",
    "weighted average shares outstanding basic",
    "shares outstanding basic",
    "shares outstanding",
]

EPS_METRIC_KEYWORDS = [
    "eps - diluted",
    "diluted eps",
    "eps diluted",
    "earnings per share diluted",
    "eps - basic",
    "basic eps",
    "eps basic",
    "earnings per share basic",
    "eps",
]

DARK = {
    "bg": "#020603",
    "panel": "#07140B",
    "fg": "#D8FFE1",
    "muted": "#7C9B83",
    "accent": "#00FF41",
    "highlight": "#39FF88",
    "plot": "#000F06",
    "grid": "#12451F",
    "line": "#00FF41",
    "avg": "#B6FF00",
}


@dataclass
class FetchResult:
    data: Dict[str, Dict[str, object]]
    logs: List[str]


def init_state() -> None:
    st.session_state.setdefault("data", {})
    st.session_state.setdefault("logs", [])
    st.session_state.setdefault("last_summary", pd.DataFrame())


def macrotrends_client_name() -> str:
    return "original requests session"


def macrotrends_was_blocked() -> bool:
    return macrotrends_blocked_in_logs(st.session_state.logs)


def macrotrends_blocked_in_logs(logs: List[str]) -> bool:
    return any("403 for https://www.macrotrends.net" in line for line in logs)


def apply_theme() -> None:
    st.set_page_config(page_title="Financial Dashboard", layout="wide")
    st.markdown(
        f"""
        <style>
        .stApp {{
            background: {DARK["bg"]};
            color: {DARK["fg"]};
        }}
        [data-testid="stSidebar"] {{
            background: {DARK["panel"]};
            border-right: 1px solid #0D7A2A;
        }}
        h1, h2, h3, h4, h5, h6, p, label, span {{
            color: {DARK["fg"]};
        }}
        div[data-testid="stMetric"] {{
            background: {DARK["panel"]};
            border: 1px solid #0D7A2A;
            border-radius: 6px;
            padding: 12px;
            box-shadow: 0 0 14px rgba(0, 255, 65, 0.14);
        }}
        .stTabs [data-baseweb="tab-list"] {{
            gap: 4px;
        }}
        .stTabs [data-baseweb="tab"] {{
            background: {DARK["panel"]};
            border: 1px solid #0D7A2A;
            border-radius: 6px 6px 0 0;
            color: {DARK["fg"]};
        }}
        .stTabs [aria-selected="true"] {{
            background: {DARK["accent"]};
            color: #001904;
        }}
        .stButton button, .stDownloadButton button {{
            background: #031F0B;
            border: 1px solid {DARK["accent"]};
            color: {DARK["fg"]};
        }}
        .stButton button:hover, .stDownloadButton button:hover {{
            background: {DARK["accent"]};
            color: #001904;
            border-color: {DARK["highlight"]};
        }}
        input, textarea {{
            background-color: #000F06 !important;
            color: {DARK["fg"]} !important;
            border-color: #0D7A2A !important;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def clean_value(value_str):
    if isinstance(value_str, (int, float, np.number)):
        return value_str
    if value_str is None or not isinstance(value_str, str) or value_str.strip() in ["", "-"]:
        return np.nan
    cleaned = value_str.replace("$", "").replace(",", "").strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    try:
        return float(cleaned)
    except ValueError:
        return np.nan


def format_large_number(val_in_millions) -> str:
    if pd.isna(val_in_millions):
        return "N/A"
    val = float(val_in_millions) * 1_000_000
    sign = "-" if val < 0 else ""
    val = abs(val)
    if val >= 1e12:
        return f"{sign}{val / 1e12:,.2f}T"
    if val >= 1e9:
        return f"{sign}{val / 1e9:,.2f}B"
    if val >= 1e6:
        return f"{sign}{val / 1e6:,.2f}M"
    if val >= 1e3:
        return f"{sign}{val / 1e3:,.0f}K"
    return f"{sign}{val:,.2f}"


def fmt_pct(v, digits=1) -> str:
    return "N/A" if pd.isna(v) else f"{float(v):,.{digits}f}%"


def fmt_num(v, digits=2) -> str:
    return "N/A" if pd.isna(v) else f"{float(v):,.{digits}f}"


def make_request_with_retry(
    url: str,
    headers: Dict[str, str],
    max_retries: int = 3,
    base_backoff_seconds: float = 3.0,
    log: Optional[List[str]] = None,
    max_403_retries: int = 1,
) -> Optional[str]:
    session = requests.Session()
    seen_403 = 0
    for attempt in range(max_retries):
        try:
            actual_headers = dict(headers)
            actual_headers["User-Agent"] = random.choice(USER_AGENTS)
            response = session.get(url, headers=actual_headers, timeout=20, allow_redirects=True)
            if response.ok:
                if response.history and "?freq=Q" in url and "?freq=Q" not in response.url:
                    corrected_url = f"{response.url}?freq=Q"
                    if log is not None:
                        log.append("Redirect detected; pausing before re-fetching corrected quarterly URL")
                    time.sleep(random.uniform(5, 8))
                    for corrected_attempt in range(max_retries):
                        corrected_headers = dict(headers)
                        corrected_headers["User-Agent"] = random.choice(USER_AGENTS)
                        corrected = session.get(corrected_url, headers=corrected_headers, timeout=20)
                        if corrected.ok:
                            if log is not None:
                                log.append(f"Fetched corrected quarterly URL: {corrected_url}")
                            return corrected.text
                        if corrected.status_code in (404, 410):
                            if log is not None:
                                log.append(f"Corrected URL not found: {corrected_url}")
                            return None
                        if log is not None:
                            log.append(
                                f"{corrected.status_code} for corrected URL attempt "
                                f"{corrected_attempt + 1}; retrying"
                            )
                        if corrected_attempt < max_retries - 1:
                            sleep_for = min(base_backoff_seconds * (2**corrected_attempt), 120) + random.uniform(1, 3)
                            time.sleep(sleep_for)
                return response.text
            if response.status_code in (404, 410):
                if log is not None:
                    log.append(f"Not found: {url}")
                return None
            if response.status_code == 403:
                seen_403 += 1
                if log is not None:
                    log.append(f"403 for {url}; retrying with original requests/backoff")
                if seen_403 >= max_403_retries:
                    if log is not None:
                        log.append(f"Stopping retries for {url}; repeated 403 block detected")
                    return None
            elif response.status_code == 429:
                if log is not None:
                    log.append(f"429 rate limit for {url}; backing off")
                time.sleep(min((base_backoff_seconds + 10) * (2**attempt), 90) + random.uniform(1, 5))
                continue
            if log is not None:
                log.append(f"{response.status_code} for {url}; retrying")
        except Exception as exc:
            if log is not None:
                log.append(f"Request failed for {url}: {exc}")
        if attempt < max_retries - 1:
            time.sleep(min(base_backoff_seconds * (2**attempt), 120) + random.uniform(1, 2))
    return None


def fetch_and_parse_financial_statement_single_html(html_content: str) -> Optional[pd.DataFrame]:
    if not html_content:
        return None
    pattern = re.search(r"var\s+(originalData|vData)\s*=\s*(\[.*?\]);", html_content, re.DOTALL)
    if not pattern:
        return None
    try:
        data_list = json.loads(pattern.group(2))
        if not data_list:
            return None
        non_date_keys = {"field_name", "popup_icon", "comp_name", "link_type", "freq"}
        first_obj = data_list[0]
        html_key = "field_name"
        if html_key not in first_obj:
            html_key = next(
                (
                    k
                    for k, v in first_obj.items()
                    if isinstance(v, str) and ("<div" in v or "<span" in v or "<a href" in v)
                ),
                None,
            )
            if html_key is None:
                return None
            non_date_keys.add(html_key)
        headers = ["Financials"] + [key for key in first_obj.keys() if key not in non_date_keys]
        rows = []
        for item in data_list:
            metric_html = item.get(html_key, "")
            metric_name = BeautifulSoup(metric_html, "html.parser").get_text(strip=True)
            if metric_name:
                rows.append([metric_name] + [clean_value(item.get(header)) for header in headers[1:]])
        df = pd.DataFrame(rows, columns=headers).set_index("Financials")
        df.columns = pd.to_datetime(df.columns, errors="coerce")
        df = df.loc[:, ~pd.isna(df.columns)]
        df = df.dropna(axis=1, how="all").sort_index(axis=1)
        df.columns = df.columns.strftime("%Y-%m-%d")
        return df
    except Exception:
        traceback.print_exc()
        return None


def get_macrotrends_financial_url(ticker: str, company_slug: str, statement_slug: str) -> str:
    return f"https://www.macrotrends.net/stocks/charts/{ticker.upper()}/{company_slug}/{statement_slug}?freq=Q"


def get_company_slug_from_yfinance(ticker: str) -> Optional[str]:
    if yf is None:
        return ticker.lower()
    try:
        info = yf.Ticker(ticker.replace(".", "-")).info
        long_name = info.get("longName")
        if not long_name:
            return ticker.lower()
        slug = long_name.lower()
        slug = re.sub(r"\s(corporation|corp|inc|incorporated|limited|ltd|company|plc|group)\.?$", "", slug)
        slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
        return re.sub(r"^the-", "", slug) or ticker.lower()
    except Exception:
        return ticker.lower()


def fetch_employee_count_data(
    ticker: str,
    company_slug: str,
    headers: Dict[str, str],
    log: Optional[List[str]] = None,
    max_retries: int = 3,
) -> Optional[pd.DataFrame]:
    url = f"https://www.macrotrends.net/stocks/charts/{ticker.upper()}/{company_slug}/number-of-employees"
    html = make_request_with_retry(url, headers, log=log, max_retries=max_retries)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    table = None
    for tbl in soup.find_all("table", class_="historical_data_table"):
        header = tbl.find("th")
        if header and "Employee Count" in header.get_text():
            table = tbl
            break
    if table is None:
        return None
    year_values = {}
    for row in table.find_all("tr"):
        cols = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if len(cols) == 2 and cols[0].isdigit():
            val = re.sub(r"[^0-9]", "", cols[1])
            if val:
                year_values[cols[0]] = int(val)
    if not year_values:
        return None
    years = sorted(year_values)
    return pd.DataFrame(
        [[year_values[y] for y in years]],
        index=["Employee Count"],
        columns=[f"{y}-12-31" for y in years],
    )


def find_metric_name(df: Optional[pd.DataFrame], candidates: Iterable[str]) -> Optional[str]:
    if df is None or df.empty:
        return None
    lower_map = {str(name).lower().strip(): name for name in df.index}
    for candidate in candidates:
        c = candidate.lower().strip()
        if c in lower_map:
            return lower_map[c]
    for candidate in candidates:
        c = candidate.lower().strip()
        for lower_name, original in lower_map.items():
            if c in lower_name:
                return original
    return None


def find_shares_outstanding_metric_name(df: Optional[pd.DataFrame]) -> Optional[str]:
    if df is None or df.empty:
        return None
    normalized = {str(name).lower().strip().replace("(millions)", "").strip(): name for name in df.index}
    for keyword in SHARES_METRIC_KEYWORDS:
        if keyword in normalized:
            return normalized[keyword]
    return find_metric_name(df, SHARES_METRIC_KEYWORDS)


def find_eps_metric_name(df: Optional[pd.DataFrame]) -> Optional[str]:
    return find_metric_name(df, EPS_METRIC_KEYWORDS)


def numeric_statement_series(df: pd.DataFrame, metric_name: str) -> pd.Series:
    s = pd.to_numeric(df.loc[metric_name], errors="coerce").dropna()
    s.index = pd.to_datetime(s.index, errors="coerce")
    s = s[~pd.isna(s.index)]
    return s.sort_index()


def get_series(data: Dict[str, object], statement: str, metric_name: str) -> pd.Series:
    df = data.get(statement)
    if not isinstance(df, pd.DataFrame) or df.empty or metric_name not in df.index:
        return pd.Series(dtype=float)
    return numeric_statement_series(df, metric_name)


def align_series(df: pd.DataFrame, metric_name: str, align_index: pd.Index) -> pd.Series:
    if metric_name not in df.index:
        raise KeyError(metric_name)
    s = pd.to_numeric(df.loc[metric_name], errors="coerce")
    s.index = pd.to_datetime(s.index, errors="coerce")
    return s.reindex(align_index, method="ffill").bfill()


def calculate_custom_metrics(ticker: str, data: Dict[str, object], log: Optional[List[str]] = None) -> None:
    is_df = data.get("Income Statement")
    bs_df = data.get("Balance Sheet")
    cf_df = data.get("Cash Flow Statement")
    if not all(isinstance(df, pd.DataFrame) and not df.empty for df in [is_df, bs_df, cf_df]):
        return
    assert isinstance(is_df, pd.DataFrame)
    assert isinstance(bs_df, pd.DataFrame)
    assert isinstance(cf_df, pd.DataFrame)
    net_income_name = find_metric_name(is_df, ["Net Income"])
    if not net_income_name:
        return
    base = numeric_statement_series(is_df, net_income_name)
    base_index = base.index
    custom = {}

    def try_metric(label: str, fn) -> None:
        try:
            custom[label] = fn()
        except Exception as exc:
            if log is not None:
                log.append(f"{ticker}: skipped {label}: {exc}")

    revenue = align_series(is_df, find_metric_name(is_df, ["Revenue"]) or "Revenue", base_index).replace(0, np.nan)
    gross_profit = align_series(is_df, find_metric_name(is_df, ["Gross Profit"]) or "Gross Profit", base_index)
    operating_income = align_series(is_df, find_metric_name(is_df, ["Operating Income"]) or "Operating Income", base_index)
    total_assets = align_series(bs_df, find_metric_name(bs_df, ["Total Assets"]) or "Total Assets", base_index).replace(0, np.nan)
    equity = align_series(bs_df, find_metric_name(bs_df, ["Share Holder Equity", "Total Stockholders Equity"]) or "Share Holder Equity", base_index).replace(0, np.nan)

    custom["Gross Margin %"] = (gross_profit / revenue) * 100
    custom["Operating Margin %"] = (operating_income / revenue) * 100
    custom["Net Profit Margin %"] = (base / revenue) * 100
    custom["Gross Margin (TTM) %"] = (gross_profit.rolling(4).sum() / revenue.rolling(4).sum()) * 100
    custom["Operating Margin (TTM) %"] = (operating_income.rolling(4).sum() / revenue.rolling(4).sum()) * 100
    custom["Net Profit Margin (TTM) %"] = (base.rolling(4).sum() / revenue.rolling(4).sum()) * 100
    custom["Return on Equity (ROE) %"] = (base.rolling(4).sum() / equity) * 100
    custom["ROE %"] = custom["Return on Equity (ROE) %"]
    custom["Return on Assets (ROA) %"] = (base.rolling(4).sum() / total_assets) * 100
    custom["ROA %"] = custom["Return on Assets (ROA) %"]

    cfo_name = find_metric_name(cf_df, ["Cash Flow From Operating Activities", "Operating Cash Flow"])
    capex_name = find_metric_name(cf_df, ["Net Change In Property, Plant, And Equipment", "Capital Expenditures"])
    if cfo_name and capex_name:
        cfo = align_series(cf_df, cfo_name, base_index)
        capex = align_series(cf_df, capex_name, base_index)
        fcf = cfo + capex
        custom["Free Cash Flow (Simple)"] = fcf
        custom["FCF Margin %"] = (fcf / revenue) * 100
        custom["FCF Margin (TTM) %"] = (fcf.rolling(4).sum() / revenue.rolling(4).sum()) * 100
        custom["CFO / Net Income"] = cfo / base.replace(0, np.nan)

    ebit_name = find_metric_name(is_df, ["EBIT", "Operating Income"])
    ebitda_name = find_metric_name(is_df, ["EBITDA"])
    debt_name = find_metric_name(bs_df, ["Total Debt", "Long Term Debt"])
    cash_name = find_metric_name(bs_df, ["Cash On Hand", "Cash And Cash Equivalents"])
    if ebit_name:
        ebit = align_series(is_df, ebit_name, base_index)
        custom["EBIT"] = ebit
    if debt_name:
        debt = align_series(bs_df, debt_name, base_index)
        custom["Debt/Equity"] = debt / equity
        if ebitda_name and cash_name:
            ebitda = align_series(is_df, ebitda_name, base_index)
            cash = align_series(bs_df, cash_name, base_index)
            custom["EBITDA"] = ebitda
            custom["Net Debt/EBITDA"] = (debt - cash) / ebitda.rolling(4).sum().replace(0, np.nan)
    current_assets = find_metric_name(bs_df, ["Total Current Assets"])
    current_liabilities = find_metric_name(bs_df, ["Total Current Liabilities"])
    inventory = find_metric_name(bs_df, ["Inventory"])
    if current_assets and current_liabilities:
        ca = align_series(bs_df, current_assets, base_index)
        cl = align_series(bs_df, current_liabilities, base_index).replace(0, np.nan)
        custom["Current Ratio"] = ca / cl
        if inventory:
            custom["Quick Ratio"] = (ca - align_series(bs_df, inventory, base_index)) / cl

    cost_parts = []
    for keywords in [
        ["cost of goods sold", "cost of revenue"],
        ["research and development", "r&d"],
        ["selling general and administrative", "sg&a"],
        ["operating expenses", "total operating expenses"],
    ]:
        name = find_metric_name(is_df, keywords)
        if name:
            cost_parts.append(align_series(is_df, name, base_index).fillna(0))
    if cost_parts:
        custom["Total Costs"] = sum(cost_parts)

    div_name = find_metric_name(cf_df, ["dividends paid", "common dividends paid", "cash dividends paid"])
    shares_name = find_shares_outstanding_metric_name(is_df)
    if div_name:
        dividends_paid = abs(align_series(cf_df, div_name, base_index))
        custom["Dividend Payout Ratio %"] = (dividends_paid / base.replace(0, np.nan)) * 100
        if shares_name:
            shares = align_series(is_df, shares_name, base_index).replace(0, np.nan)
            custom["TTM Dividend/Shr"] = dividends_paid.rolling(4).sum() / shares

    if custom:
        custom_df = pd.DataFrame(custom).apply(pd.to_numeric, errors="coerce").sort_index()
        data["Custom Metrics"] = custom_df.T


def fetch_ticker_data(
    tickers: List[str],
    polite_delay: float = 1.5,
    max_retries: int = 3,
    stop_on_403: bool = True,
) -> FetchResult:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Referer": "https://www.macrotrends.net/",
        "Accept-Language": "en-US,en;q=0.9",
    }
    logs: List[str] = []
    out: Dict[str, Dict[str, object]] = {}
    progress = st.progress(0)
    status = st.empty()

    for i, ticker in enumerate(tickers, start=1):
        ticker = ticker.upper().strip()
        status.write(f"Fetching {ticker} ({i}/{len(tickers)})")
        logs.append(f"Processing {ticker}")
        data: Dict[str, object] = {}
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
                logs.append(f"{ticker}: yfinance failed: {exc}")

        slug = get_company_slug_from_yfinance(ticker) or ticker.lower()
        logs.append(f"{ticker}: company slug {slug}")
        for statement_title, statement_slug in STATEMENT_TYPES.items():
            url = get_macrotrends_financial_url(ticker, slug, statement_slug)
            log_start = len(logs)
            html = make_request_with_retry(url, headers, log=logs, max_retries=max_retries)
            if html:
                df = fetch_and_parse_financial_statement_single_html(html)
                if df is not None and not df.empty:
                    data[statement_title] = df
                    logs.append(f"{ticker}: parsed {statement_title} ({df.shape[0]} x {df.shape[1]})")
            if stop_on_403 and any("repeated 403 block detected" in line for line in logs[log_start:]):
                logs.append(f"{ticker}: stopping statement fetch early due to Macrotrends 403 block")
                break
            if statement_title != list(STATEMENT_TYPES)[-1]:
                sleep_for = max(0.0, polite_delay) + random.uniform(0, 4)
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
            logs.append(f"{ticker}: no Macrotrends statement data parsed; ticker omitted from tables")
            if stop_on_403 and macrotrends_blocked_in_logs(logs):
                logs.append("Stopping remaining tickers because Macrotrends appears blocked from this host")
                progress.progress(i / len(tickers))
                break
        if i < len(tickers):
            sleep_for = max(0.0, polite_delay) + random.uniform(2, 7)
            logs.append(f"{ticker}: sleeping {sleep_for:.1f}s before next ticker")
            time.sleep(sleep_for)
        progress.progress(i / len(tickers))

    status.write("Fetch complete")
    return FetchResult(out, logs)


def calculate_percentage_change(old_val, new_val):
    if pd.isna(old_val) or pd.isna(new_val):
        return np.nan
    if old_val == 0:
        return np.nan
    return ((new_val / old_val) - 1) * 100


def calculate_cagr(start_val, end_val, years):
    if pd.isna(start_val) or pd.isna(end_val) or years <= 0 or start_val <= 0 or end_val < 0:
        return np.nan
    if end_val == 0:
        return -100.0
    return ((end_val / start_val) ** (1 / years) - 1) * 100


def linear_fit_stats(values: np.ndarray) -> Tuple[float, float, float]:
    values = values.astype(float)
    values = values[~np.isnan(values)]
    if len(values) < 3:
        return np.nan, np.nan, np.nan
    x = np.arange(len(values), dtype=float)
    slope, intercept = np.polyfit(x, values, 1)
    pred = slope * x + intercept
    ss_tot = float(np.sum((values - np.mean(values)) ** 2))
    ss_res = float(np.sum((values - pred) ** 2))
    rmse = float(np.sqrt(np.mean((values - pred) ** 2)))
    norm_rmse = (rmse / abs(float(np.mean(values))) * 100) if np.mean(values) != 0 else np.nan
    r2 = 1 - ss_res / ss_tot if ss_tot else np.nan
    return float(slope), float(r2), float(norm_rmse)


def ttm_series(data: Dict[str, object], statement: str, metric: str) -> pd.Series:
    raw = get_series(data, statement, metric)
    if raw.empty:
        return raw
    if "Balance Sheet" in statement or statement == "Employee Count":
        return raw.rolling(4, min_periods=1).mean().dropna()
    return raw.rolling(4, min_periods=4).sum().dropna()


def per_share_ttm_series(data: Dict[str, object], statement: str, metric: str) -> pd.Series:
    ttm = ttm_series(data, statement, metric)
    shares = data.get("_shares_outstanding_")
    if not isinstance(shares, pd.Series) or shares.empty:
        shares_val = data.get("_yfinance_shares_outstanding_")
        if shares_val:
            return ttm / (float(shares_val) / 1_000_000)
        return pd.Series(dtype=float)
    shares = pd.to_numeric(shares, errors="coerce").replace(0, np.nan).dropna()
    shares.index = pd.to_datetime(shares.index, errors="coerce")
    aligned = shares.reindex(ttm.index, method="ffill")
    return (ttm / aligned).replace([np.inf, -np.inf], np.nan).dropna()


def metric_stats(data: Dict[str, object], statement: str, metric: str) -> Dict[str, float]:
    ps = per_share_ttm_series(data, statement, metric)
    if ps.empty:
        ps = ttm_series(data, statement, metric)
    stats = {
        "Latest": np.nan,
        "1Y Growth": np.nan,
        "3Y CAGR": np.nan,
        "5Y CAGR": np.nan,
        "10Y CAGR": np.nan,
        "Obs": len(ps),
        "# Positive": np.nan,
        "% Positive": np.nan,
        "# Records": np.nan,
        "# Increases": np.nan,
        "Slope": np.nan,
        "R2": np.nan,
        "RMSE %": np.nan,
        "CV %": np.nan,
    }
    if ps.empty:
        return stats
    stats["Latest"] = float(ps.iloc[-1])
    stats["# Positive"] = int((ps > 0).sum())
    stats["% Positive"] = float((ps > 0).mean() * 100)
    stats["# Records"] = int((ps == ps.cummax()).sum())
    stats["# Increases"] = int((ps.diff() > 0).sum())
    for years, key in [(1, "1Y Growth"), (3, "3Y CAGR"), (5, "5Y CAGR"), (10, "10Y CAGR")]:
        periods = years * 4
        if len(ps) > periods:
            stats[key] = calculate_cagr(ps.iloc[-periods - 1], ps.iloc[-1], years)
    vals = ps.values.astype(float)
    if len(vals) >= 3:
        stats["Slope"], stats["R2"], stats["RMSE %"] = linear_fit_stats(vals)
        mean = np.mean(vals)
        stats["CV %"] = (np.std(vals) / abs(mean) * 100) if mean else np.nan
    return stats


def live_ratio(data: Dict[str, object], statement: str, metric: str) -> float:
    price = data.get("_yfinance_current_price_")
    if not price:
        return np.nan
    ps = per_share_ttm_series(data, statement, metric)
    if ps.empty or ps.iloc[-1] == 0:
        return np.nan
    return float(price) / float(ps.iloc[-1])


def build_summary_df(all_data: Dict[str, Dict[str, object]]) -> pd.DataFrame:
    rows = []
    summary_specs = {
        "EPS": ("Income Statement", "Net Income"),
        "Rev": ("Income Statement", "Revenue"),
        "FCF": ("Custom Metrics", "Free Cash Flow (Simple)"),
        "EBIT": ("Custom Metrics", "EBIT"),
        "CFO": ("Cash Flow Statement", "Cash Flow From Operating Activities"),
        "GP": ("Income Statement", "Gross Profit"),
    }
    ratio_specs = {
        "P/E": ("Income Statement", "Net Income"),
        "P/S": ("Income Statement", "Revenue"),
        "P/FCF": ("Custom Metrics", "Free Cash Flow (Simple)"),
        "P/EBIT": ("Custom Metrics", "EBIT"),
        "P/CFO": ("Cash Flow Statement", "Cash Flow From Operating Activities"),
        "P/GP": ("Income Statement", "Gross Profit"),
    }
    for ticker, data in all_data.items():
        row = {
            "Ticker": ticker,
            "Price": data.get("_yfinance_current_price_"),
            "Market Cap": data.get("_yfinance_market_cap_"),
            "Sector": data.get("_yfinance_sector_"),
            "Industry": data.get("_yfinance_industry_"),
        }
        for label, (statement, metric) in summary_specs.items():
            stats = metric_stats(data, statement, metric)
            row[f"{label} Latest"] = stats["Latest"]
            row[f"{label} 1Y"] = stats["1Y Growth"]
            row[f"{label} 3Y"] = stats["3Y CAGR"]
            row[f"{label} 5Y"] = stats["5Y CAGR"]
            row[f"{label} #Pos"] = stats["# Positive"]
            row[f"{label} R2"] = stats["R2"]
            row[f"{label} Slope"] = stats["Slope"]
        for label, (statement, metric) in ratio_specs.items():
            row[label] = live_ratio(data, statement, metric)
        for margin in ["Gross Margin (TTM) %", "Operating Margin (TTM) %", "Net Profit Margin (TTM) %", "FCF Margin (TTM) %"]:
            s = get_series(data, "Custom Metrics", margin)
            row[margin] = float(s.iloc[-1]) if not s.empty else np.nan
        rows.append(row)
    df = pd.DataFrame(rows)
    if not df.empty and "Market Cap" in df:
        df["Market Cap"] = df["Market Cap"] / 1_000_000
    return df


def build_balance_sheet_df(all_data: Dict[str, Dict[str, object]]) -> pd.DataFrame:
    rows = []
    for ticker, data in all_data.items():
        bs = data.get("Balance Sheet")
        if not isinstance(bs, pd.DataFrame) or bs.empty:
            continue
        row = {"Ticker": ticker}
        for label, candidates in {
            "Cash": ["Cash On Hand", "Cash And Cash Equivalents"],
            "Total Assets": ["Total Assets"],
            "Total Debt": ["Total Debt", "Long Term Debt"],
            "Total Liabilities": ["Total Liabilities"],
            "Shareholder Equity": ["Share Holder Equity", "Total Stockholders Equity"],
            "Current Assets": ["Total Current Assets"],
            "Current Liabilities": ["Total Current Liabilities"],
            "Retained Earnings": ["Retained Earnings (Accumulated Deficit)", "Retained Earnings"],
        }.items():
            name = find_metric_name(bs, candidates)
            s = numeric_statement_series(bs, name) if name else pd.Series(dtype=float)
            row[label] = float(s.iloc[-1]) if not s.empty else np.nan
        row["Debt/Equity"] = row["Total Debt"] / row["Shareholder Equity"] if row["Shareholder Equity"] else np.nan
        row["Current Ratio"] = row["Current Assets"] / row["Current Liabilities"] if row["Current Liabilities"] else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def build_rising_costs_df(all_data: Dict[str, Dict[str, object]]) -> pd.DataFrame:
    rows = []
    for ticker, data in all_data.items():
        revenue = get_series(data, "Income Statement", "Revenue").rolling(4).sum().dropna()
        costs = get_series(data, "Custom Metrics", "Total Costs").rolling(4).sum().dropna()
        if revenue.empty or costs.empty:
            continue
        idx = revenue.index.intersection(costs.index)
        if len(idx) < 5:
            continue
        revenue, costs = revenue.loc[idx], costs.loc[idx]
        cost_ratio = (costs / revenue.replace(0, np.nan)) * 100
        rows.append(
            {
                "Ticker": ticker,
                "Latest Cost/Revenue %": cost_ratio.iloc[-1],
                "1Y Cost/Revenue Change": cost_ratio.iloc[-1] - cost_ratio.iloc[-5],
                "Revenue 1Y Growth": calculate_cagr(revenue.iloc[-5], revenue.iloc[-1], 1),
                "Cost 1Y Growth": calculate_cagr(costs.iloc[-5], costs.iloc[-1], 1),
            }
        )
    return pd.DataFrame(rows)


def intrinsic_value(current_eps, growth_rate, years, discount_rate, current_price, multiples):
    eps = float(current_eps)
    projections = []
    for year in range(int(years) + 1):
        if year > 0:
            eps *= 1 + growth_rate / 100
        projections.append({"Year": year, "Projected EPS": eps})
    final_eps = projections[-1]["Projected EPS"]
    result_rows = []
    for dr in [discount_rate - 2, discount_rate, discount_rate + 2]:
        if dr <= 0:
            continue
        dr_dec = dr / 100
        interim_pv = sum(projections[y]["Projected EPS"] / ((1 + dr_dec) ** y) for y in range(1, int(years) + 1))
        for multiple in multiples:
            future_price = final_eps * multiple
            intrinsic = future_price / ((1 + dr_dec) ** int(years)) + interim_pv
            upside = ((intrinsic / current_price) - 1) * 100
            result_rows.append(
                {
                    "Discount Rate": dr,
                    "Exit P/E": multiple,
                    "Future Price": future_price,
                    "Intrinsic Value": intrinsic,
                    "Upside/Downside %": upside,
                }
            )
    return pd.DataFrame(projections), pd.DataFrame(result_rows)


def reverse_dcf(target_price, current_eps, years, discount_rate, terminal_multiple):
    target_price = float(target_price)
    current_eps = float(current_eps)
    years = int(years)
    dr = float(discount_rate) / 100
    terminal_multiple = float(terminal_multiple)

    def forward(growth_rate):
        projected_eps = current_eps * ((1 + growth_rate) ** years)
        terminal = projected_eps * terminal_multiple / ((1 + dr) ** years)
        interim = sum((current_eps * ((1 + growth_rate) ** y)) / ((1 + dr) ** y) for y in range(1, years + 1))
        return terminal + interim

    low, high = -0.99, 5.0
    implied = np.nan
    for _ in range(120):
        mid = (low + high) / 2
        calc = forward(mid)
        if abs(calc - target_price) < 0.01:
            implied = mid
            break
        if calc > target_price:
            high = mid
        else:
            low = mid
        implied = mid
    return implied * 100


def bulk_dcf_df(all_data: Dict[str, Dict[str, object]], metric_statement: str, metric_name: str, discount_rate: float, terminal_multiple: float) -> pd.DataFrame:
    rows = []
    for ticker, data in all_data.items():
        price = data.get("_yfinance_current_price_")
        ps = per_share_ttm_series(data, metric_statement, metric_name)
        if not price or ps.empty:
            continue
        latest = float(ps.iloc[-1])
        growths = {}
        for years in [1, 3, 5, 10]:
            periods = years * 4
            growths[f"{years}Y CAGR"] = calculate_cagr(ps.iloc[-periods - 1], latest, years) if len(ps) > periods else np.nan
        chosen_growth = np.nanmedian([v for v in growths.values() if pd.notna(v)])
        if pd.isna(chosen_growth):
            chosen_growth = 0
        _, vals = intrinsic_value(latest, chosen_growth, 5, discount_rate, float(price), [terminal_multiple])
        iv = vals.loc[vals["Discount Rate"].sub(discount_rate).abs().idxmin(), "Intrinsic Value"] if not vals.empty else np.nan
        rows.append(
            {
                "Ticker": ticker,
                "Price": price,
                "Metric/Shr": latest,
                "Chosen Growth %": chosen_growth,
                "5Y Value": iv,
                "Upside %": ((iv / price) - 1) * 100 if price else np.nan,
                **growths,
            }
        )
    return pd.DataFrame(rows)


def build_dividend_map_df(all_data: Dict[str, Dict[str, object]], min_yield: float) -> pd.DataFrame:
    rows = []
    for ticker, data in all_data.items():
        price = data.get("_yfinance_current_price_")
        div = get_series(data, "Custom Metrics", "TTM Dividend/Shr")
        payout = get_series(data, "Custom Metrics", "Dividend Payout Ratio %")
        if not price or div.empty:
            continue
        dividend = div.iloc[-1]
        yield_pct = (dividend / price) * 100 if price else np.nan
        if pd.notna(yield_pct) and yield_pct >= min_yield:
            rows.append(
                {
                    "Ticker": ticker,
                    "Price": price,
                    "TTM Dividend/Shr": dividend,
                    "Dividend Yield %": yield_pct,
                    "Payout Ratio %": payout.iloc[-1] if not payout.empty else np.nan,
                }
            )
    return pd.DataFrame(rows)


def to_excel_bytes(sheets: Dict[str, pd.DataFrame]) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            safe_name = re.sub(r"[\[\]\*\?\/\\:]", "_", sheet_name)[:31]
            df.to_excel(writer, sheet_name=safe_name, index=True)
    return buffer.getvalue()


def pickle_snapshot_bytes(data: Dict[str, Dict[str, object]]) -> bytes:
    buffer = io.BytesIO()
    pd.to_pickle(data, buffer)
    return buffer.getvalue()


def load_snapshot(uploaded_file) -> Dict[str, Dict[str, object]]:
    return pd.read_pickle(uploaded_file)


def available_tickers() -> List[str]:
    return sorted(st.session_state.data.keys())


def render_data_loader() -> None:
    st.sidebar.header("Data")
    st.sidebar.caption(f"Macrotrends client: {macrotrends_client_name()}")
    tickers_text = st.sidebar.text_area("Tickers", value="MSFT, AAPL, GOOGL", height=90)
    tickers = [t.strip().upper() for t in re.split(r"[\s,;]+", tickers_text) if t.strip()]
    polite_delay = st.sidebar.number_input("Delay between Macrotrends requests", 0.0, 60.0, 8.0, 0.5)
    max_retries = st.sidebar.number_input("Max retries per Macrotrends page", 1, 10, 2, 1)
    stop_on_403 = st.sidebar.checkbox("Stop quickly on Macrotrends 403", value=True)
    col_a, col_b = st.sidebar.columns(2)
    if col_a.button("Fetch", type="primary", use_container_width=True):
        result = fetch_ticker_data(
            tickers,
            polite_delay=polite_delay,
            max_retries=int(max_retries),
            stop_on_403=stop_on_403,
        )
        st.session_state.data = result.data
        st.session_state.logs = result.logs
        st.session_state.last_summary = build_summary_df(result.data)
        st.rerun()
    if col_b.button("Clear", use_container_width=True):
        st.session_state.data = {}
        st.session_state.logs = []
        st.session_state.last_summary = pd.DataFrame()
        st.rerun()
    uploaded = st.sidebar.file_uploader("Load snapshot", type=["pkl", "pickle"])
    if uploaded is not None and st.sidebar.button("Use snapshot", use_container_width=True):
        st.session_state.data = load_snapshot(uploaded)
        st.session_state.last_summary = build_summary_df(st.session_state.data)
        st.rerun()
    if st.session_state.data:
        st.sidebar.download_button(
            "Download snapshot",
            pickle_snapshot_bytes(st.session_state.data),
            "financial_dashboard_snapshot.pkl",
            use_container_width=True,
        )
    elif macrotrends_was_blocked():
        st.sidebar.info("No snapshot is available because Macrotrends blocked this fetch.")
    if yf is None:
        st.sidebar.warning("yfinance is not installed. Price and market data will be limited.")


def render_summary_tab() -> None:
    if not st.session_state.data:
        if macrotrends_was_blocked():
            st.warning(
                "Macrotrends returned 403 for the statement pages. The app keeps financial statements "
                "Macrotrends-only, so no factor rows can be built until Macrotrends returns parseable data. "
                "This usually means the deployment host/network is blocked by Macrotrends."
            )
            st.markdown(
                """
                **Use the snapshot workflow**

                1. Run this app locally from your machine.
                2. Fetch the tickers locally.
                3. Click `Download snapshot` in the sidebar.
                4. Upload that `.pkl` snapshot into the hosted app.

                The hosted app will then use the Macrotrends-derived statement data from the snapshot without using yfinance statement fallback.
                """
            )
            with st.expander("Latest Macrotrends fetch diagnostics"):
                st.write(f"HTTP client: `{macrotrends_client_name()}`")
                st.text("\n".join(st.session_state.logs[-40:]))
        else:
            st.info("Fetch tickers or load a snapshot to populate the dashboard.")
        return
    df = build_summary_df(st.session_state.data)
    st.session_state.last_summary = df
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tickers", len(df))
    c2.metric("Median P/E", fmt_num(df["P/E"].median()) if "P/E" in df else "N/A")
    c3.metric("Median P/FCF", fmt_num(df["P/FCF"].median()) if "P/FCF" in df else "N/A")
    c4.metric("Median FCF 5Y", fmt_pct(df["FCF 5Y"].median()) if "FCF 5Y" in df else "N/A")
    st.dataframe(df, use_container_width=True, height=560)
    st.download_button("Export summary CSV", df.to_csv(index=False), "summary.csv", "text/csv")


def render_chart_tab() -> None:
    tickers = available_tickers()
    if not tickers:
        st.info("Load data first.")
        return
    top = st.columns([1, 1, 1, 1])
    ticker = top[0].selectbox("Ticker", tickers)
    data = st.session_state.data[ticker]
    statements = [k for k, v in data.items() if isinstance(v, pd.DataFrame)]
    statement = top[1].selectbox("Statement", statements, index=statements.index("Income Statement") if "Income Statement" in statements else 0)
    df = data[statement]
    assert isinstance(df, pd.DataFrame)
    metric = top[2].selectbox("Metric", list(df.index))
    mode = top[3].selectbox("View", ["Raw", "TTM", "TTM per share"])
    series = get_series(data, statement, metric)
    if mode == "TTM":
        series = ttm_series(data, statement, metric)
    elif mode == "TTM per share":
        series = per_share_ttm_series(data, statement, metric)
    if series.empty:
        st.warning("No numeric data for that selection.")
        return
    fig, ax = plt.subplots(figsize=(12, 5), facecolor=DARK["bg"])
    ax.set_facecolor(DARK["plot"])
    ax.plot(series.index, series.values, marker="o", color=DARK["line"], label=metric)
    avg_window = st.slider("Average line window", 0, min(40, len(series)), 4)
    if avg_window > 1:
        ax.plot(series.index, series.rolling(avg_window).mean(), color=DARK["avg"], label=f"{avg_window}-period average")
    ax.grid(True, color=DARK["grid"], linestyle="--", alpha=0.7)
    ax.tick_params(colors=DARK["fg"])
    for spine in ax.spines.values():
        spine.set_color(DARK["fg"])
    ax.set_title(f"{ticker} - {metric} ({mode})", color=DARK["fg"])
    ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: format_large_number(x) if abs(x) >= 1_000 else f"{x:,.2f}"))
    ax.legend()
    st.pyplot(fig, use_container_width=True)
    st.dataframe(series.rename(metric).to_frame(), use_container_width=True, height=300)
    stats = metric_stats(data, statement, metric)
    st.dataframe(pd.DataFrame([stats]), use_container_width=True)


def render_calculator_tabs() -> None:
    calc_tab, reverse_tab = st.tabs(["Intrinsic Value", "Reverse DCF"])
    with calc_tab:
        left, right = st.columns([1, 2])
        with left:
            current_eps = st.number_input("Current TTM EPS", value=2.50)
            growth_rate = st.number_input("Growth Rate %", value=15.0)
            years = st.number_input("Projection Horizon", min_value=1, max_value=50, value=5)
            discount_rate = st.number_input("Discount Rate %", value=10.0)
            current_price = st.number_input("Current Share Price", min_value=0.01, value=100.0)
            multiples_text = st.text_input("Exit P/E Multiples", value="10, 15, 20, 25")
            multiples = [float(x.strip()) for x in multiples_text.split(",") if x.strip()]
        projections, values = intrinsic_value(current_eps, growth_rate, years, discount_rate, current_price, multiples)
        with right:
            st.dataframe(projections, use_container_width=True)
            st.dataframe(values, use_container_width=True)
    with reverse_tab:
        left, right = st.columns([1, 2])
        with left:
            target_price = st.number_input("Current Stock Price", value=150.0)
            current_eps = st.number_input("Current TTM EPS", value=5.0, key="rdcf_eps")
            years = st.number_input("Projection Horizon", min_value=1, max_value=50, value=10, key="rdcf_years")
            discount_rate = st.number_input("Discount Rate %", value=10.0, key="rdcf_dr")
            terminal_multiple = st.number_input("Terminal P/E Multiple", value=15.0)
        implied = reverse_dcf(target_price, current_eps, years, discount_rate, terminal_multiple)
        right.metric("Implied Annual Growth Rate", fmt_pct(implied, 2))
        right.write(
            f"To justify ${target_price:,.2f}, earnings need to grow at roughly {fmt_pct(implied, 2)} "
            f"per year for {years} years, assuming a {discount_rate:.1f}% discount rate and "
            f"{terminal_multiple:.1f}x terminal multiple."
        )


def render_bulk_tabs() -> None:
    if not st.session_state.data:
        st.info("Load data first.")
        return
    dcf_tab, eps_tab = st.tabs(["Bulk FCF DCF", "Bulk EPS DCF"])
    with dcf_tab:
        c1, c2 = st.columns(2)
        dr = c1.number_input("Discount Rate %", value=10.0, key="bulk_fcf_dr")
        multiple = c2.number_input("Terminal Multiple", value=15.0, key="bulk_fcf_mult")
        df = bulk_dcf_df(st.session_state.data, "Custom Metrics", "Free Cash Flow (Simple)", dr, multiple)
        st.dataframe(df, use_container_width=True, height=550)
        st.download_button("Export FCF DCF CSV", df.to_csv(index=False), "bulk_fcf_dcf.csv", "text/csv")
    with eps_tab:
        c1, c2 = st.columns(2)
        dr = c1.number_input("Discount Rate %", value=10.0, key="bulk_eps_dr")
        multiple = c2.number_input("Terminal P/E", value=15.0, key="bulk_eps_mult")
        df = bulk_dcf_df(st.session_state.data, "Income Statement", "Net Income", dr, multiple)
        st.dataframe(df, use_container_width=True, height=550)
        st.download_button("Export EPS DCF CSV", df.to_csv(index=False), "bulk_eps_dcf.csv", "text/csv")


def render_tables_tab() -> None:
    if not st.session_state.data:
        st.info("Load data first.")
        return
    summary, bs, costs, divs = st.tabs(["All Tabs", "Balance Sheet Health", "Rising Costs", "Dividend Map"])
    with summary:
        df = build_summary_df(st.session_state.data)
        st.dataframe(df, use_container_width=True, height=560)
    with bs:
        df = build_balance_sheet_df(st.session_state.data)
        st.dataframe(df, use_container_width=True, height=560)
    with costs:
        df = build_rising_costs_df(st.session_state.data)
        st.dataframe(df, use_container_width=True, height=560)
    with divs:
        min_yield = st.number_input("Minimum dividend yield %", min_value=0.0, value=0.0)
        df = build_dividend_map_df(st.session_state.data, min_yield)
        st.dataframe(df, use_container_width=True, height=560)
    sheets = {
        "Summary": build_summary_df(st.session_state.data),
        "Balance Sheet": build_balance_sheet_df(st.session_state.data),
        "Rising Costs": build_rising_costs_df(st.session_state.data),
        "Dividend Map": build_dividend_map_df(st.session_state.data, 0),
    }
    st.download_button("Export workbook", to_excel_bytes(sheets), "dashboard_tables.xlsx")


def render_raw_tab() -> None:
    tickers = available_tickers()
    if not tickers:
        st.info("Load data first.")
        return
    ticker = st.selectbox("Ticker", tickers, key="raw_ticker")
    data = st.session_state.data[ticker]
    statements = [k for k, v in data.items() if isinstance(v, pd.DataFrame)]
    statement = st.selectbox("Statement", statements, key="raw_statement")
    df = data[statement]
    assert isinstance(df, pd.DataFrame)
    st.dataframe(df, use_container_width=True, height=650)
    st.download_button("Export statement CSV", df.to_csv(), f"{ticker}_{statement}.csv", "text/csv")


def render_logs_tab() -> None:
    st.text_area("Fetch logs", "\n".join(st.session_state.logs), height=650)


def main() -> None:
    init_state()
    apply_theme()
    render_data_loader()
    st.title("Financial Dashboard")
    tabs = st.tabs(
        [
            "Summary",
            "Charts & Analysis",
            "Calculators",
            "Bulk DCF",
            "Tables",
            "Raw Statements",
            "Logs",
        ]
    )
    with tabs[0]:
        render_summary_tab()
    with tabs[1]:
        render_chart_tab()
    with tabs[2]:
        render_calculator_tabs()
    with tabs[3]:
        render_bulk_tabs()
    with tabs[4]:
        render_tables_tab()
    with tabs[5]:
        render_raw_tab()
    with tabs[6]:
        render_logs_tab()


if __name__ == "__main__":
    main()
