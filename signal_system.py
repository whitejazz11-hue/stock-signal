"""
株式シグナル通知システム v5.0
毎日自動実行 → 2段階メール配信

【v5.0 変更点】
  処理①: 前日シグナルのT+1値動きを確認 → 条件を満たした銘柄を「エントリー推奨」メール
  処理②: 当日シグナルを検知 → 「要注目候補」メール

エントリー条件（T+2始値買い）:
  出来高C  : T+1が+5%以上
  ギャップN : T+1が+5%以上 または -5%以下
  両方一致  : T+1が+5%以上

Gemini分析: かぶたんニュースを取得してGeminiに渡す
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
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")

# ============================================================
# 設定
# ============================================================

VOL_WINDOW    = 20
VOL_MULT      = 3.0
GAP_THRESHOLD = -0.05
HOLDING_DAYS  = 90

# エントリー条件（T+1値動きの閾値）
VOLC_ENTRY_UP    =  5.0  # 出来高C: T+1が+5%以上
GAPN_ENTRY_UP    =  5.0  # ギャップN: T+1が+5%以上
GAPN_ENTRY_DOWN  = -5.0  # ギャップN: T+1が-5%以下
BOTH_ENTRY_UP    =  5.0  # 両方一致: T+1が+5%以上

JST = ZoneInfo("Asia/Tokyo")

SHEET_SIGNALS  = "シグナル履歴"
SHEET_RUNLOG   = "配信ログ"
SHEET_FOLLOWUP = "フォローアップ"

HEADERS = [
    "シグナル日", "コード", "銘柄名", "シグナル種別",
    "買値目安(終値)", "エントリー推奨日", "推奨売却日",
    "現在値", "損益(%)", "保有日数", "ステータス",
    "期待値(5日)", "期待値(10日)", "期待値(20日)"
]

FOLLOWUP_HEADERS = [
    "シグナル日", "コード", "銘柄名", "シグナル種別",
    "T日終値", "T+1値動%", "エントリー判定", "T+2推奨エントリー日", "Gemini分析"
]

RUNLOG_HEADERS = [
    "対象日", "実行日時", "新規シグナル数", "メール件名"
]

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
# エントリー条件判定
# ============================================================

def check_entry_condition(signal_type: str, t1_ret: float) -> bool:
    """T+1の値動きがエントリー条件を満たすか判定する"""
    if "両方一致" in signal_type or "⭐" in signal_type:
        return t1_ret >= BOTH_ENTRY_UP
    elif "出来高C" in signal_type or "🔵" in signal_type:
        return t1_ret >= VOLC_ENTRY_UP
    elif "ギャップN" in signal_type or "🟠" in signal_type:
        return t1_ret >= GAPN_ENTRY_UP or t1_ret <= GAPN_ENTRY_DOWN
    return False


def get_entry_reason(signal_type: str, t1_ret: float) -> str:
    if "ギャップN" in signal_type or "🟠" in signal_type:
        if t1_ret <= GAPN_ENTRY_DOWN:
            return f"翌日続落({t1_ret:+.1f}%) → 底値接近シグナル"
        else:
            return f"翌日反発({t1_ret:+.1f}%) → 悪材料出尽くし確認"
    else:
        return f"翌日大幅反発({t1_ret:+.1f}%) → 反転確認"


def get_expected_return(signal_type: str) -> str:
    """シグナル種別ごとの期待リターンを返す（T+2始値買い・5日後）"""
    if "両方一致" in signal_type or "⭐" in signal_type:
        return "5日後+9.8% / 勝率100%"
    elif "出来高C" in signal_type or "🔵" in signal_type:
        return "5日後+8.7% / 勝率94%"
    elif "ギャップN" in signal_type or "🟠" in signal_type:
        return "5日後+7.5〜8.2% / 勝率93〜95%"
    return ""


# ============================================================
# かぶたんニュース取得
# ============================================================

def fetch_kabutan_news(code: str, name: str) -> str:
    """Google ニュースRSSから銘柄関連ニュースを取得する"""
    try:
        import xml.etree.ElementTree as ET
        from urllib.parse import quote

        query = quote(f"{name} {code}")
        url   = (
            f"https://news.google.com/rss/search"
            f"?q={query}&hl=ja&gl=JP&ceid=JP:ja"
        )
        headers = {"User-Agent": "Mozilla/5.0"}
        resp    = requests.get(url, timeout=10, headers=headers)

        if resp.status_code != 200:
            return f"ニュース取得失敗（HTTP {resp.status_code}）"

        root  = ET.fromstring(resp.content)
        items = []

        for item in root.findall(".//item")[:5]:
            title = item.findtext("title")
            if title and len(title) > 5:
                items.append(title.strip())

        if not items:
            return f"{name}の直近ニュースなし"

        return "\n".join(f"・{t}" for t in items[:4])

    except Exception as e:
        return f"ニュース取得エラー: {e}"


# ============================================================
# Gemini分析
# ============================================================

def analyze_with_gemini(code: str, name: str, signal_type: str,
                         t1_ret: float, news_text: str, date_str: str) -> str:
    """かぶたんニュースをもとにGeminiで下落要因を分析する"""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "（GEMINI_API_KEY未設定）"

    try:
        from google import genai as gai
        from google.genai.types import GenerateContentConfig

        client = gai.Client(api_key=api_key)

        prompt = (
            f"{date_str}に{name}(コード{code})の株価が急落し、"
            f"翌日({t1_ret:+.1f}%)の値動きを経てエントリー条件を満たしました。\n\n"
            f"直近のニュース:\n{news_text}\n\n"
            f"上記をもとに日本語5行以内で教えてください:\n"
            f"1. 下落の主な要因\n"
            f"2. 一時的か構造的か\n"
            f"3. 逆張り適性(高い/中程度/低い)と理由"
        )

        response = client.models.generate_content(
            model="gemini-2.0-flash-lite",
            contents=prompt,
            config=GenerateContentConfig(max_output_tokens=300)
        )

        return response.text.strip()

    except Exception as e:
        return f"（分析エラー: {str(e)[:80]}）"


# ============================================================
# JPX データ取得
# ============================================================

def fetch_prime_stocks() -> dict:
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
        print(f"⚠️ プライム銘柄取得失敗: {e} → フォールバック50銘柄を使用")
        return STOCKS_FALLBACK


def fetch_margin_ratios() -> dict:
    today = datetime.now(JST).date()

    for weeks_back in range(4):
        days_back = (today.weekday() - 4) % 7 + weeks_back * 7
        target    = today - timedelta(days=days_back)
        date_str  = target.strftime("%Y%m%d")
        url = (
            "https://www.jpx.co.jp/markets/statistics-equities"
            f"/margin/nlsgeu000000xbna-att/data_{date_str}.csv"
        )

        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code != 200:
                continue

            df = pd.read_csv(io.BytesIO(resp.content), encoding="shift_jis", skiprows=1)

            code_cols  = [c for c in df.columns if "コード" in str(c)]
            ratio_cols = [c for c in df.columns if "倍率" in str(c)]

            if not code_cols or not ratio_cols:
                continue

            result = {}
            for _, row in df.iterrows():
                try:
                    code  = str(int(row[code_cols[0]])).zfill(4)
                    ratio = float(row[ratio_cols[0]])
                    if ratio > 0:
                        result[code] = ratio
                except Exception:
                    continue

            print(f"✅ 信用倍率取得: {len(result)}銘柄 (基準日: {date_str})")
            return result

        except Exception as e:
            print(f"  {date_str} 取得失敗: {e}")
            continue

    print("⚠️ 信用倍率データ取得失敗 → フィルターなしで続行")
    return {}

# ============================================================
# 処理①-補足: T+2始値を買値列に後付け記録
# ============================================================

def update_t2_entry_prices(spreadsheet, open_, latest_date):
    """
    T+2日の実行時に、本日エントリー推奨だった銘柄のT+2始値を
    シグナル履歴の「買値」列(E列)に上書き記録する
    """
    today_str = pd.Timestamp(latest_date).strftime("%Y/%m/%d")
    print(f"  T+2始値記録チェック: エントリー予定日={today_str}")

    # フォローアップシートから「T+2推奨エントリー日 == 今日 かつ ✅」の行を抽出
    # FOLLOWUP_HEADERS インデックス: 0=シグナル日, 1=コード, 6=エントリー判定, 7=T+2推奨エントリー日
    try:
        ws_fu   = spreadsheet.worksheet(SHEET_FOLLOWUP)
        fu_rows = ws_fu.get_all_values()
    except Exception as e:
        print(f"  ⚠️ フォローアップシート読み取りエラー: {e}")
        return

    target = {}  # {code: signal_date_str}
    for row in fu_rows[1:]:
        if len(row) < 8:
            continue
        if row[7] == today_str and "✅" in row[6]:
            target[row[1]] = row[0]  # code -> シグナル日

    if not target:
        print(f"  T+2始値記録対象なし")
        return

    print(f"  T+2始値記録対象: {len(target)}件 → {list(target.keys())}")

    # シグナル履歴の対象行を探してE列（買値）を更新
    # HEADERS インデックス: 0=シグナル日, 1=コード, 4=買値, 8=損益(%)
    try:
        ws_sig   = spreadsheet.worksheet(SHEET_SIGNALS)
        sig_rows = ws_sig.get_all_values()
    except Exception as e:
        print(f"  ⚠️ シグナル履歴読み取りエラー: {e}")
        return

    updated = 0
    for i, row in enumerate(sig_rows[1:], start=2):
        if len(row) < 5:
            continue
        code        = row[1]
        signal_date = row[0]

        if code not in target or target[code] != signal_date:
            continue

        try:
            t2_open = open_.loc[latest_date, code]
            if not is_valid_number(t2_open):
                print(f"    {code}: T+2始値データなし（スキップ）")
                continue

            t2_open_val = round(float(t2_open), 0)
            old_val     = row[4]

            # E列（買値）を上書き
            ws_sig.update(f"E{i}", [[t2_open_val]])
            print(f"    ✅ {code}: 買値 {old_val} → {t2_open_val}（T+2始値）")
            updated += 1

        except Exception as e:
            print(f"    {code} 更新エラー: {e}")

    print(f"  T+2始値記録完了: {updated}件")
# ============================================================
# Google Sheets 接続
# ============================================================

def get_sheets_client():
    raw        = get_env("GOOGLE_SHEETS_CREDENTIALS")
    creds_dict = json.loads(raw)
    scopes     = [
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

    latest    = valid_close.index[-1]
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

    return latest, build_hits(sig_volC), build_hits(sig_gapN)


# ============================================================
# 処理①: フォローアップ（前日シグナルのT+1チェック）
# ============================================================

def process_followup(spreadsheet, close, latest_date, stocks: dict) -> list:
    """
    前日のシグナル銘柄のT+1値動きを確認し、
    エントリー条件を満たした銘柄のリストを返す
    """
    # 前営業日を計算
    t1_date  = pd.Timestamp(latest_date)
    t_date   = t1_date - pd.offsets.BDay(1)
    t_date_str = t_date.strftime("%Y/%m/%d")

    print(f"📋 フォローアップ確認: シグナル日={t_date_str} / T+1日={t1_date.strftime('%Y/%m/%d')}")

    # シグナル履歴から前日シグナルを読み取る
    try:
        ws       = spreadsheet.worksheet(SHEET_SIGNALS)
        all_rows = ws.get_all_values()
    except Exception as e:
        print(f"  ⚠️ シグナル履歴読み取りエラー: {e}")
        return []

    yesterday_signals = []
    for row in all_rows[1:]:
        if len(row) < 5:
            continue
        if row[0] == t_date_str:
            yesterday_signals.append({
                "code":        row[1],
                "name":        row[2],
                "signal_type": row[3],
                "t_close":     to_float(row[4]),
            })

    if not yesterday_signals:
        print(f"  前日（{t_date_str}）のシグナルなし")
        return []

    print(f"  前日シグナル: {len(yesterday_signals)}件")

    # T+1値動きを計算してエントリー条件判定
    qualified = []
    followup_rows = []

    for sig in yesterday_signals:
        code    = sig["code"]
        t_close = sig["t_close"]

        if t_close <= 0:
            continue

        if code not in close.columns:
            continue

        try:
            t1_close = close.loc[t1_date, code]
            if not is_valid_number(t1_close):
                continue

            t1_ret = (float(t1_close) / t_close - 1) * 100

        except Exception:
            continue

        meets_condition = check_entry_condition(sig["signal_type"], t1_ret)
        entry_judgment  = "✅エントリー推奨" if meets_condition else "⏭スキップ"
        t2_entry_date   = next_business_day(t1_date)

        followup_rows.append([
            t_date_str,
            code,
            sig["name"],
            sig["signal_type"],
            round(t_close, 0),
            round(t1_ret, 2),
            entry_judgment,
            t2_entry_date if meets_condition else "-",
            "",  # Gemini分析は後で追記
        ])

        if meets_condition:
            qualified.append({
                "code":        code,
                "name":        sig["name"],
                "signal_type": sig["signal_type"],
                "t_close":     t_close,
                "t1_ret":      t1_ret,
                "t2_entry":    t2_entry_date,
                "reason":      get_entry_reason(sig["signal_type"], t1_ret),
                "expected":    get_expected_return(sig["signal_type"]),
                "t_date_str":  t_date_str,
            })

    print(f"  エントリー推奨: {len(qualified)}件 / スキップ: {len(yesterday_signals)-len(qualified)}件")

    # フォローアップシートに記録
    if followup_rows:
        try:
            ws_fu = get_or_create_sheet(spreadsheet, SHEET_FOLLOWUP, FOLLOWUP_HEADERS)
            ws_fu.append_rows(followup_rows, value_input_option="RAW")
        except Exception as e:
            print(f"  ⚠️ フォローアップシート記録エラー: {e}")

    # ⏭スキップ銘柄をシグナル履歴から削除（買いサインのみ残す）
    qualified_codes = {r["code"] for r in qualified}
    skip_codes = {
        sig["code"] for sig in yesterday_signals
        if sig["code"] not in qualified_codes
    }

    if skip_codes:
        try:
            ws_sig   = get_or_create_sheet(spreadsheet, SHEET_SIGNALS, HEADERS)
            sig_rows = ws_sig.get_all_values()

            rows_to_delete = []
            for i, row in enumerate(sig_rows[1:], start=2):
                if len(row) < 2:
                    continue
                if row[0] == t_date_str and row[1] in skip_codes:
                    rows_to_delete.append(i)

            # 後ろから削除（行番号のズレを防ぐ）
            for idx in sorted(rows_to_delete, reverse=True):
                ws_sig.delete_rows(idx)

            print(f"  🗑️ スキップ銘柄を削除: {len(rows_to_delete)}件")

        except Exception as e:
            print(f"  ⚠️ スキップ銘柄削除エラー: {e}")

    return qualified


# ============================================================
# 処理①: フォローアップメール作成・送信
# ============================================================

def build_followup_email(qualified: list, t1_date_str: str, t2_date_str: str, signal_date_str: str) -> str:
    lines = [
        f"【買いサイン】{t2_date_str}（明日）寄り付きエントリー候補",
        "",
        f"シグナル検知日  : {signal_date_str}",
        f"翌日確認日      : {t1_date_str}",
        f"推奨エントリー  : {t2_date_str} 寄り付き",
        "",
        f"エントリー推奨銘柄: {len(qualified)}件",
        "",
    ]

    for r in qualified:
        lines += [
            "━" * 27,
            f"{r['signal_type']}",
            "━" * 27,
            f"  {r['code']} {r['name']}",
            f"  シグナル日終値  : ¥{r['t_close']:,.0f}",
            f"  翌日の値動き    : {r['t1_ret']:+.1f}%",
            f"  推奨理由        : {r['reason']}",
            f"  期待リターン    : {r['expected']}（過去検証ベース）",
            "",
        ]

        if r.get("gemini"):
            lines += [
                "【関連ニュース】",
                r["gemini"],
                "",
            ]

    lines += [
        "━" * 27,
        "※ 過去検証上の傾向であり、将来の利益を保証するものではありません",
        "※ ニュース・決算・地合いを確認したうえで判断してください",
        "⚠️ 投資判断はご自身でお願いします",
    ]

    return "\n".join(lines)


# ============================================================
# 処理②: 当日シグナル（候補通知）
# ============================================================

def get_timing_advice(signal, ret_pct, vol_ratio, gap_pct):
    ret = float(ret_pct)   if is_valid_number(ret_pct)   else 0.0
    vol = float(vol_ratio) if is_valid_number(vol_ratio)  else 0.0
    gap = float(gap_pct)   if is_valid_number(gap_pct)   else 0.0

    if signal == "volC":
        if ret <= -10:
            strength = "強🔴"; e5, e10, e20 = "+8.7%", "-", "-"
            point = "大幅下落。翌日+5%以上反発すれば翌々日エントリー"
        elif ret <= -5:
            strength = "中🟡"; e5, e10, e20 = "+8.7%", "-", "-"
            point = f"出来高{vol:.1f}x。翌日の反発を確認してから判断"
        else:
            strength = "弱🟢"; e5, e10, e20 = "+8.7%", "-", "-"
            point = "翌日+5%以上反発した場合のみエントリー検討"

    elif signal == "gapN":
        if gap <= -10:
            strength = "強🔴"; e5, e10, e20 = "+7.5〜8.2%", "-", "-"
            point = "大きな窓開け下落。翌日-5%以下続落 or +5%以上反発でエントリー"
        elif gap <= -7:
            strength = "中🟡"; e5, e10, e20 = "+7.5〜8.2%", "-", "-"
            point = "翌日の値動きで判断。続落 or 大幅反発を待つ"
        else:
            strength = "弱🟢"; e5, e10, e20 = "+7.5〜8.2%", "-", "-"
            point = "翌日-5%以下続落 or +5%以上反発でエントリー検討"

    else:  # both
        strength = "最強⭐"; e5, e10, e20 = "+9.8%", "-", "-"
        point = "翌日+5%以上反発でエントリー"

    advice = (
        f"  シグナル強度  : {strength}\n"
        f"  翌々日期待値  : {e5}（翌日条件達成時・過去検証ベース）\n"
        f"  明日の判断    : {point}"
    )

    return advice, e5, e10, e20


def build_candidate_email(latest_date, volC_rows, gapN_rows):
    """T日の候補通知メール"""
    both_codes = {r["code"] for r in volC_rows} & {r["code"] for r in gapN_rows}
    date_str   = latest_date.strftime("%Y年%m月%d日")
    weekday    = ["月", "火", "水", "木", "金", "土", "日"][latest_date.weekday()]
    t1_date    = pd.Timestamp(latest_date) + pd.offsets.BDay(1)
    t1_str     = t1_date.strftime("%m月%d日")
    t2_date    = pd.Timestamp(latest_date) + pd.offsets.BDay(2)
    t2_str     = t2_date.strftime("%m月%d日")

    total = len(set(r["code"] for r in volC_rows) | set(r["code"] for r in gapN_rows))

    lines = [
        f"【逆張り候補】{date_str}（{weekday}）シグナル検知",
        "",
        f"明日（{t1_str}）の値動きを確認してください。",
        f"明日の引け後に条件を満たした銘柄をお知らせします。",
        "",
        f"本日の候補: {total}銘柄",
        "",
    ]

    # ⭐ 両方一致
    for r in volC_rows:
        if r["code"] in both_codes:
            lines += [
                "━" * 27,
                "⭐ 両方一致（最注目候補）",
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
            advice, _, _, _ = get_timing_advice("both", r["ret"], r["vol_ratio"], r["gap"])
            lines += [advice, ""]

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
            advice, _, _, _ = get_timing_advice("volC", r["ret"], r["vol_ratio"], r["gap"])
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
            advice, _, _, _ = get_timing_advice("gapN", r["ret"], r["vol_ratio"], r["gap"])
            lines += [advice, ""]

    lines += [
        "━" * 27,
        f"買いサインメール配信予定: {t1_str}（翌日引け後）",
        "条件: 翌日+5%以上反発 / 窓開け下落銘柄は-5%以下続落も対象",
        "",
        "⚠️ 投資判断はご自身でお願いします",
        "⚠️ 必ずニュース・決算を確認してから判断してください",
    ]

    return "\n".join(lines)


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
                buy_price   = to_float(row[4])
                if buy_price <= 0:
                    continue
                current_raw = close.loc[latest_date, code]
                if not is_valid_number(current_raw):
                    continue
                current   = float(current_raw)
                pnl_pct   = (current / buy_price - 1) * 100
                sig_dt    = datetime.strptime(row[0], "%Y/%m/%d")
                hold_days = business_days_between(sig_dt, latest_date)
                sell_by   = row[6] if len(row) > 6 else ""
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
        _, e5, e10, e20 = get_timing_advice(sig_key, r.get("ret"), r.get("vol_ratio"), r.get("gap"))
        price = float(r["price"])

        new_rows.append([
            date_str, r["code"], r["name"], signal_type,
            round(price, 0),
            next_business_day(latest_date),
            add_business_days(latest_date, HOLDING_DAYS),
            round(price, 0), 0.0, 0, "保有中📊",
            e5, e10, e20,
        ])

    for r in volC_rows:
        make_row(r, "⭐両方一致" if r["code"] in both_codes else "🔵出来高C")
    for r in gapN_rows:
        if r["code"] not in both_codes:
            make_row(r, "🟠ギャップN")

    if new_rows:
        ws.append_rows(new_rows, value_input_option="RAW")

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
                    "code":    row[1], "name": row[2],
                    "buy":     to_float(row[4]) if row[4] else 0.0,
                    "current": to_float(row[7]) if row[7] else 0.0,
                    "pnl":     to_float(row[8]) if row[8] else 0.0,
                    "days":    int(to_float(row[9])) if row[9] else 0,
                    "status":  status,
                })
            except Exception:
                continue

    positions.sort(key=lambda x: x["pnl"], reverse=True)
    return positions


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
        excluded = {code for code, ratio in margin_ratios.items() if ratio > 5.0}
        stocks   = {k: v for k, v in stocks.items() if k not in excluded}
        print(f"   信用倍率フィルター: {len(excluded)}銘柄除外 / 残り{len(stocks)}銘柄")

    # 株価データ取得
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
    t1_date        = pd.Timestamp(latest) + pd.offsets.BDay(1)
    t2_date        = pd.Timestamp(latest) + pd.offsets.BDay(2)

    total = len(set(r["code"] for r in volC_rows) | set(r["code"] for r in gapN_rows))
    print(f"   対象日：{date_str_mail} / 出来高C:{len(volC_rows)} / ギャップN:{len(gapN_rows)}")

    spreadsheet   = None
    update_result = {"updated": 0, "new": 0}
    qualified     = []

    try:
        gc          = get_sheets_client()
        spreadsheet = gc.open_by_key(get_env("SHEETS_ID"))

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 処理①: フォローアップ（前日シグナルのT+1チェック）
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        print("\n🔍 処理①: フォローアップチェック...")
        qualified = process_followup(spreadsheet, close, latest, stocks)

        # T+2始値の後付け記録（本日エントリー予定だった銘柄）
        print("\n📌 T+2始値記録チェック...")
        update_t2_entry_prices(spreadsheet, open_, latest)

        # Gemini分析（エントリー推奨銘柄のみ）
        if qualified:
            print(f"📰 関連ニュース取得中... ({len(qualified)}銘柄)")
            for r in qualified:
                print(f"   {r['code']} {r['name']}")
                r["gemini"] = fetch_kabutan_news(r["code"], r["name"])

        # フォローアップメール送信
        if qualified:
            followup_body    = build_followup_email(
                qualified,
                t1_date_str     = latest.strftime("%Y年%m月%d日"),
                t2_date_str     = t2_date.strftime("%Y年%m月%d日"),
                signal_date_str = (pd.Timestamp(latest) - pd.offsets.BDay(1)).strftime("%Y年%m月%d日")
            )
            followup_subject = (
                f"【買いサイン】{t2_date.strftime('%m月%d日')}寄り付き "
                f"/ {len(qualified)}銘柄"
            )
            print("\n📧 買いサインメール送信中...")
            send_email(followup_subject, followup_body)
        else:
            print("   買いサイン銘柄なし → メールなし")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 処理②: 当日シグナル（候補通知）
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        print("\n📊 処理②: 当日シグナル → Sheets更新...")
        update_result = update_sheets(spreadsheet, latest, volC_rows, gapN_rows, close)

        positions = get_portfolio_summary(spreadsheet)
        print(f"   保有ポジション：{len(positions)} 件")

    except Exception as e:
        print(f"⚠️ Sheets処理エラー（メール送信は継続）: {e}")

    # 候補通知メール送信
    if total > 0:
        candidate_body    = build_candidate_email(latest, volC_rows, gapN_rows)
        candidate_subject = (
            f"【逆張り候補】{date_str_mail} / {total}銘柄"
            f"（{t1_date.strftime('%m月%d日')}値動き確認）"
        )
        print("\n📧 候補通知メール送信中...")
        send_email(candidate_subject, candidate_body)
    else:
        print("本日はシグナルなし → 候補メールなし")

    # 配信ログ記録
    if spreadsheet is not None:
        try:
            add_run_log(spreadsheet, date_str_sheet, update_result["new"],
                        f"候補{total}件/推奨{len(qualified)}件")
            print("✅ 配信ログを記録しました")
        except Exception as e:
            print(f"⚠️ 配信ログ記録エラー: {e}")

    print("▶ 完了")


if __name__ == "__main__":
    main()
