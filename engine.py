"""ffmpeg 推流引擎：每个推流目标一个工作线程，负责命令构建、进程管理与断流重试。"""
import logging
import shlex
import subprocess
import threading
import time

from media import can_copy, probe

log = logging.getLogger("engine")

# 协议 -> ffmpeg 输出封装格式
OUTPUT_FORMAT = {
    "rtmp": "flv",
    "srt": "mpegts",
    # WebRTC 通过 MediaMTX 中转：ffmpeg 以 RTSP 推给 MediaMTX，
    # MediaMTX 对外提供 WebRTC(WHEP) 播放地址 http://<host>:8889/<path>
    "webrtc": "rtsp",
}

TERMINATE_GRACE = 5


def build_command(target, path, info, no_transcode):
    """根据目标配置与探测结果构建 ffmpeg 命令行。"""
    protocol = target["protocol"].lower()
    if protocol not in OUTPUT_FORMAT:
        raise ValueError(f"不支持的协议: {protocol}")

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin"]
    cmd += ["-re", "-fflags", "+genpts", "-i", str(path)]

    video_copy, audio_copy = can_copy(info) if no_transcode else (False, False)

    # ---- 视频 ----
    if video_copy:
        cmd += ["-c:v", "copy"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
        bitrate = target.get("video_bitrate")
        if bitrate:
            cmd += ["-b:v", str(bitrate), "-maxrate", str(bitrate), "-bufsize", str(bitrate)]
        resolution = target.get("resolution")  # 形如 "1280x720"
        if resolution:
            w, h = str(resolution).lower().split("x")
            cmd += ["-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                           f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"]
        gop = target.get("gop") or 50
        cmd += ["-g", str(gop)]

    # ---- 音频 ----
    if info and not info["audio"]:
        cmd += ["-an"]
    elif audio_copy:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", str(target.get("audio_bitrate") or "128k"),
                "-ar", "44100", "-ac", "2"]

    # ---- 输出 ----
    cmd += ["-f", OUTPUT_FORMAT[protocol]]
    if protocol == "webrtc":
        cmd += ["-rtsp_transport", "tcp"]
    if protocol == "srt":
        cmd += ["-muxdelay", "0.1"]
    for extra in target.get("extra_args") or []:
        cmd += shlex.split(str(extra))
    cmd.append(target["url"])
    return cmd


class StreamWorker(threading.Thread):
    """单个推流目标的守护线程：循环取文件推流，断流自动重试，坏文件自动跳过。"""

    def __init__(self, target, playlist, app_cfg, stop_event):
        name = target.get("name") or f"{target['protocol']}-{target['url'][:32]}"
        super().__init__(name=f"worker-{name}", daemon=True)
        self.target = target
        self.playlist = playlist
        self.stop_event = stop_event

        retry = app_cfg.get("retry") or {}
        self.initial_delay = float(retry.get("initial_delay", 3))
        self.max_delay = float(retry.get("max_delay", 60))
        self.backoff_factor = float(retry.get("backoff_factor", 2))
        # 运行时长低于该值即视为“文件本身有问题”而非网络断流
        self.stable_seconds = float(retry.get("stable_seconds", 10))
        self.max_file_retries = int(retry.get("max_file_retries", 2))
        self.no_transcode = bool(app_cfg.get("no_transcode", False))

        self._proc = None
        self._proc_lock = threading.Lock()

    # ------------------------------------------------------------------
    def run(self):
        log.info("[%s] 工作线程启动: %s -> %s", self.name, self.target["protocol"], self.target["url"])
        delay = self.initial_delay
        file_fails = 0

        while not self.stop_event.is_set():
            path = self.playlist.current()
            log.info("[%s] 开始推流: %s", self.name, path)

            exitcode, runtime = self._run_file(path)
            if self.stop_event.is_set():
                break

            if exitcode == 0:
                # 文件正常播完
                file_fails = 0
                delay = self.initial_delay
                self.playlist.advance()
                continue

            if runtime < self.stable_seconds:
                # 很快退出：多半是文件损坏/格式不支持
                file_fails += 1
                log.warning("[%s] 文件推流快速失败(exit=%s, %.1fs): %s (%d/%d)",
                            self.name, exitcode, runtime, path, file_fails, self.max_file_retries)
                if file_fails >= self.max_file_retries:
                    log.error("[%s] 跳过疑似损坏文件: %s", self.name, path)
                    file_fails = 0
                    self.playlist.advance()
                else:
                    self.stop_event.wait(min(delay, self.max_delay))
            else:
                # 推流中断（网络/服务端断开），退避后重试同一文件
                log.warning("[%s] 断流(exit=%s, 已运行 %.0fs)，%.0f 秒后重试: %s",
                            self.name, exitcode, runtime, delay, path)
                file_fails = 0
                self.stop_event.wait(delay)
                delay = min(delay * self.backoff_factor, self.max_delay)

        log.info("[%s] 工作线程退出", self.name)

    # ------------------------------------------------------------------
    def _run_file(self, path):
        """推流单个文件，返回 (exitcode, 运行秒数)。被停止时 exitcode 固定为 0。"""
        info = probe(path) if self.no_transcode else None
        if self.no_transcode:
            v_copy, a_copy = can_copy(info)
            if v_copy and (a_copy or not (info and info["audio"])):
                log.info("[%s] 免转码推流(copy): %s", self.name, path)
            elif v_copy:
                log.info("[%s] 视频 copy + 音频转码: %s", self.name, path)
            else:
                log.info("[%s] 源不满足免转码条件，执行转码: %s", self.name, path)

        cmd = build_command(self.target, path, info, self.no_transcode)
        log.debug("[%s] 命令: %s", self.name, " ".join(shlex.quote(c) for c in cmd))

        start = time.monotonic()
        try:
            with self._proc_lock:
                self._proc = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                )
                proc = self._proc
            self._drain_stderr(proc)
            exitcode = self._wait(proc)
        except FileNotFoundError:
            log.error("找不到 ffmpeg，请先安装: apt install ffmpeg 或 yum install ffmpeg")
            self.stop_event.set()
            return 0, 0.0
        finally:
            with self._proc_lock:
                self._proc = None
        return exitcode, time.monotonic() - start

    def _drain_stderr(self, proc):
        """后台读取 ffmpeg stderr，避免管道写满阻塞，并把日志转发出来。"""
        def reader():
            try:
                for line in proc.stderr:
                    line = line.strip()
                    if line:
                        log.debug("[%s] ffmpeg: %s", self.name, line)
            except Exception:
                pass
        t = threading.Thread(target=reader, daemon=True)
        t.start()

    def _wait(self, proc):
        while True:
            try:
                return proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                if self.stop_event.is_set():
                    self._terminate(proc)
                    return 0

    def _terminate(self, proc):
        log.info("[%s] 停止 ffmpeg (pid=%s)", self.name, proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=TERMINATE_GRACE)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=TERMINATE_GRACE)

    def stop(self):
        with self._proc_lock:
            if self._proc and self._proc.poll() is None:
                self._terminate(self._proc)
