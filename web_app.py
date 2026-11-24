"""仅监听本机的网页界面；无需安装第三方 Python 包。"""
import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from split_media import (
    DEFAULT_AUDIO_FORMAT, DEFAULT_OUTPUT, parse_time_ranges, positive_number, split, validate_audio_format,
)

ROOT = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
STATE = {"status": "idle", "completed": 0, "total": 0, "folder": "", "files": [], "error": ""}


def choose_path(kind):
    # 独立进程运行 Tk，避免 HTTP 工作线程与 Tk 主线程冲突。
    code = '''import tkinter as tk, json, sys
from tkinter import filedialog
root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
if sys.argv[1] == "folder":
    path = filedialog.askdirectory(title="选择保存目录")
else:
    audio = "*.mp3 *.wav *.m4a *.flac *.aac *.ogg *.opus *.wma *.aif *.aiff *.mka"
    video = "*.mp4 *.mov *.mkv *.avi *.webm *.m4v"
    path = filedialog.askopenfilename(title="选择音频或视频", filetypes=[
        ("媒体文件", audio + " " + video), ("音频文件", audio), ("视频文件", video), ("所有文件", "*.*")
    ])
print(json.dumps(path)); root.destroy()
'''
    result = subprocess.run([sys.executable, "-c", code, kind], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(result.stdout)


def work(source, output, mode, duration, minimum, maximum, audio_format, time_ranges=None):
    def progress(done, total, folder, item):
        with LOCK:
            STATE.update(completed=done, total=total, folder=str(folder))
            if item:
                STATE["files"].append(item)
    try:
        split(source, output, mode, duration, minimum, maximum, progress=progress,
              audio_format=audio_format, time_ranges=time_ranges)
        with LOCK:
            STATE["status"] = "complete"
    except Exception as error:
        with LOCK:
            STATE.update(status="error", error=str(error))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, data, status=200):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def authorized(self):
        return secrets.compare_digest(self.headers.get("X-App-Token", ""), TOKEN)

    def do_GET(self):
        if self.path == "/":
            content = (ROOT / "web" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
        elif self.path == "/api/status" and self.authorized():
            with LOCK:
                self.respond({**STATE, "missing": [x for x in ("ffmpeg", "ffprobe") if not shutil.which(x)]})
        else:
            self.respond({"error": "请求不可用，请从启动器打开页面"}, 403)

    def do_POST(self):
        if not self.authorized():
            self.respond({"error": "页面会话已失效，请重新启动"}, 403)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 16384:
                raise ValueError("请求大小无效")
            data = json.loads(self.rfile.read(size))
            if self.path == "/api/choose":
                self.respond({"path": choose_path(data.get("kind"))})
            elif self.path == "/api/preview-ranges":
                self.respond({"ranges": parse_time_ranges(data.get("ranges"))})
            elif self.path == "/api/reset":
                with LOCK:
                    if STATE["status"] == "running":
                        raise ValueError("正在切分，请等待任务结束后再重置")
                    STATE.update(status="idle", completed=0, total=0, folder="", files=[], error="")
                self.respond({"ok": True})
            elif self.path == "/api/open":
                with LOCK:
                    folder = STATE["folder"]
                if not folder or not Path(folder).is_dir():
                    raise ValueError("尚未生成输出文件夹")
                os.startfile(folder)
                self.respond({"ok": True})
            elif self.path == "/api/start":
                if any(not shutil.which(x) for x in ("ffmpeg", "ffprobe")):
                    raise ValueError("请先安装 FFmpeg 并添加到 PATH，然后重新启动本工具。安装说明见 README.md。")
                source = Path(str(data.get("source", "")).strip().strip('"')).expanduser().resolve()
                if not source.is_file():
                    raise ValueError("请选择存在的视频或音频文件")
                mode = data.get("mode")
                if mode not in ("auto", "video", "audio"):
                    raise ValueError("输出模式无效")
                audio_format = validate_audio_format(data.get("audio_format", DEFAULT_AUDIO_FORMAT))
                duration = minimum = maximum = None
                time_ranges = None
                if data.get("timing") == "fixed":
                    duration = positive_number(data.get("duration"))
                elif data.get("timing") == "range":
                    minimum = positive_number(data.get("minimum"))
                    maximum = positive_number(data.get("maximum"))
                    if minimum > maximum:
                        raise ValueError("最短时长不能大于最长时长")
                elif data.get("timing") == "custom":
                    time_ranges = parse_time_ranges(data.get("ranges"))
                else:
                    raise ValueError("切分方式无效")
                output = str(data.get("output", "")).strip().strip('"')
                output = Path(output).expanduser().resolve() if output else DEFAULT_OUTPUT
                with LOCK:
                    if STATE["status"] == "running":
                        raise ValueError("已有切分任务正在运行")
                    STATE.update(status="running", completed=0, total=0, folder="", files=[], error="")
                args = (source, output, mode, duration, minimum, maximum, audio_format, time_ranges)
                threading.Thread(target=work, args=args, daemon=True).start()
                self.respond({"ok": True})
            else:
                self.respond({"error": "接口不存在"}, 404)
        except Exception as error:
            self.respond({"error": str(error)}, 400)


def main():
    parser = argparse.ArgumentParser(description="启动本地音视频切分页面")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{server.server_port}/#{TOKEN}"
    print(f"本地页面：{url}\n请保持此窗口打开。按 Ctrl+C 退出。", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
