# Photo Guard - Watches an Incoming folder, dedupes by SHA256, organizes
# new photos/videos into Library\YYYY\MM\, quarantines duplicates.
#
# Designed to live as a Windows scheduled task that auto-starts on logon.
# All paths are parameters; defaults work if you map your share as P:.

param(
    [string]$IncomingPath  = (Join-Path $env:USERPROFILE 'PhotoLibrary\Incoming'),
    [string]$LibraryPath   = (Join-Path $env:USERPROFILE 'PhotoLibrary\Library'),
    [string]$DuplicatePath = (Join-Path $env:USERPROFILE 'PhotoLibrary\Duplicates'),
    [string]$HashDbPath    = (Join-Path $env:USERPROFILE '.nas-photo-tools\photo_hashes.db'),
    [string]$LogPath       = (Join-Path $env:USERPROFILE '.nas-photo-tools\photo_guard.log')
)

function Write-Log {
    param($msg)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content -Path $LogPath -Value $line
    Write-Host $line
}

function Get-FileHash256 {
    param($path)
    (Get-FileHash -Path $path -Algorithm SHA256).Hash
}

function Get-PhotoDate {
    param($path)
    try {
        $shell = New-Object -ComObject Shell.Application
        $folder = $shell.Namespace((Split-Path $path -Parent))
        $file = $folder.ParseName((Split-Path $path -Leaf))
        # Property index 12 is "Date taken" on most Windows builds
        $dateStr = $folder.GetDetailsOf($file, 12)
        if ($dateStr) {
            $clean = $dateStr -replace '[^\d/:\s‎‏]', ''
            $d = [DateTime]::Parse($clean.Trim())
            return $d
        }
    } catch { }
    return (Get-Item $path).LastWriteTime
}

function Ensure-Directory {
    param($path)
    if (-not (Test-Path $path)) {
        New-Item -ItemType Directory -Path $path -Force | Out-Null
    }
}

function Load-HashDb {
    $hashes = @{}
    if (Test-Path $HashDbPath) {
        Get-Content $HashDbPath | ForEach-Object {
            $parts = $_ -split '\|', 2
            if ($parts.Count -eq 2) { $hashes[$parts[0]] = $parts[1] }
        }
    }
    return $hashes
}

function Save-HashDb {
    param($hashes)
    $lines = $hashes.GetEnumerator() | ForEach-Object { "$($_.Key)|$($_.Value)" }
    Set-Content -Path $HashDbPath -Value $lines
}

function Process-NewFile {
    param($path, $hashes)

    if (-not (Test-Path $path)) { return }

    $ext = [IO.Path]::GetExtension($path).ToLower()
    $mediaExts = @('.jpg', '.jpeg', '.png', '.heic', '.heif', '.gif', '.bmp',
                   '.tiff', '.webp', '.raw', '.cr2', '.nef', '.arw', '.dng',
                   '.mp4', '.mov', '.avi', '.mkv', '.m4v', '.3gp')
    if ($mediaExts -notcontains $ext) { return }

    # Wait for the writer to release the file
    Start-Sleep -Seconds 2
    if (-not (Test-Path $path)) { return }

    $hash = Get-FileHash256 $path
    $filename = Split-Path $path -Leaf

    if ($hashes.ContainsKey($hash)) {
        Ensure-Directory $DuplicatePath
        $dupeName = "dupe_$(Get-Date -Format 'yyyyMMdd_HHmmss')_$filename"
        $destPath = Join-Path $DuplicatePath $dupeName
        Write-Log "DUPLICATE: $filename (matches $($hashes[$hash])) -> $destPath"
        Move-Item -Path $path -Destination $destPath -Force
        return
    }

    $photoDate = Get-PhotoDate $path
    $yearMonth = "{0:yyyy}\{0:MM}" -f $photoDate
    $destDir = Join-Path $LibraryPath $yearMonth
    Ensure-Directory $destDir

    $destPath = Join-Path $destDir $filename
    $counter = 1
    while (Test-Path $destPath) {
        $base = [IO.Path]::GetFileNameWithoutExtension($filename)
        $destPath = Join-Path $destDir "$base`_$counter$ext"
        $counter++
    }

    Move-Item -Path $path -Destination $destPath -Force
    $hashes[$hash] = $destPath
    Save-HashDb $hashes
    Write-Log "FILED: $filename -> $destPath (hash: $($hash.Substring(0,8))...)"
}

function Start-GuardWatcher {
    Ensure-Directory (Split-Path $LogPath -Parent)
    Ensure-Directory $IncomingPath
    Ensure-Directory $LibraryPath

    Write-Log "Photo Guard started. Watching: $IncomingPath"
    $hashes = Load-HashDb
    Write-Log "Loaded $($hashes.Count) known file hashes"

    Get-ChildItem -Path $IncomingPath -File -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
        Process-NewFile $_.FullName $hashes
    }

    $watcher = New-Object System.IO.FileSystemWatcher
    $watcher.Path = $IncomingPath
    $watcher.IncludeSubdirectories = $true
    $watcher.EnableRaisingEvents = $true

    $action = {
        $path = $Event.SourceEventArgs.FullPath
        Start-Sleep -Seconds 3
        Process-NewFile $path $script:hashes
    }

    $script:hashes = $hashes
    Register-ObjectEvent $watcher Created -Action $action | Out-Null
    Register-ObjectEvent $watcher Changed -Action $action | Out-Null

    Write-Log "Watcher active. Press Ctrl+C to stop."
    while ($true) { Start-Sleep -Seconds 60 }
}

function Scan-Library {
    Write-Log "Scanning entire library for duplicates..."
    Ensure-Directory (Split-Path $LogPath -Parent)
    $hashes = Load-HashDb
    $allFiles = Get-ChildItem -Path $LibraryPath -File -Recurse -ErrorAction SilentlyContinue
    $seen = @{}
    $dupeCount = 0

    foreach ($file in $allFiles) {
        $h = Get-FileHash256 $file.FullName
        if ($seen.ContainsKey($h)) {
            Ensure-Directory $DuplicatePath
            $dupeName = "dupe_$(Get-Date -Format 'yyyyMMdd_HHmmss')_$($file.Name)"
            $destPath = Join-Path $DuplicatePath $dupeName
            Write-Log "DUPE IN LIBRARY: $($file.FullName) (matches $($seen[$h])) -> $destPath"
            Move-Item -Path $file.FullName -Destination $destPath -Force
            $dupeCount++
        } else {
            $seen[$h] = $file.FullName
            $hashes[$h] = $file.FullName
        }
    }
    Save-HashDb $hashes
    Write-Log "Scan complete. Found $dupeCount duplicates. Library has $($seen.Count) unique files."
}

if ($args -contains '-Scan') { Scan-Library } else { Start-GuardWatcher }
