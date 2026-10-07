"""媒体文件探测：判断是否可以免转码（stream copy）推流。"""
import json
import logging
import subprocess

log = logging.getLogger("media")

# 各输出协议在 copy 模式下可接受的编码
COPYABLE_VIDEO_CODECS = {"h264"}
COPYABLE_AUDIO_CODECS = {"aac"}

PROBE_TIMEOUT = 30


def probe(path):
    """用 ffprobe 探测文件，返回 {"video": stream|None, "audio": stream|None}，失败返回 None。"""
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=PROBE_TIMEOUT)
        data = json.loads(result.stdout or b"{}")
    except Exception as exc:
        log.warning("ffprobe 探测失败 %s: %s", path, exc)
        return None

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return {"video": video, "audio": audio}


def can_copy(info):
    """返回 (video_copy, audio_copy)：在 no_transcode 开启时，视频/音频是否可直接 copy。"""
    if not info:
        return False, False
    video_copy = bool(
        info["video"]
        and info["video"].get("codec_name") in COPYABLE_VIDEO_CODECS
    )
    audio_copy = bool(
        info["audio"]
        and info["audio"].get("codec_name") in COPYABLE_AUDIO_CODECS
    )
    return video_copy, audio_copy
