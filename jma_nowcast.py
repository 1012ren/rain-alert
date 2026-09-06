#!/usr/bin/env python3
"""
jma_nowcast.py
気象庁の「高解像度降水ナウキャスト」から、指定地点の降水強度を読み取る。

【仕組み】
気象庁はナウキャストを「地図タイル画像(PNG)」として配信している。
数値のAPIは無いので、次の手順で降水強度を取り出す:

  1. targetTimes_N1.json (実況) / targetTimes_N2.json (予測) から対象時刻を取得
  2. 緯度経度を「タイル番号(x,y)」と「タイル内のピクセル位置」に変換
  3. そのタイルのPNGをダウンロードし、該当ピクセルの色を読む
  4. 色を降水強度(mm/h)に変換する ← 気象庁のカラースケールは決まっている

【特徴】
- 解像度 約250m(Open-Meteoの格子より遥かに細かい) → ゲリラ豪雨向き
- 5分間隔で更新、1時間先まで5分刻みの予測あり
- APIキー不要

【注意】
公式に「API」として公開されているものではなく、気象庁サイトが内部で
使っているタイル配信を利用している。将来仕様が変わる可能性がある。
"""

import io
import math
from datetime import datetime, timedelta, timezone

import requests
from PIL import Image

# 気象庁ナウキャストのタイル配信元
BASE_URL = "https://www.jma.go.jp/bosai/jmatile/data/nowc"

# hrpns = 高解像度降水ナウキャスト
ELEMENT = "surf/hrpns"

# タイルのズームレベル。10が最高解像度(1ピクセル≒120m)。
ZOOM = 10

# 自宅の1点だけを見ると、すぐ隣の豪雨を見逃してしまう。
# 自宅を中心に±この範囲(ピクセル)を調べて最大値を採用する。
# z=10では1ピクセル≒120mなので、8なら半径約1km。
SAMPLE_RADIUS_PX = 8

# 日本標準時
JST = timezone(timedelta(hours=9))

# 気象庁の降水強度カラースケール(実測で確認済み)。
# 各色は「範囲」を表すので、下限値を採用する(控えめに見積もる)。
COLOR_TO_MM_H = {
    (242, 242, 255): 0.1,   # 0.1〜1 mm/h
    (160, 210, 255): 1.0,   # 1〜5
    (33, 140, 255): 5.0,    # 5〜10
    (0, 65, 255): 10.0,     # 10〜20
    (250, 245, 0): 20.0,    # 20〜30
    (255, 153, 0): 30.0,    # 30〜50
    (255, 40, 0): 50.0,     # 50〜80
    (180, 0, 104): 80.0,    # 80以上
}


