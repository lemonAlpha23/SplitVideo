"""用 FFmpeg 切分音频、视频，或提取视频中的音频（仅使用 Python 标准库）。"""

import argparse
import html
import json
import math
import random
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

DEFAULT_OUTPUT = Path(__file__).resolve().parent / "data"
DEFAULT_AUDIO_FORMAT = "source"
AUDIO_FORMATS = {
    "mp3": ("-c:a", "libmp3lame", "-q:a", "2"),
    "wav": ("-c:a", "pcm_s24le"),
    "flac": ("-c:a", "flac"),
    "m4a": ("-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"),
    "ogg": ("-c:a", "libvorbis", "-q:a", "5"),
}
AUDIO_FORMAT_CHOICES = (DEFAULT_AUDIO_FORMAT, *AUDIO_FORMATS)

# FFprobe 的一个容器名称有时对应多个扩展名。
SOURCE_EXTENSIONS = {
    "mov": ("m4a", "m4b", "mp4", "mov", "3gp", "3g2"),
    "matroska": ("mka", "mkv", "webm"),
    "ogg": ("ogg", "oga", "opus", "spx"),
    "asf": ("wma", "asf"),
    "aiff": ("aiff", "aif", "aifc"),
    "wav": ("wav", "wave"),
    "mp3": ("mp3", "mp2"),
}


def validate_audio_format(value):
    if value not in AUDIO_FORMAT_CHOICES:
        raise ValueError(f"音频输出格式请选择 {', '.join(AUDIO_FORMAT_CHOICES)}")
    return value


def positive_number(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("时长必须是大于 0 的有限数字（秒）")
    return number


SECONDS = r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)"
RANGE_ROW = re.compile(rf"({SECONDS})\s*[-~–—−至]\s*({SECONDS})(?:\s+({SECONDS})\s*秒?)?")
RANGE_HEADER = re.compile(r"截取位置\s*(?:\(\s*单位\s*:\s*秒\s*\))?\s*(?:时长)?")


def parse_time_ranges(text):
    """每行的起止秒数指向原素材；可附时长列进行核对。"""
    if not isinstance(text, str):
        raise ValueError("请填写自定义时间段")
    rows = []
    for line_number, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = unicodedata.normalize("NFKC", html.unescape(raw)).strip().removesuffix("\\").strip()
        if not line or (not rows and RANGE_HEADER.fullmatch(line)):
            continue
        match = RANGE_ROW.fullmatch(line)
        if not match:
            raise ValueError(f"第 {line_number} 行格式有误，请填写「开始秒数-结束秒数」，例如 22-40")
        start, end = map(float, match.group(1, 2))
        if not (math.isfinite(end) and 0 <= start < end):
            raise ValueError(f"第 {line_number} 行的结束时间必须大于开始时间，且两者都须是有限秒数")
        duration = match.group(3)
        if duration and not math.isclose(float(duration), end - start, rel_tol=0, abs_tol=1e-9):
            raise ValueError(f"第 {line_number} 行标注了 {duration} 秒，按起止时间计算为 {end - start:g} 秒")
        rows.append((start, end))
    if not rows:
        raise ValueError("请至少填写一个时间段，例如 0-22")
    return rows


def plan_segments(total, duration=None, minimum=None, maximum=None):
    """返回每段秒数；范围模式分配时预留剩余片段的可行区间。"""
    # 小数秒整除时，浮点商可能与整数相差一个舍入单位。
    if duration is not None:
        count = math.ceil(math.nextafter(total / duration, 0))
        return [duration] * (count - 1) + [total - duration * (count - 1)]
    if minimum > maximum:
        raise ValueError("最短时长不能大于最长时长")
    least = math.ceil(math.nextafter(total / maximum, 0))
    most = math.floor(math.nextafter(total / minimum, math.inf))
    if least > most:
        raise ValueError(
            f"总时长 {total:.3f} 秒无法完整分成 {minimum:g}～{maximum:g} 秒的片段。"
            "请降低最短时长、提高最长时长，或改用固定时长模式（允许末段较短）。"
        )
    count = random.randint(least, most)
    remaining = total
    segments = []
    for left in range(count - 1, 0, -1):
        lower = max(minimum, remaining - left * maximum)
        upper = min(maximum, remaining - left * minimum)
        length = random.uniform(lower, upper)
        segments.append(length)
        remaining -= length
    segments.append(remaining)
    return segments


def clip_schedule(offset, total, duration, minimum, maximum, time_ranges):
    """返回按导出顺序排列的（原素材起点，时长）。"""
    clips = []
    if time_ranges is not None:
        if any(value is not None for value in (duration, minimum, maximum)):
            raise ValueError("自定义时间段不能与固定时长或随机范围同时使用")
        if not time_ranges:
            raise ValueError("请至少填写一个时间段")
        limit = offset + total
        for index, (start, end) in enumerate(time_ranges, 1):
            # FFprobe 的起止时间精确到微秒。
            if not (0 <= start < end and offset - 1e-6 <= start and end <= limit + 1e-6):
                raise ValueError(f"第 {index} 段 {start:g}–{end:g} 秒无效，素材可截取范围为 {offset:g}–{limit:g} 秒")
            clips.append((start, end - start))
    else:
        start = offset
        for length in plan_segments(total, duration, minimum, maximum):
            clips.append((start, length))
            start += length
    return clips


