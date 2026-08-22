#!/usr/bin/env python3
"""
daily_summary.py
毎朝、その日1日分の降水予報をグラフにしてLINEへ送るスクリプト。

【全体の流れ】
1. Open-Meteoから24時間分の時間ごとの降水量を取得
2. matplotlibで棒グラフの画像(PNG)を作る
3. LINEは「画像のURL」を渡す仕組みなので、画像をimgbb(無料の画像置き場)に
   アップロードし、公開URLをもらう
4. そのURLと、要約テキストをLINEに送る

【事前準備】
1. https://api.imgbb.com/ で無料アカウント登録 → APIキーを取得
2. .env に IMGBB_API_KEY="取得したキー" を追記

【学習ポイント】
- matplotlibでのグラフ作成(棒グラフ、色分け、日本語フォント指定)
- ファイルをAPIにアップロードする書き方(multipart/form-data)
- rain_alert.pyの設定(閾値など)を import して使い回す方法
"""

import base64
import os
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")  # 画面を使わず画像ファイルとして描画するモード
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import requests

import rain_alert  # 既存の設定(HOME_LAT, 閾値など)とLINE送信関数を再利用する

# ============ CONFIG ============

# 保存先ディレクトリ(このファイルと同じ場所に charts/ フォルダを作る)
CHART_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "charts")

IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY", "")
IMGBB_UPLOAD_URL = "https://api.imgbb.com/1/upload"

# 日本語を表示できるフォントを選ぶ。
# Macでは "Hiragino Sans"、GitHub Actions(Linux)では "Noto Sans CJK JP" が入っている。
# 実行環境に存在するものを先頭から探して使う。
_JP_FONT_CANDIDATES = ["Hiragino Sans", "Noto Sans CJK JP", "IPAexGothic", "Noto Sans JP"]
_available = {f.name for f in fm.fontManager.ttflist}
for _font in _JP_FONT_CANDIDATES:
    if _font in _available:
        plt.rcParams["font.family"] = _font
        break
else:
    print("警告: 日本語フォントが見つかりません。グラフの日本語が文字化けする可能性があります。")


# ============ 予報データの取得 ============


