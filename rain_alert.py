#!/usr/bin/env python3
"""
rain_alert.py
自宅の座標をもとに降水予報を取得し、雨やゲリラ豪雨が
予想される場合にLINEへ通知するスクリプト。

【2つのデータ源を併用する】
- 気象庁 高解像度降水ナウキャスト (jma_nowcast.py)
    約250mメッシュ・5分間隔更新。急に発達するゲリラ豪雨に強い。主役。
- Open-Meteo API
    格子は粗いが世界的な予報モデル。ナウキャストが取れない時の保険。
  → 両方を取得し、「より強い方」を採用する(見逃しを減らすため)。

【使い方】
1. 下の CONFIG セクションを自分の環境に合わせて書き換える
2. LINE_CHANNEL_ACCESS_TOKEN と LINE_USER_ID を環境変数で渡す
     export LINE_CHANNEL_ACCESS_TOKEN="xxxxx"
     export LINE_USER_ID="Uxxxxxxxxxxxxxxxxxxxx"
3. 動作確認: python3 rain_alert.py
4. cronに登録して定期実行(例: 3分おき)
     crontab -e
     */3 * * * * /usr/bin/python3 /path/to/rain_alert.py >> /path/to/rain_alert.log 2>&1

【学習ポイント】
- requests で外部APIを呼ぶ基本パターン
- JSONレスポンスのパース
- 状態をファイルに保存して「連続通知」を防ぐやり方(クールダウン)
- try/except で「片方のデータ源が落ちても動き続ける」書き方
- 環境変数で秘密情報(トークン)を扱う方法
"""

import os
import json
import requests
from datetime import datetime, timedelta

import jma_nowcast

# 日本標準時。実行環境(Mac=JST, GitHub Actions=UTC)に左右されないよう、
# 時刻の比較・表示はすべてこれを基準に行う。
JST = jma_nowcast.JST

# ============ CONFIG(ここを自分用に調整) ============

# 自宅の座標。
# 【重要】ここに直接書かないこと。このリポジトリは公開されているため、
# 座標をコードに書くと自宅の場所が誰にでも見えてしまう。
# ローカルでは .env に、GitHub Actionsでは Secrets に入れて渡す。
#   .env の例:
#     HOME_LAT=35.1234
#     HOME_LON=139.5678
HOME_LAT = float(os.environ.get("HOME_LAT", "0") or "0")
HOME_LON = float(os.environ.get("HOME_LON", "0") or "0")

if HOME_LAT == 0 and HOME_LON == 0:
    raise SystemExit(
        "自宅の座標が設定されていません。\n"
        "  ローカル : .env に HOME_LAT と HOME_LON を書いてください\n"
        "  クラウド : GitHubのSecretsに HOME_LAT と HOME_LON を登録してください"
    )

# 通知を2段階に分ける。
# 「雨が降る」の目安 (mm/h)。1.0mm/hは傘がほしくなる程度の雨。
# もっと敏感にしたければ0.5、小雨は無視したければ2.0あたりに調整する。
RAIN_THRESHOLD_MM_H = 1.0

# 「ゲリラ豪雨」の目安 (mm/h)
# 気象庁の目安: 20mm/h以上で「強い雨」、30mm/h以上で「激しい雨」
HEAVY_RAIN_THRESHOLD_MM_H = 20.0

# 何分先までの予報をチェックするか
LOOKAHEAD_MINUTES = 30

# 同じ警報を再送しないためのクールダウン時間(分)
# 弱い雨と豪雨で別々に管理するので、小雨の通知後でも豪雨はすぐ通知される。
RAIN_COOLDOWN_MINUTES = 60
HEAVY_COOLDOWN_MINUTES = 30

# 状態保存用ファイル(通知時刻と、前回取得したデータを記録する)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rain_alert_state.json")

# LINE Messaging API の設定(環境変数から読む。直接書いてもOKだが非推奨)
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")

# ============ Open-Meteo(補助のデータ源) ============


