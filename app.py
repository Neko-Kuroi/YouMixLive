#%%writefile app.py
""" streamlink yt-dlp + LiveStream """
from fastapi import FastAPI, Query, HTTPException, Request, WebSocket
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import asyncio
from urllib.parse import urlparse
import sys
import io

import os
import uuid
import shutil
from fastapi import UploadFile, File
from fastapi.staticfiles import StaticFiles
from asyncio import TimeoutError
import json

# --- FastAPI App Setup ---
app = FastAPI()

# CORS設定（フロントエンドとドメインが異なる場合に必要）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# スタティックファイルのディレクトリ準備
import os
os.makedirs("/content/static", exist_ok=True)
app.mount("/static", StaticFiles(directory="/content/static"), name="static")

@app.get("/")
async def root():
    return FileResponse("/content/static/index.html")

# --- Constants & Semaphores ---
YTDLP_TIMEOUT = 15
MAX_YTDLP_CONCURRENCY = 2
MAX_FFMPEG_CONCURRENCY = 5

yt_dlp_semaphore = asyncio.Semaphore(MAX_YTDLP_CONCURRENCY)
ffmpeg_semaphore = asyncio.Semaphore(MAX_FFMPEG_CONCURRENCY)

MEDIA_TYPES = {
    "webm": "audio/webm",
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "mp4": "video/mp4"
}

FORMAT_ARGS = {
    "webm": ["-c:a", "libopus", "-b:a", "128k", "-f", "webm"],
    "wav": ["-c:a", "pcm_s16le", "-f", "wav"],
    "mp3": ["-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3"],
    "mp4": [
        "-c:v", "copy",
        "-c:a", "aac",
        "-f", "mp4",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof"
    ],
}

# --- Utilities ---
def validate_youtube_url(url: str):
    parsed = urlparse(url)
    hosts = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
    if parsed.netloc not in hosts:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")

# YouTube以外のサービス拡張
def validate_url(url: str):
    parsed = urlparse(url)
    allowed_hosts = ["youtube.com", "youtu.be", "twitch.tv", "bilibili.com", "b23.tv", "dailymotion.com"]

    # ホスト名の先頭にドットを付けるか、完全一致ならOKとする
    host_name = parsed.netloc
    is_valid = any(host_name == host or host_name.endswith("." + host) for host in allowed_hosts)

    if not is_valid:
        raise HTTPException(status_code=400, detail="Unsupported platform")

