"""
シグナル履歴 一括クリーンアップ＆買値修正
・フォローアップ✅推奨のみ残す（A）
・買値をT+2始値に修正（B）
手動実行専用スクリプト
"""

import io, os, json
import warnings
import requests
import numpy as np
import pandas as pd
import yfinance as yf
import gspread
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials

warnings.filterwarnings("ignore")

JST            = ZoneInfo("Asia/Tokyo")
SHEET_SIGNALS  = "シグナル履歴"
SHEET_FOLLOWUP = "フォローアップ"

def get_client():
    creds_dict = json.loads(os.environ["GOOGLE_SHEETS_CREDENTIALS"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

def fetch_open_prices(codes: list, signal_dates: list) -> dict:
    """対象銘柄のT+2始値をyfinanceから取得"""
    if not codes:
        return {}

    # 必要な日付範囲
    dates = pd.to_datetime(signal_dates)
    start = (dates.min() - pd.offsets.BDay(1)).strftime("%Y-%m-%d")
    end   = (dates.max() + pd.offsets.BDay(5)).strftime("%Y-%m-%d")

    tickers = [f"{c}.T" for c in codes]
    print(f"  yfinance取得中: {len(tickers)}銘柄 ({start} ～ {end})")

    raw = yf.download(
        tickers, start=start, end=end,
        interval="1d", auto_adjust=True,
        progress=False, group_by="column", threads=True,
    )

    if raw is None or raw.empty:
        return {}

    open_ = raw["Open"].copy() if "Open" in raw.columns else pd.DataFrame()
    if open_.empty:
        return {}

    open_.columns = [str(c).replace(".T", "") for c in open_.columns]
    return open_

def main():
    print("▶ クリーンアップ＆買値修正 開始")

    gc          = get_client()
    spreadsheet = gc.open_by_key(os.environ["SHEETS_ID"])

    # ── A: フォローアップから✅推奨を収集 ──
    ws_fu   = spreadsheet.worksheet(SHEET_FOLLOWUP)
    fu_rows = ws_fu.get_all_values()

    recommended    = set()   # (シグナル日, コード)
    t2_dates       = {}      # (シグナル日, コード) -> T+2エントリー日

    followup_dates = set()
    for row in fu_rows[1:]:
        if len(row) < 8:
            continue
        followup_dates.add(row[0])
        if "✅" in row[6]:
            key = (row[0], row[1])
            recommended.add(key)
            t2_dates[key] = row[7]  # T+2推奨エントリー日

    print(f"✅ 推奨銘柄: {len(recommended)}件")
    print(f"フォローアップ対象日: {sorted(followup_dates)}")

    # ── シグナル履歴を読み込み ──
    ws_sig   = spreadsheet.worksheet(SHEET_SIGNALS)
    all_rows = ws_sig.get_all_values()
    header   = all_rows[0]
    data     = all_rows[1:]

    # キープする行を選別
    keep_rows    = []
    delete_count = 0

    for row in data:
        if len(row) < 2:
            keep_rows.append(row)
            continue
        signal_date = row[0]
        code        = row[1]

        if signal_date not in followup_dates:
            keep_rows.append(row)  # 対象外日付はキープ
        elif (signal_date, code) in recommended:
            keep_rows.append(row)  # ✅推奨はキープ
        else:
            delete_count += 1      # ⏭スキップは削除

    print(f"\n削除: {delete_count}件 / キープ: {len(keep_rows)}件")

    # ── B: T+2始値を取得して買値を修正 ──
    # 修正が必要な行（フォローアップ対象日のもの）
    fix_targets = []
    for row in keep_rows:
        if len(row) < 5:
            continue
        sig_date = row[0]
        code     = row[1]
        key      = (sig_date, code)
        if key in t2_dates and t2_dates[key] and t2_dates[key] != "-":
            fix_targets.append({
                "sig_date": sig_date,
                "code":     code,
                "t2_date":  t2_dates[key],
            })

    # yfinanceで始値取得
    open_prices = {}
    if fix_targets:
        codes        = list({r["code"] for r in fix_targets})
        signal_dates = list({r["sig_date"] for r in fix_targets})
        open_df      = fetch_open_prices(codes, signal_dates)

        for r in fix_targets:
            t2_dt = pd.Timestamp(r["t2_date"])
            code  = r["code"]
            try:
                if code in open_df.columns and t2_dt in open_df.index:
                    val = open_df.loc[t2_dt, code]
                    if val is not None and not pd.isna(val):
                        open_prices[(r["sig_date"], code)] = round(float(val), 0)
            except Exception:
                pass

    print(f"T+2始値取得: {len(open_prices)}件")

    # keep_rowsのE列（買値）を修正
    fixed_count = 0
    for row in keep_rows:
        if len(row) < 5:
            continue
        key = (row[0], row[1])
        if key in open_prices:
            old = row[4]
            row[4] = open_prices[key]
            print(f"  {row[0]} {row[1]} {row[2]}: 買値 {old} → {row[4]}")
            fixed_count += 1

    print(f"買値修正: {fixed_count}件")

    # ── シートを書き直す ──
    print("\nシートを更新中...")
    ws_sig.clear()
    ws_sig.append_row(header)
    if keep_rows:
        ws_sig.append_rows(keep_rows, value_input_option="RAW")

    print(f"✅ 完了: {len(keep_rows)}件を残しました（削除{delete_count}件 / 買値修正{fixed_count}件）")

if __name__ == "__main__":
    main()
