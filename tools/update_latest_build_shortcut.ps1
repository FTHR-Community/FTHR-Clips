$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$bundleDirectory = Join-Path $projectRoot 'dist\FTHRClips'
$executable = Join-Path $bundleDirectory 'FTHRClips.exe'
if (-not (Test-Path -LiteralPath $executable)) {
    throw "Build the app with PyInstaller first: $executable"
}

# The build output has a stable path; subsequent builds replace its contents.
# Keep this separate from the installed-app and source-development shortcuts.
$shell = New-Object -ComObject WScript.Shell
$destinations = @(
    [Environment]::GetFolderPath('Desktop'),
    (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs')
)
foreach ($directory in $destinations) {
    $path = Join-Path $directory 'FTHR Clips - Latest Build.lnk'
    $shortcut = $shell.CreateShortcut($path)
    $shortcut.TargetPath = $executable
    $shortcut.WorkingDirectory = $bundleDirectory
    $shortcut.IconLocation = $executable + ',0'
    $shortcut.Description = 'Open the latest packaged build from this workspace. Quit any running FTHR Clips instance first.'
    $shortcut.Save()
    Write-Output "Updated $path -> $executable"
}
