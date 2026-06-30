"""
日本股市資料抓取（J-Quants 代號清單 + Yahoo Finance 收盤價）
- 代號清單：J-Quants API V2（免費）
- 收盤價：Yahoo Finance API（免費）
- 市場範圍：東証プライム + スタンダード

產生兩份 CSV：
  data/{今天日期}/日本上市收盤價.csv
  data/{今天日期}/日本排行榜.csv

使用方式：
  python fetch_japan_stocks.py                 # 整批抓全市場
  python fetch_japan_stocks.py --symbol 6981    # 只查單一代號（跨市場對應股價用，4或5位數皆可）
"""

import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

# ─────────────────────────────────────────────
# ⚠️ 填入你的設定
# ─────────────────────────────────────────────
JQUANTS_API_KEY = "jbuGTyLu8wTS7iS8qvkFPSz4sLtBST9EtKqfWRGDAiQ"

TODAY = date.today().strftime("%Y-%m-%d")
DATA_DIR = Path("data") / TODAY

JQUANTS_BASE = "https://api.jquants.com/v2"
YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"

HEADERS_JQUANTS = {"x-api-key": JQUANTS_API_KEY}
HEADERS_YAHOO = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

# 市場代碼：Prime=111, Standard=112
TARGET_MARKETS = {"111", "112"}


# ─────────────────────────────────────────────
# Step 1: 取得代號清單（J-Quants）
# ─────────────────────────────────────────────

def fetch_symbol_list() -> list[dict]:
    """取得 Prime + Standard 上場銘柄清單"""
    print("  取得上場銘柄清單（J-Quants）...", flush=True)
    r = requests.get(
        f"{JQUANTS_BASE}/equities/master",
        headers=HEADERS_JQUANTS,
        timeout=30
    )
    if r.status_code != 200:
        print(f"  [錯誤] 銘柄清單取得失敗: {r.status_code} {r.text[:200]}")
        return []

    all_stocks = r.json().get("data", [])
    print(f"  全市場銘柄：{len(all_stocks)} 檔")

    # 過濾 Prime + Standard，排除 ETF（代號非純4位數字）
    # V2 API 欄位：Mkt=市場代碼(0111=Prime, 0112=Standard)
    filtered = [
        s for s in all_stocks
        if str(s.get("Mkt", "")).strip() in {"0111", "0112"}
    ]

    print(f"  Prime + Standard：{len(filtered)} 檔", flush=True)
    return filtered


# ─────────────────────────────────────────────
# Step 2: 用 Yahoo Finance 抓收盤價
# ─────────────────────────────────────────────

def normalize_yahoo_code(code: str) -> str:
    """J-Quants 代號是5位數（如13010），Yahoo 需要4位數（如1301.T）"""
    code = str(code).strip()
    if len(code) == 5 and code.endswith("0"):
        return code[:-1]
    return code


def fetch_yahoo_price(code: str) -> dict | None:
    """抓單一股票當日收盤價"""
    yahoo_code = normalize_yahoo_code(code)
    symbol = f"{yahoo_code}.T"
    try:
        r = requests.get(
            f"{YAHOO_BASE}/{symbol}",
            headers=HEADERS_YAHOO,
            timeout=10
        )
        if r.status_code != 200:
            return None
        result = r.json().get("chart", {}).get("result", [])
        if not result:
            return None
        meta = result[0].get("meta", {})
        close = float(meta.get("regularMarketPrice", 0) or 0)
        prev_close = float(meta.get("chartPreviousClose", 0) or meta.get("previousClose", 0) or 0)
        if close == 0:
            return None
        change = round(close - prev_close, 2) if prev_close else 0.0
        change_pct = round((change / prev_close * 100), 2) if prev_close else 0.0
        volume = int(meta.get("regularMarketVolume", 0) or 0)
        high = float(meta.get("regularMarketDayHigh", 0) or 0)
        low = float(meta.get("regularMarketDayLow", 0) or 0)
        open_ = float(meta.get("regularMarketOpen", 0) or 0)
        amount_oku = round(volume * close / 1e8, 4) if volume and close else 0.0
        return {
            "收盤": close,
            "漲跌": change,
            "漲跌幅": change_pct,
            "開盤": open_,
            "最高": high,
            "最低": low,
            "昨收": prev_close,
            "成交量_千股": volume // 1000,
            "成交金額_億": amount_oku,
        }
    except Exception:
        return None


def batch_fetch_yahoo(symbols: list[dict]) -> list[dict]:
    """批次抓取，回傳 records"""
    records = []
    total = len(symbols)
    success = 0
    failed = 0

    for i, info in enumerate(symbols, 1):
        code = str(info.get("Code", "")).strip()
        name = info.get("CoName", info.get("CompanyName", info.get("Name", "")))
        sector = info.get("S17Nm", info.get("Sector17CodeName", info.get("SectorName", "その他")))

        if i % 200 == 0:
            print(f"  進度：{i}/{total}（成功 {success}，失敗 {failed}）", flush=True)

        price = fetch_yahoo_price(code)
        if price:
            records.append({
                "代號": code,
                "名稱": name,
                "分類": sector,
                **price,
            })
            success += 1
        else:
            failed += 1

        # 每50筆休息避免限速
        if i % 50 == 0:
            time.sleep(1)
        else:
            time.sleep(0.05)

    print(f"  完成：成功 {success}，失敗 {failed}", flush=True)
    return records


# ─────────────────────────────────────────────
# Step 3: 組合 CSV
# ─────────────────────────────────────────────

