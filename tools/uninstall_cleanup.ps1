[CmdletBinding()]
param(
    [string] $InstallRoot,
    [ValidateSet('Shutdown', 'Cleanup')]
    [string] $Phase
)

function ConvertTo-FthrPath([string] $Path) {
    if ($Path -notmatch '^(?:[A-Za-z]:[\\/]|\\\\[^?.\\][^\\]*\\[^\\]+)') {
        throw "Expected an absolute Windows path."
    }
    return [IO.Path]::GetFullPath($Path).TrimEnd('\')
}

function Test-FthrWithin([string] $Path, [string] $Root) {
    return $Path.Equals($Root, [StringComparison]::OrdinalIgnoreCase) -or
        $Path.StartsWith($Root + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Get-FthrItem([string] $Path) {
    try {
        Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    } catch [System.Management.Automation.ItemNotFoundException] {
        # A missing path has already been cleaned up.
    }
}

function Test-FthrReparse($Item) {
    return ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
}

function Get-FthrSafeItem([string] $Path) {
    # Walk from the drive/share root. Even looking up a child of a junction
    # would access its target. The caller may still inspect the final link.
    $root = [IO.Path]::GetPathRoot($Path + '\')
    $current = $root
    $item = Get-FthrItem $current
    foreach ($component in ($Path.Substring($root.TrimEnd('\').Length).Trim('\') -split '\\')) {
        if (-not $item -or (Test-FthrReparse $item)) { return }
        if (-not $component) { continue }
        $current = [IO.Path]::Combine($current, $component)
        $item = Get-FthrItem $current
    }
    return $item
}

function Get-FthrProcesses([string] $InstallRoot, [string] $LocalRoot) {
    $uploader = $LocalRoot + '\FTHR Clips\plugins\uploader'
    $session = (Get-Process -Id $PID -ErrorAction Stop).SessionId
    $installPrefix = $InstallRoot + '\'
    $uploaderPrefix = $uploader + '\'
    # The uninstaller may itself be running from the install directory.
    # Track the entire installed runtime, including native child executables.
    foreach ($process in (Get-CimInstance Win32_Process -Filter "SessionId = $session" -OperationTimeoutSec 5 -ErrorAction Stop)) {
        $path = $process.ExecutablePath
        if (-not $path) {
            # Process may have exited during the query. An inaccessible app
            # process must never be treated as proof that shutdown completed.
            if ($process.Name -in @('FTHRClips.exe', 'FTHR Uploader.exe') -and
                (Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue)) {
                throw "Cannot verify the path of FTHR process $($process.ProcessId)."
            }
            continue
        }
        if ($path.StartsWith($installPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            $relative = $path.Substring($installPrefix.Length)
            if ($relative -notmatch '^unins\d+\.exe$') { $process }
        } elseif ($path.StartsWith($uploaderPrefix, [StringComparison]::OrdinalIgnoreCase) -and
            $path.Substring($uploaderPrefix.Length) -match '^[^\\]+\\FTHR Uploader\.exe$') {
            $process
        }
    }
}

function Send-FthrShutdown([uint32[]] $ProcessIds, [string] $Executable) {
    if (-not ('Fthr.UninstallWindow' -as [type])) {
        Add-Type -ErrorAction Stop -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

namespace Fthr {
    public static class UninstallWindow {
        private delegate bool EnumWindowsProc(IntPtr window, IntPtr param);

        [DllImport("user32.dll")]
        private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr param);

        [DllImport("user32.dll")]
        private static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);

        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint RegisterWindowMessage(string name);

        [DllImport("user32.dll")]
        private static extern bool PostMessage(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);

        [DllImport("kernel32.dll")]
        private static extern IntPtr OpenProcess(uint access, bool inherit, uint process);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        private static extern bool QueryFullProcessImageName(IntPtr process, uint flags,
            StringBuilder path, ref uint size);

        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr handle);

        public static uint[] Request(uint[] processIds, string executable) {
            uint message = RegisterWindowMessage("FTHRClips.UninstallShutdown.v1");
            if (message == 0) throw new Win32Exception(Marshal.GetLastWin32Error());
            var targets = new HashSet<uint>(processIds);
            var posted = new HashSet<uint>();
            EnumWindows(delegate(IntPtr window, IntPtr param) {
                uint process;
                if (GetWindowThreadProcessId(window, out process) != 0 && targets.Contains(process)) {
                    // Recheck the executable in case a PID was reused after CIM.
                    IntPtr handle = OpenProcess(0x1000, false, process);
                    if (handle != IntPtr.Zero) {
                        try {
                            var path = new StringBuilder(32768);
                            uint size = (uint)path.Capacity;
                            if (QueryFullProcessImageName(handle, 0, path, ref size) &&
                                String.Equals(path.ToString(), executable, StringComparison.OrdinalIgnoreCase) &&
                                PostMessage(window, message, IntPtr.Zero, IntPtr.Zero)) {
                                posted.Add(process);
                            }
                        } finally {
                            CloseHandle(handle);
                        }
                    }
                }
                return true;
            }, IntPtr.Zero);
            var results = new uint[posted.Count];
            posted.CopyTo(results);
            return results;
        }
    }
}
'@
    }
    [Fthr.UninstallWindow]::Request($ProcessIds, $Executable)
}

function Wait-FthrShutdown([string] $InstallRoot, [string] $LocalRoot) {
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $mainExe = $InstallRoot + '\FTHRClips.exe'
    $sent = @{}
    do {
        $running = @(Get-FthrProcesses $InstallRoot $LocalRoot)
        if ($running.Count -eq 0) { return }
        $targets = @($running | Where-Object {
            $_.ExecutablePath -eq $mainExe -and -not $sent.ContainsKey($_.ProcessId)
        })
        if ($targets.Count) {
            foreach ($processId in (Send-FthrShutdown $targets.ProcessId $mainExe)) {
                $sent[$processId] = $true
            }
        }
        Start-Sleep -Milliseconds 500
    } while ($timer.Elapsed.TotalSeconds -lt 30)
    throw "FTHR Clips or one of its installed helpers is still running."
}

function Get-FthrSettings([string] $StateRoot) {
    $item = Get-FthrSafeItem ($StateRoot + '\settings.json')
    if (-not $item -or $item.PSIsContainer -or (Test-FthrReparse $item)) { return }
    $content = Get-Content -LiteralPath $item.FullName -Raw -Encoding UTF8 -ErrorAction Stop
    try {
        ConvertFrom-Json -InputObject $content -ErrorAction Stop
    } catch {
        # The file can be corrupt. Keep recognized media and sidecars even
        # when configured paths cannot be recovered.
        Write-Warning 'FTHR settings could not be parsed. Preserving recognized media files.'
    }
}

function Get-FthrProtectedPaths($Settings, [string[]] $CleanupRoots) {
    $configured = @()
    if ($Settings) {
        $configured = @(
            $Settings.clips_directory
            $Settings.recording_directory
            $Settings.export_directory
            $Settings.screenshot_directory
            $Settings.imported_clip_folders
        )
    }
    foreach ($value in $configured) {
        if ($value -isnot [string] -or -not $value) { continue }
        try {
            $path = ConvertTo-FthrPath $value
        } catch {
            continue
        }
        foreach ($root in $CleanupRoots) {
            if (Test-FthrWithin $path $root) {
                $path
                break
            }
        }
    }
}

function Test-FthrKeepItem($Item, [string] $StateRoot, [string[]] $ProtectedPaths, [switch] $Assets) {
    $path = $Item.FullName
    foreach ($protected in $ProtectedPaths) {
        if (Test-FthrWithin $path $protected) { return $true }
        if ((Test-FthrReparse $Item) -and (Test-FthrWithin $protected $path)) { return $true }
    }
    $name = $Item.Name
    if ($Item.PSIsContainer) {
        return $name -in @('Recordings', 'Exported', 'Exports', 'Screenshots', 'Shared',
            '.fthr-trash', 'FTHR_CLIPS_BACKUP')
    }
    # These names are emitted by playback_proxy.py and running_apps.py.
    if ((Test-FthrWithin $path ($StateRoot + '\playback')) -and
        $name -match '^[a-f0-9]{64}(?:\.sdr-720|\.\d+-\d+\.partial)?\.mp4$') { return $false }
    if ((Test-FthrWithin $path ($StateRoot + '\app-icons')) -and
        $name -match '^[a-f0-9]{64}\.png$') { return $false }
    if ((Test-FthrWithin $path ($StateRoot + '\thumbnails')) -and
        $name -match '^[a-f0-9]{32,64}\.jpe?g(?:\.tmp)?$') { return $false }
    if ((Test-FthrWithin $path ($StateRoot + '\clip_metadata.sqlite3.covers')) -and
        $name -match '^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\.png$') { return $false }
    if ($name -match '(?:\.(?:mp4|m4v|mkv|avi|mov|webm|wmv|flv|mpg|mpeg|ts|m2ts)(?:\.(?:partial|part|tmp|recovery|json))?|\.fthr-audio\.json(?:\.partial)?)$') {
        return $true
    }
    # These directories contain extension assets and extracted bundles.
    if ($Assets -or (Test-FthrWithin $path ($StateRoot + '\plugins')) -or
        (Test-FthrWithin $path ($StateRoot + '\theme'))) { return $false }
    return $name -match '\.(?:wav|mp3|m4a|aac|flac|ogg|opus|wma|png|jpe?g|webp|gif|bmp|tiff?|heic|avif|srt|vtt|xmp)(?:\.(?:partial|part|tmp|recovery))?$'
}

function Remove-FthrItem($Item, [string] $StateRoot, [string[]] $ProtectedPaths, [switch] $Assets) {
    if (Test-FthrKeepItem $Item $StateRoot $ProtectedPaths -Assets:$Assets) { return }
    $path = $Item.FullName
    if (Test-FthrReparse $Item) {
        # Remove only recognized link entries, with nonrecursive operations.
        # Other reparse types can represent data such as cloud placeholders.
        if ($Item.LinkType -notin @('Junction', 'SymbolicLink')) { return }
        if ($Item.PSIsContainer) { [IO.Directory]::Delete($path, $false) }
        else { [IO.File]::Delete($path) }
        return
    }
    if ($Item.PSIsContainer) {
        foreach ($child in (Get-ChildItem -LiteralPath $path -Force -ErrorAction Stop)) {
            # Keep path preferences on a failed run so a retry can protect the
            # same subfolders even if some state has already been removed.
            if ($path -eq $StateRoot -and $child.Name -eq 'settings.json') { continue }
            Remove-FthrItem $child $StateRoot $ProtectedPaths -Assets:$Assets
        }
        if (-not (Get-ChildItem -LiteralPath $path -Force -ErrorAction Stop | Select-Object -First 1)) {
            [IO.Directory]::Delete($path, $false)
        }
        return
    }
    # Force permits read-only ordinary files. The reparse branch never clears
    # attributes through a link or uses PowerShell's recursive removal.
    Remove-Item -LiteralPath $path -Force -ErrorAction Stop
}

function Remove-FthrReplaySegments([string] $Directory) {
    $folder = Get-FthrSafeItem $Directory
    if (-not $folder -or -not $folder.PSIsContainer -or (Test-FthrReparse $folder)) { return }
    $segments = Get-ChildItem -LiteralPath $folder.FullName -File -Force -Filter 'fthr-replay-v1-*.seg' -ErrorAction Stop
    foreach ($segment in $segments) {
        if ($segment.Name -notmatch '^fthr-replay-v1-[a-f0-9]{8}(?:-[a-f0-9]{16}){3}\.seg$' -or
            (Test-FthrReparse $segment) -or $segment.Length -lt 56) { continue }
        $reader = [IO.BinaryReader]::new([IO.File]::OpenRead($segment.FullName))
        try {
            $owned = [Text.Encoding]::ASCII.GetString($reader.ReadBytes(8)) -ceq 'FTHRDS01' -and
                $reader.ReadUInt32() -eq 1 -and $reader.ReadUInt32() -eq 56
        } finally {
            $reader.Dispose()
        }
        if ($owned) { Remove-Item -LiteralPath $segment.FullName -Force -ErrorAction Stop }
    }
}

function Invoke-FthrCleanup([string] $InstallRoot, [string] $UserRoot, [string] $LocalRoot, [string] $TempRoot) {
    $InstallRoot = ConvertTo-FthrPath $InstallRoot
    $UserRoot = ConvertTo-FthrPath $UserRoot
    $LocalRoot = ConvertTo-FthrPath $LocalRoot
    $TempRoot = ConvertTo-FthrPath $TempRoot
    $state = $UserRoot + '\.fthr'
    $localState = $LocalRoot + '\FTHR Clips'
    # The profile install may itself be inside localState if a user chose a
    # custom application directory. The installer owns its tracked payload.
    $assetRoots = @(
        ($localState + '\plugins')
        ($localState + '\cache')
        ($LocalRoot + '\cache\FTHRClips\rnnoise')
    )
    $roots = @(
        ($InstallRoot + '\logs')
        ($InstallRoot + '\cache')
        $state
    )
    $settings = Get-FthrSettings $state
    $protected = @(Get-FthrProtectedPaths $settings ($roots + $assetRoots + @($TempRoot)))
    foreach ($root in $assetRoots + $roots) {
        if (Test-FthrWithin $InstallRoot $root) { continue }
        $item = Get-FthrSafeItem $root
        if (-not $item) { continue }
        if (-not (Test-FthrReparse $item)) {
            Remove-FthrItem $item $state $protected -Assets:($root -in $assetRoots)
        }
    }
    foreach ($directory in @(($LocalRoot + '\cache\FTHRClips'), $localState)) {
        $item = Get-FthrSafeItem $directory
        if ($item -and $item.PSIsContainer -and -not (Test-FthrReparse $item) -and
            -not (Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop | Select-Object -First 1)) {
            [IO.Directory]::Delete($directory, $false)
        }
    }

    $temp = Get-FthrSafeItem $TempRoot
    if ($temp -and $temp.PSIsContainer -and -not (Test-FthrReparse $temp)) {
        foreach ($item in (Get-ChildItem -LiteralPath $TempRoot -Directory -Force -ErrorAction Stop)) {
            if ($item.Name -match '^fthr-(?:feature|theme-plugin|interface-appearance)-[a-z0-9_]{8}$' -and
                -not (Test-FthrReparse $item)) {
                Remove-FthrItem $item $state $protected -Assets
            }
        }
        Remove-FthrReplaySegments $TempRoot
    }
    if ($settings -and $settings.replay_storage.folder -is [string] -and $settings.replay_storage.folder) {
        try { $replayRoot = ConvertTo-FthrPath $settings.replay_storage.folder } catch { $replayRoot = $null }
        if ($replayRoot -and $replayRoot -ne $TempRoot) {
            Remove-FthrReplaySegments $replayRoot
        }
    }

    $dumps = Get-FthrSafeItem ($LocalRoot + '\CrashDumps')
    if ($dumps -and $dumps.PSIsContainer -and -not (Test-FthrReparse $dumps)) {
        foreach ($dump in (Get-ChildItem -LiteralPath $dumps.FullName -File -Force -Filter '*.dmp' -ErrorAction Stop)) {
            if ($dump.Name -match '^(?:FTHRClips|FTHR Uploader)\.exe\.\d+\.dmp$' -and
                -not (Test-FthrReparse $dump)) {
                Remove-Item -LiteralPath $dump.FullName -Force -ErrorAction Stop
            }
        }
    }

    if (-not (Test-FthrWithin $InstallRoot $state)) {
        $settingsFile = Get-FthrSafeItem ($state + '\settings.json')
        if ($settingsFile -and -not $settingsFile.PSIsContainer) {
            Remove-FthrItem $settingsFile $state $protected
        }
        $stateDirectory = Get-FthrSafeItem $state
        if ($stateDirectory -and $stateDirectory.PSIsContainer -and -not (Test-FthrReparse $stateDirectory) -and
            -not (Get-ChildItem -LiteralPath $state -Force -ErrorAction Stop | Select-Object -First 1)) {
            [IO.Directory]::Delete($state, $false)
        }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        if (-not $Phase -or -not $InstallRoot) { throw 'InstallRoot and Phase are required.' }
        $InstallRoot = ConvertTo-FthrPath $InstallRoot
        if ($InstallRoot -ne (ConvertTo-FthrPath $PSScriptRoot) -or $InstallRoot -match '^[A-Za-z]:$') {
            throw 'InstallRoot must be the directory containing the installed cleanup helper.'
        }
        $localRoot = ConvertTo-FthrPath ([Environment]::GetFolderPath('LocalApplicationData'))
        if ($Phase -eq 'Shutdown') {
            Wait-FthrShutdown $InstallRoot $localRoot
        } else {
            if (@(Get-FthrProcesses $InstallRoot $localRoot).Count) {
                throw 'FTHR Clips or one of its installed helpers is still running.'
            }
            Invoke-FthrCleanup $InstallRoot $env:USERPROFILE $localRoot ([IO.Path]::GetTempPath())
        }
        exit 0
    } catch {
        [Console]::Error.WriteLine($_.Exception.Message)
        exit 1
    }
}
