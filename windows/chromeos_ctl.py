#!/usr/bin/env python3
"""
ChromeOS recovery watcher - control tool (script 2 of 3).

Normally you start this with the "ChromeOS Watch" shortcut (.bat) the installer puts on your
Desktop. With no arguments it opens a menu. It talks to the background daemon
(chromeos_daemon.py) through files in state/: shows what it is doing, changes what it does,
and flashes images to a USB drive.

Menu items can also be run directly (python chromeos_ctl.py <command>):
    status [-w]            what is the daemon doing?  -w = live view
    list [-b BOARD ...]    images available, newest first
    get [TEXT ...]         download NOW (interrupts the current one, which resumes after)
    get --queue ...        add to the end of the queue instead
    get --force ...        delete any local copy and download again
    refresh                check for new images right now
    pause | resume         stop / start downloading (checking continues)
    cancel                 skip the current download
    clear                  empty the queue
    set interval=600 boards=octopus,dedede channel=any
    logs [-f]              daemon log
    restart                restart the daemon
    find                   search your Chromebook model, download its image, flash it (Windows)
    flash [TEXT]           write an already-downloaded image to a USB drive (Windows)

With no TEXT, `get` and `flash` open a picker. TEXT is matched against file names.

"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import chromeos_daemon as cd  # noqa: E402

P = cd.make_paths(HERE)
IS_WIN = os.name == "nt"
TASK_NAME = "ChromeOS Recovery Watch"
CHUNK = 4 * 1024 * 1024
SECTOR = 512


# --------------------------------------------------------------------------- small helpers

def dur(s: float) -> str:
    s = int(max(s, 0))
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m:02}m" if h else (f"{m}m {s:02}s" if m else f"{s}s")


def tstr(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "never"


def bar(frac: float, width: int = 32) -> str:
    n = max(0, min(width, int(frac * width)))
    return "[" + "#" * n + "-" * (width - n) + "]"


def find_fzf() -> str | None:
    f = shutil.which("fzf")
    if f:
        return f
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles")):
        if base:
            for sub in ("Microsoft/WinGet/Links", "WinGet/Links"):
                cand = Path(base) / sub / "fzf.exe"
                if cand.exists():
                    return str(cand)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        for cand in (Path(local) / "Microsoft" / "WinGet" / "Packages").glob("junegunn.fzf*/fzf.exe"):
            return str(cand)
    return None


def pick(lines: list[str], prompt: str, header: str = "", multi: bool = False) -> list[str]:
    """Choose from lines of 'key<TAB>text'. Uses fzf if installed, else a numbered list."""
    if not lines:
        return []
    fzf = find_fzf()
    if fzf:
        cmd = [fzf, "--delimiter", "\t", "--with-nth", "2..", "--prompt", prompt,
               "--header", header, "--reverse", "--height", "80%"]
        if multi:
            cmd.append("--multi")
        r = subprocess.run(cmd, input="\n".join(lines), text=True, encoding="utf-8", stdout=subprocess.PIPE)
        if r.returncode != 0:
            return []
        return [row.split("\t", 1)[0] for row in r.stdout.splitlines() if row.strip()]
    if header:
        print(header)
    if len(lines) > 25:
        needle = input(f"{len(lines)} entries. Type part of a name to narrow the list (blank = show all): ").strip().lower()
        if needle:
            lines = [ln for ln in lines if needle in ln.split("\t", 1)[1].lower()]
            if not lines:
                print("Nothing matches.")
                return []
    for n, line in enumerate(lines, 1):
        print(f"{n:4}) {line.split(chr(9), 1)[1]}")
    ans = input("Number" + (" (several: 1 3 5)" if multi else "") + ", blank to cancel: ").strip()
    out = []
    for tok in ans.replace(",", " ").split():
        if tok.isdigit() and 1 <= int(tok) <= len(lines):
            out.append(lines[int(tok) - 1].split("\t", 1)[0])
    return out if multi else out[:1]


def daemon_running() -> bool:
    """The daemon holds an exclusive lock for its whole life, so a failed lock attempt means it is up."""
    lock = cd.SingleInstance(P.lock)
    if lock.acquire(0):
        lock.release()
        return False
    return True


def read_status() -> dict | None:
    return cd.read_json(P.status, None)


def send(cmd: str, **args) -> bool:
    """Drop a command for the daemon and wait for its reply."""
    if not daemon_running():
        print("The background daemon is not running.")
        print(f'  Start it with:  schtasks /run /tn "{TASK_NAME}"')
        print("  or run Setup-ChromeOSWatch.ps1 again from an Administrator PowerShell.")
        return False
    cid = uuid.uuid4().hex[:10]
    P.commands.mkdir(parents=True, exist_ok=True)
    cd.atomic_write_json(P.commands / f"{time.time():.3f}-{cid}.json", {"id": cid, "cmd": cmd, **args})
    deadline = time.time() + 12
    while time.time() < deadline:
        st = read_status()
        ack = (st or {}).get("acks", {}).get(cid)
        if ack:
            print(("OK: " if ack["ok"] else "Failed: ") + ack["msg"])
            return bool(ack["ok"])
        time.sleep(0.25)
    print("No reply yet (the daemon may be busy with a network request). The command is still queued.")
    return True


def is_admin() -> bool:
    if not IS_WIN:
        return hasattr(os, "geteuid") and os.geteuid() == 0
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- status

def render_status() -> str:
    st = read_status()
    out: list[str] = []
    running = daemon_running()
    if not st:
        out.append("No status yet: the daemon has never run in this folder.")
        out.append(f'Start it:  schtasks /run /tn "{TASK_NAME}"   (or run Setup-ChromeOSWatch.ps1 as Administrator)')
        return "\n".join(out)

    age = time.time() - st.get("updated", 0)
    if running:
        head = f"RUNNING (pid {st.get('pid')}, up {dur(time.time() - st.get('started', time.time()))})"
        if age > 30:
            head += f"  - no update for {dur(age)}, probably busy with a slow network request"
    else:
        head = "NOT RUNNING  (last seen " + tstr(st.get("updated")) + ")"
    out.append(f"ChromeOS recovery watcher: {head}")
    out.append("-" * 72)

    cur = st.get("current")
    state = st.get("state", "?")
    if cur:
        done, total, speed = cur.get("done", 0), cur.get("total", 0), cur.get("speed", 0)
        out.append(f"Now     : DOWNLOADING  {cur['board']}  ({cur.get('source', 'auto')})")
        out.append(f"          {cur['name']}")
        if total:
            eta = f"   ETA {dur((total - done) / speed)}" if speed > 1000 else ""
            out.append(f"          {bar(done / total)} {done * 100 // total:3d}%   "
                       f"{cd.human(done)} / {cd.human(total)}   {cd.human(speed)}/s{eta}")
        else:
            out.append(f"          {cd.human(done)} so far   {cd.human(speed)}/s")
        if cur.get("resumed_from"):
            out.append(f"          (resumed from {cd.human(cur['resumed_from'])})")
    elif state == "paused":
        out.append("Now     : PAUSED  (checking continues; choose Resume in the menu to download)")
    elif st.get("checking"):
        out.append("Now     : checking for new images...")
    else:
        out.append("Now     : idle, nothing to download")

    q = st.get("queue", [])
    if q:
        out.append(f"Queue   : {len(q)} waiting")
        for i, j in enumerate(q[:8], 1):
            out.append(f"          {i}. {j['board']:<10} {j['name']}" + ("  [force]" if j.get("force") else ""))
        if len(q) > 8:
            out.append(f"          ... and {len(q) - 8} more")
    else:
        out.append("Queue   : empty")

    c = st.get("config", {})
    nxt = st.get("next_check", 0) - time.time()
    chk = "ok" if st.get("last_check_ok") else ("FAILED" if st.get("last_check_ok") is False else "-")
    out.append(f"Checks  : last {tstr(st.get('last_check'))} ({chk}), "
               + (f"next in {dur(nxt)}" if nxt > 0 else "due now") + f"   every {c.get('interval')}s")
    out.append(f"Watching: {', '.join(c.get('boards', []))}   channel: {c.get('channel')}")
    if st.get("disk_free") is not None:
        out.append(f"Disk    : {cd.human(st['disk_free'])} free")
    if st.get("last_error"):
        out.append(f"Last error: {st['last_error']}")

    ev = st.get("events", [])[-6:]
    if ev:
        out.append("Recent  :")
        for e in ev:
            mark = "!" if e["level"] in ("ERROR", "WARNING") else " "
            out.append(f"  {mark} {tstr(e['t'])}  {e['msg']}")
    return "\n".join(out)


def cmd_status(watch: bool) -> int:
    if not watch:
        print(render_status())
        return 0
    if IS_WIN:
        os.system("")  # turns on ANSI escape handling in the Windows console
    try:
        while True:
            text = render_status()
            sys.stdout.write("\x1b[H\x1b[J" + text + "\n\n(Ctrl-C to exit)\n")
            sys.stdout.flush()
            time.sleep(1)
    except KeyboardInterrupt:
        print()
        return 0


# --------------------------------------------------------------------------- image listing / get

def available(boards: list[str] | None, channel: str | None) -> list[dict]:
    """Images from the live index for the given boards, with local state attached."""
    cfg = cd.load_config(P)
    if channel:
        cfg["channel"] = channel
    boards = [cd.normalize_board(b) for b in (boards or cfg["boards"])]
    print("Fetching release index...", file=sys.stderr)
    data, _ = cd.fetch_index()
    st = read_status() or {}
    cur = st.get("current") or {}
    queued = {j["url"]: n for n, j in enumerate(st.get("queue", []), 1)}
    rows = []
    for b in boards:
        if b not in data:
            print(f"warning: board '{b}' is not in the index", file=sys.stderr)
            continue
        for img in cd.get_images(data, b, cd.channel_filter(cfg)):
            name = img["url"].rsplit("/", 1)[-1]
            f = P.images / b / name
            part = P.images / b / (name + ".part")
            if f.exists():
                state = "ON DISK"
            elif cur.get("url") == img["url"]:
                state = "downloading"
            elif img["url"] in queued:
                state = f"queued #{queued[img['url']]}"
            elif part.exists():
                state = f"partial {cd.human(part.stat().st_size)}"
            else:
                state = ""
            rows.append({"board": b, "img": img, "name": name, "state": state, "url": img["url"]})
    return rows


def row_line(r: dict) -> str:
    i = r["img"]
    return (f"{r['url']}\t{r['board']:<10} {i['platform_version']:<13} chrome {i['chrome_version']:<14} "
            f"{cd.fmt_date(i['last_modified'])}  {i['channel'][:-8] if i['channel'].endswith('-channel') else i['channel']:<7} "
            f"{r['state']:<16} {r['name']}")


def cmd_list(boards, channel, n: int) -> int:
    rows = available(boards, channel)
    per: dict[str, int] = {}
    for r in rows:
        per[r["board"]] = per.get(r["board"], 0) + 1
        if per[r["board"]] <= n:
            print(row_line(r).split("\t", 1)[1])
    print(f"\n(newest {n} per board; use -n to show more)")
    return 0


def cmd_get(patterns, boards, channel, now: bool, force: bool) -> int:
    rows = available(boards, channel)
    if not rows:
        print("No images found.")
        return 1
    if patterns:
        chosen = []
        for pat in patterns:
            hits = [r for r in rows if pat.lower() in r["name"].lower()]
            if not hits:
                print(f"No image name contains '{pat}'.")
                return 1
            if len(hits) > 5:
                print(f"'{pat}' matches {len(hits)} images; be more specific, or use the menu to pick from a list.")
                return 1
            chosen += hits
    else:
        head = ("ENTER: download now" if now else "ENTER: add to queue") + (" (FORCE re-download)" if force else "") \
               + " | TAB: select several | ESC: cancel"
        keys = pick([row_line(r) for r in rows], "image> ", head, multi=True)
        by_url = {r["url"]: r for r in rows}
        chosen = [by_url[k] for k in keys]
    if not chosen:
        print("Nothing selected.")
        return 0
    if not force:
        have = [r for r in chosen if r["state"] == "ON DISK"]
        for r in have:
            print(f"Already on disk, skipping (use --force to download again): {r['name']}")
        chosen = [r for r in chosen if r["state"] != "ON DISK"]
        if not chosen:
            return 0
    return 0 if send("get", items=[{"url": r["url"], "board": r["board"]} for r in chosen],
                     now=now, force=force) else 1


# --------------------------------------------------------------------------- other commands

def cmd_cancel(pattern: str | None) -> int:
    if not pattern:
        return 0 if send("cancel") else 1
    st = read_status() or {}
    pool = ([st["current"]] if st.get("current") else []) + st.get("queue", [])
    hits = [j for j in pool if pattern.lower() in j["name"].lower()]
    if len(hits) != 1:
        print("No match." if not hits else f"'{pattern}' matches {len(hits)} items; be more specific.")
        return 1
    return 0 if send("cancel", url=hits[0]["url"]) else 1


def cmd_set(pairs: list[str], hint: bool = True) -> int:
    if not pairs:
        cfg = (read_status() or {}).get("config") or cd.load_config(P)
        for k, v in cfg.items():
            print(f"  {k:<17} {', '.join(v) if isinstance(v, list) else v}")
        if hint:
            print("\nChange with:  chromeos_ctl.py set interval=600 boards=octopus,dedede channel=any keep=2")
        return 0
    values = {}
    for p in pairs:
        if "=" not in p:
            print(f"Expected key=value, got '{p}'")
            return 1
        k, v = p.split("=", 1)
        values[k.strip()] = v.strip()
    return 0 if send("set", values=values) else 1


def cmd_logs(follow: bool, n: int = 40) -> int:
    if not P.log.exists():
        print("No log yet.")
        return 1
    with open(P.log, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
        print("".join(lines[-n:]), end="")
        if not follow:
            return 0
        try:
            while True:
                line = f.readline()
                if line:
                    print(line, end="")
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            return 0


# --------------------------------------------------------------------------- flashing (Windows)

def _ps(script: str, check: bool = True) -> str:
    """Run PowerShell. The script is passed encoded so quoting and newlines survive."""
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"PowerShell failed: {r.stderr.strip()[:400]}")
    return r.stdout.strip()


def windows_drives() -> list[dict]:
    """USB / SD / MMC disks, never the boot or system disk, never one holding the system drive letter."""
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$sys = $env:SystemDrive.TrimEnd(':')
$out = @()
foreach ($d in (Get-Disk | Where-Object { $_.BusType -in 'USB','SD','MMC' -and -not $_.IsBoot -and -not $_.IsSystem })) {
    $parts = @(Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue | Where-Object { $_.DriveLetter })
    $letters = @($parts | ForEach-Object { [string]$_.DriveLetter })
    if ($letters -contains $sys) { continue }
    $vols = @()
    foreach ($l in $letters) {
        $v = Get-Volume -DriveLetter $l -ErrorAction SilentlyContinue
        $vols += ('{0}: {1}' -f $l, $v.FileSystemLabel)
    }
    $out += [pscustomobject]@{ Number = [int]$d.Number; Model = [string]$d.FriendlyName; Size = [int64]$d.Size; Letters = $letters; Volumes = $vols; Sector = [int]$d.LogicalSectorSize }
}
ConvertTo-Json -InputObject @($out) -Compress -Depth 4
"""
    raw = _ps(script)
    if not raw:
        return []
    items = json.loads(raw)
    items = [items] if isinstance(items, dict) else [i for i in items if i]
    for i in items:
        for k in ("Letters", "Volumes"):
            if isinstance(i.get(k), str):
                i[k] = [i[k]]
            i[k] = i.get(k) or []
    return items


