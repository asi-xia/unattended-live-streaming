"""播放列表管理：支持指定文件与目录，无限循环。"""
import logging
import random
import threading
from pathlib import Path

log = logging.getLogger("playlist")

DEFAULT_EXTENSIONS = {".mp4", ".mkv", ".flv", ".ts", ".mov", ".webm", ".m4v", ".mpg", ".mpeg", ".avi"}


class PlaylistEmptyError(Exception):
    pass


class Playlist:
    """线程安全的无限循环播放列表。

    多个推流目标各自独立调用 current()/advance()，共享同一份文件序列。
    """

    def __init__(self, cfg):
        self._shuffle = bool(cfg.get("shuffle", False))
        self._items = self._build(cfg)
        if not self._items:
            raise PlaylistEmptyError("播放列表为空：请检查 playlist.files / playlist.directories 配置")
        if self._shuffle:
            random.shuffle(self._items)
        self._index = 0
        self._lock = threading.Lock()
        log.info("播放列表共 %d 个文件", len(self._items))

    @staticmethod
    def _build(cfg):
        extensions = {
            e.lower() if e.startswith(".") else "." + e.lower()
            for e in cfg.get("extensions", DEFAULT_EXTENSIONS)
        }
        items = []
        seen = set()

        for f in cfg.get("files") or []:
            p = Path(f).expanduser()
            if p.is_file():
                if p not in seen:
                    seen.add(p)
                    items.append(p)
            else:
                log.warning("文件不存在，已忽略: %s", p)

        for d in cfg.get("directories") or []:
            root = Path(d).expanduser()
            if not root.is_dir():
                log.warning("目录不存在，已忽略: %s", root)
                continue
            for p in sorted(root.rglob("*")):
                if p.is_file() and p.suffix.lower() in extensions and p not in seen:
                    seen.add(p)
                    items.append(p)

        sort_mode = str(cfg.get("sort", "name")).lower()
        if sort_mode == "name":
            items.sort(key=lambda p: str(p))
        elif sort_mode == "mtime":
            items.sort(key=lambda p: p.stat().st_mtime)
        return items

    def __len__(self):
        return len(self._items)

    def current(self):
        with self._lock:
            return self._items[self._index % len(self._items)]

    def advance(self):
        """前进到下一个文件；一轮播完回到开头时，如开启 shuffle 则重新洗牌。"""
        with self._lock:
            self._index = (self._index + 1) % len(self._items)
            if self._index == 0:
                log.info("播放列表完成一轮，从头开始")
                if self._shuffle:
                    random.shuffle(self._items)
