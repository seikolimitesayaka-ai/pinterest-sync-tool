#!/usr/bin/env python3
"""
Pinterest ボード自動同期スクリプト
==================================
自分のPinterestボードに保存済みのピンを取得し、
まだダウンロードしていない画像だけを日付フォルダに保存します。

【前提】
- Pinterest Developer でアプリを作成し、アクセストークンを取得済みであること
  https://developers.pinterest.com/
- 対象ボードは自分のアカウントが所有 or コラボレーターであること

【必要な環境変数】
  PINTEREST_ACCESS_TOKEN  Pinterest API v5 のアクセストークン
  PINTEREST_BOARD_ID      同期したいボードのID
  EMAIL_SMTP_HOST         (任意) 完了通知を送る場合のSMTPサーバー（例: smtp.gmail.com）
  EMAIL_SMTP_PORT         (任意) SMTPポート（例: 587）
  EMAIL_ADDRESS           (任意) 送信元メールアドレス
  EMAIL_PASSWORD          (任意) 送信元メールのパスワード（Gmailの場合はアプリパスワード）
  EMAIL_TO                (任意) 通知の宛先メールアドレス

【使い方】
  python3 pinterest_sync.py --output ./pinterest_downloads

【定期実行の例（cron / macOS・Linux）】
  毎日9時に実行する場合、crontab -e で以下を追加:
  0 9 * * * cd /path/to/script && /usr/bin/python3 pinterest_sync.py --output ./pinterest_downloads >> sync.log 2>&1

【Windowsの場合】
  タスクスケジューラーで「プログラムの開始」に python.exe、
  引数に pinterest_sync.py のフルパスを指定し、毎日のトリガーを設定してください。
"""

import argparse
import json
import os
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlencode

import urllib.request
import urllib.error

API_BASE = "https://api.pinterest.com/v5"


def get_env_or_die(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"[ERROR] 環境変数 {name} が設定されていません。", file=sys.stderr)
        sys.exit(1)
    return value


def api_get(path: str, token: str, params: dict | None = None) -> dict:
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"[ERROR] API呼び出し失敗: {e.code} {e.read().decode('utf-8')}", file=sys.stderr)
        sys.exit(1)


def fetch_all_pins(board_id: str, token: str) -> list[dict]:
    """ボード内の全ピンをページネーションしながら取得"""
    pins = []
    bookmark = None
    while True:
        params = {"page_size": 100}
        if bookmark:
            params["bookmark"] = bookmark
        data = api_get(f"/boards/{board_id}/pins", token, params)
        pins.extend(data.get("items", []))
        bookmark = data.get("bookmark")
        if not bookmark:
            break
    return pins


def download_image(url: str, dest: Path) -> None:
    urllib.request.urlretrieve(url, dest)


def load_seen_ids(state_file: Path) -> set:
    if state_file.exists():
        return set(json.loads(state_file.read_text(encoding="utf-8")))
    return set()


def save_seen_ids(state_file: Path, seen_ids: set) -> None:
    state_file.write_text(json.dumps(sorted(seen_ids)), encoding="utf-8")


def notify_email(smtp_host: str, smtp_port: int, from_addr: str, password: str, to_addr: str, subject: str, message: str) -> None:
    msg = MIMEText(message, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.starttls()
            server.login(from_addr, password)
            server.send_message(msg)
    except Exception as e:
        print(f"[WARN] メール通知に失敗しました: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Pinterestボードの新規ピンを自動ダウンロード")
    parser.add_argument(
        "--output", default="./pinterest_downloads", help="保存先ディレクトリ（デフォルト: ./pinterest_downloads）"
    )
    args = parser.parse_args()

    token = get_env_or_die("PINTEREST_ACCESS_TOKEN")
    board_id = get_env_or_die("PINTEREST_BOARD_ID")
    smtp_host = os.environ.get("EMAIL_SMTP_HOST")
    smtp_port = os.environ.get("EMAIL_SMTP_PORT")
    email_address = os.environ.get("EMAIL_ADDRESS")
    email_password = os.environ.get("EMAIL_PASSWORD")
    email_to = os.environ.get("EMAIL_TO")
    email_configured = all([smtp_host, smtp_port, email_address, email_password, email_to])

    today = datetime.now().strftime("%Y-%m-%d")
    output_root = Path(args.output)
    today_dir = output_root / today
    today_dir.mkdir(parents=True, exist_ok=True)

    state_file = output_root / "downloaded_ids.json"
    seen_ids = load_seen_ids(state_file)

    print(f"[INFO] ボード {board_id} のピンを取得中...")
    pins = fetch_all_pins(board_id, token)
    print(f"[INFO] 合計 {len(pins)} 件のピンを検出")

    new_count = 0
    for pin in pins:
        pin_id = pin.get("id")
        if not pin_id or pin_id in seen_ids:
            continue

        media = pin.get("media", {})
        images = media.get("images", {})
        # 最大解像度の画像URLを選択
        best_url = None
        best_size = -1
        for size_key, img in images.items():
            w = img.get("width", 0)
            if w > best_size:
                best_size = w
                best_url = img.get("url")

        if not best_url:
            continue

        ext = best_url.split(".")[-1].split("?")[0]
        if len(ext) > 4:
            ext = "jpg"
        dest_path = today_dir / f"{pin_id}.{ext}"

        try:
            download_image(best_url, dest_path)
            seen_ids.add(pin_id)
            new_count += 1
            print(f"[OK] 保存: {dest_path.name}")
        except Exception as e:
            print(f"[ERROR] ダウンロード失敗 ({pin_id}): {e}", file=sys.stderr)

    save_seen_ids(state_file, seen_ids)

    summary = f"Pinterest同期完了: 新規 {new_count} 件を {today_dir} に保存しました。"
    print(f"[INFO] {summary}")

    if email_configured and new_count > 0:
        notify_email(
            smtp_host,
            int(smtp_port),
            email_address,
            email_password,
            email_to,
            "Pinterest自動同期の完了通知",
            summary,
        )


if __name__ == "__main__":
    main()
