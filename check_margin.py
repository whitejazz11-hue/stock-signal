"""
信用倍率 分布確認スクリプト（デバッグ版）
"""

import io
import requests
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def fetch_margin_ratios():
    today = datetime.now(JST).date()
    print(f"本日: {today} (weekday={today.weekday()})")

    for weeks_back in range(8):
        days_back = (today.weekday() - 4) % 7 + weeks_back * 7
        target    = today - timedelta(days=days_back)
        date_str  = target.strftime("%Y%m%d")
        url = (
            "https://www.jpx.co.jp/markets/statistics-equities"
            f"/margin/nlsgeu000000xbna-att/data_{date_str}.csv"
        )

        print(f"\n試行: {date_str} → {url}")

        try:
            resp = requests.get(url, timeout=30)
            print(f"  ステータス: {resp.status_code}")
            print(f"  Content-Type: {resp.headers.get('Content-Type', '不明')}")
            print(f"  サイズ: {len(resp.content)} bytes")

            if resp.status_code != 200:
                print("  → スキップ（200以外）")
                continue

            try:
                df = pd.read_csv(
                    io.BytesIO(resp.content),
                    encoding="shift_jis",
                    skiprows=1,
                )
                print(f"  カラム: {list(df.columns)}")
                print(f"  行数: {len(df)}")

                code_cols  = [c for c in df.columns if "コード" in str(c)]
                ratio_cols = [c for c in df.columns if "倍率" in str(c)]
                print(f"  コード列: {code_cols}")
                print(f"  倍率列: {ratio_cols}")

                if not code_cols or not ratio_cols:
                    print("  → カラムが見つからずスキップ")
                    continue

                df = df[[code_cols[0], ratio_cols[0]]].copy()
                df.columns = ["コード", "信用倍率"]
                df["信用倍率"] = pd.to_numeric(df["信用倍率"], errors="coerce")
                df = df.dropna(subset=["信用倍率"])
                df = df[df["信用倍率"] > 0]

                print(f"\n✅ 取得成功: {len(df)}銘柄 (基準日: {date_str})")
                return df

            except Exception as e:
                print(f"  CSV読み込みエラー: {e}")
                try:
                    df = pd.read_csv(
                        io.BytesIO(resp.content),
                        encoding="utf-8",
                        skiprows=1,
                    )
                    print(f"  UTF-8で再試行 → カラム: {list(df.columns)}")
                except Exception as e2:
                    print(f"  UTF-8も失敗: {e2}")
                continue

        except Exception as e:
            print(f"  接続エラー: {e}")
            continue

    return pd.DataFrame()


def main():
    print("=" * 50)
    print("信用倍率データ取得テスト")
    print("=" * 50)

    df = fetch_margin_ratios()

    if df.empty:
        print("\n❌ 全ての試行が失敗しました")
        print("→ JPXのURL形式が変更された可能性があります")
        return

    total  = len(df)
    ratios = df["信用倍率"]

    print()
    print("=" * 45)
    print("📊 信用倍率 基本統計")
    print("=" * 45)
    print(f"  対象銘柄数 : {total}銘柄")
    print(f"  最小値     : {ratios.min():.1f}倍")
    print(f"  中央値     : {ratios.median():.1f}倍")
    print(f"  平均値     : {ratios.mean():.1f}倍")
    print(f"  最大値     : {ratios.max():.1f}倍")

    print()
    print("=" * 45)
    print("📋 閾値別 除外銘柄数シミュレーション")
    print("=" * 45)
    print(f"  {'閾値':>6}  {'除外数':>6}  {'除外率':>7}  {'残り':>6}")
    print("  " + "─" * 35)

    for threshold in [2, 3, 5, 7, 10, 15, 20, 30, 50]:
        excluded = (ratios > threshold).sum()
        rate     = excluded / total * 100
        remain   = total - excluded
        print(f"  {threshold:>5.0f}倍  {excluded:>6}銘柄  {rate:>6.1f}%  {remain:>6}銘柄")

    print()
    print("=" * 45)
    print("📋 倍率帯ごとの銘柄数分布")
    print("=" * 45)

    bins   = [0, 1, 2, 3, 5, 7, 10, 20, 50, float("inf")]
    labels = ["〜1倍", "1〜2倍", "2〜3倍", "3〜5倍",
              "5〜7倍", "7〜10倍", "10〜20倍", "20〜50倍", "50倍超"]

    df["倍率帯"] = pd.cut(ratios, bins=bins, labels=labels)
    dist = df["倍率帯"].value_counts().sort_index()

    for label, count in dist.items():
        bar  = "█" * (count // 5)
        rate = count / total * 100
        print(f"  {label:>8}  {count:>5}銘柄 ({rate:4.1f}%)  {bar}")


if __name__ == "__main__":
    main()
