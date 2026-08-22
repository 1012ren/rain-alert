#!/bin/sh
# cronから実行するためのラッパー。
# cronは普段のシェル設定を読み込まないので、ここで .env を読んでから
# Pythonスクリプトを起動する。

# このスクリプトが置かれているディレクトリに移動する
cd "$(dirname "$0")" || exit 1

# .env の中身を環境変数として読み込む
# (set -a は「以降の変数定義を自動的に環境変数にする」という意味)
set -a
. ./.env
set +a

exec /usr/local/bin/python3 rain_alert.py
