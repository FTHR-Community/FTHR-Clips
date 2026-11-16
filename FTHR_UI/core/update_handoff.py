"""Launch a verified installer only after the app and owned helpers exit."""

import base64
import os
from pathlib import Path
import re
import subprocess


def _literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def handoff_script(installer, sha256, process_ids):
    if not re.fullmatch(r'[0-9a-fA-F]{64}', sha256):
        raise ValueError('Invalid installer hash')
    ids = sorted({int(pid) for pid in process_ids})
    if not ids or any(pid <= 0 for pid in ids):
        raise ValueError('Invalid process IDs')
    path = Path(installer).resolve()
    log = path.parent / 'installer-handoff.log'
    return f"""
$ErrorActionPreference = 'Stop'
$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')
try {{
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    foreach ($processId in @({','.join(map(str, ids))})) {{
        $owner = $null
        try {{ $owner = [Diagnostics.Process]::GetProcessById($processId) }}
        catch [ArgumentException] {{ continue }}
        try {{
            $remaining = [int][Math]::Max(0, ($deadline - [DateTime]::UtcNow).TotalMilliseconds)
            if (-not $owner.WaitForExit($remaining)) {{
                throw 'FTHR did not fully exit. Installation was not started.'
            }}
        }} finally {{ $owner.Dispose() }}
    }}
    $installerPath = {_literal(path)}
    $actualHash = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash
    if ($actualHash -ne {_literal(sha256)}) {{ throw 'Installer hash changed. Download it again.' }}
    Start-Process -FilePath $installerPath
}} catch {{
    $_.Exception.Message | Out-File -LiteralPath {_literal(log)} -Encoding utf8
    $shell = New-Object -ComObject WScript.Shell
    $null = $shell.Popup(('The update installer could not start. ' + $_.Exception.Message +
        ' Your downloaded installer is still in ' + {_literal(path.parent)}),
        0, 'FTHRClips update', 16)
    exit 1
}}
"""


def launch_after_exit(installer, sha256, process_ids):
    """Start a hidden helper; never kill processes or launch Setup immediately."""
    script = handoff_script(installer, sha256, process_ids)
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / (
        'System32/WindowsPowerShell/v1.0/powershell.exe')
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    return subprocess.Popen(
        [str(powershell), '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
         '-EncodedCommand', encoded],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW)
