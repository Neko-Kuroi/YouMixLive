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

        # 2. Liveでなければyt-dlpで通常取得
        proc_yt = await asyncio.create_subprocess_exec(
            "yt-dlp", "-f", "best[ext=mp4]/best", "-g", youtube_url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout_yt, _ = await asyncio.wait_for(proc_yt.communicate(), timeout=YTDLP_TIMEOUT)
            if proc_yt.returncode != 0:
                raise HTTPException(status_code=400, detail="Audio extraction failed")
            return stdout_yt.decode().strip().splitlines()[0], False
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Extraction timeout")

# --- Endpoints ---

@app.get("/audio")
async def audio_proxy(
    request: Request,
    url: str = Query(...),
    start: float = Query(0.0, ge=0.0),
    format: str = Query("webm", regex="^(webm|wav|mp4)$"),
    repeat: bool = False,
):
    validate_youtube_url(url)
    audio_url, is_live = await get_audio_url(url)
    
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error"]

    if repeat and not is_live:
        cmd += ["-stream_loop", "-1"]

    if not is_live:
        cmd += [
            "-reconnect", "1", "-reconnect_at_eof", "1",
            "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        ]

    cmd += ["-i", audio_url]
    if not is_live:
        cmd += ["-ss", str(start)]

    cmd += ["-fflags", "+nobuffer", "-ac", "2", "-ar", "48000"]
    cmd += [*FORMAT_ARGS[format], "pipe:1"]

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
        video_track = data.get("video_track", 1)  # 1 or 2
        
        if not rtmp_url:
            raise HTTPException(status_code=400, detail="RTMP URL required")
        
        # WebSocketでブラウザからのストリームを受け取る準備
        # ここでは簡易的にファイアウォール経由での実装を想定
        # 実際にはWebSocket経由でブラウザのMediaStreamを受信する必要がある
        
        livestream_status = {
            "active": True,
            "rtmp_url": rtmp_url,
            "video_track": video_track
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
    
    try:
        # 1. Browser側からプレイリスト情報を受信
        print("[LiveStream] Waiting for playlist data...")
        init_msg = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)
        
        playlist_urls = init_msg.get("playlist", [])
        current_index = init_msg.get("current_index", 0)
        repeat_mode = init_msg.get("repeat_mode", "none")
        
        print(f"[LiveStream] Received playlist: {len(playlist_urls)} items, starting at index {current_index}")
        print(f"[LiveStream] Repeat mode: {repeat_mode}")
        
        if not playlist_urls:
            await websocket.send_json({"error": "Empty playlist"})
            await websocket.close()
            return
        
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
            
            # 3. YouTube URLから動画ストリーム取得
            try:
                video_url, is_live = await get_audio_url(current_url)
                print(f"[LiveStream] Video URL obtained: {video_url[:100]}...")
            except Exception as e:
                print(f"[LiveStream] Failed to get video URL: {e}")
                current_index += 1
                continue
            
            # 4. FFmpegコマンド構築（ノイズ対策版）
            cmd = [
                "ffmpeg",
                
                # === Video入力 ===
                "-re",  # リアルタイム読み込み
                "-i", video_url,
                
                # === Audio入力（WebSocket）===
                "-thread_queue_size", "2048",     # バッファ大幅増量（512→2048）
                "-use_wallclock_as_timestamps", "1",
                "-fflags", "+genpts",             # タイムスタンプ生成を強制
                "-f", "webm",
                "-i", "pipe:0",
                
                # === マッピング ===
                "-map", "0:v",
                "-map", "1:a",
                
                # === Video エンコード ===
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-b:v", "2500k",
                "-maxrate", "2500k",
                "-bufsize", "5000k",
                "-pix_fmt", "yuv420p",
                "-g", "60",
                
                # === Audio エンコード（音切れ対策強化） ===
                "-c:a", "aac",
                "-b:a", "128k",
                "-ar", "48000",
                "-ac", "2",
                "-async", "1",
                "-vsync", "cfr",                  # 一定フレームレート強制
                "-af", "aresample=async=1:min_hard_comp=0.100000:first_pts=0,apad",  # パディング追加
                
                # === 出力 ===
                "-f", "flv",
                rtmp_url
            ]
            
            print(f"[LiveStream] Starting FFmpeg for item {current_index + 1}")
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            livestream_process = process
            
            # 5. Audioデータ転送タスク（バッファリング強化版）
            audio_task_cancelled = False
            buffer_threshold = 5  # 5チャンク溜まってから一気に送る
            
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
                        if len(buffer) >= buffer_threshold or chunk_count % 100 == 0:
                            if process.stdin and not process.stdin.is_closing():
                                for buffered_data in buffer:
                                    process.stdin.write(buffered_data)
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
                            for buffered_data in buffer:
                                process.stdin.write(buffered_data)
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
                            # 進捗ログ（定期的に出力）
                            if "time=" in msg:
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
                await websocket.send_json({
                    "type": "progress",
                    "current_index": current_index,
                    "total": len(playlist_urls)
                })
            except:
                break
        
        # プレイリスト完了通知
        print("[LiveStream] Playlist completed")
        await websocket.send_json({"type": "completed"})
        
    except asyncio.TimeoutError:
        print("[LiveStream] Timeout waiting for playlist data")
        await websocket.send_json({"error": "Timeout"})
    except Exception as e:
        print(f"[LiveStream] Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if livestream_process and livestream_process.returncode is None:
            livestream_process.terminate()
            await livestream_process.wait()
        await websocket.close()
        print("[LiveStream] WebSocket closed")

if __name__ == "__main__":
    import uvicorn
    # WebSocket over HTTPS (wss://) support
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=8000,
        ws_ping_interval=20,  # WebSocket ping間隔
        ws_ping_timeout=20,   # タイムアウト設定
        timeout_keep_alive=75 # 接続維持
    )