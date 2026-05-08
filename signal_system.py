"""
株式シグナル通知システム
毎日自動実行 → Gmail送信

GitHub Actions で動かすスクリプト
"""

import os
import smtplib
import warnings
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

# ============================================================
# 設定
# ============================================================

VOL_WINDOW    = 20    # 出来高平均の計算期間
VOL_MULT      = 3.0   # 出来高急増の閾値（平均の何倍）
GAP_THRESHOLD = -0.05 # ギャップNの閾値（-5%以下）
HOLDING_DAYS  = 60    # 推奨保有期間（営業日）

STOCKS = {
    "7203": "トヨタ自動車",
    "7267": "ホンダ",
    "7269": "スズキ",
    "7270": "SUBARU",
    "7201": "日産自動車",
    "6758": "ソニーグループ",
    "6861": "キーエンス",
    "6954": "ファナック",
    "6981": "村田製作所",
    "6367": "ダイキン工業",
    "6702": "富士通",
    "6701": "NEC",
    "6752": "パナソニック",
    "7751": "キヤノン",
    "7733": "オリンパス",
    "6273": "SMC",
    "6503": "三菱電機",
    "9984": "ソフトバンクG",
    "9432": "NTT",
    "9433": "KDDI",
    "9434": "ソフトバンク",
    "8306": "三菱UFJ FG",
    "8316": "三井住友FG",
    "8411": "みずほFG",
    "8031": "三井物産",
    "8058": "三菱商事",
    "8001": "伊藤忠商事",
    "8053": "住友商事",
    "4063": "信越化学工業",
    "4188": "三菱ケミカルG",
    "4183": "三井化学",
    "5401": "日本製鉄",
    "5108": "ブリヂストン",
    "4502": "武田薬品工業",
    "4519": "中外製薬",
    "4568": "第一三共",
    "4543": "テルモ",
    "3382": "セブン&アイHD",
    "2802": "味の素",
    "2914": "JT",
    "3407": "旭化成",
    "8802": "三菱地所",
    "8801": "三井不動産",
    "9020": "JR東日本",
    "9022": "JR東海",
    "9064": "ヤマトHD",
    "6098": "リクルートHD",
    "7974": "任天堂",
    "8035": "東京エレクトロン",
    "4307": "野村総合研究所",
}


# ============================================================
# データ取得・シグナル計算
# ============================================================

def fetch_data():
    """株価・出来高データを取得"""
    tickers = [f"{c}.T" for c in STOCKS]
    start   = (datetime.today() - timedelta(days=180)).strftime("%Y-%m-%d")
    today   (datetime.today() + timedelta(days=1)).strftime("%Y-%m-%d")

    raw = yf.download(tickers, start=start, end=today,
                      interval="1d", auto_adjust=True, progress=False)

    close  = raw["Close"].copy()
    open_  = raw["Open"].copy()
    volume = raw["Volume"].copy()

    close.columns  = [c.replace(".T", "") for c in close.columns]
    open_.columns  = [c.replace(".T", "") for c in open_.columns]
    volume.columns = [c.replace(".T", "") for c in volume.columns]

    return close, open_, volume


def calc_signals(close, open_, volume):
    """シグナルを計算して今日のヒットを返す"""
    # 出来高C
    avg_vol   = volume.shift(1).rolling(VOL_WINDOW, min_periods=VOL_WINDOW).mean()
    daily_ret = close.pct_change()
    vol_ratio = volume / avg_vol
    sig_volC  = (volume > VOL_MULT * avg_vol) & (daily_ret < 0)

    # ギャップN
    gap      = open_ / close.shift(1) - 1
    sig_gapN = gap < GAP_THRESHOLD

    # 最新日
    latest = close.dropna(how="all").index[-1]

    volC_hits = sig_volC.loc[latest][sig_volC.loc[latest]].index.tolist()
    gapN_hits = sig_gapN.loc[latest][sig_gapN.loc[latest]].index.tolist()

    # 詳細情報
    def get_detail(codes):
        rows = []
        for code in codes:
            rows.append({
                "code":      code,
                "name":      STOCKS.get(code, code),
                "price":     close.loc[latest, code],
                "ret":       daily_ret.loc[latest, code] * 100,
                "gap":       gap.loc[latest, code] * 100,
                "vol_ratio": vol_ratio.loc[latest, code],
            })
        return rows

    return latest, get_detail(volC_hits), get_detail(gapN_hits)


