"""Launch the visible installer through local WMI, outside the host process tree."""
import base64
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import subprocess


class IndependentProcess:
    def __init__(self, pid):
        self.pid = int(pid)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.kernel.OpenProcess.restype = w.HANDLE
        self.kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.kernel.OpenProcess(0x1000, False, self.pid)
        if not self.handle:
            raise OSError('无法确认独立更新助手是否正在运行，请保持字字动画打开。')

    def poll(self):
        code = w.DWORD()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return None if code.value == 259 else code.value

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def launch(args, job):
    if os.name != 'nt':
        raise OSError('独立更新助手需要 Windows。')
    # JSON is passed as encoded data, never interpolated as executable shell text.
    data = dict(command=subprocess.list2cmdline([str(a) for a in args]),
                directory=str(Path(job).resolve()), localappdata=os.environ.get('LOCALAPPDATA', ''))
    encoded = base64.b64encode(json.dumps(data).encode('utf-8')).decode('ascii')
    script = r'''
$ErrorActionPreference = 'Stop'
$request = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__REQUEST__')) | ConvertFrom-Json
$current = Get-CimInstance Win32_Process -Filter "ProcessId = $PID"
$environment = [Environment]::GetEnvironmentVariables('Process')
if ($request.localappdata) { $environment['LOCALAPPDATA'] = $request.localappdata }
$startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{
    ShowWindow = [uint16]1
    WinstationDesktop = 'WinSta0\Default'
    EnvironmentVariables = [string[]]@($environment.GetEnumerator() | ForEach-Object { $_.Key + '=' + $_.Value })
}
$result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = [string]$request.command
    CurrentDirectory = [string]$request.directory
    ProcessStartupInformation = $startup
}
if ($result.ReturnValue -ne 0 -or -not $result.ProcessId) { throw ('WMI process creation failed: ' + $result.ReturnValue) }
$child = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $result.ProcessId)
if (-not $child -or $child.SessionId -ne $current.SessionId) {
    if ($child) { Invoke-CimMethod -InputObject $child -MethodName Terminate | Out-Null }
    throw 'The installer is not in the current interactive session'
}
@{pid=[int]$result.ProcessId; parent_pid=[int]$child.ParentProcessId; session=[int]$child.SessionId} | ConvertTo-Json -Compress
'''.replace('__REQUEST__', encoded)
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-EncodedCommand',
                             base64.b64encode(script.encode('utf-16le')).decode('ascii')],
                            capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise OSError('Windows 无法启动独立更新助手。请保持字字动画打开，检查 Windows Management Instrumentation 服务或使用完整安装包。')
    value = json.loads(result.stdout.decode('utf-8-sig'))
    return IndependentProcess(value['pid'])
