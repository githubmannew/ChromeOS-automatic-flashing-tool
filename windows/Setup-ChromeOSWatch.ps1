#Requires -RunAsAdministrator
<#
.SYNOPSIS
  One-shot setup for the ChromeOS recovery watcher (script 3 of 3). Run as Administrator.

.DESCRIPTION
  Safe to run again at any time (it updates the task and keeps your downloads/settings).
    1. unblocks the scripts
    2. finds Python 3.9+ (installs it with winget if missing) and fzf (optional, for menus)
    3. gives your user write access to this folder
    4. writes the default settings (state\config.json)
    5. registers a Task Scheduler task that starts chromeos_daemon.py hidden at logon,
       re-checks every 5 minutes that it is alive, and restarts it if it dies
    6. puts a launcher, 'ChromeOS Watch.bat', on your Desktop that opens the control menu.
       The launcher always runs as Administrator (it asks Windows for permission each time)
    7. starts the daemon and checks that it came up

  Everything stays inside this folder: scripts, state\, and ChromeOS-recovery\ (downloads).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\Setup-ChromeOSWatch.ps1

.EXAMPLE
  .\Setup-ChromeOSWatch.ps1 -Boards octopus,dedede -IntervalSeconds 600 -Channel any

.EXAMPLE
  .\Setup-ChromeOSWatch.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string[]]$Boards,                 # default: octopus, dedede, skywalker, corsola
    [string]$Channel,                  # default: stable-channel ("any" = all channels)
    [int]$IntervalSeconds,             # default: 300 (minimum 60)
    [switch]$IncludeExisting,          # also fetch the newest image that already exists for each board
    [string]$User = [Security.Principal.WindowsIdentity]::GetCurrent().Name,   # who the daemon runs as
    [string]$DesktopDir = [Environment]::GetFolderPath('Desktop'),   # where the launcher .bat is placed
    [switch]$NoInstall,                # do not try to install Python / fzf with winget
    [switch]$Uninstall                 # remove the task and the Desktop launcher (keeps downloads)
)

$ErrorActionPreference = 'Stop'
$TaskName = 'ChromeOS Recovery Watch'
$Root     = $PSScriptRoot
$Daemon   = Join-Path $Root 'chromeos_daemon.py'
$Ctl      = Join-Path $Root 'chromeos_ctl.py'
$Launcher = Join-Path $DesktopDir 'ChromeOS Watch.bat'

function Say([string]$m)  { Write-Host "[setup] $m" -ForegroundColor Cyan }
function Warn([string]$m) { Write-Host "[setup] $m" -ForegroundColor Yellow }
function Fail([string]$m) { Write-Host "[setup] ERROR: $m" -ForegroundColor Red; exit 1 }

function Refresh-Path {
    $m = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $u = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$m;$u"
}

