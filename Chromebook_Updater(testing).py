#!/usr/bin/env python3
"""
Pick ChromeOS recovery images with fzf and download the zips.

Data source: https://github.com/MercuryWorkshop/chromeos-releases-data

Usage:
    ./chromeos_dl.py                       # stable-channel only: octopus, dedede, skywalker, corsola
    ./chromeos_dl.py -b octopus dedede     # only these boards
    ./chromeos_dl.py -c any                # show every channel (beta, ltc, lts, ...)
    ./chromeos_dl.py -c beta-channel       # a specific channel
    ./chromeos_dl.py -o ~/Downloads/cros   # custom output dir

fzf keys: TAB = multi-select, ENTER = confirm, ESC = skip.

Flashing is linux only

GPT-OSS:20B did help make this using opencode on my own machine
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

DATA_URL = "https://cdn.jsdelivr.net/gh/MercuryWorkshop/chromeos-releases-data/data.json"
DEFAULT_BOARDS = ["octopus", "dedede", "skywalker", "corsola"]

ALIASES = {"crosola": "corsola"}
UA = {"User-Agent": "Mozilla/5.0 (chromeos_dl.py)"}

SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/efi", "/home", "/usr", "/var", "/opt", "/nix"}

def fzf_pick(lines: list[str], prompt: str, header: str = "", multi: bool = False) -> list[str]:
    """Show `lines` in fzf. Field 1 (tab separated) is a hidden key; returns the chosen keys."""
    cmd = [
        "fzf",
        "--delimiter", "\t",
        "--with-nth", "2..",
        "--prompt", prompt,
        "--header", header,
        "--reverse",
        "--height", "80%",
    ]
    if multi:
        cmd.append("--multi")
    proc = subprocess.run(cmd, input="\n".join(lines), text=True, stdout=subprocess.PIPE)
    if proc.returncode != 0:  # 1 = no match, 130 = ESC / Ctrl-C
        return []
    return [row.split("\t", 1)[0] for row in proc.stdout.splitlines() if row.strip()]

def fetch_data() -> dict:
    print(f"Fetching {DATA_URL} ...")
    req = urllib.request.Request(DATA_URL, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def get_images(data: dict, board: str, channel: str | None) -> list[dict]:
    """Real image entries for a board (skips the credit placeholder entry)."""
    out = []
    for img in data[board].get("images", []):
        url = img.get("url", "")
        # Must be a zip, and the URL must really belong to this board.
        if not url.endswith(".zip") or f"_{board}_recovery_" not in url:
            continue
        if channel and img.get("channel") != channel:
            continue
        out.append(img)
    out.sort(key=lambda i: i.get("last_modified", 0), reverse=True)
    return out


def fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def download(url: str, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / url.rsplit("/", 1)[-1]
    if dest.exists():
        print(f"  already have {dest.name}, skipping")
        return

    part = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while chunk := r.read(1024 * 256):
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  {dest.name}: {done / 1e6:7.1f} / {total / 1e6:.1f} MB "
                          f"({done * 100 // total}%)", end="", flush=True)
                else:
                    print(f"\r  {dest.name}: {done / 1e6:7.1f} MB", end="", flush=True)
        part.rename(dest)
        print(f"\n  saved -> {dest}")
    except Exception as e:
        part.unlink(missing_ok=True)
        print(f"\n  FAILED {url}: {e}", file=sys.stderr)

def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1000
    return str(n)


def _truthy(v) -> bool:
    return v in (True, 1, "1", "true")


def _mounts(dev: dict, key: str) -> list[str]:
    m = dev.get(key)
    if isinstance(m, str):
        m = [m]
    out = [x for x in (m or []) if x]
    for child in dev.get("children") or []:
        out += _mounts(child, key)
    return out


def lsblk_devices() -> tuple[list[dict], str]:
    """Return (block devices, name of the mountpoint key). Handles old and new util-linux."""
    for mp_col in ("MOUNTPOINTS", "MOUNTPOINT"):
        cols = f"NAME,PATH,SIZE,MODEL,VENDOR,TRAN,RM,HOTPLUG,TYPE,{mp_col}"
        r = subprocess.run(["lsblk", "-J", "-b", "-o", cols], capture_output=True, text=True)
        if r.returncode == 0:
            return json.loads(r.stdout)["blockdevices"], mp_col.lower()
    sys.exit("lsblk failed; is it installed?")


def usb_candidates(devices: list[dict], mp_key: str) -> list[dict]:
    """Removable / hotplug / USB whole disks that have no system mount on them."""
    out = []
    for d in devices:
        if d.get("type") != "disk":
            continue
        if not (_truthy(d.get("rm")) or _truthy(d.get("hotplug")) or d.get("tran") == "usb"):
            continue
        if any(m in SYSTEM_MOUNTS or m.startswith("/boot") for m in _mounts(d, mp_key)):
            continue
        out.append(d)
    return out


def find_zips(root: Path) -> list[Path]:
    zips = [p for p in root.rglob("*.zip") if p.is_file()]
    return sorted(zips, key=lambda p: p.stat().st_mtime, reverse=True)


def flash(zip_path: Path, dev: dict, mp_key: str) -> None:
    path, size = dev["path"], int(dev["size"])
    label = " ".join(filter(None, [(dev.get("vendor") or "").strip(), (dev.get("model") or "").strip()]))

    with zipfile.ZipFile(zip_path) as zf:
        member = next((i for i in zf.infolist() if i.filename.lower().endswith(".bin")), None)
        if member is None:
            print("No .bin file inside that zip, aborting.", file=sys.stderr)
            return
        if member.file_size > size:
            print(f"Image ({human(member.file_size)}) is bigger than {path} ({human(size)}), aborting.",
                  file=sys.stderr)
            return

        print("\n" + "=" * 60)
        print(f"  Image : {zip_path.name}  ->  {member.filename} ({human(member.file_size)})")
        print(f"  Drive : {path}  {human(size)}  {label}")
        print("  ALL DATA ON THIS DRIVE WILL BE ERASED.")
        print("=" * 60)
        if input(f"Type the device path ({path}) to confirm, anything else cancels: ").strip() != path:
            print("Cancelled.")
            return

        sudo = [] if os.geteuid() == 0 else ["sudo"]
        if sudo:
            subprocess.run(["sudo", "-v"], check=True)  # ask for the password up front

        for mnt in sorted(set(_mounts(dev, mp_key)), reverse=True):
            subprocess.run(sudo + ["umount", mnt], check=False)

        print("\nFlashing, do not unplug the drive...")
        dd = subprocess.Popen(
            sudo + ["dd", f"of={path}", "bs=4M", "iflag=fullblock", "conv=fsync", "status=progress"],
            stdin=subprocess.PIPE,
        )
        try:
            with zf.open(member) as src:
                while chunk := src.read(4 * 1024 * 1024):
                    dd.stdin.write(chunk)
            dd.stdin.close()
        except BrokenPipeError:
            print("\ndd stopped early (see the error above).", file=sys.stderr)
        rc = dd.wait()

    subprocess.run(sudo + ["sync"], check=False)
    if rc == 0:
        print(f"\nDone. {path} is ready, you can unplug it.")
    else:
        print(f"\ndd exited with code {rc}; the drive may be incomplete.", file=sys.stderr)


def flash_flow(out_dir: Path) -> None:
    if not sys.platform.startswith("linux"):
        print("Flashing is only supported on Linux, skipping.")
        return
    zips = find_zips(out_dir)
    if not zips:
        print(f"No zip files found under {out_dir}, nothing to flash.")
        return

    lines = [f"{i}\t{p.relative_to(out_dir)}   ({human(p.stat().st_size)})" for i, p in enumerate(zips)]
    pick = fzf_pick(lines, "image> ", "newest first | ENTER: choose image to flash, ESC: cancel")
    if not pick:
        print("No image selected.")
        return
    zip_path = zips[int(pick[0])]

    devices, mp_key = lsblk_devices()
    cands = usb_candidates(devices, mp_key)
    if not cands:
        print("No removable/USB drives found. Plug one in and run again with --flash-only.")
        return
    lines = []
    for d in cands:
        label = " ".join(filter(None, [(d.get("vendor") or "").strip(), (d.get("model") or "").strip()]))
        note = "  (mounted)" if _mounts(d, mp_key) else ""
        lines.append(f"{d['path']}\t{d['path']:<12} {human(int(d['size'])):>10}  {label}{note}")
    drive = fzf_pick(lines, "drive> ", "removable drives only | ENTER: choose drive to ERASE, ESC: cancel")
    if not drive:
        print("No drive selected.")
        return
    dev = next(d for d in cands if d["path"] == drive[0])
    flash(zip_path, dev, mp_key)

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-b", "--boards", nargs="+", default=DEFAULT_BOARDS, help="boards to offer")
    ap.add_argument("-c", "--channel", default="stable-channel",
                    help="channel to show (default: stable-channel; use 'any' for all)")
    ap.add_argument("-o", "--out", type=Path, default=Path("chromeos-recovery"), help="output directory")
    ap.add_argument("--flash-only", action="store_true", help="skip downloading, only flash an existing zip")
    ap.add_argument("--no-flash", action="store_true", help="don't offer to flash after downloading")
    args = ap.parse_args()

    if not shutil.which("fzf"):
        sys.exit("fzf not found. Install it first (e.g. `sudo apt install fzf`).")

    if args.flash_only:
        flash_flow(args.out)
        return

    if args.channel.lower() == "any":
        args.channel = None

    wanted = [ALIASES.get(b.lower(), b.lower()) for b in args.boards]
    data = fetch_data()

    boards = []
    for b in wanted:
        if b in data:
            boards.append(b)
        else:
            print(f"warning: board '{b}' not found in data.json, skipping", file=sys.stderr)
    if not boards:
        sys.exit("None of the requested boards exist in data.json.")

    # 1) choose boards
    lines = []
    for b in boards:
        n = len(get_images(data, b, args.channel))
        names = ", ".join(data[b].get("brand_names", [])[:3])
        lines.append(f"{b}\t{b:<12} {n:>4} images   {names}")
    chosen_boards = fzf_pick(lines, "board> ", "TAB: select multiple boards, ENTER: confirm", multi=True)
    if not chosen_boards:
        sys.exit("No board selected.")

    # 2) choose images per board, then download
    for board in chosen_boards:
        images = get_images(data, board, args.channel)
        if not images:
            print(f"{board}: no matching images")
            continue

        lines = []
        for i, img in enumerate(images):
            lines.append(
                f"{i}\t{img['channel']:<15} {img['platform_version']:<14} "
                f"chrome {img['chrome_version']:<16} {fmt_date(img['last_modified'])}  "
                f"{img['url'].rsplit('/', 1)[-1]}"
            )
        picks = fzf_pick(lines, f"{board}> ",
                         "newest first | TAB: select multiple, ENTER: download, ESC: skip board",
                         multi=True)
        if not picks:
            print(f"{board}: nothing selected")
            continue

        print(f"\n== {board} ==")
        for key in picks:
            download(images[int(key)]["url"], args.out / board)

    print("\nDownloads finished.")

    # 3) optionally flash
    if not args.no_flash:
        ans = fzf_pick(["y\tYes, flash an image to a USB drive", "n\tNo, I'm done"],
                       "flash?> ", "Flash a drive now?")
        if ans == ["y"]:
            flash_flow(args.out)

    print("\nDone.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nAborted.")