# YouTube以外のサービス拡張
def get_platform_headers(url: str) -> str:
    """
    サイトごとのアクセス制限（リファラチェック等）を回避するためのヘッダーを生成
    """
    headers = []
    if "bilibili.com" in url or "b23.tv" in url:
        headers.append("Referer: https://www.bilibili.com/")
        headers.append("User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    elif "dailymotion.com" in url:
        headers.append("Referer: https://www.dailymotion.com/")

    # FFmpegの-headersオプションは各行を\r\nで区切った1つの文字列である必要がある
    return "\r\n".join(headers) + "\r\n" if headers else ""

async def get_audio_url(youtube_url: str):
    """
    StreamlinkでLive判定を行い、失敗すればyt-dlpでVODとして取得するハイブリッド方式
    """
    async with yt_dlp_semaphore:
        # 1. まずStreamlinkでLiveとして試行
        proc_sl = await asyncio.create_subprocess_exec(
            "streamlink", "--stream-url", youtube_url, "best",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout_sl, _ = await proc_sl.communicate()
        if proc_sl.returncode == 0:
            return stdout_sl.decode().strip(), True # (url, is_live)

        # 2. Liveでなければyt-dlpで通常取得（高画質優先）
        proc_yt = await asyncio.create_subprocess_exec(
            "yt-dlp",
            "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
            "-g",
            youtube_url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout_yt, stderr_yt = await asyncio.wait_for(proc_yt.communicate(), timeout=YTDLP_TIMEOUT)
            if proc_yt.returncode != 0:
                print(f"[yt-dlp error] {stderr_yt.decode()}")
                raise HTTPException(status_code=400, detail="Audio extraction failed")

            # 最初のURLを返す（video+audioの場合は結合されたURL、または最高画質のURL）
            url_output = stdout_yt.decode().strip().splitlines()[0]

            # URLから画質情報を抽出（itagパラメーター）
            import re
            itag_match = re.search(r'itag[=/](\d+)', url_output)
            if itag_match:
                itag = itag_match.group(1)
                # 主要なitagの解像度マッピング
                itag_quality = {
                    '137': '1080p', '136': '720p', '135': '480p', '134': '360p',
                    '22': '720p', '18': '360p', '43': '360p'
                }
                quality = itag_quality.get(itag, f'itag={itag}')
                print(f"[yt-dlp] Selected format: {quality}")

            print(f"[yt-dlp] URL length: {len(url_output)} chars")
            return url_output, False
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Extraction timeout")
# YouTube以外のサービス拡張
async def get_audio_url_2(target_url: str):
    async with yt_dlp_semaphore:
        # 1. Streamlink で試行 (Liveに強い)
        proc_sl = await asyncio.create_subprocess_exec(
            "streamlink", "--stream-url", target_url, "best",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout_sl, _ = await proc_sl.communicate()
        if proc_sl.returncode == 0:
            return stdout_sl.decode().strip(), True

        # 2. yt-dlp で試行 (VODに強い)
        # 汎用的なフォーマット指定に変更 (b=best, audio only も考慮)
        ytdl_format = "bestaudio/best" if "audio" in target_url else "bestvideo+bestaudio/best"

        proc_yt = await asyncio.create_subprocess_exec(
            "yt-dlp", "-g", "-f", ytdl_format, target_url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout_yt, stderr_yt = await asyncio.wait_for(proc_yt.communicate(), timeout=YTDLP_TIMEOUT)
            if proc_yt.returncode == 0:
                url_output = stdout_yt.decode().strip().splitlines()[0]
                return url_output, False
            else:
                raise HTTPException(status_code=400, detail="URL extraction failed")
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Extraction timeout")
# YouTube以外のサービス拡張
# blibili 対応
async def get_audio_url_3(target_url: str, is_audio_only: bool = False):
    """
    1. Streamlink (Live) -> 2. yt-dlp (VOD) の順でストリームURLを取得する。
    Bilibili等のDASH形式に対応するため、URLはリスト形式で返却する。
    """
    async with yt_dlp_semaphore:
        # --- 第1段階: Streamlinkでライブを優先判定 ---
        try:
            proc_sl = await asyncio.create_subprocess_exec(
                "streamlink", "--stream-url", target_url, "best",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout_sl, _ = await asyncio.wait_for(proc_sl.communicate(), timeout=10.0)

            if proc_sl.returncode == 0:
                url_str = stdout_sl.decode().strip()
                if url_str:
                    return [url_str], True  # is_live = True
        except Exception as e:
            print(f"[Streamlink Skip] {e}")

        # --- 第2段階: yt-dlp (VOD / アーカイブ) ---
        # ログインなしで安定する720p以下をターゲットにする
        format_spec = "bestaudio/best" if is_audio_only else "bestvideo[height<=720]+bestaudio/best"

        cmd = [
            "yt-dlp", "-g", "-f", format_spec,
            "--no-playlist",
            target_url
        ]

        proc_yt = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        try:
            stdout_yt, stderr_yt = await asyncio.wait_for(proc_yt.communicate(), timeout=YTDLP_TIMEOUT)

            if proc_yt.returncode != 0:
                error_msg = stderr_yt.decode().strip()
                print(f"[yt-dlp failed] {error_msg}")
                raise HTTPException(status_code=400, detail="Extraction failed")

            # URLをリストとして取得（Bilibili等は2行返ってくる）
            urls = [line.strip() for line in stdout_yt.decode().splitlines() if line.strip()]

            if not urls:
                raise HTTPException(status_code=404, detail="No stream URLs found")

            print(f"[DEBUG] Extracted {len(urls)} URL(s)")
            return urls, False  # is_live = False

        except asyncio.TimeoutError:
            if proc_yt: proc_yt.terminate()
            raise HTTPException(status_code=504, detail="Extraction timeout")
# --- Endpoints ---

@app.get("/audio")
async def audio_proxy(
    request: Request,
    url: str = Query(...),
    #start: float = Query(0.0, ge=0.0),
    start: str = Query("0"),
    format: str = Query("webm", regex="^(webm|wav|mp4)$"),
    repeat: bool = False,
):
    # 数値に変換（失敗したら0）
    try:
        start_sec = float(start)
    except (ValueError, TypeError):
        start_sec = 0.0
    validate_url(url) # YouTube 以外のサービス拡張
    # 1. URLの抽出
    urls, is_live = await get_audio_url_3(url, is_audio_only=False)
    extra_headers = get_platform_headers(url)

    # 2. FFmpegコマンド構築
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    # === 動画の解析 ===
    cmd += ["-probesize", "10M",        # 解析するデータ量を10MBに増やす
            "-analyzeduration", "5M"]   # 解析時間を5秒(5,000,000μs)に設定
    # 全ての入力URLに対して個別設定を適用
    for u in urls:
        if extra_headers:
            cmd += ["-headers", extra_headers]
        if start_sec > 0: # is_live の場合もスタート秒指定できる?
            cmd += ["-ss", str(start_sec)]
        cmd += ["-i", u]

    # 3. マッピング（重要）
    if len(urls) > 1:
        # 映像URL(0番目)と音声URL(最後)を合体
        cmd += ["-map", "0:v?", "-map", f"{len(urls)-1}:a"]
    else:
        # 入力が1つの場合
        cmd += ["-map", "0"]

    # 4. 出力エンコード設定
    if repeat and not is_live:
        cmd += ["-stream_loop", "-1"]

    # 再接続設定 (安定性のために追加)
    cmd += [
        "-reconnect", "1", "-reconnect_at_eof", "1",
        "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
    ]
    cmd += [
        "-c:v", "copy",           # 映像は再エンコードせずコピー（負荷軽減）
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-bsf:a", "aac_adtstoasc", # Bilibili等の音声不整合を修正
        "-f", "mp4",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "pipe:1"
    ]

    print(f"DEBUG: cmd is {cmd}")
    # 5. 実行とレスポンス
    async def stream():
        await ffmpeg_semaphore.acquire()
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )

        async def drain_stderr():
            try:
                while True:
                    if not await proc.stderr.read(1024): break
            except: pass
        stderr_task = asyncio.create_task(drain_stderr())

        try:
            buffer = bytearray()
            target = 512 * 1024 if not is_live else 128 * 1024
            while len(buffer) < target:
                chunk = await proc.stdout.read(16384)
                if not chunk: break
                buffer.extend(chunk)
            if buffer:
                yield bytes(buffer)

            while True:
                if await request.is_disconnected(): break
                chunk = await proc.stdout.read(16384)
                if not chunk: break
                yield chunk

        finally:
            stderr_task.cancel()
            if proc.returncode is None:
                proc.terminate()
            ffmpeg_semaphore.release()

    return StreamingResponse(stream(), media_type=MEDIA_TYPES[format])

# @app.get("/audio")
# async def audio_proxy(
#     request: Request,
#     url: str = Query(...),
#     #start: float = Query(0.0, ge=0.0),
#     start: str = Query("0"),
#     format: str = Query("webm", regex="^(webm|wav|mp4)$"),
#     repeat: bool = False,
# ):
#     # 数値に変換（失敗したら0）
#     try:
#         start_sec = float(start)
#     except (ValueError, TypeError):
#         start_sec = 0.0

#     validate_url(url) # YouTube 以外のサービス拡張
#     audio_url, is_live = await get_audio_url_2(url) # YouTube 以外のサービス拡張
#     # サイト固有のヘッダーを取得
#     extra_headers = get_platform_headers(url)
#     #validate_youtube_url(url)
#     #audio_url, is_live = await get_audio_url(url)

#     # すべてが is_live True になる可能性がある。

#     cmd = ["ffmpeg", "-nostdin", "-loglevel", "error"]

#     if start_sec > 0:
#         cmd += ["-ss", str(start_sec)]
#     # YouTube 以外のサービス拡張
#     if extra_headers:
#         cmd += ["-headers", extra_headers]

#     if repeat:
#     #if repeat and not is_live:
#         cmd += ["-stream_loop", "-1"]

#     # if not is_live:
#     #     cmd += [
#     #         "-reconnect", "1", "-reconnect_at_eof", "1",
#     #         "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
#     #     ]

#     # 再接続設定 (安定性のために追加)
#     cmd += [
#         "-reconnect", "1", "-reconnect_at_eof", "1",
#         "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
#     ]

#     cmd += ["-i", audio_url]
#     # Note: startパラメータはフロントエンドのvideo.currentTimeで処理される
#     cmd += ["-copyts"] # タイムスタンプを維持する
#     cmd += ["-fflags", "+nobuffer", "-ac", "2", "-ar", "48000"]
#     cmd += [*FORMAT_ARGS[format], "pipe:1"]
#     print(f"DEBUG: cmd is {cmd}")
#     async def stream():
#         await ffmpeg_semaphore.acquire()
#         proc = await asyncio.create_subprocess_exec(
#             *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
#         )

#         async def drain_stderr():
#             try:
#                 while True:
#                     if not await proc.stderr.read(1024): break
#             except: pass
#         stderr_task = asyncio.create_task(drain_stderr())

#         try:
#             buffer = bytearray()
#             target = 512 * 1024 if not is_live else 128 * 1024
#             while len(buffer) < target:
#                 chunk = await proc.stdout.read(16384)
#                 if not chunk: break
#                 buffer.extend(chunk)
#             if buffer:
#                 yield bytes(buffer)

#             while True:
#                 if await request.is_disconnected(): break
#                 chunk = await proc.stdout.read(16384)
#                 if not chunk: break
#                 yield chunk

#         finally:
#             stderr_task.cancel()
#             if proc.returncode is None:
#                 proc.terminate()
#             ffmpeg_semaphore.release()

#     return StreamingResponse(stream(), media_type=MEDIA_TYPES[format])

# --- Recording Conversion ---
UPLOAD_DIR = "temp_webm"
STATIC_DIR = "static"
MAX_CONCURRENT_CONVERSIONS = 2
CONVERSION_TIMEOUT = 1800

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

jobs = {}
conversion_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CONVERSIONS)

async def process_mp3_task(job_id: str, input_path: str, output_path: str, output_filename: str):
    async with conversion_semaphore:
        try:
            print(f"[*] Starting conversion: {job_id}")

            process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y",
                "-i", input_path,
                "-acodec", "libmp3lame",
                "-ab", "192k",
                output_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=CONVERSION_TIMEOUT
                )

                if process.returncode == 0:
                    jobs[job_id] = {
                        "status": "completed",
                        "url": f"/static/{output_filename}"
                    }
                    print(f"[+] Job {job_id} completed successfully.")
                else:
                    error_log = stderr.decode()
                    print(f"[-] FFmpeg Error ({job_id}): {error_log}")
                    jobs[job_id] = {"status": "failed"}

            except TimeoutError:
                print(f"[!] Job {job_id} timed out. Terminating...")
                try:
                    process.kill()
                    await process.wait()
                except:
                    pass
                jobs[job_id] = {"status": "failed", "reason": "timeout"}

        except Exception as e:
            print(f"[!] Unexpected error in task {job_id}: {e}")
            jobs[job_id] = {"status": "failed"}

        finally:
            if os.path.exists(input_path):
                os.remove(input_path)
                print(f"[*] Cleaned up input file: {input_path}")

@app.post("/convert/start")
async def start_convert(file: UploadFile = File(...)):
    job_id = str(uuid.uuid4())
    input_filename = f"{job_id}.webm"
    input_path = os.path.join(UPLOAD_DIR, input_filename)
    output_filename = f"mixed_{job_id}.mp3"
    output_path = os.path.join(STATIC_DIR, output_filename)

    try:
        with open(input_path, "wb") as buffer:
            await asyncio.to_thread(shutil.copyfileobj, file.file, buffer)

        jobs[job_id] = {"status": "processing", "url": None}

        asyncio.create_task(
            process_mp3_task(job_id, input_path, output_path, output_filename)
        )

        return {"job_id": job_id}

    except Exception as e:
        print(f"[!] Upload Error: {e}")
        if os.path.exists(input_path):
            os.remove(input_path)
        raise HTTPException(status_code=500, detail="Failed to start conversion")

@app.get("/convert/status/{job_id}")
async def get_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

# --- LiveStream ---
livestream_process = None
livestream_status = {"active": False, "rtmp_url": None}

@app.post("/livestream/start")
async def start_livestream(request: Request):
    global livestream_process, livestream_status

    if livestream_status["active"]:
        raise HTTPException(status_code=400, detail="LiveStream already running")

    try:
        data = await request.json()
        rtmp_url = data.get("rtmp_url")
        video_track = data.get("video_track", 1)
        ffmpeg_params = data.get("ffmpeg_params", {})

        if not rtmp_url:
            raise HTTPException(status_code=400, detail="RTMP URL required")

        # デフォルト値設定
        video_bitrate = ffmpeg_params.get("video_bitrate", 750)
        audio_bitrate = ffmpeg_params.get("audio_bitrate", 128)
        gop_size = ffmpeg_params.get("gop_size", 60)
        buffer_size = ffmpeg_params.get("buffer_size", 4096)
        async_param = ffmpeg_params.get("async_param", 0)  # デフォルト0に変更
        denoise_enabled = ffmpeg_params.get("denoise_enabled", True)  # DeNoiseデフォルトON

        print(f"[LiveStream] Parameters: vb={video_bitrate}k, ab={audio_bitrate}k, gop={gop_size}, buf={buffer_size}, async={async_param}, denoise={denoise_enabled}")

        livestream_status = {
            "active": True,
            "rtmp_url": rtmp_url,
            "video_track": video_track,
            "ffmpeg_params": ffmpeg_params
        }

        return {"status": "started", "message": "LiveStream initialized"}

    except Exception as e:
        print(f"[!] LiveStream start error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/livestream/stop")
async def stop_livestream():
    global livestream_process, livestream_status

    if livestream_process and livestream_process.returncode is None:
        livestream_process.terminate()
        await livestream_process.wait()

    livestream_status = {"active": False, "rtmp_url": None}
    livestream_process = None

    return {"status": "stopped"}

@app.get("/livestream/status")
async def get_livestream_status():
    return livestream_status

# WebSocket endpoint for receiving browser MediaStream
@app.websocket("/livestream/ws")
async def livestream_websocket(websocket: WebSocket):
    """
    ハイブリッド方式：
    - Video: Backend側でYouTube URLから取得（プレイリスト対応）
    - Audio: Browser側のMaster出力をWebSocketで受信
    """
    await websocket.accept()

    global livestream_process, livestream_status

    if not livestream_status.get("active"):
        await websocket.close(code=1008, reason="LiveStream not initialized")
        return

    rtmp_url = livestream_status["rtmp_url"]
    video_track_id = livestream_status["video_track"]
    ffmpeg_params = livestream_status.get("ffmpeg_params", {})

    # パラメーター取得（デフォルト値付き）
    video_bitrate = ffmpeg_params.get("video_bitrate", 750)
    audio_bitrate = ffmpeg_params.get("audio_bitrate", 128)
    gop_size = ffmpeg_params.get("gop_size", 60)
    buffer_size = ffmpeg_params.get("buffer_size", 4096)
    async_param = ffmpeg_params.get("async_param", 0)  # デフォルト0に変更
    denoise_enabled = ffmpeg_params.get("denoise_enabled", True)  # DeNoiseデフォルトON

    try:
        # 1. Browser側からプレイリスト情報を受信
        print("[LiveStream] Waiting for playlist data...")
        init_msg = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)

        playlist_urls = init_msg.get("playlist", [])
        current_index_raw = init_msg.get("current_index", 0)
        #current_index = init_msg.get("current_index", 0)
        repeat_mode = init_msg.get("repeat_mode", "none")

        print(f"[LiveStream] Received playlist: {len(playlist_urls)} items, starting at index {current_index_raw}")
        #print(f"[LiveStream] Received playlist: {len(playlist_urls)} items, starting at index {current_index}")
        print(f"[LiveStream] Repeat mode: {repeat_mode}")

        if not playlist_urls:
            await websocket.send_json({"error": "Empty playlist"})
            await websocket.close()
            return
        # === ここにガードを追加 ===
        # インデックスが範囲外なら 0 に引き戻す
        if current_index_raw < 0 or current_index_raw >= len(playlist_urls):
            print(f"[LiveStream] Warning: Index {current_index_raw} is out of bounds. Resetting to 0.")
            current_index = 0
        else:
            current_index = current_index_raw
        # =========================
        # 2. プレイリストループ
        playlist_finished = False

        while not playlist_finished:
            if current_index >= len(playlist_urls):
                if repeat_mode == "list":
                    print("[LiveStream] List repeat: restarting from beginning")
                    current_index = 0
                else:
                    print("[LiveStream] Playlist finished")
                    break

            current_url = playlist_urls[current_index]
            print(f"[LiveStream] Processing item {current_index + 1}/{len(playlist_urls)}: {current_url}")

            """# 3. YouTube URLから動画ストリーム取得
            try:
                video_url, is_live = await get_audio_url(current_url)
                print(f"[LiveStream] Video URL obtained: {video_url[:100]}...")
            """
            # 3. YouTube URLから動画ストリーム取得 bilibili 拡張
            try:
                # 新しい関数(get_audio_url_3)を呼び出し、リストを受け取る
                urls, is_live = await get_audio_url_3(current_url, is_audio_only=False)

                # リストの 0 番目（映像が含まれる URL）を video_url として採用
                # ブラウザの音(WebSocket)を主役にするため、Bilibili側の音声URLは無視する
                video_url = urls[0]

                print(f"[LiveStream] Video URL obtained: {video_url[:100]}...")
            except Exception as e:
                print(f"[LiveStream] Failed to get video URL: {e}")
                current_index += 1
                continue
            # 4. FFmpegコマンド構築（UIパラメーター適用）
            maxrate = int(video_bitrate * 1.5)
            bufsize_video = max(video_bitrate * 2, video_bitrate * 3)

            extra_headers = get_platform_headers(current_url)

            cmd = [
                "ffmpeg",
                "-loglevel", "info",  # デバッグ用

                "-probesize", "10M",             # 解析するデータ量を10MBに増やす
                "-analyzeduration", "5M"]       # 解析時間を5秒(5,000,000μs)に設定

            if extra_headers:
                cmd += ["-headers", extra_headers]

            cmd += [
                # === Video入力（再接続オプション追加）===
                "-reconnect", "1",               # 接続が切れたら再試行
                "-reconnect_at_eof", "1",        # ファイル末尾でも再試行
                "-reconnect_streamed", "1",      # ストリーム形式でも再試行
                "-reconnect_delay_max", "5",     # 最大5秒待機してリトライ
                "-re",
                "-i", video_url,

                # === Audio入力（WebSocket）===
                "-thread_queue_size", str(buffer_size),
                "-use_wallclock_as_timestamps", "1",
                "-fflags", "+genpts+igndts",
                "-avoid_negative_ts", "make_zero",
                "-max_delay", "5000000",
                "-f", "webm",
                "-i", "pipe:0",

                # === マッピング ===
                "-map", "0:v",
                "-map", "1:a",

                # === Video エンコード（UI設定適用）===
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-b:v", f"{video_bitrate}k",
                "-maxrate", f"{maxrate}k",      # 1.5倍に修正
                "-bufsize", f"{bufsize_video}k", # 3倍に修正
                "-pix_fmt", "yuv420p",
                "-g", str(gop_size),

                # === Audio エンコード（UI設定適用）===
                "-c:a", "aac",
                "-b:a", f"{audio_bitrate}k",
                "-ar", "48000",
                "-ac", "2",
            ]

            # async パラメーター適用（フィルターと統一）
            if async_param > 0:
                cmd.extend(["-async", str(async_param)])
                if denoise_enabled:
                    af_filter = f"aresample=async={async_param}:min_hard_comp=0.100000:first_pts=0,apad=whole_dur=0.1,afftdn=nf=-25"
                else:
                    af_filter = f"aresample=async={async_param}:min_hard_comp=0.100000:first_pts=0,apad=whole_dur=0.1"
            else:
                # async=0の場合はaresampleを使わない（スムーズな音声）
                if denoise_enabled:
                    af_filter = "apad=whole_dur=0.1,afftdn=nf=-25"
                else:
                    af_filter = "anull"  # 何もしない（パススルー）

            # 残りのパラメーター
            cmd.extend([
                "-vsync", "cfr",
                "-af", af_filter,
                "-flush_packets", "0",
                "-f", "flv",
                rtmp_url
            ])

            print(f"[FFmpeg] Video: {video_bitrate}k (max:{maxrate}k, buf:{bufsize_video}k), Audio: {audio_bitrate}k, GOP: {gop_size}, Buffer: {buffer_size}, Async: {async_param}")
            print(f"[FFmpeg] Command: {' '.join(cmd[:20])}...")  # 最初の20要素を表示

            print(f"[LiveStream] Starting FFmpeg for item {current_index + 1}")

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            livestream_process = process

            # 5. Audioデータ転送タスク（バッファリング最適化版）
            audio_task_cancelled = False
            buffer_threshold = 4  # 4チャンク（200ms分）溜めてから送る
            #buffer_threshold = 3  # 3チャンク（150ms分）溜めてから送る

            async def forward_audio():
                nonlocal audio_task_cancelled
                try:
                    chunk_count = 0
                    buffer = []

                    while True:
                        data = await websocket.receive_bytes()
                        chunk_count += 1
                        buffer.append(data)

                        # バッファが溜まったら一気に書き込み
                        if len(buffer) >= buffer_threshold:
                            if process.stdin and not process.stdin.is_closing():
                                combined = b''.join(buffer)  # 結合して一度に書き込み
                                process.stdin.write(combined)
                                await process.stdin.drain()
                                buffer.clear()

                                if chunk_count % 100 == 0:
                                    print(f"[Audio] Forwarded {chunk_count} chunks")
                            else:
                                break

                except asyncio.CancelledError:
                    audio_task_cancelled = True
                    print("[Audio] Task cancelled (video finished)")
                except Exception as e:
                    print(f"[Audio] Error: {e}")
                finally:
                    # 残りのバッファを書き込み
                    if buffer and process.stdin and not process.stdin.is_closing():
                        try:
                            combined = b''.join(buffer)
                            process.stdin.write(combined)
                            await process.stdin.drain()
                        except:
                            pass

                    if process.stdin and not process.stdin.is_closing():
                        try:
                            process.stdin.close()
                            await process.stdin.wait_closed()
                        except:
                            pass

            # 6. FFmpegログ出力タスク
            async def log_stderr():
                try:
                    async for line in process.stderr:
                        msg = line.decode().strip()
                        if "frame=" in msg or "time=" in msg:
                            # 進捗ログ（ビットレート情報を抽出）
                            if "bitrate=" in msg:
                                # bitrate情報を抽出して表示
                                import re
                                bitrate_match = re.search(r'bitrate=\s*([\d.]+)(\w+)/s', msg)
                                if bitrate_match:
                                    bitrate_val = bitrate_match.group(1)
                                    bitrate_unit = bitrate_match.group(2)
                                    print(f"[FFmpeg] Current bitrate: {bitrate_val}{bitrate_unit}/s (target: {video_bitrate}k)")
                            else:
                                print(f"[FFmpeg] {msg}")
                        elif msg:
                            print(f"[FFmpeg] {msg}")
                except:
                    pass

            # 7. 両タスクを並行実行
            audio_task = asyncio.create_task(forward_audio())
            stderr_task = asyncio.create_task(log_stderr())

            # 8. FFmpegの終了を待つ（動画終了 or エラー）
            returncode = await process.wait()

            print(f"[LiveStream] FFmpeg finished with code {returncode}")

            # タスククリーンアップ
            if not audio_task.done():
                audio_task.cancel()
                try:
                    await audio_task
                except asyncio.CancelledError:
                    pass

            if not stderr_task.done():
                stderr_task.cancel()
                try:
                    await stderr_task
                except asyncio.CancelledError:
                    pass

            # 9. 次の動画へ
            if repeat_mode == "item":
                print("[LiveStream] Item repeat: replaying same video")
                # current_indexはそのまま
            else:
                current_index += 1

            # Browser側に進捗通知
            try:
                if websocket.client_state.name == "CONNECTED":
                    await websocket.send_json({
                        "type": "progress",
                        "current_index": current_index,
                        "total": len(playlist_urls)
                    })
            except Exception as e:
                print(f"[LiveStream] Failed to send progress: {e}")
                break

        # プレイリスト完了通知
        print("[LiveStream] Playlist completed")
        try:
            if websocket.client_state.name == "CONNECTED":
                await websocket.send_json({"type": "completed"})
        except Exception as e:
            print(f"[LiveStream] Failed to send completion message: {e}")

    except asyncio.TimeoutError:
        print("[LiveStream] Timeout waiting for playlist data")
        try:
            if websocket.client_state.name == "CONNECTED":
                await websocket.send_json({"error": "Timeout"})
        except:
            pass
    except Exception as e:
        print(f"[LiveStream] Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if livestream_process and livestream_process.returncode is None:
            livestream_process.terminate()
            await livestream_process.wait()

        # WebSocketが既にcloseされていないか確認
        try:
            if websocket.client_state.name == "CONNECTED":
                await websocket.close()
        except:
            pass
        print("[LiveStream] WebSocket closed")

if __name__ == "__main__":
    import uvicorn
    # WebSocket over HTTPS (wss://) support
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        ws_ping_interval=60,  # WebSocket ping間隔（60秒に延長）
        ws_ping_timeout=60,   # タイムアウト設定（60秒に延長）
        timeout_keep_alive=75 # 接続維持
    )