function Stop-Daemon {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue
    foreach ($p in @($procs)) {
        if ($p.CommandLine -like '*chromeos_daemon.py*') {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
}

function Update-UserPath([string]$Dir, [bool]$Add) {
    $cur = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @()
    if ($cur) { $parts = @($cur.Split(';') | Where-Object { $_ -ne '' }) }
    $target = $Dir.TrimEnd('\')
    $has = @($parts | Where-Object { $_.TrimEnd('\') -ieq $target }).Count -gt 0
    if ($Add) {
        if ($has) { return }
        $parts = $parts + $Dir
    } else {
        if (-not $has) { return }
        $parts = @($parts | Where-Object { $_.TrimEnd('\') -ine $target })
    }
    [Environment]::SetEnvironmentVariable('Path', ($parts -join ';'), 'User')
}

function Remove-LegacyCli {
    # An earlier version installed a 'cros' command: cros.cmd in this folder and this folder on the user PATH.
    Remove-Item (Join-Path $Root 'cros.cmd') -Force -ErrorAction SilentlyContinue
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    if ($User -ieq $me) { Update-UserPath $Root $false }
}

# ---------------------------------------------------------------- uninstall
if ($Uninstall) {
    Say 'Stopping the daemon and removing the scheduled task...'
    Stop-Daemon
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item $Launcher -Force -ErrorAction SilentlyContinue
    Remove-LegacyCli
    Say 'Done. Your downloads (ChromeOS-recovery\) and settings (state\) were left in place; delete the folder to remove everything.'
    return
}

# ---------------------------------------------------------------- checks
foreach ($f in @($Daemon, $Ctl)) {
    if (-not (Test-Path $f)) { Fail "Missing $f. Keep all three scripts in the same folder." }
}
if ($Root -match '[^\x00-\x7F]') {
    Warn 'This folder path contains non-ASCII characters; the Desktop launcher may not work. A plain path like C:\ChromeOS-Watch is safer.'
}
if ($Root -like "$env:ProgramFiles*") {
    Warn 'This folder is under Program Files. It works (the user is granted write access) but C:\ChromeOS-Watch is simpler.'
}

Say "Folder : $Root"
Say "User   : $User"

# ---------------------------------------------------------------- unblock
Get-ChildItem -Path $Root -File | Unblock-File -ErrorAction SilentlyContinue

# ---------------------------------------------------------------- python
function Find-Python {
    $ErrorActionPreference = 'Continue'
    $cands = New-Object System.Collections.Generic.List[string]
    foreach ($c in @(Get-Command python.exe -All -ErrorAction SilentlyContinue)) { $cands.Add([string]$c.Source) }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $p = @(& $launcher.Source -3 -c "import sys; print(sys.executable)" 2>$null)
        if ($LASTEXITCODE -eq 0 -and $p.Count -ge 1) { $cands.Add([string]$p[0]) }
    }
    foreach ($c in ($cands | Select-Object -Unique)) {
        if ($c -like '*\WindowsApps\*') { continue }      # Microsoft Store stubs / Store Python
        $o = @(& $c -c "import sys; print(sys.version_info[0]*100+sys.version_info[1]); print(sys.executable)" 2>$null)
        if ($LASTEXITCODE -ne 0 -or $o.Count -lt 2) { continue }
        if ([int]$o[0] -lt 309) { continue }
        $exe = [string]$o[1]
        $pyw = Join-Path (Split-Path $exe -Parent) 'pythonw.exe'
        if (-not (Test-Path $pyw)) { continue }
        return [pscustomobject]@{ Exe = $exe; Pythonw = $pyw; Version = [int]$o[0] }
    }
    return $null
}

function Install-WithWinget([string]$Id, [string[]]$Extra) {
    $w = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $w) { Warn "winget is not available, cannot install $Id automatically."; return $false }
    $a = @('install', '--id', $Id, '-e', '--silent', '--accept-package-agreements', '--accept-source-agreements')
    if ($Extra) { $a = $a + $Extra }
    & $w.Source @a | Out-Host
    return ($LASTEXITCODE -eq 0)
}

$py = Find-Python
if (-not $py) {
    if ($NoInstall) { Fail 'Python 3.9+ (from python.org) was not found. Install it, tick "Add python.exe to PATH", then run this again.' }
    Say 'Python not found. Installing Python 3.12 with winget (this can take a minute)...'
    $ok = Install-WithWinget 'Python.Python.3.12' @('--scope', 'machine')
    if (-not $ok) { $ok = Install-WithWinget 'Python.Python.3.12' @() }
    Refresh-Path
    $py = Find-Python
    if (-not $py) { Fail 'Could not install or find Python. Install Python 3.12 from https://www.python.org/downloads/ (tick "Add python.exe to PATH") and run this again.' }
}
Say ("Python  : {0} (3.{1})" -f $py.Exe, ($py.Version % 100))

# ---------------------------------------------------------------- fzf (optional)
function Get-FzfPath {
    $c = Get-Command fzf.exe -ErrorAction SilentlyContinue
    if ($c) { return [string]$c.Source }
    foreach ($f in @((Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\fzf.exe'),
                     (Join-Path $env:ProgramFiles 'WinGet\Links\fzf.exe'))) {
        if (Test-Path $f) { return $f }
    }
    $pk = Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages') -Filter 'fzf.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pk) { return [string]$pk.FullName }
    return $null
}
if (-not (Get-FzfPath)) {
    if ($NoInstall) {
        Warn 'fzf not found; menus will use numbered lists instead (optional).'
    } else {
        Say 'Installing fzf (nicer menus) with winget...'
        [void](Install-WithWinget 'junegunn.fzf' @())
        Refresh-Path
        if (-not (Get-FzfPath)) { Warn 'fzf did not install; menus will use numbered lists instead. Not a problem.' }
    }
}

# ---------------------------------------------------------------- folder + permissions
New-Item -ItemType Directory -Force -Path (Join-Path $Root 'state'), (Join-Path $Root 'ChromeOS-recovery') | Out-Null
& icacls.exe $Root /grant "${User}:(OI)(CI)M" /T /C /Q | Out-Null

# ---------------------------------------------------------------- stop any old daemon before changing things
Stop-Daemon

# ---------------------------------------------------------------- settings
$cfgArgs = @($Daemon, '--root', $Root, '--write-config')
if ($PSBoundParameters.ContainsKey('Boards') -and $Boards) { $cfgArgs += '--boards'; $cfgArgs += $Boards }
if ($PSBoundParameters.ContainsKey('Channel') -and $Channel) { $cfgArgs += '--channel'; $cfgArgs += $Channel }
if ($PSBoundParameters.ContainsKey('IntervalSeconds') -and $IntervalSeconds -gt 0) { $cfgArgs += '--interval'; $cfgArgs += [string]$IntervalSeconds }
if ($IncludeExisting) { $cfgArgs += '--include-existing' }
$cfgOut = & $py.Exe @cfgArgs
if ($LASTEXITCODE -ne 0) { Fail "Could not write settings: $cfgOut" }
Say "Settings: $cfgOut"

# ---------------------------------------------------------------- scheduled task
$action = New-ScheduledTaskAction -Execute $py.Pythonw -Argument ('"{0}" --root "{1}"' -f $Daemon, $Root) -WorkingDirectory $Root
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

function Register-Watch($trigger) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal -Settings $settings `
        -Trigger $trigger -Description 'ChromeOS recovery image downloader (see chromeos_daemon.py)' -Force | Out-Null
}

# Start at logon, then re-check every 5 minutes. A second copy is ignored (IgnoreNew + the daemon's own lock),
# so this only matters when the daemon has died: it then comes back within 5 minutes.
$logon = New-ScheduledTaskTrigger -AtLogOn -User $User
$repeating = $false
try {
    $rep = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
    $logon.Repetition = $rep.Repetition
    $repeating = $true
} catch {
    Warn "Could not build the 5-minute watchdog trigger ($($_.Exception.Message)); using logon + restart-on-failure only."
}
try {
    Register-Watch $logon
} catch {
    if (-not $repeating) { throw }
    Warn "Registering with the watchdog trigger failed ($($_.Exception.Message)); retrying with logon trigger only."
    Register-Watch (New-ScheduledTaskTrigger -AtLogOn -User $User)
    $repeating = $false
}
Say ("Task    : '{0}' registered ({1})" -f $TaskName, $(if ($repeating) { 'starts at logon, watchdog every 5 min' } else { 'starts at logon' }))

# ---------------------------------------------------------------- desktop launcher
Remove-LegacyCli
$fzf = Get-FzfPath
# The launcher re-starts itself as Administrator (Windows shows its permission prompt) and never runs without it.
# 'fltmc' only succeeds in an elevated window; '--elevated' stops it from looping if elevation fails.
$batLines = @(
    '@echo off',
    'title ChromeOS Recovery Watch',
    'fltmc >nul 2>&1 && goto :admin',
    'if "%~1"=="--elevated" goto :noadmin',
    'powershell -NoProfile -Command "Start-Process -FilePath ''%~f0'' -ArgumentList ''--elevated'' -Verb RunAs" >nul 2>&1',
    'if errorlevel 1 (',
    '    echo Administrator permission was declined, so nothing was started.',
    '    pause',
    ')',
    'exit /b',
    ':noadmin',
    'echo Could not get Administrator rights, so nothing was started.',
    'pause',
    'exit /b 1',
    ':admin'
)
if ($fzf) { $batLines += ('set "PATH={0};%PATH%"' -f (Split-Path $fzf -Parent)) }
$batLines += ('"{0}" "{1}"' -f $py.Exe, $Ctl)
$batLines += 'if errorlevel 1 pause'
$launcherPath = $Launcher
if (-not (Test-Path $DesktopDir)) {
    Warn "Desktop folder '$DesktopDir' not found; putting the launcher in the install folder instead."
    $launcherPath = Join-Path $Root 'ChromeOS Watch.bat'
}
if ($launcherPath -match "['%&^!]") {
    Warn "The launcher path contains one of ' % & ^ ! which can break its Administrator prompt. Use -DesktopDir with a plain folder if it fails."
}
$me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($User -ine $me) {
    Warn "Running as $me but the daemon is for $User; the launcher is going to $me's Desktop. Copy it to $User's Desktop if needed."
}
[IO.File]::WriteAllText($launcherPath, (($batLines -join "`r`n") + "`r`n"), [Text.Encoding]::ASCII)
Say "Launcher: $launcherPath"

# ---------------------------------------------------------------- start + verify
Say 'Starting the daemon...'
Start-ScheduledTask -TaskName $TaskName
$up = $false
$statusFile = Join-Path $Root 'state\status.json'
for ($i = 0; $i -lt 30 -and -not $up; $i++) {
    Start-Sleep -Seconds 1
    if (-not (Test-Path $statusFile)) { continue }
    try {
        $s = Get-Content $statusFile -Raw | ConvertFrom-Json
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $alive = Get-Process -Id ([int]$s.pid) -ErrorAction SilentlyContinue
        if ($alive -and $s.state -ne 'stopped' -and (($now - [double]$s.updated) -lt 10)) { $up = $true }
    } catch { }
}
if ($up) {
    Say 'The daemon is running.'
} else {
    Warn 'The daemon did not report in within 30 seconds.'
    Warn "Check: $(Join-Path $Root 'state\crash.log') and $(Join-Path $Root 'state\daemon.log')"
    Warn "Or try:  schtasks /query /tn `"$TaskName`" /v /fo list"
}

Write-Host ''
Write-Host 'Done. Next steps:' -ForegroundColor Green
Write-Host "  1. Double-click 'ChromeOS Watch' ($launcherPath) to open the control menu."
Write-Host '     Windows asks for Administrator permission every time; that is deliberate (flashing needs it).'
Write-Host '     It shows live progress and lets you download a specific image now, force a refresh, pause, change settings.'
Write-Host '  2. To flash a USB drive choose "Flash" in that menu.'
Write-Host "  Downloads go to: $(Join-Path $Root 'ChromeOS-recovery')"
Write-Host '  Note: the first run only records what is already published. Only NEWER images are downloaded'
Write-Host '        automatically. Use the menu to grab any existing one, or re-run setup with -IncludeExisting.'
Write-Host '  Remove everything with:   .\Setup-ChromeOSWatch.ps1 -Uninstall'
