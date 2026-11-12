#!/usr/bin/env python3
"""
ChromeOS recovery image downloader - background service (script 1 of 3).

Runs forever (started by Task Scheduler via Setup-ChromeOSWatch.ps1):
  * checks the releases index every few minutes for new recovery images
  * downloads them ONE AT A TIME, resuming partial files
  * publishes what it is doing to state/status.json
  * obeys commands dropped into state/commands/ by chromeos_ctl.py

Everything lives next to this script:
    ChromeOS-recovery/<board>/*.zip     downloaded images
    state/                              config, status, log, queue bookkeeping

You normally never run this by hand. For a one-off test:
    python chromeos_daemon.py --once -v

Data: https://github.com/MercuryWorkshop/chromeos-releases-data (CC-BY-4.0)
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace

# raw.githubusercontent.com caches for 5 minutes (matches our poll interval).
# jsDelivr caches branch files for 12 hours, so it is only used as a fallback.
DATA_URLS = [
    "https://raw.githubusercontent.com/MercuryWorkshop/chromeos-releases-data/main/data.json",
    "https://cdn.jsdelivr.net/gh/MercuryWorkshop/chromeos-releases-data/data.json",
]
UA = {"User-Agent": "Mozilla/5.0 (chromeos-watch)"}
ALIASES = {"crosola": "corsola"}
MIN_INTERVAL = 60
HEARTBEAT = 15  # ctl treats the daemon as dead if status.json is older than this (seconds)

DEFAULT_CONFIG = {
    "boards": ["octopus", "dedede", "skywalker", "corsola"],
    "channel": "stable-channel",   # or "any"
    "interval": 300,               # seconds between index checks
    "paused": False,
    "keep": 1,                     # images kept per board+channel; older ones are deleted when a new release arrives
}

SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_BOARD = re.compile(r"^[a-z0-9_-]+$")

log = logging.getLogger("chromeos_daemon")


# --------------------------------------------------------------------------- shared helpers
# (chromeos_ctl.py imports these)

def make_paths(root: Path) -> SimpleNamespace:
    state = root / "state"
    return SimpleNamespace(
        root=root,
        state=state,
        images=root / "ChromeOS-recovery",
        config=state / "config.json",
        status=state / "status.json",
        seen=state / "seen.json",
        commands=state / "commands",
        log=state / "daemon.log",
        lock=state / "daemon.lock",
        crash=state / "crash.log",
        models=state / "models.json",
    )


def read_json(path: Path, default=None):
    """Read JSON, tolerating a writer swapping the file under us (common on Windows)."""
    for _ in range(6):
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return default
        except (ValueError, OSError):
            time.sleep(0.05)
    return default


def atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    err = None
    for _ in range(20):  # on Windows the replace fails while a reader has the file open
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:
            err = e
            time.sleep(0.05)
    tmp.unlink(missing_ok=True)
    raise err  # type: ignore[misc]


def normalize_board(b: str) -> str:
    b = b.strip().lower()
    return ALIASES.get(b, b)


def load_config(p) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    saved = read_json(p.config, {}) or {}
    for k in DEFAULT_CONFIG:
        if k in saved:
            cfg[k] = saved[k]
    cfg["boards"] = list(dict.fromkeys(normalize_board(b) for b in cfg["boards"]))
    cfg["interval"] = max(int(cfg["interval"]), MIN_INTERVAL)
    cfg["keep"] = max(int(cfg["keep"]), 1)
    return cfg


def coerce_setting(key: str, value):
    """Validate one setting from the command line / ctl. Returns the normalised value."""
    if key == "boards":
        if isinstance(value, str):
            value = [x for x in re.split(r"[,\s]+", value) if x]
        boards = list(dict.fromkeys(normalize_board(str(b)) for b in value))   # de-duplicated, order kept
        if not boards or not all(SAFE_BOARD.match(b) for b in boards):
            raise ValueError("boards must be one or more names like octopus,dedede")
        return boards
    if key == "channel":
        v = str(value).strip().lower()
        if not v or not SAFE_NAME.match(v):
            raise ValueError("channel must be e.g. stable-channel, beta-channel or any")
        return v
    if key == "interval":
        v = int(value)
        if v < MIN_INTERVAL:
            raise ValueError(f"interval must be at least {MIN_INTERVAL} seconds")
        return v
    if key == "keep":
        v = int(value)
        if v < 1:
            raise ValueError("keep must be at least 1")
        return v
    if key == "paused":
        if isinstance(value, bool):
            return value
        v = str(value).strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{key} must be true or false")
    raise ValueError(f"unknown setting '{key}' (known: {', '.join(DEFAULT_CONFIG)})")


def data_urls() -> list[str]:
    env = os.environ.get("CROS_DATA_URL")
    return [env] if env else list(DATA_URLS)


def fetch_index(etag: str | None = None) -> tuple[dict | None, str | None]:
    """Download the releases index. Returns (data, etag); data is None on 304 Not Modified."""
    last: Exception | None = None
    for url in data_urls():
        headers = dict(UA)
        if etag:
            headers["If-None-Match"] = etag
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                return json.load(r), r.headers.get("ETag")
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return None, etag
            last = e
        except Exception as e:  # noqa: BLE001 - try the next mirror
            last = e
    raise RuntimeError(f"could not fetch release index: {last}")


def get_images(data: dict, board: str, channel: str | None) -> list[dict]:
    """Real image entries for a board (skips the credit placeholder entry), newest first."""
    out = []
    for img in (data.get(board) or {}).get("images", []):
        url = img.get("url", "")
        if not url.endswith(".zip") or f"_{board}_recovery_" not in url:
            continue
        if channel and img.get("channel") != channel:
            continue
        out.append(img)
    out.sort(key=lambda i: i.get("last_modified", 0), reverse=True)
    return out


def split_models(names) -> list[str]:
    """Turn the index's brand_names (some hold several devices in one string) into single model names."""
    out: list[str] = []
    for raw in names or []:
        parts, cur, depth = [], [], 0
        for ch in str(raw):
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth = max(0, depth - 1)
            if depth == 0 and ch in ",/":      # only split on commas / slashes outside brackets
                parts.append("".join(cur))
                cur = []
            else:
                cur.append(ch)
        parts.append("".join(cur))
        for part in parts:
            name = " ".join(part.split()).strip(" .;")
            if name and name not in out:
                out.append(name)
    return out