def probe(source):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    return json.loads(result.stdout)


def select_track(streams, mode):
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    # 专辑封面在 FFprobe 中也是视频流，不能用它判断文件是否为视频。
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    if mode == "auto":
        if video is None and audio is None:
            raise ValueError("输入文件没有可用的音频或视频轨道")
        mode = "video" if video is not None else "audio"
    if mode not in ("audio", "video"):
        raise ValueError("输出模式无效")
    track = audio if mode == "audio" else video
    if track is None:
        raise ValueError(f"输入文件没有可用的{'音频' if mode == 'audio' else '视频'}轨道")
    return track


def audio_output(source, info, track, requested):
    if requested != "source":
        return requested, AUDIO_FORMATS[requested]

    codec = track["codec_name"]
    if select_track(info["streams"], "auto")["codec_type"] == "audio":
        container = info["format"]["format_name"].split(",")[0]
        extensions = SOURCE_EXTENSIONS.get(container, (container,))
        suffix = source.suffix.lower().lstrip(".")
        extension = suffix if suffix in extensions else extensions[0]
    elif codec.startswith("pcm_"):
        extension = "aiff" if codec.endswith("be") else "wav"
    else:
        # 从视频提取时，为原音频编码选择可单独播放的音频容器。
        extension = {
            "aac": "m4a", "alac": "m4a", "mp3": "mp3", "mp2": "mp2",
            "flac": "flac", "opus": "opus", "vorbis": "ogg", "ac3": "ac3",
            "eac3": "eac3", "wmav1": "wma", "wmav2": "wma",
        }.get(codec, "mka")
    encoder_args = {
        "mp3": AUDIO_FORMATS["mp3"], "vorbis": AUDIO_FORMATS["ogg"],
        "aac": ("-c:a", "aac", "-b:a", "192k"), "opus": ("-c:a", "libopus"),
    }.get(codec, ("-c:a", codec))
    if extension == "wave":
        encoder_args += ("-f", "wav")
    return extension, encoder_args


def audio_duration(source, track, container):
    # MP3、Opus 可能把编码填充计入时长，裸 AAC 的时长则是按码率估算的。
    # 对这几类文件和缺少轨道时长的容器，用解码结束时间避免生成空的末段。
    if ("duration" in track and track["codec_name"] not in ("mp3", "opus")
            and container["format_name"] != "aac"):
        return positive_number(track["duration"])
    result = subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-nostats", "-i", str(source),
        "-map", f"0:{track['index']}", "-af", "asetpts=PTS-STARTPTS",
        "-progress", "pipe:1", "-f", "null", "-",
    ], stdout=subprocess.PIPE, text=True, check=True)
    for line in reversed(result.stdout.splitlines()):
        if line.startswith("out_time_us="):
            return positive_number(line.split("=", 1)[1]) / 1_000_000
    raise ValueError("无法读取音轨时长")