def fetch_hourly_forecast(lat: float, lon: float) -> list[dict]:
    """
    Open-Meteoから今日1日分(24時間)の時間ごとの降水量を取得する。
    戻り値: [{"time": datetime, "precip_mm_h": 2.3}, ...]
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "precipitation",
        "timezone": "Asia/Tokyo",
        "forecast_days": 1,
    }
    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()

    times = data["hourly"]["time"]
    precip = data["hourly"]["precipitation"]  # 時間ごとの降水量(mm) = そのまま mm/h

    return [
        {"time": datetime.fromisoformat(t), "precip_mm_h": p}
        for t, p in zip(times, precip)
    ]


# ============ グラフ作成 ============


def bar_color(mm_h: float) -> str:
    """降水強度に応じて棒の色を変える(rain_alert.pyの閾値をそのまま使う)。"""
    if mm_h >= rain_alert.HEAVY_RAIN_THRESHOLD_MM_H:
        return "#d81b1b"  # 赤: 豪雨
    if mm_h >= rain_alert.RAIN_THRESHOLD_MM_H:
        return "#3b82f6"  # 青: 雨
    return "#d1d5db"  # グレー: ほぼ降らない


def build_chart(forecast: list[dict]) -> str:
    """
    降水予報から棒グラフのPNG画像を作り、ファイルパスを返す。
    """
    os.makedirs(CHART_DIR, exist_ok=True)

    hours = [entry["time"].strftime("%H") for entry in forecast]
    values = [entry["precip_mm_h"] for entry in forecast]
    colors = [bar_color(v) for v in values]

    fig, ax = plt.subplots(figsize=(9, 4), dpi=150)
    ax.bar(hours, values, color=colors)

    # 閾値の位置に目安線を引く(グラフだけ見ても危険度がわかるように)
    ax.axhline(rain_alert.RAIN_THRESHOLD_MM_H, color="#3b82f6", linewidth=1, linestyle="--", alpha=0.5)
    ax.axhline(rain_alert.HEAVY_RAIN_THRESHOLD_MM_H, color="#d81b1b", linewidth=1, linestyle="--", alpha=0.5)

    today_str = forecast[0]["time"].strftime("%Y年%m月%d日")
    ax.set_title(f"{today_str} の降水予報 (mm/h)")
    ax.set_xlabel("時刻")
    ax.set_ylabel("降水強度 (mm/h)")

    fig.tight_layout()

    filename = f"daily_{forecast[0]['time']:%Y%m%d}.png"
    filepath = os.path.join(CHART_DIR, filename)
    fig.savefig(filepath)
    plt.close(fig)

    return filepath


# ============ 画像のアップロード(LINEに送るには公開URLが必要) ============


def upload_image(filepath: str) -> str:
    """
    imgbbに画像をアップロードし、公開URL(HTTPS)を返す。
    LINEの画像メッセージは、インターネット上のURLでないと送れないため。
    """
    if not IMGBB_API_KEY:
        raise RuntimeError("IMGBB_API_KEYが未設定です。.envに追記してください。")

    with open(filepath, "rb") as f:
        # imgbbは画像データをbase64文字列にして送る仕様
        image_b64 = base64.b64encode(f.read())

    response = requests.post(
        IMGBB_UPLOAD_URL,
        data={"key": IMGBB_API_KEY, "image": image_b64},
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    return result["data"]["url"]


# ============ LINE送信(画像+テキスト) ============


def send_line_image_and_text(image_url: str, caption: str):
    """LINE Messaging APIで画像1枚とテキスト1通をまとめて送る。"""
    if not rain_alert.LINE_CHANNEL_ACCESS_TOKEN or not rain_alert.LINE_USER_ID:
        print("LINEの設定(トークン/ユーザーID)が未設定です。")
        return

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {rain_alert.LINE_CHANNEL_ACCESS_TOKEN}",
    }
    body = {
        "to": rain_alert.LINE_USER_ID,
        "messages": [
            {
                "type": "image",
                "originalContentUrl": image_url,
                "previewImageUrl": image_url,
            },
            {"type": "text", "text": caption},
        ],
    }

    response = requests.post(url, headers=headers, json=body, timeout=10)
    if response.status_code == 200:
        print("LINEへグラフを送信しました。")
    else:
        print(f"LINE送信に失敗しました: {response.status_code} {response.text}")


# ============ メイン処理 ============


def build_caption(forecast: list[dict]) -> str:
    """グラフに添えるテキストの要約を作る。"""
    max_entry = max(forecast, key=lambda e: e["precip_mm_h"])
    total = sum(e["precip_mm_h"] for e in forecast)

    if max_entry["precip_mm_h"] >= rain_alert.HEAVY_RAIN_THRESHOLD_MM_H:
        headline = "☔ 今日は激しい雨に警戒してください"
    elif max_entry["precip_mm_h"] >= rain_alert.RAIN_THRESHOLD_MM_H:
        headline = "🌂 今日は雨が降りそうです"
    else:
        headline = "☀️ 今日は雨の心配は少なそうです"

    return (
        f"{headline}\n"
        f"最大 {max_entry['precip_mm_h']:.1f}mm/h"
        f"({max_entry['time']:%H時}ごろ)\n"
        f"1日の合計降水量目安: {total:.1f}mm"
    )


def main():
    print(f"[{datetime.now(rain_alert.JST):%Y-%m-%d %H:%M:%S} JST] 1日分の降水予報を作成中...")

    forecast = fetch_hourly_forecast(rain_alert.HOME_LAT, rain_alert.HOME_LON)
    chart_path = build_chart(forecast)
    print(f"グラフを保存しました: {chart_path}")

    image_url = upload_image(chart_path)
    print(f"アップロード完了: {image_url}")

    caption = build_caption(forecast)
    send_line_image_and_text(image_url, caption)


if __name__ == "__main__":
    main()