def channel_filter(cfg: dict) -> str | None:
    return None if cfg["channel"] == "any" else cfg["channel"]


def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return str(n)


def fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- single instance

class SingleInstance:
    """Exclusive lock on a file; released automatically if the process dies."""

    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def _try(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self.fh = fh
        return True

    def acquire(self, wait: float = 0) -> bool:
        deadline = time.monotonic() + wait
        while True:
            if self._try():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)

    def release(self) -> None:
        if self.fh:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self.fh.close()
            self.fh = None


# --------------------------------------------------------------------------- daemon

class Job:
    def __init__(self, url: str, board: str, force: bool = False, source: str = "auto"):
        self.url = url
        self.board = board
        self.force = force
        self.source = source
        self.name = url.rsplit("/", 1)[-1]

    def brief(self) -> dict:
        return {"url": self.url, "board": self.board, "name": self.name,
                "force": self.force, "source": self.source}


def make_job(item: dict, force: bool, source: str) -> Job:
    url = str(item.get("url", ""))
    board = normalize_board(str(item.get("board", "")))
    name = url.rsplit("/", 1)[-1]
    secure = url.startswith("https://") or url.startswith("http://127.0.0.1:")   # loopback http = local testing
    if not secure or not name.endswith(".zip") or not SAFE_NAME.match(name):
        raise ValueError(f"refusing odd URL: {url[:100]}")
    if not SAFE_BOARD.match(board):
        raise ValueError(f"bad board name: {board[:40]}")
    return Job(url, board, force, source)