def to_csv_df(records: list[dict]) -> pd.DataFrame:
    rows = []
    for rec in records:
        rows.append({
            "分類": rec["分類"],
            "名稱": rec["名稱"],
            "代號": rec["代號"],
            "股價(收盤/最新)": rec["收盤"],
            "漲跌": rec["漲跌"],
            "漲跌幅(%)": f"{rec['漲跌幅']:.2f}%",
            "開盤": rec["開盤"],
            "昨收": rec["昨收"],
            "最高": rec["最高"],
            "最低": rec["最低"],
            "成交量(千股)": rec["成交量_千股"],
            "成交金額(億日圓)": rec["成交金額_億"],
            "時間": TODAY,
            "漲停": "是" if is_limit_up(rec["收盤"], rec["昨收"]) else "",
        })
    return pd.DataFrame(rows)


def calc_daily_limit(prev_close: float) -> float:
    """依前收盤價計算日股當日漲停幅度（円）"""
    if prev_close <= 0:
        return 0.0
    limits = [
        (100, 30), (200, 50), (500, 80), (700, 100),
        (1000, 150), (1500, 300), (2000, 400), (3000, 500),
        (5000, 700), (7000, 1000), (10000, 1500),
    ]
    for threshold, limit in limits:
        if prev_close < threshold:
            return float(limit)
    return prev_close * 0.15  # 10000円以上：15%


def is_limit_up(close: float, prev_close: float) -> bool:
    """判斷是否漲停"""
    if prev_close <= 0 or close <= 0:
        return False
    limit = calc_daily_limit(prev_close)
    return (close - prev_close) >= limit * 0.99  # 允許1%誤差


def build_ranking(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    def parse_pct(s):
        try:
            return float(str(s).replace("%", "").strip())
        except:
            return 0.0

    df = df.copy()
    df["_pct"] = df["漲跌幅(%)"].apply(parse_pct)
    df["_vol"] = pd.to_numeric(df["成交量(千股)"], errors="coerce").fillna(0)
    df["_amt"] = pd.to_numeric(df["成交金額(億日圓)"], errors="coerce").fillna(0)

    all_records = []
    for rank_type, sort_col, only_up in [
        ("成交量排行", "_vol", False),
        ("漲幅排行", "_pct", True),
        ("成交金額排行", "_amt", False),
    ]:
        work = df.copy()
        if only_up:
            work = work[work["_pct"] > 0]
        work = work.sort_values(sort_col, ascending=False).head(100)
        for i, (_, row) in enumerate(work.iterrows(), 1):
            all_records.append({
                "排行類型": rank_type,
                "名次": i,
                "名稱": row["名稱"],
                "代號": row["代號"],
                "股價": row["股價(收盤/最新)"],
                "漲跌": row["漲跌"],
                "漲跌幅(%)": row["漲跌幅(%)"],
                "最高": row["最高"],
                "最低": row["最低"],
                "成交量(千股)": row["成交量(千股)"],
                "成交金額(億日圓)": row["成交金額(億日圓)"],
            })
    return pd.DataFrame(all_records)


# ─────────────────────────────────────────────
# 單一代號查詢模式（給跨市場對應股價用）
# ─────────────────────────────────────────────

def lookup_single_symbol(code: str) -> dict | None:
    """給一個代號（4或5位數皆可），查詢當日漲跌幅"""
    price = fetch_yahoo_price(code)
    if price and price.get("收盤", 0):
        price["代號"] = normalize_yahoo_code(code)
        price["漲停"] = is_limit_up(price["收盤"], price["昨收"])
        return price
    return None


def cmd_single_symbol(code: str):
    """CLI 單一代號查詢，輸出 JSON 方便 Claude Code 解析"""
    result = lookup_single_symbol(code)
    if result is None:
        print(json.dumps({"代號": code, "找到": False}, ensure_ascii=False))
        sys.exit(1)
    output = {
        "代號": result["代號"],
        "找到": True,
        "收盤": result["收盤"],
        "漲跌幅(%)": result["漲跌幅"],
        "漲停": result["漲停"],
        "資料日期": TODAY,
    }
    print(json.dumps(output, ensure_ascii=False))
    sys.exit(0)


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    print(f"=== 日本股市資料抓取 ===")
    print(f"執行日期：{TODAY}\n")

    if JQUANTS_API_KEY == "填入你的J-Quants_API_Key":
        print("[錯誤] 請先填入 JQUANTS_API_KEY")
        sys.exit(1)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 取代號清單
    symbols = fetch_symbol_list()
    if not symbols:
        print("[錯誤] 無法取得代號清單")
        sys.exit(1)

    # 批次抓 Yahoo 收盤價
    print(f"\n開始抓取 {len(symbols)} 檔股票收盤價（Yahoo Finance）...")
    records = batch_fetch_yahoo(symbols)

    if not records:
        print("[錯誤] 無法取得任何收盤價")
        sys.exit(1)

    # 組合並儲存
    print("\n組合資料...")
    main_df = to_csv_df(records)
    rank_df = build_ranking(main_df)

    main_df.to_csv(DATA_DIR / "日本上市收盤價.csv", index=False, encoding="utf-8-sig")
    rank_df.to_csv(DATA_DIR / "日本排行榜.csv", index=False, encoding="utf-8-sig")

    print(f"\n[完成] 日本上市收盤價.csv：{len(main_df)} 筆")
    print(f"[完成] 日本排行榜.csv：{len(rank_df)} 筆")

    ok = True
    if len(main_df) < 500:
        print(f"[驗證失敗] 日本股票 {len(main_df)} < 500 筆"); ok = False
    if ok:
        print("[驗證通過]")
    return ok


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", help="只查詢單一代號的當日漲跌幅（不執行整批爬蟲）")
    args = parser.parse_args()

    if args.symbol:
        cmd_single_symbol(args.symbol)
    else:
        sys.exit(0 if main() else 1)