def split(source, output_root, mode, duration, minimum, maximum, progress=None,
          *, audio_format=DEFAULT_AUDIO_FORMAT, time_ranges=None):
    validate_audio_format(audio_format)
    info = probe(source)
    container = info["format"]
    track = select_track(info["streams"], mode)
    mode = track["codec_type"]
    offset = 0.0
    if mode == "audio":
        extension, encoder_args = audio_output(source, info, track, audio_format)
        offset = max(0.0, float(track.get("start_time", 0)) - float(container.get("start_time", 0)))
        total = audio_duration(source, track, container)
    else:
        extension = "mp4"
        total = positive_number(container["duration"])
    segments = clip_schedule(offset, total, duration, minimum, maximum, time_ranges)
    # 每次运行新建文件夹，不覆盖之前的结果。
    output_root.mkdir(parents=True, exist_ok=True)
    folder = output_root / f"{source.stem}_{mode}_{datetime.now():%Y%m%d_%H%M%S_%f}"
    folder.mkdir()
    manifest = {
        "source": str(source), "mode": mode,
        "source_duration_seconds": float(container.get("duration", offset + total)),
        "status": "running", "segments": [],
    }
    if mode == "audio":
        manifest["audio_format"] = extension
    manifest_path = folder / "segments.json"

    def save_manifest():
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    save_manifest()
    if progress:
        progress(0, len(segments), folder, None)
    print(f"总时长：{total:.3f} 秒，共 {len(segments)} 段\n保存位置：{folder}")
    try:
        for index, (start, length) in enumerate(segments, 1):
            target = folder / f"{index:04d}.{extension}"
            temporary = folder / f"{index:04d}.partial.{extension}"
            command = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
                "-ss", f"{start:.9f}", "-i", str(source), "-t", f"{length:.9f}",
            ]
            if mode == "audio":
                command += ["-map", f"0:{track['index']}", "-vn", *encoder_args,
                            "-abort_on", "empty_output"]
            else:
                command += [
                    "-map", f"0:{track['index']}", "-map", "0:a:0?", "-c:v", "libx264",
                    "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
                    "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                ]
            command.append(str(temporary))
            print(f"[{index}/{len(segments)}] 起点 {start:.3f} 秒，时长 {length:.3f} 秒", flush=True)
            result = subprocess.run(command, stderr=subprocess.PIPE, encoding="utf-8", errors="replace")
            if result.returncode:
                message = f"FFmpeg 导出 {target.name} 失败。"
                if mode == "audio" and audio_format == "source":
                    message += "如原格式缺少可用编码器或封装器，请手动选择其他音频输出格式。"
                raise ValueError(f"{message}\n{result.stderr.strip()}")
            temporary.rename(target)
            manifest["segments"].append({
                "file": target.name, "start_seconds": start, "end_seconds": start + length,
                "duration_seconds": length,
            })
            save_manifest()
            if progress:
                progress(index, len(segments), folder, manifest["segments"][-1])
    except BaseException:
        manifest["status"] = "incomplete"
        save_manifest()
        raise
    manifest["status"] = "complete"
    save_manifest()
    print(f"完成：{folder}")
    return folder


def main():
    parser = argparse.ArgumentParser(description="切分音频或视频，时长单位为秒。不传参数进入交互模式。")
    parser.add_argument("input", nargs="?", help="输入视频或音频文件路径")
    parser.add_argument("--mode", choices=["auto", "video", "audio"], default="auto",
                        help="默认自动识别音频或视频。指定 audio 可提取视频音轨")
    parser.add_argument("--audio-format", choices=AUDIO_FORMAT_CHOICES, type=str.lower,
                        default=DEFAULT_AUDIO_FORMAT, help="音频输出格式，默认 source 跟随原素材")
    parser.add_argument("--duration", type=positive_number, help="固定每段时长，最后一段可较短")
    parser.add_argument("--min", dest="minimum", type=positive_number, help="每段最短时长")
    parser.add_argument("--max", dest="maximum", type=positive_number, help="每段最长时长")
    parser.add_argument("--segments-file", type=Path, help="自定义时间表（UTF-8 文本，每行 开始秒数-结束秒数）")
    parser.add_argument("--output", type=Path, help="输出根目录，默认项目根目录下的 data")
    args = parser.parse_args()
    time_ranges = None
    if args.input is None:
        args.input = input("请输入视频或音频文件路径：").strip().strip('"')
        selection = input("输出类型：1 视频 MP4，2 音频，回车自动识别：").strip()
        if selection not in ("", "1", "2"):
            raise ValueError("输出类型请选择 1、2 或按回车")
        if selection:
            args.mode = "audio" if selection == "2" else "video"
        if args.mode != "video":
            formats = " / ".join(AUDIO_FORMAT_CHOICES)
            choice = input(f"音频片段格式（{formats}，source 跟随原素材）[{args.audio_format}]：").strip().lower()
            args.audio_format = validate_audio_format(choice or args.audio_format)
        choice = input("切分方式：1 固定时长，2 时长范围，3 自定义时间段 [2]：").strip()
        if choice == "1":
            args.duration = positive_number(input("每段时长（秒）："))
        elif choice in ("", "2"):
            args.minimum = positive_number(input("最短时长（秒）："))
            args.maximum = positive_number(input("最长时长（秒）："))
        elif choice == "3":
            if args.segments_file is None:
                print("请粘贴时间表，每行一个时间段（例如 22-40），输入空行结束：")
                lines = []
                for line in sys.stdin:
                    if not line.strip():
                        break
                    lines.append(line)
                time_ranges = parse_time_ranges("".join(lines))
        else:
            raise ValueError("切分方式请选择 1、2 或 3")
    if args.segments_file is not None or time_ranges is not None:
        if any(value is not None for value in (args.duration, args.minimum, args.maximum)):
            parser.error("自定义时间段不能与 --duration、--min 或 --max 同时使用")
        if args.segments_file is not None:
            time_ranges = parse_time_ranges(args.segments_file.expanduser().read_text(encoding="utf-8-sig"))
    elif args.duration is not None:
        if args.minimum is not None or args.maximum is not None:
            parser.error("--duration 不能与 --min/--max 同时使用")
    elif args.minimum is None or args.maximum is None:
        parser.error("请指定 --segments-file、--duration，或同时指定 --min 和 --max")
    source = Path(args.input).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"输入文件不存在：{source}")
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        raise ValueError(f"找不到 {', '.join(missing)}。请安装 FFmpeg 并添加到 PATH，详见 README.md。")
    split(source, args.output or DEFAULT_OUTPUT, args.mode,
          args.duration, args.minimum, args.maximum, audio_format=args.audio_format, time_ranges=time_ranges)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中止，已完成片段保留在输出目录。", file=sys.stderr)
        sys.exit(130)
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        print(f"错误：{error}", file=sys.stderr)
        sys.exit(1)
