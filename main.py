#!/usr/bin/env python3
"""24 小时无人直播推流系统入口。

用法:
    python3 main.py -c config.yaml

支持协议: rtmp / srt / webrtc(经 MediaMTX 中转)
依赖: ffmpeg、ffprobe、PyYAML
"""
import argparse
import logging
import logging.handlers
import shutil
import signal
import sys
import threading

try:
    import yaml
except ImportError:
    print("缺少依赖 PyYAML，请执行: pip3 install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

from engine import StreamWorker
from playlist import Playlist, PlaylistEmptyError

log = logging.getLogger("main")


def setup_logging(cfg):
    log_cfg = cfg.get("logging") or {}
    level = getattr(logging, str(log_cfg.get("level", "INFO")).upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    log_file = log_cfg.get("file")
    if log_file:
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=log_cfg.get("max_bytes", 50 * 1024 * 1024),
            backupCount=log_cfg.get("backup_count", 5), encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    targets = [t for t in (cfg.get("targets") or []) if t.get("enabled", True)]
    if not targets:
        raise ValueError("config 中没有任何 enabled 的推流目标(targets)")
    for t in targets:
        if not t.get("url"):
            raise ValueError(f"推流目标 {t.get('name', '?')} 缺少 url")
        protocol = str(t.get("protocol", "")).lower()
        if protocol not in ("rtmp", "srt", "webrtc"):
            raise ValueError(f"推流目标 {t.get('name', '?')} 协议无效: {protocol}（支持 rtmp/srt/webrtc）")
        t.setdefault("name", f"{protocol}-{targets.index(t)}")
        t["protocol"] = protocol
    cfg["targets"] = targets
    return cfg


def check_dependencies():
    missing = [tool for tool in ("ffmpeg", "ffprobe") if not shutil.which(tool)]
    if missing:
        raise EnvironmentError(
            f"缺少可执行程序: {', '.join(missing)}。请安装 ffmpeg（含 ffprobe）后重试。")


def main():
    parser = argparse.ArgumentParser(description="24 小时无人直播推流系统")
    parser.add_argument("-c", "--config", default="config.yaml", help="配置文件路径")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except Exception as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        sys.exit(1)

    setup_logging(cfg)

    try:
        check_dependencies()
        playlist = Playlist(cfg.get("playlist") or {})
    except (EnvironmentError, PlaylistEmptyError) as exc:
        log.error("%s", exc)
        sys.exit(1)

    stop_event = threading.Event()

    def on_signal(signum, _frame):
        log.info("收到信号 %s，准备退出...", signal.Signals(signum).name)
        stop_event.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    workers = [StreamWorker(t, playlist, cfg, stop_event) for t in cfg["targets"]]
    for w in workers:
        w.start()
    log.info("已启动 %d 个推流目标，no_transcode=%s",
             len(workers), cfg.get("no_transcode", False))

    try:
        while not stop_event.is_set():
            stop_event.wait(1)
    finally:
        stop_event.set()
        for w in workers:
            w.stop()
        for w in workers:
            w.join(timeout=10)
        log.info("全部推流已停止，进程退出")


if __name__ == "__main__":
    main()