def latlon_to_tile_pixel(lat: float, lon: float, zoom: int):
    """
    緯度経度を「タイル番号(x, y)」と「タイル内のピクセル位置(px, py)」に変換する。

    地図タイルはWebメルカトル図法という決まった方式で並んでいるので、
    数式で機械的に計算できる。1タイルは256x256ピクセル。
    """
    n = 2 ** zoom
    lat_rad = math.radians(lat)

    # 経度は素直に横方向の位置になる
    fx = (lon + 180.0) / 360.0 * n
    # 緯度はメルカトル図法の式で縦方向の位置に変換する
    fy = (1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n

    x, y = int(fx), int(fy)
    # 小数部分がタイル内での位置。256倍してピクセル座標にする。
    px, py = int((fx - x) * 256), int((fy - y) * 256)
    return x, y, px, py


def color_to_mm_h(pixel) -> float:
    """タイルのピクセル色を降水強度(mm/h)に変換する。"""
    r, g, b, a = pixel

    # 透明なピクセル = 降水なし
    if a == 0:
        return 0.0

    # 完全一致する色があればそれを使う
    key = (r, g, b)
    if key in COLOR_TO_MM_H:
        return COLOR_TO_MM_H[key]

    # 画像の縮小などで中間色になっている場合に備え、一番近い色を探す
    # (色空間上のユークリッド距離が最小のものを選ぶ)
    best_color = min(
        COLOR_TO_MM_H,
        key=lambda c: (c[0] - r) ** 2 + (c[1] - g) ** 2 + (c[2] - b) ** 2,
    )
    return COLOR_TO_MM_H[best_color]


def fetch_target_times(kind: str) -> list[dict]:
    """
    対象時刻の一覧を取得する。
    kind="N1" → 実況(過去〜現在), kind="N2" → 予測(1時間先まで)
    """
    url = f"{BASE_URL}/targetTimes_{kind}.json"
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return response.json()


def fetch_rain_at(basetime: str, validtime: str, lat: float, lon: float) -> float:
    """
    指定の時刻・地点周辺の降水強度(mm/h)の最大値をタイルから読み取る。
    タイルが存在しない場合は0.0を返す。
    """
    x, y, px, py = latlon_to_tile_pixel(lat, lon, ZOOM)
    url = f"{BASE_URL}/{basetime}/none/{validtime}/{ELEMENT}/{ZOOM}/{x}/{y}.png"

    response = requests.get(url, timeout=15)
    if response.status_code != 200:
        return 0.0

    image = Image.open(io.BytesIO(response.content)).convert("RGBA")

    # 自宅を中心とした正方形の範囲を調べ、一番強い雨を採用する。
    # max(0, ...) と min(255, ...) は、タイルの外にはみ出さないようにするため。
    # (自宅がタイルの端にある場合、隣のタイル側は見られないという制約は残る)
    max_rain = 0.0
    for sy in range(max(0, py - SAMPLE_RADIUS_PX), min(256, py + SAMPLE_RADIUS_PX + 1)):
        for sx in range(max(0, px - SAMPLE_RADIUS_PX), min(256, px + SAMPLE_RADIUS_PX + 1)):
            rain = color_to_mm_h(image.getpixel((sx, sy)))
            if rain > max_rain:
                max_rain = rain

    return max_rain


def parse_jma_time(s: str) -> datetime:
    """'20260822161000' 形式(UTC)をJSTのdatetimeに変換する。"""
    dt_utc = datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(JST)


def get_max_rain_within(lat: float, lon: float, minutes: int):
    """
    現在の実況＋指定分数先までの予測を調べ、最大の降水強度を返す。

    戻り値: (最大降水強度mm/h, その時刻のdatetime または None, 実況のbasetime)

    時刻を文字列ではなく datetime で返すのが大事なところ。
    実況(N1)は必ず数分前のデータなので、そのまま「◯◯時ごろから降ります」と
    案内すると過去の時刻を伝えてしまう。呼び出し側で過去/未来を判定できるようにする。
    """
    now = datetime.now(JST)
    cutoff = now + timedelta(minutes=minutes)

    # 実況(N1)の最新1件 = 「今降っている雨」
    observed = fetch_target_times("N1")
    latest = observed[0]
    checks = [(latest["basetime"], latest["validtime"])]

    # 予測(N2)のうち、指定時間内のものだけを対象にする
    for entry in fetch_target_times("N2"):
        if parse_jma_time(entry["validtime"]) <= cutoff:
            checks.append((entry["basetime"], entry["validtime"]))

    max_rain = 0.0
    max_dt = None

    for basetime, validtime in checks:
        rain = fetch_rain_at(basetime, validtime, lat, lon)
        if rain > max_rain:
            max_rain = rain
            max_dt = parse_jma_time(validtime)

    return max_rain, max_dt, latest["basetime"]


if __name__ == "__main__":
    # 単体で実行すると、自宅付近の状況を表示する(動作確認用)。
    # 座標はコードに書かず、環境変数(.env)から読む。理由は rain_alert.py を参照。
    import os

    LAT = float(os.environ.get("HOME_LAT", "0") or "0")
    LON = float(os.environ.get("HOME_LON", "0") or "0")
    if LAT == 0 and LON == 0:
        raise SystemExit(".env に HOME_LAT と HOME_LON を設定してください。")

    rain, when, basetime = get_max_rain_within(LAT, LON, 30)
    print(f"基準時刻: {parse_jma_time(basetime).strftime('%Y-%m-%d %H:%M')} (JST)")
    label = when.strftime("%H:%M") if when else "降水なし"
    print(f"直近30分の最大降水強度: {rain:.1f}mm/h ({label})")
