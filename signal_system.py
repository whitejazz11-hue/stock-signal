"""
株式シグナル通知システム v2
毎日自動実行 → Gmail送信（反転タイミング分析付き）

【分析結果に基づく買いタイミング指針】
出来高C: 翌日寄り付きエントリー。当日引けには大半がプラス。
         底打ち中央値11日。出来高が5日で正常化するのが目安。
ギャップN: 翌日1〜2日は下落継続の可能性あり（平均-0.2%）。
          3日目以降から急回復。10日後平均+2.4%、20日後+4.3%。
          底打ち中央値7日。

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

VOL_WINDOW    = 20
VOL_MULT      = 3.0
GAP_THRESHOLD = -0.05
HOLDING_DAYS  = 90    # 分析結果により60→90日に更新

STOCKS = {
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
# 反転タイミング分析に基づく買いアドバイス生成
# ============================================================

def get_timing_advice(signal, ret_pct, vol_ratio, gap_pct):
    """
    分析結果に基づいてシグナル別の買いタイミングと期待値を返す

    出来高C分析結果:
      翌日+1.0%, 5日+1.0%, 10日+1.5%, 20日+1.9%, 40日+2.7%
      底打ち中央値11日 / 回復率92%(40日以内)
      出来高正常化: 約5日

    ギャップN分析結果:
      翌日-0.2%, 5日+1.0%, 10日+2.4%, 20日+4.3%, 40日+5.3%
      底打ち中央値7日 / 回復率94%(40日以内)
      大きく落ちるほど底打ちが遅れる傾向(r=0.18)
    """
    lines = []

    if signal == "volC":
        # 下落幅で強度分類
        if ret_pct <= -10:
            strength = "強🔴"
            entry_msg = "翌朝寄り付き成行で即エントリー推奨"
            expect = "5日後+1.0%→10日後+1.5%→20日後+1.9%が期待値"
            hold   = "底打ち中央値11日。出来高が落ち着く5日目が底値確認の目安"
            caution = "大きく落ちているため底打ちに時間がかかる可能性あり（損切りは不要）"
        elif ret_pct <= -5:
            strength = "中🟡"
            entry_msg = "翌朝寄り付き成行でエントリー"
            expect = "5日後+1.0%→10日後+1.5%→20日後+1.9%が期待値"
            hold   = "底打ち中央値11日。出来高倍率{:.1f}xは5日程度で正常化見込み".format(vol_ratio)
            caution = "翌日から即プラス圏に入るケースが多い（回復率92%）"
        else:
            strength = "弱🟢"
            entry_msg = "翌朝寄り付き成行でエントリー（比較的穏やか）"
            expect = "5日後+1.0%→10日後+1.5%が期待値"
            hold   = "出来高倍率{:.1f}xは軽めのシグナル。早めの利確も選択肢".format(vol_ratio)
            caution = "出来高の押し上げが小さいため、確認してから判断でも可"

        lines.append(f"  シグナル強度: {strength}（当日{ret_pct:+.1f}% / 出来高{vol_ratio:.1f}x）")
        lines.append(f"  エントリー  : {entry_msg}")
        lines.append(f"  期待リターン: {expect}")
        lines.append(f"  保有目安   : {hold}")
        lines.append(f"  ポイント   : {caution}")

    elif signal == "gapN":
        gap = gap_pct if gap_pct else ret_pct
        # ギャップ幅で強度分類
        if gap <= -10:
            strength = "強🔴"
            entry_msg = "翌朝は1〜2日さらに下落の可能性あり → 翌朝寄り付きより「3日目以降」の確認も有効"
            expect = "5日後+1.0%→10日後+2.4%→20日後+4.3%が期待値（最も高リターン）"
            hold   = "底打ち中央値7日。-10%超の大幅ギャップは底打ちまで10〜15日かかる場合も"
            caution = "ギャップが大きいほど最終リターンも大きい。焦らず保有継続が鍵"
        elif gap <= -7:
            strength = "中🟡"
            entry_msg = "翌朝寄り付きエントリー。初日はわずかにマイナスの可能性あり"
            expect = "5日後+1.0%→10日後+2.4%→20日後+4.3%が期待値"
            hold   = "底打ち中央値7日。出来高が落ち着く5日目が転換点"
            caution = "回復率94%と高い。辛抱強く持つことが重要"
        else:
            strength = "弱🟢"
            entry_msg = "翌朝寄り付きエントリー"
            expect = "5日後+1.0%→10日後+2.4%が期待値"
            hold   = "ギャップ{:.1f}%は軽めの水準。7日前後で底打ちが期待される".format(gap)
            caution = "出来高C同時シグナルがあれば確度がさらに高まる"

        lines.append(f"  シグナル強度: {strength}（ギャップ{gap:+.1f}%）")
        lines.append(f"  エントリー  : {entry_msg}")
        lines.append(f"  期待リターン: {expect}")
        lines.append(f"  保有目安   : {hold}")
        lines.append(f"  ポイント   : {caution}")

    elif signal == "both":
        lines.append(f"  シグナル強度: 最強⭐（出来高C＋ギャップN同時）")
        lines.append(f"  エントリー  : 翌朝寄り付き成行エントリー推奨")
        lines.append(f"  期待リターン: 両シグナル合算。20日後+3〜4%台が目安")
        lines.append(f"  保有目安   : 最低7〜11日。90日保有で最大リターン")
        lines.append(f"  ポイント   : 両方一致は最も確度が高いシグナル。損切り不要、持ち続けることが重要")

    return "\n".join(lines)


# ============================================================
# データ取得・シグナル計算
# ============================================================

def fetch_data():
    tickers = [f"{c}.T" for c in STOCKS]
    start   = (datetime.today() - timedelta(days=180)).strftime("%Y-%m-%d")
    today   = (datetime.today() + timedelta(days=1)).strftime("%Y-%m-%d")

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
    avg_vol   = volume.shift(1).rolling(VOL_WINDOW, min_periods=VOL_WINDOW).mean()
    daily_ret = close.pct_change()
    vol_ratio = volume / avg_vol
    sig_volC  = (volume > VOL_MULT * avg_vol) & (daily_ret < 0)

    gap      = open_ / close.shift(1) - 1
    sig_gapN = gap < GAP_THRESHOLD

    latest = close.dropna(how="all").index[-1]

    volC_hits = sig_volC.loc[latest][sig_volC.loc[latest]].index.tolist()
    gapN_hits = sig_gapN.loc[latest][sig_gapN.loc[latest]].index.tolist()

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
# メール作成
# ============================================================

def build_email(latest_date, volC_rows, gapN_rows):
    both_codes = {r["code"] for r in volC_rows} & {r["code"] for r in gapN_rows}
    date_str   = latest_date.strftime("%Y年%m月%d日")
    hold_end   = (latest_date + timedelta(days=int(HOLDING_DAYS * 1.4))).strftime("%m月%d日")
    weekday    = ["月", "火", "水", "木", "金", "土", "日"][latest_date.weekday()]

    lines = []
    lines.append(f"【株式シグナルレポート】{date_str}（{weekday}）")
    lines.append("")

    total = len(set([r["code"] for r in volC_rows]) | set([r["code"] for r in gapN_rows]))

    if total == 0:
        lines.append("本日はシグナルなし")
        lines.append("")
        lines.append(f"推奨保有期間の目安：{HOLDING_DAYS}営業日（〜{hold_end}）")
        lines.append("⚠️ 投資判断はご自身でお願いします")
        return "\n".join(lines)

    lines.append(f"シグナル銘柄：合計 {total} 銘柄")
    lines.append("")

    # ━━━ 両方一致（最強） ━━━
    both_rows = [r for r in volC_rows if r["code"] in both_codes]
    if both_rows:
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("⭐ 両方一致（最強シグナル）")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        for r in both_rows:
            lines.append(
                f"  {r['code']} {r['name']}"
                f"  ¥{r['price']:,.0f}"
                f"  当日{r['ret']:+.1f}%"
                f"  ギャップ{r['gap']:+.1f}%"
                f"  出来高{r['vol_ratio']:.1f}x"
            )
        lines.append("")
        lines.append("【買いタイミング・期待値】")
        lines.append(get_timing_advice("both", None, None, None))
        lines.append("")

    # ━━━ 出来高Cのみ ━━━
    volC_only = [r for r in volC_rows if r["code"] not in both_codes]
    if volC_only:
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("🔵 出来高C のみ")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        for r in volC_only:
            lines.append(
                f"  {r['code']} {r['name']}"
                f"  ¥{r['price']:,.0f}"
                f"  当日{r['ret']:+.1f}%"
                f"  出来高{r['vol_ratio']:.1f}x"
            )
            lines.append("")
            lines.append(f"  ▼ {r['name']} 買いタイミング")
            lines.append(get_timing_advice("volC", r["ret"], r["vol_ratio"], r["gap"]))
            lines.append("")

    # ━━━ ギャップNのみ ━━━
    gapN_only = [r for r in gapN_rows if r["code"] not in both_codes]
    if gapN_only:
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("🟠 ギャップN のみ")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        for r in gapN_only:
            lines.append(
                f"  {r['code']} {r['name']}"
                f"  ¥{r['price']:,.0f}"
                f"  ギャップ{r['gap']:+.1f}%"
            )
            lines.append("")
            lines.append(f"  ▼ {r['name']} 買いタイミング")
            lines.append(get_timing_advice("gapN", r["ret"], r["vol_ratio"], r["gap"]))
            lines.append("")

    # ━━━ 共通フッター ━━━
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("📊 分析ベース期待値サマリー")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("  出来高C: 5日後+1.0% / 10日後+1.5% / 20日後+1.9%")
    lines.append("  ギャップN: 5日後+1.0% / 10日後+2.4% / 20日後+4.3%")
    lines.append("  ※ 異常値除外後の過去実績に基づく期待値（税引前）")
    lines.append("  ※ 損切りは逆効果。-10%下落後でも平均+6.5%回復")
    lines.append("  ※ 保有90日が最適（60日より+0.6%/トレード改善）")
    lines.append("")
    lines.append(f"推奨保有期間の目安：{HOLDING_DAYS}営業日（〜{hold_end}）")
    lines.append("⚠️ 投資判断はご自身でお願いします")
    lines.append("⚠️ サバイバーシップバイアスあり・税金未考慮")
    lines.append("⚠️ 必ずニュース・決算を5分確認してから判断してください")

    return "\n".join(lines)


# ============================================================
# メール送信
# ============================================================

def send_email(subject, body):
    gmail_user = os.environ["GMAIL_USER"]
    gmail_pass = os.environ["GMAIL_APP_PASSWORD"]
    to_email   = os.environ["NOTIFY_EMAIL"]

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

    print("📥 データ取得中...")
    close, open_, volume = fetch_data()

    print("📊 シグナル計算中...")
    latest, volC_rows, gapN_rows = calc_signals(close, open_, volume)

    date_str = latest.strftime("%Y年%m月%d日")
    total    = len(set([r["code"] for r in volC_rows]) | set([r["code"] for r in gapN_rows]))

    print(f"   対象日：{date_str}")
    print(f"   出来高C：{len(volC_rows)} 銘柄")
    print(f"   ギャップN：{len(gapN_rows)} 銘柄")
    print(f"   合計：{total} 銘柄")

    body    = build_email(latest, volC_rows, gapN_rows)
    subject = f"【株式シグナル】{date_str} / {total}銘柄"

    print(f"\n📧 メール送信中...")
    send_email(subject, body)
    print("▶ 完了")


if __name__ == "__main__":
    main()
