"""
株式シグナル通知システム v4.1
毎日自動実行 → Gmail送信 + Google Sheets記録・損益追跡

変更点 (v4.0 → v4.1):
  - Gemini API連携追加（⭐両方一致銘柄の下落要因を自動分析）
  - ⭐銘柄の冗長な「ポイント」行を削除（Gemini分析に置き換え）
  - タイムゾーン比較をUTCに変更（GitHub Actions遅延対策）

GitHub Actions で動かすスクリプト
"""

import io
import os
import json
import smtplib
import warnings
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import numpy as np
import pandas as pd
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials

warnings.filterwarnings("ignore")

# ============================================================
# 設定
# ============================================================

VOL_WINDOW       = 20
VOL_MULT         = 3.0
GAP_THRESHOLD    = -0.05
HOLDING_DAYS     = 90
MAX_MARGIN_RATIO = 5.0

JST = ZoneInfo("Asia/Tokyo")

SHEET_SIGNALS = "シグナル履歴"
SHEET_RUNLOG  = "配信ログ"

HEADERS = [
    "シグナル日", "コード", "銘柄名", "シグナル種別",
    "買値目安(終値)", "エントリー推奨日", "推奨売却日",
    "現在値", "損益(%)", "保有日数", "ステータス",
    "期待値(5日)", "期待値(10日)", "期待値(20日)"
]

RUNLOG_HEADERS = [
    "対象日", "実行日時", "新規シグナル数", "メール件名"
]

# JPX取得失敗時のフォールバック（旧来の50銘柄）
STOCKS_FALLBACK = {
    "7203": "トヨタ自動車",    "7267": "ホンダ",
    "7269": "スズキ",          "7270": "SUBARU",
    "7201": "日産自動車",      "6758": "ソニーグループ",
    "6861": "キーエンス",      "6954": "ファナック",
    "6981": "村田製作所",      "6367": "ダイキン工業",
    "6702": "富士通",          "6701": "NEC",
    "6752": "パナソニック",    "7751": "キヤノン",
    "7733": "オリンパス",      "6273": "SMC",
    "6503": "三菱電機",        "9984": "ソフトバンクG",
    "9432": "NTT",             "9433": "KDDI",
    "9434": "ソフトバンク",    "8306": "三菱UFJ FG",
    "8316": "三井住友FG",      "8411": "みずほFG",
    "8031": "三井物産",        "8058": "三菱商事",
    "8001": "伊藤忠商事",      "8053": "住友商事",
    "4063": "信越化学工業",    "4188": "三菱ケミカルG",
    "4183": "三井化学",        "5401": "日本製鉄",
    "5108": "ブリヂストン",    "4502": "武田薬品工業",
    "4519": "中外製薬",        "4568": "第一三共",
    "4543": "テルモ",          "3382": "セブン&アイHD",
    "2802": "味の素",          "2914": "JT",
    "3407": "旭化成",          "8802": "三菱地所",
    "8801": "三井不動産",      "9020": "JR東日本",
    "9022": "JR東海",          "9064": "ヤマトHD",
    "6098": "リクルートHD",    "7974": "任天堂",
    "8035": "東京エレクトロン","4307": "野村総合研究所",
}


# ============================================================
# 共通ユーティリティ
# ============================================================

def get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"環境変数 {name} が設定されていません")
    return value


def to_float(value) -> float:
    if value is None or value == "":
        return 0.0
    return float(str(value).replace(",", "").replace("¥", "").replace("%", "").strip())


def is_valid_number(value) -> bool:
    try:
        return value is not None and not pd.isna(value) and np.isfinite(float(value))
    except Exception:
        return False


def next_business_day(dt) -> str:
    return (pd.Timestamp(dt) + pd.offsets.BDay(1)).strftime("%Y/%m/%d")


def add_business_days(dt, days: int) -> str:
    return (pd.Timestamp(dt) + pd.offsets.BDay(days)).strftime("%Y/%m/%d")


def business_days_between(start_dt, end_dt) -> int:
    start_date = pd.Timestamp(start_dt).date()
    end_date   = pd.Timestamp(end_dt).date()
    return int(np.busday_count(start_date, end_date))


