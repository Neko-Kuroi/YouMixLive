import subprocess
import threading
import time
import re
import os
# -nc オプションは、ファイルが既に存在する場合に再ダウンロードしないようにします。
os.system('wget -nc https://github.com/ekzhang/bore/releases/download/v0.6.0/bore-v0.6.0-x86_64-unknown-linux-musl.tar.gz')
# ダウンロードしたアーカイブを解凍します。
os.system('tar -zxvf bore-v0.6.0-x86_64-unknown-linux-musl.tar.gz')
# 実行権限を付与。
os.system('chmod 764 bore')
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

# bore tunnel を起動
def run_bore():
    time.sleep(3)  # FastAPI 起動待ち
    print("\n🌍 bore tunnel を起動中...\n")
    # boreは実行時にクライアントにダウンロードされるため、パスを通しておく
    os.environ['PATH'] += ":/usr/local/bin"
    proc = subprocess.Popen(
        [
            "./bore",
            "local", # 公開されているboreサーバーを使用
            "8000",
            "--to",
            "bore.pub",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )

    url_pattern = re.compile(r"bore+\.pub\:[0-9]+")

    for line in proc.stdout:
        match = url_pattern.search(line)
        if match:
            print("\n" + "=" * 60)
            print("🌐 公開URL:")
            print(match.group(0))
            print("=" * 60 + "\n")
        else:
            print(f"[bore] {line.rstrip()}")

# 並列実行
threading.Thread(target=run_fastapi, daemon=True).start()
threading.Thread(target=run_bore, daemon=True).start()

# 実行維持
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\n🛑 停止しました。")
