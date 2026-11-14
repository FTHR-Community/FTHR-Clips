param(
    [string]$ShortcutName = 'FTHR Clips.lnk',
    [string]$StartMenuShortcutName = 'FTHR_Clips.lnk',
    [string[]]$LegacyShortcutNames = @(
        'FTHR Clips - Latest.lnk',
        'fthrlauncher.bat.lnk'
    )
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $projectRoot 'launch_windows.bat'
$iconPath = Join-Path $projectRoot 'FTHR_UI\assets\favicon.ico'

if (-not (Test-Path -LiteralPath $launcher)) {
    throw "FTHR launcher not found: $launcher"
}

$shell = New-Object -ComObject WScript.Shell

function Set-FthrShortcut {
    param([Parameter(Mandatory)][string]$Path)

    $shortcut = $shell.CreateShortcut($Path)
    $shortcut.TargetPath = $launcher
    $shortcut.Arguments = ''
    $shortcut.WorkingDirectory = $projectRoot
    $shortcut.Description = 'Launch the latest FTHR Clips source checkout with safe fallbacks'
    if (Test-Path -LiteralPath $iconPath) {
        $shortcut.IconLocation = $iconPath + ',0'
    }
    $shortcut.Save()
    Write-Output "Updated $Path -> $launcher"
}

$desktop = [Environment]::GetFolderPath('Desktop')
Set-FthrShortcut -Path (Join-Path $desktop $ShortcutName)

$startMenuPrograms = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
Set-FthrShortcut -Path (Join-Path $startMenuPrograms $StartMenuShortcutName)

foreach ($legacyName in $LegacyShortcutNames) {
    $legacyPath = Join-Path $desktop $legacyName
    if (Test-Path -LiteralPath $legacyPath) {
        Remove-Item -LiteralPath $legacyPath -Force
        Write-Output "Removed legacy shortcut $legacyPath"
    }
}