# ============================================================
# Gemini API（下落要因分析）
# ============================================================

def analyze_drop_reason(code: str, name: str, ret: float, gap: float, date_str: str) -> str:
    """Gemini + Google検索で⭐銘柄の下落要因を分析する"""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "（GEMINI_API_KEY未設定）"

    try:
        from google import genai as gai
        from google.genai.types import Tool, GenerateContentConfig, GoogleSearch

        client = gai.Client(api_key=api_key)

        prompt = (
            f"{date_str}に{name}(コード{code})の株価が{ret:+.1f}%下落しました。"
            f"最新ニュースをもとに日本語5行以内で教えてください。"
            f"1. 下落の主な要因(1〜2文) "
            f"2. 一時的か構造的か "
            f"3. 逆張りの適性(高い/中程度/低い)と理由(1文)"
        )
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
            config=GenerateContentConfig(
                tools=[Tool(google_search=GoogleSearch())]
            )
        )

        return response.text.strip()

    except Exception as e:
        return f"（分析エラー: {e}）"


# ============================================================
# JPX データ取得
# ============================================================

def fetch_prime_stocks() -> dict:
    """JPXからプライム市場の全銘柄一覧を取得する"""
    url = (
        "https://www.jpx.co.jp/markets/statistics-equities"
        "/misc/tvdivq0000001vg2-att/data_j.xls"
    )
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()

        df = pd.read_excel(io.BytesIO(resp.content))
        prime = df[df["市場・商品区分"].str.contains("プライム", na=False)]

        stocks = {}
        for _, row in prime.iterrows():
            try:
                code = str(int(row["コード"])).zfill(4)
                name = str(row["銘柄名"])
                stocks[code] = name
            except Exception:
                continue

        print(f"✅ プライム銘柄取得: {len(stocks)}社")
        return stocks

    except Exception as e:
        print(f"⚠️ プライム銘柄取得失敗: {e}")
        print("   → フォールバック: 組み込み50銘柄を使用")
        return STOCKS_FALLBACK


def fetch_margin_ratios() -> dict:
    """JPXから最新の信用倍率を取得する（コード → 倍率）"""
    today = datetime.now(JST).date()

    for weeks_back in range(4):
        days_back  = (today.weekday() - 4) % 7 + weeks_back * 7
        target     = today - timedelta(days=days_back)
        date_str   = target.strftime("%Y%m%d")
        url = (
            "https://www.jpx.co.jp/markets/statistics-equities"
            f"/margin/nlsgeu000000xbna-att/data_{date_str}.csv"
        )

        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code != 200:
                continue

            df = pd.read_csv(
                io.BytesIO(resp.content),
                encoding="shift_jis",
                skiprows=1,
            )

            code_cols  = [c for c in df.columns if "コード" in str(c)]
            ratio_cols = [c for c in df.columns if "倍率" in str(c)]

            if not code_cols or not ratio_cols:
                continue

            code_col  = code_cols[0]
            ratio_col = ratio_cols[0]

            result = {}
            for _, row in df.iterrows():
                try:
                    code  = str(int(row[code_col])).zfill(4)
                    ratio = float(row[ratio_col])
                    if ratio > 0:
                        result[code] = ratio
                except Exception:
                    continue

            print(f"✅ 信用倍率取得: {len(result)}銘柄 (基準日: {date_str})")
            return result

        except Exception as e:
            print(f"  {date_str} 取得失敗: {e}")
            continue

    print("⚠️ 信用倍率データ取得失敗 → 信用倍率フィルターなしで続行")
    return {}


# ============================================================
# Google Sheets 接続
# ============================================================

def get_sheets_client():
    raw = get_env("GOOGLE_SHEETS_CREDENTIALS")
    creds_dict = json.loads(raw)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)