def _win_kernel32():
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                    wintypes.LPVOID]
    k32.DeviceIoControl.restype = wintypes.BOOL
    k32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    k32.WriteFile.restype = wintypes.BOOL
    k32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    k32.FlushFileBuffers.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


def _win_lock_volumes(letters: list[str]) -> list[int]:
    """Lock + dismount each volume so Windows lets us overwrite the disk. Held until handles close."""
    import ctypes
    from ctypes import wintypes
    k32 = _win_kernel32()
    GENERIC_READ_WRITE, SHARE_READ_WRITE, OPEN_EXISTING = 0xC0000000, 0x3, 3
    FSCTL_LOCK_VOLUME, FSCTL_DISMOUNT_VOLUME = 0x00090018, 0x00090020
    invalid = ctypes.c_void_p(-1).value
    handles: list[int] = []
    ret = wintypes.DWORD()
    try:
        for letter in letters:
            h = k32.CreateFileW(f"\\\\.\\{letter}:", GENERIC_READ_WRITE, SHARE_READ_WRITE, None,
                                OPEN_EXISTING, 0, None)
            if not h or h == invalid:
                raise RuntimeError(f"could not open volume {letter}: (error {ctypes.get_last_error()})")
            handles.append(h)
            if not k32.DeviceIoControl(h, FSCTL_LOCK_VOLUME, None, 0, None, 0, ctypes.byref(ret), None):
                raise RuntimeError(f"could not lock volume {letter}:. Close Explorer windows and any program "
                                   f"using that drive, then try again.")
            k32.DeviceIoControl(h, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0, ctypes.byref(ret), None)
    except Exception:
        _win_close(handles)
        raise
    return handles