class EventHandler(logging.Handler):
    """Keeps the last few log lines so ctl can show them without reading the log file."""

    def __init__(self, sink: collections.deque):
        super().__init__(logging.INFO)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self.sink.append({"t": record.created, "level": record.levelname, "msg": record.getMessage()})


class Daemon:
    def __init__(self, root: Path, once: bool = False):
        self.p = make_paths(root)
        for d in (self.p.state, self.p.images, self.p.commands):
            d.mkdir(parents=True, exist_ok=True)
        self.once = once
        self.events: collections.deque = collections.deque(maxlen=30)
        self.acks: collections.OrderedDict = collections.OrderedDict()
        self.queue: list[Job] = []
        self.current: Job | None = None
        self.progress: dict = {}
        self.interrupt: str | None = None
        self.stopping = False
        self.restart_requested = False
        self.started = time.time()
        self.cfg = load_config(self.p)
        saved = read_json(self.p.seen, {}) or {}
        self.seen: set[str] = set(saved.get("seen", []))
        self.baselined: set[str] = set(saved.get("baselined", []))
        self.declined: set[str] = set(saved.get("declined", []))   # images the user skipped/cancelled: never auto-fetch
        self.index: dict | None = None
        self.etag: str | None = None
        self.checking = False
        self.force_check = False
        self.next_check = 0.0
        self.last_check: float | None = None
        self.last_check_ok: bool | None = None
        self.last_error: str | None = None
        self._status_at = 0.0

    # ----- persistence
    def save_seen(self) -> None:
        atomic_write_json(self.p.seen, {"seen": sorted(self.seen), "baselined": sorted(self.baselined),
                                        "declined": sorted(self.declined)})

    def save_config(self) -> None:
        atomic_write_json(self.p.config, self.cfg)

    def write_status(self, force: bool = False, state: str | None = None) -> None:
        now = time.time()
        if not force and now - self._status_at < 1.0:
            return
        self._status_at = now
        if state is None:
            if self.stopping:
                state = "stopping"
            elif self.current:
                state = "downloading"
            elif self.cfg["paused"]:
                state = "paused"
            elif self.queue:
                state = "queued"
            else:
                state = "idle"
        try:
            free = shutil.disk_usage(self.p.images).free
        except OSError:
            free = None
        status = {
            "version": 1,
            "pid": os.getpid(),
            "started": self.started,
            "updated": now,
            "state": state,
            "checking": self.checking,
            "current": ({**self.current.brief(), **self.progress} if self.current else None),
            "queue": [j.brief() for j in self.queue],
            "config": self.cfg,
            "last_check": self.last_check,
            "last_check_ok": self.last_check_ok,
            "next_check": self.next_check,
            "last_error": self.last_error,
            "disk_free": free,
            "events": list(self.events),
            "acks": dict(self.acks),
        }
        try:
            atomic_write_json(self.p.status, status)
        except OSError as e:
            log.warning("could not write status: %s", e)

    # ----- commands
    def ack(self, cid: str, ok: bool, msg: str) -> None:
        if not cid:
            return
        self.acks[cid] = {"ok": ok, "msg": msg, "t": time.time()}
        while len(self.acks) > 30:
            self.acks.popitem(last=False)

    def process_commands(self) -> None:
        for f in sorted(self.p.commands.glob("*.json")):
            cmd = read_json(f, None)
            try:
                f.unlink()
            except OSError:
                pass
            if not isinstance(cmd, dict):
                continue
            cid = str(cmd.get("id", ""))
            try:
                ok, msg = self.handle(cmd)
            except (ValueError, TypeError, KeyError) as e:
                ok, msg = False, f"error: {e}"
            except Exception as e:  # noqa: BLE001
                log.exception("command failed")
                ok, msg = False, f"internal error: {e}"
            log.info("command %s -> %s", cmd.get("cmd"), msg)
            self.ack(cid, ok, msg)
            self.write_status(force=True)

    def handle(self, cmd: dict) -> tuple[bool, str]:
        name = cmd.get("cmd")
        cur = self.current

        if name == "ping":
            return True, "pong"

        if name == "get":
            force = bool(cmd.get("force"))
            now = bool(cmd.get("now", True))
            jobs = [make_job(i, force, "manual") for i in cmd.get("items", [])]
            if not jobs:
                return False, "nothing to download"
            notes = []
            urls = {j.url for j in jobs}
            self.queue = [q for q in self.queue if q.url not in urls]
            if cur and cur.url in urls:
                if force:
                    self.interrupt = "drop"      # throw the current copy away, restart it as a fresh job
                    cur = None
                else:
                    jobs = [j for j in jobs if j.url != cur.url]
                    notes.append("already downloading")
                    if not jobs:
                        return True, "already downloading that one"
            if now:
                if cur is not None:
                    self.queue = jobs + [cur] + self.queue   # current goes back in line right after
                    self.interrupt = "preempt"
                else:
                    self.queue = jobs + self.queue
            else:
                self.queue += jobs
            msg = f"queued {len(jobs)}" + (" at the front" if now else "") + (" (forced re-download)" if force else "")
            if self.cfg["paused"]:
                msg += " - downloads are PAUSED, run 'resume' to start"
            return True, msg + ("; " + ", ".join(notes) if notes else "")

        if name == "refresh":
            self.force_check = True
            return True, "checking for new images now"

        if name == "pause":
            self.cfg["paused"] = True
            self.save_config()
            if cur:
                self.interrupt = "pause"
            return True, "paused (current download keeps its progress)"

        if name == "resume":
            self.cfg["paused"] = False
            self.save_config()
            return True, "resumed"

        if name == "cancel":
            url = cmd.get("url")
            if url is None or (cur and cur.url == url):
                if not cur:
                    return False, "nothing is downloading"
                self.seen.add(cur.url)
                self.declined.add(cur.url)
                self.save_seen()
                self.interrupt = "cancel"
                return True, f"cancelled {cur.name} (partial file kept; use 'get' to fetch it later)"
            before = len(self.queue)
            self.queue = [q for q in self.queue if q.url != url]
            self.seen.add(url)
            self.declined.add(url)
            self.save_seen()
            return (before != len(self.queue)), ("removed from queue" if before != len(self.queue) else "not in queue")

        if name == "clear":
            n = len(self.queue)
            self.seen.update(j.url for j in self.queue)
            self.declined.update(j.url for j in self.queue)
            self.queue = []
            self.save_seen()
            return True, f"cleared {n} queued item(s)"

        if name == "set":
            changes = {}
            for k, v in (cmd.get("values") or {}).items():
                changes[k] = coerce_setting(k, v)
            for k, v in changes.items():
                self.cfg[k] = v
            self.save_config()
            if "paused" in changes and changes["paused"] and cur:
                self.interrupt = "pause"
            if {"boards", "channel"} & set(changes):
                self.force_check = True
            if "interval" in changes:
                self.next_check = min(self.next_check, time.time() + self.cfg["interval"])
            return True, "saved: " + ", ".join(f"{k}={v}" for k, v in changes.items())

        if name == "restart":
            self.restart_requested = True
            self.stopping = True
            if cur:
                self.interrupt = "stop"
            return True, "restarting"

        return False, f"unknown command '{name}'"

    # ----- index check
    def check_index(self) -> None:
        self.checking = True
        self.write_status(force=True)
        try:
            data, etag = fetch_index(self.etag if self.index is not None else None)
            if data is not None:
                self.index, self.etag = data, etag
                try:   # keep the model names so the control tool can label images without network
                    atomic_write_json(self.p.models, {b: v.get("brand_names", [])
                                                      for b, v in data.items() if isinstance(v, dict)})
                except OSError as e:
                    log.warning("could not save model names: %s", e)
            self.last_check, self.last_check_ok, self.last_error = time.time(), True, None
            self.next_check = time.time() + self.cfg["interval"]
            self._scan_index()
        except Exception as e:  # noqa: BLE001 - keep the daemon alive through network problems
            self.last_check, self.last_check_ok, self.last_error = time.time(), False, str(e)
            self.next_check = time.time() + min(self.cfg["interval"], 60)
            log.error("index check failed: %s", e)
        finally:
            self.checking = False
            self.write_status(force=True)

    def _scan_index(self) -> None:
        if not self.index:
            return
        channel = channel_filter(self.cfg)
        tag = channel or "any"
        queued = {j.url for j in self.queue} | ({self.current.url} if self.current else set())
        added = 0
        for board in self.cfg["boards"]:
            if board not in self.index:
                log.warning("board '%s' is not in the release index", board)
                continue
            imgs = get_images(self.index, board, channel)
            key = f"{board}|{tag}"
            if key not in self.baselined:
                # First time we see this board/channel: don't pull the whole back catalogue.
                self.baselined.add(key)
                self.seen.update(i["url"] for i in imgs)
                log.info("%s (%s): recorded %d existing images", board, tag, len(imgs))
            # Nothing on disk for this board? Fetch the newest image, so a fresh install (or a newly
            # added board) ends up with something usable. Skipped/cancelled images are not re-queued.
            if imgs:
                folder = self.p.images / board
                have = any((folder / i["url"].rsplit("/", 1)[-1]).exists() for i in imgs)
                newest = max(imgs, key=lambda i: i.get("last_modified", 0))
                if not have and newest["url"] not in self.declined and newest["url"] not in queued:
                    self.queue.append(Job(newest["url"], board, source="auto"))
                    queued.add(newest["url"])
                    added += 1
                    log.info("no image on disk for %s: fetching the newest (%s, %s)", board,
                             newest["platform_version"], fmt_date(newest["last_modified"]))
            for img in sorted(imgs, key=lambda i: i.get("last_modified", 0)):   # oldest first
                url = img["url"]
                if url in self.seen or url in queued:
                    continue
                if (self.p.images / board / url.rsplit("/", 1)[-1]).exists():
                    self.seen.add(url)
                    continue
                self.queue.append(Job(url, board, source="auto"))
                queued.add(url)
                added += 1
                log.info("new image: %s %s (chrome %s, %s)", board, img["platform_version"],
                         img.get("chrome_version", "?"), fmt_date(img["last_modified"]))
        self.save_seen()
        if not added:
            log.info("check complete: nothing new")

    # ----- downloading
    def tick(self) -> None:
        self.process_commands()
        if not self.stopping and (self.force_check or time.time() >= self.next_check):
            self.force_check = False
            self.check_index()
        self.write_status()

    def run_job(self, job: Job) -> None:
        self.current, self.interrupt = job, None
        self.progress = {"done": 0, "total": 0, "speed": 0.0, "began": time.time(), "resumed_from": 0}
        self.write_status(force=True)
        try:
            result = self.download(job)
        except Exception as e:  # noqa: BLE001
            log.error("download failed: %s: %s", job.name, e)
            self.last_error = f"{job.name}: {e}"
            result = "failed"
        self.current = None
        why, self.interrupt = self.interrupt, None
        if result == "done":
            self.seen.add(job.url)
            self.save_seen()
            if job.source == "auto":          # a new release arrived: tidy up the superseded ones
                self.prune(job)
        elif result == "interrupted" and why == "pause":
            self.queue.insert(0, job)
            log.info("paused %s", job.name)
        elif result == "interrupted":
            log.info("stopped %s (%s)", job.name, why)
        self.write_status(force=True)

    def prune(self, job: Job) -> None:
        """Delete older images of the same board and channel, keeping the newest `keep`.
        Only touches .zip files that the release index knows about, so anything else in the folder is left alone."""
        if not self.index:
            return
        keep = max(int(self.cfg.get("keep", 1)), 1)
        by_name = {i["url"].rsplit("/", 1)[-1]: i for i in get_images(self.index, job.board, None)}
        new = by_name.get(job.name)
        if not new:
            return
        folder = self.p.images / job.board
        same = []
        for f in folder.glob("*.zip"):
            img = by_name.get(f.name)
            if img and img["channel"] == new["channel"]:
                same.append((img.get("last_modified", 0), f))
        same.sort(key=lambda t: t[0], reverse=True)
        for _ts, f in same[keep:]:
            try:
                size = f.stat().st_size
                f.unlink()
                log.info("deleted superseded image %s (freed %s)", f.name, human(size))
            except OSError as e:               # e.g. still open in a flash or locked by antivirus: try next time
                log.warning("could not delete %s: %s", f.name, e)

    def _finish(self, part: Path, dest: Path, job: Job) -> str:
        try:
            zipfile.ZipFile(part).close()          # reads the central directory: catches truncated files
        except zipfile.BadZipFile:
            part.unlink(missing_ok=True)
            raise RuntimeError("downloaded file is not a valid zip (deleted, will retry)")
        err = None
        for _ in range(20):                        # antivirus can briefly lock a fresh file on Windows
            try:
                os.replace(part, dest)
                break
            except PermissionError as e:
                err = e
                time.sleep(0.5)
        else:
            raise err  # type: ignore[misc]
        log.info("finished %s (%s)", job.name, human(dest.stat().st_size))
        return "done"

    def download(self, job: Job) -> str:
        dest_dir = self.p.images / job.board
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / job.name
        part = dest_dir / (job.name + ".part")
        if job.force:
            dest.unlink(missing_ok=True)
            part.unlink(missing_ok=True)
            job.force = False
            log.info("forced re-download: removed existing copy of %s", job.name)
        if dest.exists():
            log.info("already have %s", job.name)
            return "done"

        for _attempt in range(2):
            resume = part.stat().st_size if part.exists() else 0
            headers = dict(UA)
            if resume:
                headers["Range"] = f"bytes={resume}-"
            try:
                resp = urllib.request.urlopen(urllib.request.Request(job.url, headers=headers), timeout=30)
            except urllib.error.HTTPError as e:
                if e.code == 416 and resume:      # our partial file is complete or unusable
                    try:
                        return self._finish(part, dest, job)
                    except RuntimeError:
                        continue
                raise
            with resp:
                clen = int(resp.headers.get("Content-Length") or 0)
                if getattr(resp, "status", 200) == 206 and resume:
                    m = re.search(r"/(\d+)\s*$", resp.headers.get("Content-Range", ""))
                    total = int(m.group(1)) if m else resume + clen
                    mode, done = "ab", resume
                else:
                    total, mode, done = clen, "wb", 0
                free = shutil.disk_usage(dest_dir).free
                need = max(total - done, 0)
                if total and free < need + 200 * 1024 * 1024:
                    raise OSError(f"not enough disk space: need {human(need)}, have {human(free)}")
                self.progress.update(done=done, total=total, resumed_from=done, began=time.time())
                log.info("%s %s (%s%s)", "resuming" if done else "downloading", job.name,
                         human(total) if total else "size unknown",
                         f", already have {human(done)}" if done else "")
                last_tick, last_done, speed = time.monotonic(), done, 0.0
                with open(part, mode) as f:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        t = time.monotonic()
                        if t - last_tick >= 0.5:
                            inst = (done - last_done) / (t - last_tick)
                            speed = inst if speed == 0 else 0.7 * speed + 0.3 * inst
                            last_tick, last_done = t, done
                            self.progress.update(done=done, speed=speed)
                            self.tick()
                            if self.interrupt:
                                return "interrupted"
                self.progress.update(done=done)
                if total and done != total:
                    raise OSError(f"connection ended early ({human(done)} of {human(total)}); will resume")
            return self._finish(part, dest, job)
        raise RuntimeError("could not get a usable download")

    # ----- main loop
    def setup_logging(self, verbose: bool) -> None:
        log.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        fh = RotatingFileHandler(self.p.log, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
        log.addHandler(EventHandler(self.events))
        if verbose and sys.stdout is not None:
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(fmt)
            log.addHandler(sh)

    def request_stop(self, *_a) -> None:
        self.stopping = True
        if self.current:
            self.interrupt = "stop"

    def run(self, wait_lock: bool, verbose: bool) -> int:
        lock = SingleInstance(self.p.lock)
        if not lock.acquire(wait=20 if wait_lock else 0):
            return 0                                   # another daemon is already running
        self.setup_logging(verbose)
        log.info("daemon started (pid %d, root %s)", os.getpid(), self.p.root)
        for sig in ("SIGINT", "SIGTERM", "SIGBREAK"):
            if hasattr(signal, sig):
                try:
                    signal.signal(getattr(signal, sig), self.request_stop)
                except (ValueError, OSError):
                    pass
        for f in self.p.commands.glob("*.json"):       # commands from before we started are stale
            f.unlink(missing_ok=True)
        try:
            while not self.stopping:
                try:
                    self.tick()
                    if self.queue and not self.cfg["paused"] and not self.stopping:
                        self.run_job(self.queue.pop(0))
                    elif self.once and self.last_check is not None and not self.queue:
                        break
                    else:
                        time.sleep(0.5)
                except KeyboardInterrupt:
                    break
                except Exception:  # noqa: BLE001 - never die from a bug in one cycle
                    log.exception("unexpected error in main loop")
                    time.sleep(5)
        finally:
            log.info("daemon stopping")
            self.write_status(force=True, state="stopped")
            lock.release()
        if self.restart_requested:
            self.relaunch()
        return 0

    def relaunch(self) -> None:
        args = [sys.executable, str(Path(__file__).resolve()), "--root", str(self.p.root), "--wait-lock"]
        kw: dict = {"close_fds": True, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kw["creationflags"] = 0x00000008 | 0x08000000 | 0x00000200   # DETACHED | NO_WINDOW | NEW_GROUP
        else:
            kw["start_new_session"] = True
        subprocess.Popen(args, **kw)


# --------------------------------------------------------------------------- entry point

def write_config_cli(root: Path, a: argparse.Namespace) -> int:
    """Used by the setup script: create/update state/config.json without starting the daemon."""
    p = make_paths(root)
    p.state.mkdir(parents=True, exist_ok=True)
    cfg = load_config(p)
    try:
        if a.boards:
            cfg["boards"] = coerce_setting("boards", a.boards)
        if a.channel:
            cfg["channel"] = coerce_setting("channel", a.channel)
        if a.interval:
            cfg["interval"] = coerce_setting("interval", a.interval)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    atomic_write_json(p.config, cfg)
    print(json.dumps(cfg))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="ChromeOS recovery image background downloader")
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent,
                    help="folder that holds state/ and ChromeOS-recovery/ (default: next to this script)")
    ap.add_argument("--once", action="store_true", help="check once, download everything queued, exit")
    ap.add_argument("-v", "--verbose", action="store_true", help="also log to the console")
    ap.add_argument("--wait-lock", action="store_true", help="wait for a previous instance to exit (used on restart)")
    ap.add_argument("--write-config", action="store_true", help="write state/config.json and exit (setup script)")
    ap.add_argument("--boards", nargs="+")
    ap.add_argument("--channel")
    ap.add_argument("--interval", type=int)
    ap.add_argument("--include-existing", action="store_true", help=argparse.SUPPRESS)   # old setup scripts; now a no-op
    a = ap.parse_args()
    root = a.root.resolve()
    if a.write_config:
        return write_config_cli(root, a)
    try:
        return Daemon(root, once=a.once).run(wait_lock=a.wait_lock, verbose=a.verbose)
    except BaseException:  # noqa: BLE001 - under pythonw nobody sees a traceback, so save it
        try:
            p = make_paths(root)
            p.state.mkdir(parents=True, exist_ok=True)
            with open(p.crash, "a", encoding="utf-8") as f:
                f.write(f"\n--- {datetime.now().isoformat()} ---\n{traceback.format_exc()}")
        except OSError:
            pass
        raise


if __name__ == "__main__":
    sys.exit(main())
