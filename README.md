# ChromeOS-automatic-flashing-tool

Tools for downloading ChromeOS recovery images and flashing them to a USB drive.

| Version | What it is | Where |
|---|---|---|
| Linux | fzf-based downloader and flasher | `Chromebook_Updater(testing).py` |
| Windows | Background downloader, control tool, and installer | `windows/` |

## Windows

Everything lives in one folder. Images are downloaded to a `ChromeOS-recovery` folder next to the scripts.

| File | Role |
|---|---|
| `Setup-ChromeOSWatch.ps1` | Installer. Run as Administrator. Installs Python and fzf if missing (via winget), registers the background task, and puts a **ChromeOS Watch.bat** launcher on the Desktop. |
| `chromeos_daemon.py` | Background downloader. Checks for new images about every 5 minutes and downloads one at a time. Keeps the newest image per board and deletes the older ones. |
| `chromeos_ctl.py` | Control tool, opened by the Desktop launcher. Shows live download status, lets you pull a specific image now, pause, skip, change settings, look up a Chromebook by model, and flash a USB drive. |

### Install

1. Copy the three files from `windows/` into a folder on the machine, for example `C:\ChromeOS-Watch`.
2. Open PowerShell **as Administrator** in that folder and run:
   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   .\Setup-ChromeOSWatch.ps1
   ```
3. Wait for "The daemon is running.", then use **ChromeOS Watch.bat** on the Desktop. It always asks for Administrator rights.

To remove it: `.\Setup-ChromeOSWatch.ps1 -Uninstall` (keeps downloads and settings).

### How it works

- The daemon watches these boards by default: octopus, dedede, skywalker, corsola. If a board has no image on disk yet (fresh install, or a newly added board), it downloads the newest one (about 3 GB each, one at a time), then keeps it up to date. Older images are deleted when a newer one arrives.
- When you flash a Chromebook model, its board is added to the watch list automatically, so the tool keeps itself up to date without maintenance.
- The model list refreshes every 24 hours.
- Flashing refuses boot and system disks, shows only USB/SD drives, and requires you to type the drive name to confirm.

### Status

The Windows scripts have been tested on Linux against a simulated server. The installer, scheduled task, Administrator launcher, and USB flashing have **not yet been tested on Windows**.

## Linux

Needs Linux for flashing.

Dependencies:
- fzf
- python 3

## Credits

Release data comes from [MercuryWorkshop/chromeos-releases-data](https://github.com/MercuryWorkshop/chromeos-releases-data), licensed CC-BY-4.0.
