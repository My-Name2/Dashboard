# Financial Dashboard Streamlit App

This is a Streamlit conversion of the pasted Tkinter financial dashboard. It keeps the same core factors and workflows where they map cleanly to Streamlit:

- Macrotrends quarterly statement scraping
- yfinance price, market cap, sector, industry, and price history metadata
- custom metrics such as margins, FCF, ROE, ROA, leverage, liquidity, total costs, dividends
- summary factors for EPS, revenue, FCF, EBIT, CFO, gross profit, valuation ratios, growth, positive observations, slope, R2, RMSE, and CV
- charts with raw, TTM, and TTM-per-share views
- intrinsic value and reverse DCF calculators
- bulk FCF and EPS DCF tables
- balance sheet health, rising costs, dividend map, raw statements, logs, CSV/Excel exports
- snapshot export/load to avoid re-fetching large ticker sets

Run it with:

```powershell
pip install -r requirements.txt
streamlit run app.py
```

Macrotrends can rate-limit or block cloud-hosted fetches. Statement-derived factors stay Macrotrends-only; if Macrotrends does not return parseable statements for a ticker, that ticker is omitted from the factor tables.

The app uses the same request style as the original Tkinter script: plain `requests`, rotating user agents, patient retry/backoff handling, redirect correction, and longer pauses between statement requests. If a hosted deployment still receives HTTP 403 responses, Macrotrends is likely blocking that host/network. In that case, run the app locally to fetch data, download a snapshot, and upload that snapshot to the hosted app.

You can also build the snapshot from the command line:

```powershell
python fetch_snapshot.py "MSFT,AAPL,GOOGL" --output financial_dashboard_snapshot.pkl --delay 8
```

Then upload `financial_dashboard_snapshot.pkl` in the hosted app sidebar.
