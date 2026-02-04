import subprocess
import threading
import time
import re
import os
os.system('wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64')
os.system('chmod +x cloudflared-linux-amd64')
os.system('sudo mv cloudflared-linux-amd64 /usr/local/bin/cloudflared')
# FastAPI サーバーを起動（ログをリアルタイム表示）
def run_fastapi():
    print("🚀 FastAPI サーバーを起動中...")
    proc = subprocess.Popen(
        ["python", "/content/app.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )
    for line in proc.stdout:
        print(f"[FastAPI] {line.rstrip()}")

# cloudflared tunnel を起動（trycloudflare）
def run_cloudflared():
    time.sleep(3)  # FastAPI 起動待ち
    print("\n☁️ cloudflared tunnel を起動中...\n")

    proc = subprocess.Popen(
        [
            "cloudflared",
            "tunnel",
            "--url",
            "http://localhost:8000",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )

    url_pattern = re.compile(r"https://[-\w]+\.trycloudflare\.com")

    for line in proc.stdout:
        match = url_pattern.search(line)
        if match:
            print("\n" + "=" * 60)
            print("🌐 公開URL（このリンクをクリック！）:")
            print(match.group(0))
            print("=" * 60 + "\n")
        else:
            print(f"[cloudflared] {line.rstrip()}")

# 並列実行
threading.Thread(target=run_fastapi, daemon=True).start()
threading.Thread(target=run_cloudflared, daemon=True).start()

# 実行維持
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\n🛑 停止しました。")
