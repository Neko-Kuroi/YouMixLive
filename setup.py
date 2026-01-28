import subprocess
import threading
import time
import json # Import json for parsing

# Display IP information
print("--- Public IP Address ---")
try:
    # Run curl to get IP info in JSON format
    ip_process = subprocess.run(["curl", "ipinfo.io"], capture_output=True, text=True, check=True)
    ip_info_json = ip_process.stdout.strip()

    # Parse the JSON output and extract the 'ip' field
    ip_data = json.loads(ip_info_json)
    public_ip = ip_data.get("ip", "N/A")

    print(f"Your public IP address is: {public_ip}")
except subprocess.CalledProcessError as e:
    print(f"Error getting public IP: {e}")
    print(f"Stderr: {e.stderr}")
except json.JSONDecodeError:
    print(f"Error parsing JSON from ipinfo.io: {ip_info_json}")
except Exception as e:
    print(f"An unexpected error occurred: {e}")
print("-------------------------\n")

# FastAPI サーバーを起動（ログをリアルタイム表示）
def run_fastapi():
    print("🚀 FastAPI サーバーを起動中...")
    proc = subprocess.Popen(
        ["python", "/content/app.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    for line in proc.stdout:
        print(f"[FastAPI] {line.strip()}")

# localtunnel を起動
def run_localtunnel():
    time.sleep(3)  # FastAPI 起動を待つ
    print("\n🚇 localtunnel を起動中...\n")
    proc = subprocess.Popen(
        ["npx", "localtunnel", "--port", "8000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    for line in proc.stdout:
        if "your url is: https://" in line:
            print("\n" + "="*60)
            print("🌐 公開URL（このリンクをクリック！）:")
            print(line.strip().replace("your url is: ", ""))
            print("="*60 + "\n")
        else:
            print(f"[lt] {line.strip()}")

# 両方を並列実行
threading.Thread(target=run_fastapi, daemon=True).start()
threading.Thread(target=run_localtunnel, daemon=True).start()

# 継続的に実行を維持
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\n🛑 停止しました。")