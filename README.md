# Unmanned Live Streaming System

A 24/7 unattended live streaming daemon written in Python for Linux servers. It reads video files (or entire directories), loops them forever, and pushes the stream to one or more destinations via **ffmpeg**, with automatic reconnection on stream failure.

## Features

- **Multiple protocols**
  - `rtmp` — push to any RTMP server (nginx-rtmp, SRS, TikTok/YouTube/Twitch ingest, etc.)
  - `srt` — push over SRT (caller mode) with MPEG-TS encapsulation
  - `webrtc` — push via [MediaMTX](https://github.com/bluenviron/mediamtx): ffmpeg sends RTSP to a local MediaMTX instance, which serves viewers over WebRTC (WHEP)
- **Zero-transcode mode** — a single config switch (`no_transcode: true`). Files are probed with `ffprobe`; if the video is already H.264 it is stream-copied (`-c:v copy`), dramatically reducing CPU usage. Non-AAC audio is re-encoded to AAC while video stays in copy mode; fully incompatible sources fall back to complete transcoding automatically.
- **Auto-retry with failure classification**
  - *Stream drop* (process died after running longer than `stable_seconds`): retried forever with exponential backoff.
  - *Bad file* (process died within `stable_seconds`): retried up to `max_file_retries` times, then skipped so one corrupt file can never stall the channel.
- **Playlist** — combine individual files and directories (scanned recursively, filtered by extension), sorted by name or mtime, optionally shuffled each round, looped infinitely.
- **Multi-target** — push to several destinations at once; each target runs in its own thread with independent retry state.
- **Production-ready** — rotating log files, graceful shutdown on `SIGINT`/`SIGTERM` (ffmpeg children are always terminated), and a systemd unit example for full unattended operation.

## Requirements

- Linux server (any modern distro)
- Python 3.8+
- [ffmpeg](https://ffmpeg.org/) with `ffprobe` on `PATH` (SRT support requires ffmpeg built with `libsrt` — most distro packages include it)
- [MediaMTX](https://github.com/bluenviron/mediamtx) (only if you want the WebRTC target)

## Installation

```bash
git clone https://github.com/<your-name>/unmanned-live-streaming.git
cd unmanned-live-streaming
pip3 install -r requirements.txt

# ffmpeg (Debian/Ubuntu)
sudo apt update && sudo apt install -y ffmpeg
# or (RHEL/CentOS/Rocky)
sudo dnf install -y ffmpeg
```

## Quick Start

1. Put your video files somewhere, e.g. `/data/videos`.
2. Edit `config.yaml`:

```yaml
no_transcode: true

playlist:
  directories:
    - /data/videos

targets:
  - name: rtmp-main
    enabled: true
    protocol: rtmp
    url: rtmp://your-server:1935/live/streamkey
```

3. Run it:

```bash
python3 main.py -c config.yaml
```

You should see log lines like:

```
[INFO] playlist: 播放列表共 12 个文件
[INFO] engine: [worker-rtmp-main] 开始推流: /data/videos/01.mp4
[INFO] engine: [worker-rtmp-main] 免转码推流(copy): /data/videos/01.mp4
```

Press `Ctrl+C` to stop gracefully.

## Configuration Reference

| Key | Description |
|---|---|
| `no_transcode` | Global switch. When `true`, compatible files are pushed without re-encoding (video `copy` when H.264; audio `copy` when AAC, otherwise re-encoded to AAC). Incompatible files are transcoded anyway. |
| `logging.level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `logging.file` | Log file path (empty = console only). Rotating: `max_bytes` (default 50 MB) x `backup_count` (default 5). |
| `retry.initial_delay` | Seconds to wait before the first retry (default 3) |
| `retry.max_delay` | Backoff cap in seconds (default 60) |
| `retry.backoff_factor` | Delay multiplier per consecutive failure (default 2) |
| `retry.stable_seconds` | Runtime below this threshold means "bad file", not "stream drop" (default 10) |
| `retry.max_file_retries` | Retries for a bad file before it is skipped (default 2) |
| `playlist.files` | Explicit list of video files |
| `playlist.directories` | Directories scanned recursively |
| `playlist.extensions` | File extensions accepted from directories |
| `playlist.sort` | `name` or `mtime` |
| `playlist.shuffle` | Shuffle the list at the start of every round |

### Per-target options

| Key | Description |
|---|---|
| `name` | Label used in logs |
| `enabled` | Set `false` to keep the target in config but inactive |
| `protocol` | `rtmp`, `srt`, or `webrtc` |
| `url` | Push destination (see protocol notes below) |
| `video_bitrate` | e.g. `2500k` — only used when transcoding |
| `audio_bitrate` | e.g. `128k` |
| `resolution` | e.g. `1280x720`; source is scaled and letterboxed/pillarboxed to fit. Empty = keep source resolution |
| `gop` | Keyframe interval in frames when transcoding (default 50 = 2s @ 25fps) |
| `extra_args` | Extra raw arguments appended to the ffmpeg command, e.g. `["-tune", "zerolatency"]` |

### Protocol notes

**RTMP**

```yaml
url: rtmp://your-server:1935/live/streamkey
```

**SRT** — caller mode is recommended; parameters go in the URL query string:

```yaml
url: "srt://your-server:9000?mode=caller&streamid=live&pkt_size=1316"
```

**WebRTC** — ffmpeg cannot emit WebRTC directly, so this system relays through MediaMTX:

1. Download and run [MediaMTX](https://github.com/bluenviron/mediamtx) (defaults: RTSP on `8554`, WebRTC on `8889`).
2. Configure the target to push RTSP into MediaMTX:

   ```yaml
   - name: webrtc
     protocol: webrtc
     url: rtsp://127.0.0.1:8554/live
   ```

3. Viewers open `http://<server-ip>:8889/live` in a browser (WHEP playback), or connect with any WebRTC client.

## Running as a System Service

A systemd unit example is provided in `deploy/livestream.service`:

```bash
sudo mkdir -p /opt/livestream
sudo cp -r *.py config.yaml requirements.txt /opt/livestream/
sudo cp deploy/livestream.service /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now livestream

# watch logs
journalctl -u livestream -f
```

The service restarts automatically on crash (`Restart=always`), and on stop it sends `SIGTERM`, which the application handles by terminating all ffmpeg child processes before exiting.

## How Failure Handling Works

```
ffmpeg exits
   ├── exit code 0 ────────────────► file finished → advance playlist
   ├── runtime < stable_seconds ───► suspected bad file
   │                                  retry up to max_file_retries, then skip
   └── runtime >= stable_seconds ──► stream drop (network/server)
                                      retry same file forever,
                                      exponential backoff (initial_delay → max_delay)
```

Backoff resets to `initial_delay` after a file completes successfully.

## Tested Behavior

Verified end-to-end with an SRT loopback (ffmpeg listener as receiver):

- H.264/AAC sources pushed in zero-transcode copy mode; receiver got complete data
- Receiver killed mid-stream → daemon detected the drop, backed off 2s → 4s → 5s, and resumed automatically once the receiver came back
- VP9 source → automatic fallback to full transcoding
- `SIGINT` → ffmpeg children terminated, clean exit with code 0

## License

MIT
