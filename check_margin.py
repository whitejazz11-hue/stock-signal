"""
信用倍率 分布確認スクリプト（softhompo版）
データソース: softhompo.a.la9.jp (JPX銘柄別信用取引週末残高)
"""

import io
import zipfile
import requests
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def fetch_margin_ratios() -> pd.DataFrame:
    today = datetime.now(JST).date()
    print(f"本日: {today}")

    # 直近8週のFridayを試みる（thisMonth → pastMonth の順）
    for weeks_back in range(8):
        days_back = (today.weekday() - 4) % 7 + weeks_back * 7
        target    = today - timedelta(days=days_back)
        date_str  = target.strftime("%Y%m%d")
        ym_str    = target.strftime("%Y%m")

        # まず当月ファイルを試す
        url = f"https://softhompo.a.la9.jp/Data/margin/thisMonth/syumatsu{date_str}00.zip"
        print(f"\n試行: {date_str} → {url}")

        df = try_download_zip(url, date_str)
        if df is not None:
            return df

        # 次に過去月ファイルを試す（月が変わった場合）
        past_url = f"https://softhompo.a.la9.jp/Data/margin/pastMonth/{ym_str}.zip"
        if past_url != url:
            print(f"  過去月ファイルを試行: {past_url}")
            df = try_download_pastmonth_zip(past_url, date_str)
            if df is not None:
                return df

    return pd.DataFrame()


def try_download_zip(url: str, date_str: str) -> pd.DataFrame | None:
    try:
        resp = requests.get(url, timeout=30)
        print(f"  ステータス: {resp.status_code} / サイズ: {len(resp.content)} bytes")

        if resp.status_code != 200:
            return None

        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            names = z.namelist()
            print(f"  ZIP内ファイル: {names}")

            csv_files = [f for f in names if f.lower().endswith('.csv')]
            if not csv_files:
                print("  CSVファイルなし")
                return None

            with z.open(csv_files[0]) as f:
                return parse_margin_csv(f.read(), date_str)

    except Exception as e:
        print(f"  エラー: {e}")
        return None


def try_download_pastmonth_zip(url: str, target_date: str) -> pd.DataFrame | None:
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            return None

        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            # 対象日付に一致するファイルを探す
            target_files = [f for f in z.namelist() if target_date in f]
            if not target_files:
                return None

            with z.open(target_files[0]) as f:
                return parse_margin_csv(f.read(), target_date)

    except Exception as e:
        print(f"  過去月エラー: {e}")
        return None


def parse_margin_csv(raw_bytes: bytes, date_str: str) -> pd.DataFrame | None:
    """JPX形式の信用取引CSVを読み込み、コード→倍率のDataFrameを返す"""
    for encoding in ["shift_jis", "utf-8", "cp932"]:
        for skiprows in [0, 1, 2]:
            try:
                df = pd.read_csv(
                    io.BytesIO(raw_bytes),
                    encoding=encoding,
                    skiprows=skiprows,
                    header=0,
                )
                print(f"  エンコード:{encoding} skiprows:{skiprows} → カラム: {list(df.columns)[:6]}")

                # コード列と倍率列を特定
                code_cols  = [c for c in df.columns if "コード" in str(c)]
                ratio_cols = [c for c in df.columns if "倍率" in str(c)]

                # 倍率列がない場合は信用買残÷信用売残で計算
                buy_cols   = [c for c in df.columns if "買残" in str(c) and "前週" not in str(c)]
                sell_cols  = [c for c in df.columns if "売残" in str(c) and "前週" not in str(c)]

                print(f"    コード列:{code_cols} / 倍率列:{ratio_cols} / 買残:{buy_cols} / 売残:{sell_cols}")

                if not code_cols:
                    continue

                code_col = code_cols[0]

                if ratio_cols:
                    ratio_col = ratio_cols[0]
                    df_out = df[[code_col, ratio_col]].copy()
                    df_out.columns = ["コード", "信用倍率"]
                elif buy_cols and sell_cols:
                    df_out = df[[code_col, buy_cols[0], sell_cols[0]]].copy()
                    df_out.columns = ["コード", "信用買残", "信用売残"]
                    df_out["信用倍率"] = pd.to_numeric(df_out["信用買残"], errors="coerce") / \
                                         pd.to_numeric(df_out["信用売残"], errors="coerce")
                else:
                    continue

                df_out["コード"] = df_out["コード"].apply(
                    lambda x: str(int(x)).zfill(4) if pd.notna(x) and str(x).strip().replace('.','').isdigit() else None
                )
                df_out = df_out.dropna(subset=["コード"])
                df_out["信用倍率"] = pd.to_numeric(df_out["信用倍率"], errors="coerce")
                df_out = df_out[df_out["信用倍率"] > 0]

                if len(df_out) > 10:
                    print(f"\n✅ 取得成功: {len(df_out)}銘柄 (基準日: {date_str})")
                    return df_out[["コード", "信用倍率"]]

            except Exception as e:
                continue

    print("  → 全エンコード・全skiprows試行失敗")
    return None


def main():
    print("=" * 50)
    print("信用倍率データ取得テスト（softhompo版）")
    print("=" * 50)

    df = fetch_margin_ratios()

    if df.empty:
        print("\n❌ 全ての試行が失敗しました")
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