# ============================================================
# メール作成・送信
# ============================================================

def build_email(latest_date, volC_rows, gapN_rows):
    """メール本文を作成"""
    both_codes = {r["code"] for r in volC_rows} & {r["code"] for r in gapN_rows}
    date_str   = latest_date.strftime("%Y年%m月%d日")
    hold_end   = (latest_date + timedelta(days=int(HOLDING_DAYS * 1.4))).strftime("%m月%d日")

    lines = []
    lines.append(f"【株式シグナルレポート】{date_str}")
    lines.append("")

    total = len(set([r["code"] for r in volC_rows]) |
                set([r["code"] for r in gapN_rows]))

    if total == 0:
        lines.append("本日はシグナルなし")
        lines.append("")
        lines.append(f"推奨保有期間の目安：{HOLDING_DAYS}営業日（〜{hold_end}）")
        lines.append("⚠️ 投資判断はご自身でお願いします")
        return "\n".join(lines)

    lines.append(f"シグナル銘柄：合計 {total} 銘柄")
    lines.append("")

    # 両方一致
    both_rows = [r for r in volC_rows if r["code"] in both_codes]
    if both_rows:
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("⭐ 両方一致（最強シグナル）")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        for r in both_rows:
            lines.append(
                f"  {r['code']} {r['name']}"
                f"  ¥{r['price']:,.0f}"
                f"  当日{r['ret']:+.1f}%"
                f"  ギャップ{r['gap']:+.1f}%"
                f"  出来高{r['vol_ratio']:.1f}x"
            )
        lines.append("")

    # 出来高Cのみ
    volC_only = [r for r in volC_rows if r["code"] not in both_codes]
    if volC_only:
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("🔵 出来高C のみ")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        for r in volC_only:
            lines.append(
                f"  {r['code']} {r['name']}"
                f"  ¥{r['price']:,.0f}"
                f"  当日{r['ret']:+.1f}%"
                f"  出来高{r['vol_ratio']:.1f}x"
            )
        lines.append("")

    # ギャップNのみ
    gapN_only = [r for r in gapN_rows if r["code"] not in both_codes]
    if gapN_only:
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("🟠 ギャップN のみ")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        for r in gapN_only:
            lines.append(
                f"  {r['code']} {r['name']}"
                f"  ¥{r['price']:,.0f}"
                f"  ギャップ{r['gap']:+.1f}%"
            )
        lines.append("")

    lines.append(f"推奨保有期間の目安：{HOLDING_DAYS}営業日（〜{hold_end}）")
    lines.append("⚠️ 投資判断はご自身でお願いします")
    lines.append("⚠️ サバイバーシップバイアスあり・税金未考慮")

    return "\n".join(lines)


def send_email(subject, body):
    """Gmail SMTPでメール送信"""
    gmail_user  = os.environ["GMAIL_USER"]
    gmail_pass  = os.environ["GMAIL_APP_PASSWORD"]
    to_email    = os.environ["NOTIFY_EMAIL"]

    msg = MIMEMultipart()
    msg["From"]    = gmail_user
    msg["To"]      = to_email
    msg["Subject"] = subject

    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_user, gmail_pass)
        server.send_message(msg)

    print(f"✅ メール送信完了 → {to_email}")


# ============================================================
# メイン処理
# ============================================================

def main():
    print(f"▶ 実行開始：{datetime.now().strftime('%Y-%m-%d %H:%M')}")

    # データ取得
    print("📥 データ取得中...")
    close, open_, volume = fetch_data()

    # シグナル計算
    print("📊 シグナル計算中...")
    latest, volC_rows, gapN_rows = calc_signals(close, open_, volume)

    date_str = latest.strftime("%Y年%m月%d日")
    total    = len(set([r["code"] for r in volC_rows]) |
                   set([r["code"] for r in gapN_rows]))

    print(f"   対象日：{date_str}")
    print(f"   出来高C：{len(volC_rows)} 銘柄")
    print(f"   ギャップN：{len(gapN_rows)} 銘柄")
    print(f"   合計：{total} 銘柄")

    # メール作成・送信
    body    = build_email(latest, volC_rows, gapN_rows)
    subject = f"【株式シグナル】{date_str} / {total}銘柄"

    print(f"\n📧 メール送信中...")
    print(f"   件名：{subject}")
    send_email(subject, body)

    print("▶ 完了")


if __name__ == "__main__":
    main()
