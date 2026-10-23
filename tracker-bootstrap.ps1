# Tiles Survive Tracker - stable updater/bootstrap for Windows PowerShell 5.1+
param([string]$AppDir = $PSScriptRoot)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$base = "https://raw.githubusercontent.com/alialooon/tiles-survive-tracker/main"
$log = Join-Path $AppDir "tracker-startup.log"
$stamp = Join-Path $AppDir ".tracker-manifest-revision"
$stage = Join-Path $AppDir ".tracker-staging"
$backup = Join-Path $AppDir ".tracker-backup"
$updated = $false

function Say([string]$message) {
    Write-Host $message
    Add-Content -LiteralPath $log -Value $message -Encoding UTF8
}
function SafeFile([string]$path) {
    $allowed = @("launcher.py", "updater.py", "app.py", "engine.py", "tracker.py",
                 "survey.py", "windows_capture.py", "capture_hotfix.py", "requirements.txt", "VERSION")
    return ($allowed -ccontains $path)
}
function Restore-Backup {
    if (Test-Path -LiteralPath $backup) {
        Get-ChildItem -LiteralPath $backup -File | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $AppDir $_.Name) -Force
        }
        Remove-Item -LiteralPath $backup -Recurse -Force
    }
}

try {
    Set-Location -LiteralPath $AppDir
    Say "Tracker start: $(Get-Date -Format s)"
    if (Test-Path -LiteralPath $stage) { Remove-Item $stage -Recurse -Force }
    if (Test-Path -LiteralPath $backup) { Remove-Item $backup -Recurse -Force }
    $manifest = $null
    $cacheNonce = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    try {
        $manifest = Invoke-RestMethod -Uri "${base}/manifest.json?rev=latest&cb=${cacheNonce}" -TimeoutSec 15
    } catch {
        Say "Update check unavailable. Launching installed version."
    }

    if ($null -ne $manifest) {
        $remoteRev = 0
        if (-not [int]::TryParse([string]$manifest.revision, [ref]$remoteRev) -or
            $manifest.schema -ne 1 -or $remoteRev -lt 1 -or $remoteRev -gt 100000000) {
            throw "Invalid update manifest."
        }
        $installedRev = 0
        if (Test-Path -LiteralPath $stamp) {
            [void][int]::TryParse((Get-Content -Raw $stamp).Trim(),
                                  [ref]$installedRev)
        }
        if ($remoteRev -gt $installedRev) {
            New-Item -ItemType Directory -Force -Path $stage | Out-Null
            $files = @($manifest.files)
            if ($files.Count -gt 20 -or $files.Count -eq 0) { throw "Invalid file count." }
            foreach ($file in $files) {
                $name = [string]$file.path
                $checksum = [string]$file.sha256
                if (-not (SafeFile $name) -or $checksum -notmatch '^[a-fA-F0-9]{64}$') {
                    throw "Invalid update file or checksum: $name"
                }
                $dest = Join-Path $stage $name
                Invoke-WebRequest -UseBasicParsing -Uri "${base}/${name}?rev=${remoteRev}&cb=${cacheNonce}" -OutFile $dest -TimeoutSec 30
                if ((Get-Item -LiteralPath $dest).Length -gt 1000000) { throw "Update file too large." }
                $actual = (Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash
                if ($actual -ne $checksum.ToUpperInvariant()) { throw "Update checksum mismatch: $name" }
            }
            New-Item -ItemType Directory -Force -Path $backup | Out-Null
            $updated = $true
            foreach ($file in $files) {
                $name = [string]$file.path
                $dest = Join-Path $AppDir $name
                if (Test-Path -LiteralPath $dest) {
                    Copy-Item -LiteralPath $dest -Destination (Join-Path $backup $name) -Force
                }
                Copy-Item -LiteralPath (Join-Path $stage $name) -Destination $dest -Force
            }
            $updated = $true
            Say "Applied GitHub update revision $($manifest.revision)."
        } else {
            Say "Tracker files are current (revision $installedRev)."
        }
    }

    $python = Join-Path $AppDir ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        Say "Setting up Python environment..."
        $py = Get-Command "py.exe" -ErrorAction SilentlyContinue
        if ($py) {
            & $py.Source -3 -m venv (Join-Path $AppDir ".venv") 2>&1 | Out-File $log -Append -Encoding UTF8
        } else {
            $py = Get-Command "python.exe" -ErrorAction SilentlyContinue
            if (-not $py) { throw "Python 3.10+ was not found. Install Python and retry." }
            & $py.Source -m venv (Join-Path $AppDir ".venv") 2>&1 | Out-File $log -Append -Encoding UTF8
        }
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $python)) {
            throw "Failed to create Python environment. See tracker-startup.log."
        }
    }

    $req = Join-Path $AppDir "requirements.txt"
    if (-not (Test-Path -LiteralPath $req)) { throw "requirements.txt not found. Extract full Tracker package." }
    $depStamp = Join-Path $AppDir ".tracker-requirements-sha256"
    $reqHash = (Get-FileHash -LiteralPath $req -Algorithm SHA256).Hash
    if (-not (Test-Path $depStamp) -or (Get-Content -Raw $depStamp).Trim() -ne $reqHash) {
        Say "Installing or updating Python packages..."
        & $python -m pip install --disable-pip-version-check -r $req 2>&1 | Out-File $log -Append -Encoding UTF8
        if ($LASTEXITCODE -ne 0) { throw "Python package installation failed. See tracker-startup.log." }
        Set-Content -LiteralPath $depStamp -Value $reqHash -Encoding ASCII
    }
    if (-not (Test-Path (Join-Path $AppDir "launcher.py"))) {
        throw "launcher.py is missing. Extract the complete Tracker package."
    }
    Say "Opening Tiles Survive Tracker..."
    & $python -u (Join-Path $AppDir "launcher.py")
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) { throw "Tracker exited with code $exitCode." }

    if ($updated) {
        Set-Content -LiteralPath $stamp -Value ([string]$manifest.revision) -Encoding ASCII
    }
    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
    if (Test-Path $backup) { Remove-Item $backup -Recurse -Force }
    exit 0
} catch {
    Say "ERROR: $($_.Exception.Message)"
    if ($updated) {
        Restore-Backup
        Say "Restored previous files; retry launching the app."
    }
    exit 1
}