def fetch_precipitation_forecast(lat: float, lon: float) -> list[dict]:
    """
    Open-Meteo APIから15分刻みの降水予報を取得する。
    戻り値: [{"time": "2026-08-21T15:00", "precip_mm_h": 5.2}, ...]
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "minutely_15": "precipitation",
        "timezone": "Asia/Tokyo",
        "forecast_days": 1,
    }

    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()

    times = data["minutely_15"]["time"]
    precip = data["minutely_15"]["precipitation"]  # mm(15分あたりの降水量)

    forecast = []
    for t, p in zip(times, precip):
        # 15分あたりの降水量を時間降水量(mm/h)に換算
        mm_per_hour = p * 4
        forecast.append({"time": t, "precip_mm_h": mm_per_hour})

    return forecast


def get_upcoming_max_rain(forecast: list[dict], minutes: int) -> tuple[float, str]:
    """
    直近 minutes 分以内で最大の降水強度と、その時刻を返す。
    """
    # 【重要】Open-MeteoにはAsia/Tokyoを指定しているので、返ってくる時刻はJST。
    # 一方 datetime.now() は「実行しているマシンの時刻」を返す。
    # GitHub Actionsのサーバーは常にUTCなので、そのまま比べると9時間ずれて
    # 予報を1件も拾えなくなる。必ずJSTで「今」を取ること。
    now = datetime.now(JST)

    # 予報は15分刻みなので、「今まさに進行中の枠」も対象に含めたい。
    # 例: 今が10:58なら、10:45〜11:00の枠(ラベルは10:45)から見る。
    # そうしないと、既に降り始めている雨を見逃してしまう。
    window_start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    window_end = now + timedelta(minutes=minutes)

    max_rain = 0.0
    max_time = ""

    for entry in forecast:
        # APIの時刻文字列にはタイムゾーンが書かれていない("2026-08-23T02:00")ので、
        # 「これはJSTである」と明示してから比べる。
        entry_time = datetime.fromisoformat(entry["time"]).replace(tzinfo=JST)
        if window_start <= entry_time <= window_end:
            if entry["precip_mm_h"] > max_rain:
                max_rain = entry["precip_mm_h"]
                max_time = entry_time.strftime("%H:%M")

    return max_rain, max_time


# ============ 2つのデータ源をまとめる ============


def get_rain_outlook(state: dict) -> tuple[float, str, str, str]:
    """
    気象庁ナウキャストとOpen-Meteoの両方を調べ、強い方を採用する。

    戻り値: (降水強度mm/h, 時刻, データ源名, 気象庁のbasetime)

    片方が失敗しても、もう片方の結果で動き続ける(try/except)。
    非公式のタイル配信を使っている都合上、これは実用上かなり重要。
    """
    candidates = []
    basetime = ""

    # --- 気象庁ナウキャスト(主役) ---
    try:
        jma_rain, jma_time, basetime = jma_nowcast.get_max_rain_within(
            HOME_LAT, HOME_LON, LOOKAHEAD_MINUTES
        )
        candidates.append((jma_rain, jma_time, "気象庁ナウキャスト"))
    except Exception as e:
        print(f"警告: 気象庁ナウキャストの取得に失敗しました: {e}")

    # --- Open-Meteo(保険) ---
    try:
        forecast = fetch_precipitation_forecast(HOME_LAT, HOME_LON)
        om_rain, om_time = get_upcoming_max_rain(forecast, LOOKAHEAD_MINUTES)
        candidates.append((om_rain, om_time, "Open-Meteo"))
    except Exception as e:
        print(f"警告: Open-Meteoの取得に失敗しました: {e}")

    if not candidates:
        raise RuntimeError("どちらのデータ源からも降水情報を取得できませんでした")

    # 内訳をログに残しておくと、後で「どちらが当たっていたか」を検証できる
    for rain, when, source in candidates:
        print(f"  {source}: {rain:.1f}mm/h ({when or '降水なし'})")

    # 見逃しを減らすため、強い方を採用する
    max_rain, max_time, source = max(candidates, key=lambda c: c[0])
    return max_rain, max_time, source, basetime


# ============ 通知のクールダウン管理 ============


def load_state() -> dict:
    """状態ファイルを読む。無ければ空の辞書を返す。"""
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def should_send_notification(state: dict, level: str, cooldown_minutes: int) -> bool:
    """
    level ("rain" または "heavy") ごとに、前回通知から
    cooldown_minutes 経過していればTrueを返す。
    """
    # .get() は「キーが無ければNoneを返す」書き方。
    # 初回はまだ記録が無いので、そのまま通知してよい。
    last_sent_str = state.get(f"last_sent_{level}")
    if last_sent_str is None:
        return True

    last_sent = datetime.fromisoformat(last_sent_str)
    # 古い形式(タイムゾーン情報なし)で保存されていた場合はJSTとみなす。
    # aware(情報あり)とnaive(情報なし)を直接引き算するとエラーになるため。
    if last_sent.tzinfo is None:
        last_sent = last_sent.replace(tzinfo=JST)

    elapsed = datetime.now(JST) - last_sent
    return elapsed > timedelta(minutes=cooldown_minutes)


# ============ LINE通知 ============


def send_line_message(message: str):
    """LINE Messaging APIで自分宛にpushメッセージを送る"""
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        print("LINEの設定(トークン/ユーザーID)が未設定です。環境変数を確認してください。")
        return

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    body = {
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": message}],
    }

    response = requests.post(url, headers=headers, json=body, timeout=10)
    if response.status_code == 200:
        print("LINE通知を送信しました。")
    else:
        print(f"LINE通知の送信に失敗しました: {response.status_code} {response.text}")


# ============ メイン処理 ============


def main():
    print(f"[{datetime.now(JST):%Y-%m-%d %H:%M:%S} JST] 降水予報をチェック中...")

    state = load_state()
    max_rain, max_time, source, basetime = get_rain_outlook(state)

    print(f"採用: {max_rain:.1f}mm/h ({max_time or 'N/A'}) ← {source}")

    # 強い方から順に判定する。豪雨なら豪雨の通知だけを出す。
    if max_rain >= HEAVY_RAIN_THRESHOLD_MM_H:
        level = "heavy"
        cooldown = HEAVY_COOLDOWN_MINUTES
        message = (
            f"【豪雨注意】\n"
            f"自宅付近で {max_time} ごろ、\n"
            f"約{max_rain:.0f}mm/h以上の激しい雨が予想されています。\n"
            f"洗濯物・窓の確認をおすすめします。\n"
            f"(出典: {source})"
        )
    elif max_rain >= RAIN_THRESHOLD_MM_H:
        level = "rain"
        cooldown = RAIN_COOLDOWN_MINUTES
        message = (
            f"【まもなく雨】\n"
            f"自宅付近で {max_time} ごろから、\n"
            f"約{max_rain:.1f}mm/hの雨が予想されています。\n"
            f"洗濯物の取り込みをおすすめします。\n"
            f"(出典: {source})"
        )
    else:
        print("しきい値未満のため通知なし。")
        save_state(state)
        return

    if should_send_notification(state, level, cooldown):
        send_line_message(message)
        state[f"last_sent_{level}"] = datetime.now(JST).isoformat()
    else:
        print(f"クールダウン中({level})のため通知をスキップしました。")

    save_state(state)


if __name__ == "__main__":
    main()