def get_or_create_sheet(spreadsheet, sheet_name, headers=None):
    try:
        ws = spreadsheet.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=sheet_name, rows=2000, cols=20)
        if headers:
            ws.append_row(headers)
            end_col = chr(ord("A") + len(headers) - 1)
            ws.format(f"A1:{end_col}1", {
                "backgroundColor": {"red": 0.13, "green": 0.36, "blue": 0.62},
                "textFormat": {
                    "bold": True,
                    "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                },
                "horizontalAlignment": "CENTER",
            })
    return ws


def add_run_log(spreadsheet, date_str: str, new_count: int, subject: str):
    ws = get_or_create_sheet(spreadsheet, SHEET_RUNLOG, RUNLOG_HEADERS)
    ws.append_row([
        date_str,
        datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S"),
        new_count,
        subject,
    ])


# ============================================================
# データ取得・シグナル計算
# ============================================================

def fetch_data(stocks: dict):
    tickers = [f"{code}.T" for code in stocks.keys()]
    start   = (datetime.now(JST) - timedelta(days=240)).strftime("%Y-%m-%d")
    end     = (datetime.now(JST) + timedelta(days=1)).strftime("%Y-%m-%d")

    raw = yf.download(
        tickers,
        start=start,
        end=end,
        interval="1d",
        auto_adjust=True,
        progress=False,
        group_by="column",
        threads=True,
    )

    if raw is None or raw.empty:
        raise RuntimeError("yfinanceから株価データを取得できませんでした")

    required = ["Close", "Open", "Volume"]
    for col in required:
        if col not in raw.columns.get_level_values(0):
            raise RuntimeError(f"yfinanceデータに {col} 列がありません")

    close  = raw["Close"].copy()
    open_  = raw["Open"].copy()
    volume = raw["Volume"].copy()

    close.columns  = [str(c).replace(".T", "") for c in close.columns]
    open_.columns  = [str(c).replace(".T", "") for c in open_.columns]
    volume.columns = [str(c).replace(".T", "") for c in volume.columns]

    close  = close.reindex(columns=list(stocks.keys()))
    open_  = open_.reindex(columns=list(stocks.keys()))
    volume = volume.reindex(columns=list(stocks.keys()))

    return close, open_, volume


def calc_signals(close, open_, volume, stocks: dict):
    valid_close = close.dropna(how="all")
    if valid_close.empty:
        raise RuntimeError("有効な終値データがありません")

    latest = valid_close.index[-1]

    avg_vol   = volume.shift(1).rolling(VOL_WINDOW, min_periods=VOL_WINDOW).mean()
    daily_ret = close.pct_change()
    vol_ratio = volume / avg_vol
    gap       = open_ / close.shift(1) - 1

    sig_volC = (volume > VOL_MULT * avg_vol) & (daily_ret < 0)
    sig_gapN = gap < GAP_THRESHOLD

    def build_hits(signal_df):
        latest_signal = signal_df.loc[latest].fillna(False)
        hit_codes     = latest_signal[latest_signal].index.tolist()

        rows = []
        for code in hit_codes:
            if code not in close.columns:
                continue

            price = close.loc[latest, code]
            if not is_valid_number(price):
                continue

            ret_value = daily_ret.loc[latest, code] * 100
            gap_value = gap.loc[latest, code] * 100
            vol_value = vol_ratio.loc[latest, code]

            rows.append({
                "code":      code,
                "name":      stocks.get(code, code),
                "price":     float(price),
                "ret":       float(ret_value)  if is_valid_number(ret_value)  else 0.0,
                "gap":       float(gap_value)  if is_valid_number(gap_value)  else 0.0,
                "vol_ratio": float(vol_value)  if is_valid_number(vol_value)  else 0.0,
            })

        return rows

    volC_hits = build_hits(sig_volC)
    gapN_hits = build_hits(sig_gapN)

    return latest, volC_hits, gapN_hits


# ============================================================
# 買いタイミングアドバイス
# ============================================================

def get_timing_advice(signal, ret_pct, vol_ratio, gap_pct):
    ret = float(ret_pct)   if is_valid_number(ret_pct)   else 0.0
    vol = float(vol_ratio) if is_valid_number(vol_ratio)  else 0.0
    gap = float(gap_pct)   if is_valid_number(gap_pct)   else 0.0

    if signal == "volC":
        if ret <= -10:
            strength = "強🔴"
            e5, e10, e20 = "+1.0%", "+1.5%", "+1.9%"
            entry = "翌営業日の寄り付き候補。ただしニュース・決算確認を優先"
            point = "大きく下げているため、過去検証上は反発余地を確認する場面"
        elif ret <= -5:
            strength = "中🟡"
            e5, e10, e20 = "+1.0%", "+1.5%", "+1.9%"
            entry = "翌営業日の寄り付き候補"
            point = (
                f"出来高{vol:.1f}x。過去検証上は短期反発が出やすい局面"
                if vol > 0 else
                "過去検証上は短期反発が出やすい局面"
            )
        else:
            strength = "弱🟢"
            e5, e10, e20 = "+1.0%", "+1.5%", "+1.9%"
            entry = "翌営業日の寄り付き候補。ただし優先度は低め"
            point = "下落幅は比較的軽め。早めの利確も選択肢"

        advice = (
            f"  シグナル強度: {strength}\n"
            f"  エントリー  : {entry}\n"
            f"  期待リターン: 5日後{e5} / 10日後{e10} / 20日後{e20}\n"
            f"  ポイント   : {point}"
        )

    elif signal == "gapN":
        if gap <= -10:
            strength = "強🔴"
            e5, e10, e20 = "+1.0%", "+2.4%", "+4.3%"
            entry = "翌営業日から候補。ただし1〜2日様子を見る選択肢もあり"
            point = "大きなギャップダウン後の反発狙い。材料確認が重要"
        elif gap <= -7:
            strength = "中🟡"
            e5, e10, e20 = "+1.0%", "+2.4%", "+4.3%"
            entry = "翌営業日の寄り付き候補"
            point = "ギャップダウン後の反発狙い。地合いとニュース確認が重要"
        else:
            strength = "弱🟢"
            e5, e10, e20 = "+1.0%", "+2.4%", "+4.3%"
            entry = "翌営業日の寄り付き候補。ただし優先度は低め"
            point = "出来高Cとの同時シグナルがあれば、より注目度が高い"

        advice = (
            f"  シグナル強度: {strength}\n"
            f"  エントリー  : {entry}\n"
            f"  期待リターン: 5日後{e5} / 10日後{e10} / 20日後{e20}\n"
            f"  ポイント   : {point}"
        )

    else:  # both → ポイント行なし（Gemini分析に置き換え）
        strength = "最強⭐"
        e5, e10, e20 = "+2.0%", "+3.0%", "+4.3%"
        entry = "翌営業日の寄り付き候補"

        advice = (
            f"  シグナル強度: {strength}\n"
            f"  エントリー  : {entry}\n"
            f"  期待リターン: 5日後{e5} / 10日後{e10} / 20日後{e20}"
        )

    return advice, e5, e10, e20


# ============================================================
# Google Sheets 更新
# ============================================================

def update_sheets(spreadsheet, latest_date, volC_rows, gapN_rows, close):
    ws         = get_or_create_sheet(spreadsheet, SHEET_SIGNALS, HEADERS)
    all_rows   = ws.get_all_values()
    both_codes = {r["code"] for r in volC_rows} & {r["code"] for r in gapN_rows}
    date_str   = latest_date.strftime("%Y/%m/%d")

    existing_keys = set()
    updates_count = 0

    if len(all_rows) > 1:
        for i, row in enumerate(all_rows[1:], start=2):
            if len(row) < 8:
                continue

            existing_keys.add(f"{row[0]}_{row[1]}")

            status = row[10] if len(row) > 10 else ""

            if ("保有中" not in status) and ("期間終了" not in status):
                continue

            code = row[1]
            if code not in close.columns:
                continue

            try:
                buy_price = to_float(row[4])
                if buy_price <= 0:
                    continue

                current_raw = close.loc[latest_date, code]
                if not is_valid_number(current_raw):
                    continue

                current   = float(current_raw)
                pnl_pct   = (current / buy_price - 1) * 100
                sig_dt    = datetime.strptime(row[0], "%Y/%m/%d")
                hold_days = business_days_between(sig_dt, latest_date)

                sell_by    = row[6] if len(row) > 6 else ""
                new_status = "保有中📈" if pnl_pct >= 0 else "保有中📉"

                try:
                    sell_by_dt = datetime.strptime(sell_by, "%Y/%m/%d").date()
                    if pd.Timestamp(latest_date).date() >= sell_by_dt:
                        new_status = "⏰期間終了（売却検討）"
                except Exception:
                    pass

                ws.update(
                    f"H{i}:K{i}",
                    [[round(current, 0), round(pnl_pct, 2), hold_days, new_status]]
                )
                updates_count += 1

            except Exception as e:
                print(f"  行{i}更新エラー: {e}")

    new_rows = []

    def make_row(r, signal_type):
        key = f"{date_str}_{r['code']}"
        if key in existing_keys:
            return

        if not is_valid_number(r.get("price")):
            return

        sig_key = "both" if r["code"] in both_codes else (
            "volC" if "出来高" in signal_type else "gapN"
        )

        _, e5, e10, e20 = get_timing_advice(
            sig_key,
            r.get("ret"),
            r.get("vol_ratio"),
            r.get("gap"),
        )

        price = float(r["price"])

        new_rows.append([
            date_str,
            r["code"],
            r["name"],
            signal_type,
            round(price, 0),
            next_business_day(latest_date),
            add_business_days(latest_date, HOLDING_DAYS),
            round(price, 0),
            0.0,
            0,
            "保有中📊",
            e5,
            e10,
            e20,
        ])

    for r in volC_rows:
        make_row(r, "⭐両方一致" if r["code"] in both_codes else "🔵出来高C")

    for r in gapN_rows:
        if r["code"] not in both_codes:
            make_row(r, "🟠ギャップN")

    if new_rows:
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")

    print(f"✅ Sheets: 既存{updates_count}件更新 / 新規{len(new_rows)}件追加")

    return {"updated": updates_count, "new": len(new_rows)}


def get_portfolio_summary(spreadsheet):
    try:
        ws       = spreadsheet.worksheet(SHEET_SIGNALS)
        all_rows = ws.get_all_values()
    except Exception:
        return []

    positions = []

    for row in all_rows[1:]:
        if len(row) < 11:
            continue

        status = row[10]

        if "保有中" in status or "期間終了" in status:
            try:
                positions.append({
                    "code":    row[1],
                    "name":    row[2],
                    "signal":  row[3],
                    "buy":     to_float(row[4]) if row[4] else 0.0,
                    "current": to_float(row[7]) if row[7] else 0.0,
                    "pnl":     to_float(row[8]) if row[8] else 0.0,
                    "days":    int(to_float(row[9])) if row[9] else 0,
                    "status":  status,
                    "sell_by": row[6] if len(row) > 6 else "",
                })
            except Exception:
                continue

    positions.sort(key=lambda x: x["pnl"], reverse=True)
    return positions


# ============================================================
# メール作成
# ============================================================

def build_email(latest_date, volC_rows, gapN_rows, positions, gemini_analyses: dict):
    both_codes = {r["code"] for r in volC_rows} & {r["code"] for r in gapN_rows}

    date_str = latest_date.strftime("%Y年%m月%d日")
    weekday  = ["月", "火", "水", "木", "金", "土", "日"][latest_date.weekday()]
    hold_end = pd.Timestamp(add_business_days(latest_date, HOLDING_DAYS)).strftime("%m月%d日")

    total = len(set(r["code"] for r in volC_rows) | set(r["code"] for r in gapN_rows))

    lines = [
        f"【株式シグナルレポート v4.1】{date_str}（{weekday}）",
        "",
    ]

    if total == 0:
        lines += ["本日はシグナルなし", ""]
    else:
        lines += [f"シグナル銘柄：合計 {total} 銘柄", ""]

        # ⭐ 両方一致
        for r in volC_rows:
            if r["code"] in both_codes:
                lines += [
                    "━" * 27,
                    "⭐ 両方一致（最注目シグナル）",
                    "━" * 27,
                    (
                        f"  {r['code']} {r['name']}  "
                        f"¥{r['price']:,.0f}  "
                        f"当日{r['ret']:+.1f}%  "
                        f"gap{r['gap']:+.1f}%  "
                        f"出来高{r['vol_ratio']:.1f}x"
                    ),
                    "",
                ]

                advice, _, _, _ = get_timing_advice(
                    "both", r["ret"], r["vol_ratio"], r["gap"]
                )
                lines += [advice, ""]

                # Gemini分析
                analysis = gemini_analyses.get(r["code"])
                if analysis:
                    lines += [
                        "【AI下落要因分析】",
                        analysis,
                        "",
                    ]

        # 🔵 出来高C
        for r in volC_rows:
            if r["code"] not in both_codes:
                lines += [
                    "━" * 27,
                    f"🔵 出来高C：{r['code']} {r['name']}",
                    "━" * 27,
                    (
                        f"  ¥{r['price']:,.0f}  "
                        f"当日{r['ret']:+.1f}%  "
                        f"出来高{r['vol_ratio']:.1f}x"
                    ),
                    "",
                ]
                advice, _, _, _ = get_timing_advice(
                    "volC", r["ret"], r["vol_ratio"], r["gap"]
                )
                lines += [advice, ""]

        # 🟠 ギャップN
        for r in gapN_rows:
            if r["code"] not in both_codes:
                lines += [
                    "━" * 27,
                    f"🟠 ギャップN：{r['code']} {r['name']}",
                    "━" * 27,
                    f"  ¥{r['price']:,.0f}  gap{r['gap']:+.1f}%",
                    "",
                ]
                advice, _, _, _ = get_timing_advice(
                    "gapN", r["ret"], r["vol_ratio"], r["gap"]
                )
                lines += [advice, ""]

    # 保有ポジション損益表
    if positions:
        lines += [
            "━" * 27,
            "📊 保有ポジション損益表",
            "━" * 27,
        ]

        lines.append(
            f"  {'コード':<6} {'銘柄':<10} {'買値':>7} {'現在値':>7} {'損益':>7} {'日数':>4}"
        )
        lines.append("  " + "─" * 50)

        total_pnl = 0.0
        wins = 0

        for p in positions:
            emoji = "📈" if p["pnl"] >= 0 else "📉"
            lines.append(
                f"  {p['code']:<6} {p['name'][:9]:<10}"
                f"  ¥{p['buy']:>6,.0f}  ¥{p['current']:>6,.0f}"
                f"  {p['pnl']:>+6.1f}%  {p['days']:>3}日 {emoji}"
            )
            total_pnl += p["pnl"]
            wins += 1 if p["pnl"] >= 0 else 0

        avg      = total_pnl / len(positions)
        win_rate = wins / len(positions) * 100

        lines += [
            "  " + "─" * 50,
            f"  {len(positions)}件 | 平均損益: {avg:+.1f}% | 勝率: {win_rate:.0f}%",
            "  ※ Google Sheetsで詳細確認できます",
            "",
        ]

    # フッター
    lines += [
        "━" * 27,
        "📋 分析ベース期待値（参考）",
        "━" * 27,
        "  出来高C : 5日後+1.0% / 10日後+1.5% / 20日後+1.9%",
        "  ギャップN: 5日後+1.0% / 10日後+2.4% / 20日後+4.3%",
        "  ※ 過去検証上の傾向であり、将来の利益を保証するものではありません",
        "  ※ ニュース・決算・地合いを確認したうえで判断してください",
        "",
        f"推奨保有期間：{HOLDING_DAYS}営業日（〜{hold_end}）",
        "⚠️ 投資判断はご自身でお願いします",
        "⚠️ 必ずニュース・決算を5分確認してから判断してください",
    ]

    return "\n".join(lines)


# ============================================================
# メール送信
# ============================================================

def send_email(subject, body):
    gmail_user = get_env("GMAIL_USER")
    gmail_pass = get_env("GMAIL_APP_PASSWORD")
    to_email   = get_env("NOTIFY_EMAIL")

    msg = MIMEMultipart()
    msg["From"]    = gmail_user
    msg["To"]      = to_email
    msg["Subject"] = subject

    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(gmail_user, gmail_pass)
        s.send_message(msg)

    print(f"✅ メール送信完了 → {to_email}")


# ============================================================
# メイン処理
# ============================================================

def main():
    print(f"▶ 実行開始：{datetime.now(JST).strftime('%Y-%m-%d %H:%M')} JST")

    # 銘柄リスト取得
    print("📋 プライム市場銘柄リスト取得中...")
    stocks = fetch_prime_stocks()

    # 信用倍率フィルター
    print("📋 信用倍率データ取得中...")
    margin_ratios = fetch_margin_ratios()

    if margin_ratios:
        excluded = {
            code for code, ratio in margin_ratios.items()
            if ratio > MAX_MARGIN_RATIO
        }
        stocks = {k: v for k, v in stocks.items() if k not in excluded}
        print(
            f"   信用倍率 > {MAX_MARGIN_RATIO} で除外: {len(excluded)}銘柄 "
            f"/ 対象残り: {len(stocks)}銘柄"
        )

    # 株価データ取得・シグナル計算
    print("📥 データ取得中...")
    close, open_, volume = fetch_data(stocks)

    print("📊 シグナル計算中...")
    latest, volC_rows, gapN_rows = calc_signals(close, open_, volume, stocks)

    today_utc        = datetime.now(timezone.utc).date()
    latest_date_only = pd.Timestamp(latest).date()

    if latest_date_only != today_utc:
        print(
            f"⚠️ 最新株価日が本日ではありません: "
            f"latest={latest_date_only} / today_utc={today_utc}"
        )
        print("休場日、または本日の株価データが未反映のため、メール送信せず終了します。")
        return

    date_str_mail  = latest.strftime("%Y年%m月%d日")
    date_str_sheet = latest.strftime("%Y/%m/%d")

    total = len(set(r["code"] for r in volC_rows) | set(r["code"] for r in gapN_rows))

    print(
        f"   対象日：{date_str_mail} / "
        f"出来高C:{len(volC_rows)} / ギャップN:{len(gapN_rows)}"
    )

    # ⭐ 両方一致銘柄のGemini分析
    both_codes = {r["code"] for r in volC_rows} & {r["code"] for r in gapN_rows}
    gemini_analyses = {}

    if both_codes and os.environ.get("GEMINI_API_KEY"):
        print(f"🤖 Gemini分析中... ({len(both_codes)}銘柄)")
        for r in volC_rows:
            if r["code"] in both_codes:
                print(f"   分析: {r['code']} {r['name']}")
                gemini_analyses[r["code"]] = analyze_drop_reason(
                    r["code"], r["name"], r["ret"], r["gap"], date_str_mail
                )

    # Google Sheets 更新
    positions     = []
    spreadsheet   = None
    update_result = {"updated": 0, "new": 0}

    print("📊 Google Sheets 更新中...")

    try:
        gc          = get_sheets_client()
        spreadsheet = gc.open_by_key(get_env("SHEETS_ID"))

        update_result = update_sheets(
            spreadsheet,
            latest,
            volC_rows,
            gapN_rows,
            close,
        )

        positions = get_portfolio_summary(spreadsheet)
        print(f"   保有ポジション：{len(positions)} 件")

    except Exception as e:
        print(f"⚠️ Sheets更新エラー（メール送信は継続）: {e}")

    body    = build_email(latest, volC_rows, gapN_rows, positions, gemini_analyses)
    subject = f"【株式シグナル v4.1】{date_str_mail} / {total}銘柄"

    if positions:
        avg      = sum(p["pnl"] for p in positions) / len(positions)
        subject += f" | 保有{len(positions)}件 平均{avg:+.1f}%"

    print("\n📧 メール送信中...")
    send_email(subject, body)

    if spreadsheet is not None:
        try:
            add_run_log(spreadsheet, date_str_sheet, update_result["new"], subject)
            print("✅ 配信ログを記録しました")
        except Exception as e:
            print(f"⚠️ 配信ログ記録エラー: {e}")

    print("▶ 完了")


if __name__ == "__main__":
    main()