def _win_close(handles: list[int]) -> None:
    k32 = _win_kernel32()
    for h in handles:
        k32.CloseHandle(h)


def _win_error(code: int) -> str:
    import ctypes
    fmt = getattr(ctypes, "FormatError", None)
    return f"Windows error {code}" + (f": {fmt(code).strip()}" if fmt and code else "")


def _last_error() -> int:
    import ctypes
    return ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0


FLASH_LOG = P.state / "flash.log"


def flash_log(msg: str) -> None:
    try:
        P.state.mkdir(parents=True, exist_ok=True)
        with open(FLASH_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    except OSError:
        pass


class _RawDisk:
    """Writes to a physical drive through the Win32 API so a failure reports the real Windows error code."""

    def __init__(self, path: str, k32=None):
        self.k32 = k32 or _win_kernel32()
        self.h = None
        GENERIC_READ_WRITE, SHARE_READ_WRITE, OPEN_EXISTING = 0xC0000000, 0x3, 3
        h = self.k32.CreateFileW(path, GENERIC_READ_WRITE, SHARE_READ_WRITE, None, OPEN_EXISTING, 0, None)
        if not h or h == -1 or h == 0xFFFFFFFFFFFFFFFF:
            code = _last_error()
            if code == 5:
                raise PermissionError(f"could not open {path} ({_win_error(code)})")
            raise OSError(f"could not open {path} ({_win_error(code)})")
        self.h = h

    def write(self, data: bytes) -> None:
        import ctypes
        from ctypes import wintypes
        buf = ctypes.create_string_buffer(data, len(data))
        done = wintypes.DWORD()
        if not self.k32.WriteFile(self.h, buf, len(data), ctypes.byref(done), None):
            raise OSError(_win_error(_last_error()))
        if done.value != len(data):
            raise OSError(f"short write ({done.value} of {len(data)} bytes)")

    def flush(self) -> None:
        self.k32.FlushFileBuffers(self.h)

    def close(self) -> None:
        if self.h is not None:
            self.k32.CloseHandle(self.h)
            self.h = None


_DIAG_PS = r"""
$ErrorActionPreference = 'Continue'
Get-Disk -Number @@N@@ | Format-List Number,FriendlyName,BusType,OperationalStatus,HealthStatus,IsOffline,IsReadOnly,PartitionStyle,LogicalSectorSize,PhysicalSectorSize | Out-String
'--- recent disk/USB events (last 10 min) ---'
Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=(Get-Date).AddMinutes(-10)} -ErrorAction SilentlyContinue |
  Where-Object { $_.ProviderName -match 'disk|stor|usb|partmgr|volmgr|nvme' } | Select-Object -First 12 |
  ForEach-Object { '{0}  {1} {2}: {3}' -f $_.TimeCreated, $_.ProviderName, $_.Id, (($_.Message -split "`n")[0]) }
"""


def _flash_diagnostics(num: int) -> None:
    try:
        flash_log("disk state after failure:\n" + _ps(_DIAG_PS.replace("@@N@@", str(num)), check=False))
    except Exception as e:  # noqa: BLE001 - diagnostics must never hide the real error
        flash_log(f"could not collect diagnostics: {e}")


def _read_full(f, n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        piece = f.read(n - len(out))
        if not piece:
            break
        out += piece
    return bytes(out)


FLASHED = P.state / "flashed.json"      # models you have flashed before (newest first)
DAY = 86400


def load_models_cache() -> dict[str, list[str]]:
    """board -> raw model names. The daemon rewrites this whenever the release index changes (it checks every few
    minutes). If the daemon has not checked successfully for a day, or the cache is missing, refresh it here."""
    st = read_status() or {}
    kept_fresh = bool(st.get("last_check_ok")) and time.time() - (st.get("last_check") or 0) < DAY
    young = P.models.exists() and time.time() - P.models.stat().st_mtime < DAY
    if not P.models.exists() or not (kept_fresh or young):
        try:
            data, _ = cd.fetch_index()
            cache = {b: v.get("brand_names", []) for b, v in (data or {}).items() if isinstance(v, dict)}
            cd.atomic_write_json(P.models, cache)
            return cache
        except Exception as e:  # noqa: BLE001 - an old list is better than none
            if P.models.exists():
                print(f"(could not refresh the model list: {e}; using the saved one)", file=sys.stderr)
            else:
                print(f"(no model list available: {e})", file=sys.stderr)
    return cd.read_json(P.models, {}) or {}


def board_models(boards: set[str]) -> dict[str, list[str]]:
    """Individual compatible model names per board."""
    cache = load_models_cache()
    return {b: sorted(cd.split_models(cache.get(b, [])), key=str.lower) for b in boards}


def flash_choices(zips: list[Path]) -> tuple[list[str], dict[str, tuple[Path, str, str | None, int]]]:
    """One picker line per (image, compatible model): 'MODEL-board\\file.zip (size)'. Typing a model narrows it down."""
    models = board_models({z.parent.name for z in zips})
    lines, meta = [], {}
    for i, z in enumerate(zips):
        board = z.parent.name
        names = models.get(board) or [None]
        for j, m in enumerate(names):
            key = f"{i}|{j}"
            label = (f"{m}-" if m else "") + f"{board}\\{z.name}"
            lines.append(f"{key}\t{label}   ({cd.human(z.stat().st_size)})")
            meta[key] = (z, board, m, len(models.get(board) or []))
    return lines, meta


def flash_windows(zip_path: Path, drive: dict, model: str | None = None, n_models: int = 0) -> bool:
    num = int(drive["Number"])
    ident = f"PhysicalDrive{num}"
    target = rf"\\.\{ident}"
    size = int(drive["Size"])
    with zipfile.ZipFile(zip_path) as zf:
        member = next((i for i in zf.infolist() if i.filename.lower().endswith(".bin")), None)
        if member is None:
            print("No .bin file inside that zip, aborting.", file=sys.stderr)
            return False
        if member.file_size > size:
            print(f"Image ({cd.human(member.file_size)}) is bigger than {ident} ({cd.human(size)}), aborting.",
                  file=sys.stderr)
            return False
        print("\n" + "=" * 64)
        print(f"  Image : {zip_path.name}")
        print(f"          -> {member.filename} ({cd.human(member.file_size)})")
        if model:
            print(f"  Model : {model}")
            print(f"          (board {zip_path.parent.name}; {n_models} models are listed for this board)")
        print(f"  Drive : {ident}   {cd.human(size)}   {drive.get('Model', '')}")
        print(f"  Holds : {', '.join(drive['Volumes']) or 'no drive letters'}")
        print("  ALL DATA ON THIS DRIVE WILL BE ERASED.")
        print("=" * 64)
        if input(f"Type {ident} to confirm, anything else cancels: ").strip() != ident:
            print("Cancelled.")
            return False
        sector = max(int(drive.get("Sector") or SECTOR), SECTOR)
        flash_log(f"START {zip_path.name} -> {ident} ({drive.get('Model', '')}, {size} bytes, sector {sector}, "
                  f"letters {drive['Letters']})")
        handles: list[int] = []
        raw = None
        done = 0
        try:
            # Remove the old partition table first so Windows has no volumes on this disk to react to
            # when the new partition table lands in the first sectors.
            print("\nPreparing the drive...")
            # Windows can renumber drives when USB devices come and go: make sure this number is still the
            # same removable disk that was shown above before anything is erased.
            same = _ps(f"$d = Get-Disk -Number {num} -ErrorAction SilentlyContinue; "
                       f"if ($d -and -not $d.IsBoot -and -not $d.IsSystem -and $d.BusType -in 'USB','SD','MMC' "
                       f"-and $d.Size -eq [uint64]{size}) {{ 'same' }} else {{ 'different' }}", check=False)
            if "same" not in same:
                print(f"\n{ident} is no longer the drive that was shown (it was unplugged or renumbered). "
                      "Nothing was changed. Start the flash again.", file=sys.stderr)
                flash_log(f"ABORT {ident} changed before writing: {same!r}")
                return False
            out = _ps(f"Set-Disk -Number {num} -IsOffline $false -ErrorAction SilentlyContinue; "
                      f"Set-Disk -Number {num} -IsReadOnly $false -ErrorAction SilentlyContinue; "
                      f"Clear-Disk -Number {num} -RemoveData -RemoveOEM -Confirm:$false -ErrorAction Stop; 'cleared'",
                      check=False)
            flash_log(f"clear disk: {out or 'no output (failed or already clean)'}")
            if "cleared" not in out:
                handles = _win_lock_volumes(drive["Letters"])    # fall back to locking what we can see
            time.sleep(1)
            print("Flashing, do not unplug the drive...")
            total, t0 = member.file_size, time.monotonic()
            raw = _RawDisk(target)
            with zf.open(member) as src:
                while True:
                    data = _read_full(src, CHUNK)
                    if not data:
                        break
                    real = len(data)
                    data += b"\0" * (-real % sector)       # raw writes must be whole sectors
                    raw.write(data)
                    done += real
                    rate = done / max(time.monotonic() - t0, 0.001)
                    print(f"\r  {bar(done / max(total, 1), 28)} {done * 100 // max(total, 1):3d}%  "
                          f"{cd.human(done)} / {cd.human(total)}  {cd.human(rate)}/s   ", end="", flush=True)
            print()
            raw.flush()
            flash_log(f"wrote {done} bytes OK")
        except PermissionError as e:
            print(f"\nAccess denied: {e}. Use an Administrator terminal and close anything using the drive.",
                  file=sys.stderr)
            flash_log(f"FAILED access denied: {e}")
            return False
        except (OSError, RuntimeError) as e:
            print(f"\nFlashing failed: {e}", file=sys.stderr)
            print("The drive is now partly written and will not boot a Chromebook. Run the flash again.\n"
                  f"Details were saved to {FLASH_LOG}", file=sys.stderr)
            flash_log(f"FAILED after {done} bytes: {e}")
            _flash_diagnostics(num)
            return False
        finally:
            if raw:
                raw.close()
            if handles:
                _win_close(handles)
    _ps(f"Update-Disk -Number {num} -ErrorAction SilentlyContinue", check=False)   # refresh Windows' view
    print(f"\nDone. {ident} is ready. Eject it from Explorer, then unplug it.")
    return True


def relaunch_elevated(args: list[str]) -> bool:
    """Run this script again in a new Administrator console (Windows shows the UAC prompt)."""
    import ctypes
    import subprocess as sp
    params = sp.list2cmdline([str(Path(__file__).resolve())] + args)
    # python.exe (a console program) gets its own window; ShellExecute returns > 32 on success.
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, str(HERE), 1)
    return rc > 32


def cmd_flash(pattern: str | None, elevated: bool = False) -> int:
    if not IS_WIN:
        print("Flashing is only implemented for Windows in this version.")
        return 1
    if not is_admin():
        if elevated:
            print("Still not running as Administrator, so flashing cannot continue.")
            return 1
        print("Flashing needs Administrator rights. Opening a new window - approve the Windows prompt...")
        if not relaunch_elevated(["flash", "--elevated", "--pause"] + ([pattern] if pattern else [])):
            print("The elevated window did not open (prompt declined?). Nothing was changed.")
            return 1
        print("Continue in the new window. Come back here when it says it is done.")
        return 0
    zips = sorted((p for p in P.images.rglob("*.zip") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    if pattern:
        zips = [z for z in zips if pattern.lower() in z.name.lower()]
    if not zips:
        print("No downloaded zip files found" + (f" matching '{pattern}'." if pattern else ". Download one first."))
        return 1
    model, n_models = None, 0
    if len(zips) == 1 and pattern:
        zip_path = zips[0]
    else:
        lines, meta = flash_choices(zips)
        pk = pick(lines, "image> ", "type your Chromebook model | newest first | ENTER: choose, ESC: cancel")
        if not pk:
            print("No image selected.")
            return 0
        zip_path, _board, model, n_models = meta[pk[0]]
    return flash_to_drive(zip_path, model, n_models)


def remember_flash(board: str, model: str, image_name: str) -> None:
    """Record a successful flash and make sure the daemon keeps watching that board from now on."""
    hist = [h for h in (cd.read_json(FLASHED, []) or []) if not (h.get("board") == board and h.get("model") == model)]
    hist.insert(0, {"board": board, "model": model, "image": image_name, "time": time.time()})
    cd.atomic_write_json(FLASHED, hist[:200])
    cfg = (read_status() or {}).get("config") or cd.load_config(P)
    if board not in cfg["boards"]:
        if send("set", values={"boards": ",".join(cfg["boards"] + [board])}):
            print(f"Now watching board '{board}': new releases for {model} will download automatically.")


def flash_to_drive(zip_path: Path, model: str | None = None, n_models: int = 0) -> int:
    """Pick a USB drive, flash zip_path to it, and (if the model is known) remember it."""
    try:
        drives = windows_drives()
    except RuntimeError as e:
        print(e)
        return 1
    if not drives:
        print("No USB/SD drives found (the boot/system disk is never listed). Plug one in and try again.")
        return 1
    lines = []
    for d in drives:
        vols = ", ".join(d["Volumes"]) or "no drive letter"
        lines.append(f"{d['Number']}\tPhysicalDrive{d['Number']:<3} {cd.human(int(d['Size'])):>10}  "
                     f"{d.get('Model', '')}  [{vols}]")
    pk = pick(lines, "drive> ", "USB/SD drives only | ENTER: choose drive to ERASE, ESC: cancel")
    if not pk:
        print("No drive selected.")
        return 0
    drive = next(d for d in drives if str(d["Number"]) == pk[0])
    if not flash_windows(zip_path, drive, model, n_models):
        return 1
    if model:
        remember_flash(zip_path.parent.name, model, zip_path.name)
    return 0




def model_choices() -> tuple[list[str], dict[str, tuple[str, str]]]:
    """Picker lines for every model on every board; models you flashed before come first."""
    mine = {(h["board"], h["model"]) for h in (cd.read_json(FLASHED, []) or [])}
    items = []
    for board, names in load_models_cache().items():
        for m in cd.split_models(names):
            items.append((board, m))
    items.sort(key=lambda bm: (bm not in mine, bm[1].lower(), bm[0]))
    lines, meta = [], {}
    for n, (board, m) in enumerate(items):
        lines.append(f"{n}\t{m}   (board: {board}){'   [flashed before]' if (board, m) in mine else ''}")
        meta[str(n)] = (board, m)
    return lines, meta


def newest_image(data: dict, board: str) -> tuple[dict | None, str | None]:
    imgs = cd.get_images(data, board, "stable-channel")
    if imgs:
        return imgs[0], "stable-channel"
    imgs = cd.get_images(data, board, None)
    return (imgs[0], imgs[0]["channel"]) if imgs else (None, None)


def wait_for_download(url: str, board: str, name: str) -> bool:
    """Show live progress until the daemon has finished downloading url. Ctrl-C stops waiting, not the download."""
    dest = P.images / board / name
    missing_since = None
    try:
        while True:
            if dest.exists():
                print()
                return True
            if not daemon_running():
                print("\nThe daemon stopped, so the download cannot continue.")
                return False
            st = read_status() or {}
            cur = st.get("current") or {}
            queue = st.get("queue", [])
            if cur.get("url") == url:
                missing_since = None
                done, total, speed = cur.get("done", 0), cur.get("total", 0), cur.get("speed", 0)
                eta = f" ETA {dur((total - done) / speed)}" if total and speed > 1000 else ""
                line = (f"  {bar(done / total, 26)} {done * 100 // total:3d}%  {cd.human(done)} / {cd.human(total)}  "
                        f"{cd.human(speed)}/s{eta}") if total else f"  {cd.human(done)} so far  {cd.human(speed)}/s"
            elif any(j["url"] == url for j in queue):
                missing_since = None
                n = [j["url"] for j in queue].index(url) + 1
                line = ("  downloads are PAUSED - choose Resume in the image options menu" if st.get("config", {}).get("paused")
                        else f"  waiting in the queue (#{n})")
            else:
                missing_since = missing_since or time.time()
                if time.time() - missing_since > 8:
                    print(f"\nThe download did not start or failed. {st.get('last_error') or 'See the log (menu: Show recent log).'}")
                    return False
                line = "  starting..."
            print("\r" + line.ljust(79), end="", flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopped waiting. The download keeps going in the background.")
        return False


def cmd_find(elevated: bool = False) -> int:
    """Search a model, download the newest image for its board, flash it, remember it."""
    if not IS_WIN:
        print("Flashing is only implemented for Windows in this version.")
        return 1
    if not is_admin():
        if elevated:
            print("Still not running as Administrator, so this cannot continue.")
            return 1
        print("This needs Administrator rights. Opening a new window - approve the Windows prompt...")
        return 0 if relaunch_elevated(["find", "--elevated", "--pause"]) else 1
    lines, meta = model_choices()
    if not lines:
        print("No model list yet. Make sure the daemon has run once (it downloads the list), then try again.")
        return 1
    pk = pick(lines, "model> ", "type your Chromebook model | ENTER: choose | ESC: cancel")
    if not pk:
        print("Nothing selected.")
        return 0
    board, model = meta[pk[0]]
    print(f"\n{model}  ->  board {board}\nLooking up the newest image...")
    data, _ = cd.fetch_index()
    img, channel = newest_image(data or {}, board)
    if not img:
        print(f"No recovery image is published for board {board}.")
        return 1
    name = img["url"].rsplit("/", 1)[-1]
    print(f"Newest: {img['platform_version']}  (Chrome {img['chrome_version']}, {channel}, {cd.fmt_date(img['last_modified'])})")
    if channel != "stable-channel":
        print("  note: no stable image exists for this board, so this is the newest from another channel.")
    dest = P.images / board / name
    if dest.exists():
        print("Already downloaded.")
    else:
        if not send("get", items=[{"url": img["url"], "board": board}], now=True, force=False):
            return 1
        if not wait_for_download(img["url"], board, name):
            return 1
    n_models = len(cd.split_models(load_models_cache().get(board, [])))
    return flash_to_drive(dest, model, n_models)


# --------------------------------------------------------------------------- menu

def getkey() -> str:
    """One key press (Windows). Elsewhere (testing) type the number and press Enter."""
    if IS_WIN:
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):          # arrow / function key: swallow its second code
            msvcrt.getwch()
            return ""
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch
    return input("").strip()[:1]


def status_oneliner() -> str:
    """One line saying what the daemon is doing right now (shown inside the menus)."""
    st = read_status() or {}
    if not daemon_running():
        return "daemon NOT running"
    cur = st.get("current")
    q = len(st.get("queue", []))
    more = f", {q} queued" if q else ""
    if cur:
        done, total, speed = cur.get("done", 0), cur.get("total", 0), cur.get("speed", 0)
        bits = [f"downloading {cur['board']}"]
        if total:
            bits.append(f"{done * 100 // total}%")
        if speed > 1000:
            bits.append(f"{cd.human(speed)}/s")
            if total:
                bits.append(f"ETA {dur((total - done) / speed)}")
        return " ".join(bits) + more
    if st.get("config", {}).get("paused"):
        return "paused" + more
    if st.get("checking"):
        return "checking for new images..." + more
    nxt = st.get("next_check", 0) - time.time()
    fail = " (last check FAILED)" if st.get("last_check_ok") is False else ""
    return "idle" + more + (f", next check in {dur(nxt)}" if nxt > 0 else ", checking now") + fail


def wait_key(render) -> str:
    """Show render() and wait for a key. On Windows the screen redraws every second so it stays live."""
    if not IS_WIN:
        if sys.stdout.isatty():
            sys.stdout.write("\x1b[2J\x1b[H")
        print(render(), end="", flush=True)
        k = getkey()
        print()
        return k
    import msvcrt
    sys.stdout.write("\x1b[2J")
    last, last_t = None, 0.0
    while True:
        if time.time() - last_t >= 1.0:
            last_t = time.time()
            text = render()
            if text != last:
                last = text
                # cursor home, clear each line's tail, clear everything below: redraws without flicker
                sys.stdout.write("\x1b[H" + "\x1b[K\n".join(text.split("\n")) + "\x1b[J")
                sys.stdout.flush()
        if msvcrt.kbhit():
            k = getkey()
            sys.stdout.write("\x1b[2J\x1b[H")      # actions print on a clean screen
            sys.stdout.flush()
            return k
        time.sleep(0.05)


def show_menu(title: str, entries_fn, back_label: str, with_status: bool = False) -> str | None:
    """Numbered menu. Press a number to choose; 0 (or Esc) goes back. Returns the key, or None for back.
    entries_fn() returns [(key, label), ...] and is re-evaluated on every redraw."""
    def render() -> str:
        lines = [title + (f"   [{status_oneliner()}]" if with_status else ""), ""]
        lines += [f"  {k}) {label}" for k, label in entries_fn()]
        lines += [f"  0) {back_label}", "", "Press a number: "]
        return "\n".join(lines)

    while True:
        k = wait_key(render)
        if k in ("0", "\x1b"):
            return None
        if k in {key for key, _ in entries_fn()}:
            return k


def _pause() -> None:
    try:
        input("\nPress Enter to continue...")
    except EOFError:
        pass


def image_options_menu() -> None:
    while True:
        def entries():
            paused = bool((read_status() or {}).get("config", {}).get("paused"))
            return [
                ("1", "Download an image NOW (interrupts the current one)"),
                ("2", "Add images to the queue"),
                ("3", "Force re-download of an image"),
                ("4", "Check for new images now"),
                ("5", "Resume downloading" if paused else "Pause downloading"),
                ("6", "Skip the current download"),
                ("7", "Clear the queue"),
            ]

        k = show_menu("ChromeOS image options", entries, "Back", with_status=True)
        if k is None:
            return
        paused = bool((read_status() or {}).get("config", {}).get("paused"))
        try:
            if k == "1":
                cmd_get([], None, None, True, False)
            elif k == "2":
                cmd_get([], None, None, False, False)
            elif k == "3":
                cmd_get([], None, None, True, True)
            elif k == "4":
                send("refresh")
            elif k == "5":
                send("resume" if paused else "pause")
            elif k == "6":
                cmd_cancel(None)
            elif k == "7":
                send("clear")
        except (KeyboardInterrupt, EOFError):
            print()
        except Exception as e:  # noqa: BLE001 - keep the menu alive
            print(f"error: {e}")
        _pause()


def cmd_menu() -> int:
    while True:
        k = show_menu("ChromeOS Recovery Watch", lambda: [
            ("1", f"Live status  -  {status_oneliner()}"),
            ("2", "Find my Chromebook: search model, download, flash"),
            ("3", "Flash an already-downloaded image"),
            ("4", "ChromeOS image options"),
            ("5", "Settings (boards, interval, channel)"),
            ("6", "Show recent log"),
            ("7", "Restart the daemon"),
        ], "Quit")
        if k is None:
            return 0
        try:
            if k == "1":
                cmd_status(True)
                continue
            if k == "4":
                image_options_menu()
                continue
            if k == "2":
                cmd_find()
            elif k == "3":
                cmd_flash(None)
            elif k == "5":
                print("Settings\n")
                cmd_set([], hint=False)
                print("\nTo change, type one or more, e.g.:  interval=600 keep=2 boards=octopus,dedede channel=any")
                raw = input("Change (blank to skip): ").strip()
                if raw:
                    cmd_set(raw.split())
            elif k == "6":
                cmd_logs(False)
            elif k == "7":
                send("restart")
        except (KeyboardInterrupt, EOFError):
            print()
        except Exception as e:  # noqa: BLE001 - keep the menu alive
            print(f"error: {e}")
        _pause()


# --------------------------------------------------------------------------- entry point

def main() -> int:
    if IS_WIN:
        os.system("")
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="chromeos_ctl", description="Control the ChromeOS recovery watcher.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("status", help="show what the daemon is doing")
    p.add_argument("-w", "--watch", action="store_true", help="live view")

    p = sub.add_parser("list", help="list available images")
    p.add_argument("-b", "--boards", nargs="+")
    p.add_argument("-c", "--channel", help="e.g. stable-channel, beta-channel, any")
    p.add_argument("-n", type=int, default=10, help="images per board to show (default 10)")

    p = sub.add_parser("get", help="download images now (or queue them)")
    p.add_argument("text", nargs="*", help="part of the file name; omit to use the picker")
    p.add_argument("-b", "--boards", nargs="+")
    p.add_argument("-c", "--channel")
    p.add_argument("-q", "--queue", action="store_true", help="add to the end of the queue instead of starting now")
    p.add_argument("-f", "--force", action="store_true", help="delete the local copy and download again")

    for name, h in (("refresh", "check for new images now"), ("pause", "pause downloading"),
                    ("resume", "resume downloading"), ("clear", "empty the queue"),
                    ("restart", "restart the daemon")):
        sub.add_parser(name, help=h)

    p = sub.add_parser("cancel", help="skip the current download (or a queued one)")
    p.add_argument("text", nargs="?")

    p = sub.add_parser("set", help="show or change settings")
    p.add_argument("pairs", nargs="*", help="key=value ...")

    p = sub.add_parser("logs", help="show the daemon log")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("-n", type=int, default=40)

    p = sub.add_parser("find", help="search your Chromebook model, download its image and flash it (Windows)")
    p.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)

    p = sub.add_parser("flash", help="write a downloaded image to a USB drive (Windows, asks for admin)")
    p.add_argument("text", nargs="?", help="part of the image file name")
    p.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)   # set by the self-elevation
    p.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)      # keep the new window open at the end

    a = ap.parse_args()
    try:
        if a.cmd is None:
            return cmd_menu()
        if a.cmd == "status":
            return cmd_status(a.watch)
        if a.cmd == "list":
            return cmd_list(a.boards, a.channel, a.n)
        if a.cmd == "get":
            return cmd_get(a.text, a.boards, a.channel, not a.queue, a.force)
        if a.cmd in ("refresh", "pause", "resume", "clear", "restart"):
            return 0 if send(a.cmd) else 1
        if a.cmd == "cancel":
            return cmd_cancel(a.text)
        if a.cmd == "set":
            return cmd_set(a.pairs)
        if a.cmd == "logs":
            return cmd_logs(a.follow, a.n)
        if a.cmd == "find":
            try:
                return cmd_find(a.elevated)
            finally:
                if a.pause:
                    input("\nPress Enter to close this window...")
        if a.cmd == "flash":
            try:
                return cmd_flash(a.text, a.elevated)
            finally:
                if a.pause:
                    input("\nPress Enter to close this window...")
    except KeyboardInterrupt:
        print()
        return 130
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
