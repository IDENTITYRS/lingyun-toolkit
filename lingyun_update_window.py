"""Small, dependency-free Win32 window for the detached update helper.

The installer runs on a worker thread. Window creation, rendering and message
dispatch stay on the main thread; ``update`` may be called from any thread.
"""
import ctypes
from ctypes import wintypes
import math
import os
import threading


WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_UPDATE = 0x8001
SW_MINIMIZE = 6
TERMINAL_STATES = frozenset(('done', 'error'))


def _bind(dll, name, result, *arguments):
    function = getattr(dll, name)
    function.restype = result
    function.argtypes = list(arguments)
    return function


class _Win32:
    """Explicit pointer-sized signatures are essential on 64-bit Python."""

    def __init__(self):
        if os.name != 'nt':
            raise OSError('更新助手窗口只能在 Windows 中打开。')
        self.user32 = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self.gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)
        self.comctl32 = ctypes.WinDLL('comctl32', use_last_error=True)
        pointer = ctypes.c_void_p
        integer = ctypes.c_int
        self.WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND,
                                        wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t)

        class WNDCLASS(ctypes.Structure):
            _fields_ = [('style', wintypes.UINT), ('lpfnWndProc', self.WNDPROC),
                        ('cbClsExtra', integer), ('cbWndExtra', integer),
                        ('hInstance', wintypes.HINSTANCE), ('hIcon', wintypes.HICON),
                        ('hCursor', wintypes.HANDLE), ('hbrBackground', wintypes.HBRUSH),
                        ('lpszMenuName', wintypes.LPCWSTR), ('lpszClassName', wintypes.LPCWSTR)]

        self.WNDCLASS = WNDCLASS
        self.GetModuleHandle = _bind(self.kernel32, 'GetModuleHandleW', wintypes.HMODULE, wintypes.LPCWSTR)
        self.RegisterClass = _bind(self.user32, 'RegisterClassW', wintypes.ATOM, ctypes.POINTER(WNDCLASS))
        self.UnregisterClass = _bind(self.user32, 'UnregisterClassW', wintypes.BOOL,
                                     wintypes.LPCWSTR, wintypes.HINSTANCE)
        self.CreateWindow = _bind(self.user32, 'CreateWindowExW', wintypes.HWND,
                                 wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                 integer, integer, integer, integer, wintypes.HWND, wintypes.HMENU,
                                 wintypes.HINSTANCE, pointer)
        self.DefWindowProc = _bind(self.user32, 'DefWindowProcW', ctypes.c_ssize_t,
                                  wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t)
        self.PostMessage = _bind(self.user32, 'PostMessageW', wintypes.BOOL,
                                wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t)
        self.SendMessage = _bind(self.user32, 'SendMessageW', ctypes.c_ssize_t,
                                wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t)
        self.ShowWindow = _bind(self.user32, 'ShowWindow', wintypes.BOOL, wintypes.HWND, integer)
        self.IsWindowVisible = _bind(self.user32, 'IsWindowVisible', wintypes.BOOL, wintypes.HWND)
        self.IsIconic = _bind(self.user32, 'IsIconic', wintypes.BOOL, wintypes.HWND)
        self.UpdateWindow = _bind(self.user32, 'UpdateWindow', wintypes.BOOL, wintypes.HWND)
        self.DestroyWindow = _bind(self.user32, 'DestroyWindow', wintypes.BOOL, wintypes.HWND)
        self.SetWindowText = _bind(self.user32, 'SetWindowTextW', wintypes.BOOL,
                                  wintypes.HWND, wintypes.LPCWSTR)
        self.GetMessage = _bind(self.user32, 'GetMessageW', wintypes.BOOL,
                               ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
        self.TranslateMessage = _bind(self.user32, 'TranslateMessage', wintypes.BOOL,
                                     ctypes.POINTER(wintypes.MSG))
        self.DispatchMessage = _bind(self.user32, 'DispatchMessageW', ctypes.c_ssize_t,
                                    ctypes.POINTER(wintypes.MSG))
        self.PostQuitMessage = _bind(self.user32, 'PostQuitMessage', None, integer)
        self.LoadCursor = _bind(self.user32, 'LoadCursorW', wintypes.HANDLE, wintypes.HINSTANCE, pointer)
        self.LoadIcon = _bind(self.user32, 'LoadIconW', wintypes.HICON, wintypes.HINSTANCE, pointer)
        self.GetSystemMetrics = _bind(self.user32, 'GetSystemMetrics', integer, integer)
        self.AdjustWindowRect = _bind(self.user32, 'AdjustWindowRect', wintypes.BOOL,
                                     ctypes.POINTER(wintypes.RECT), wintypes.DWORD, wintypes.BOOL)
        self.CreateFont = _bind(self.gdi32, 'CreateFontW', wintypes.HFONT,
                               integer, integer, integer, integer, integer,
                               wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR)
        self.DeleteObject = _bind(self.gdi32, 'DeleteObject', wintypes.BOOL, wintypes.HANDLE)
        self.SetForegroundWindow = _bind(self.user32, 'SetForegroundWindow', wintypes.BOOL, wintypes.HWND)
        # The helper is a separate process, so DPI awareness does not affect the host.
        _bind(self.user32, 'SetProcessDPIAware', wintypes.BOOL)()
        self.dpi = 96
        if hasattr(self.user32, 'GetDpiForSystem'):
            self.dpi = _bind(self.user32, 'GetDpiForSystem', wintypes.UINT)() or 96

        class Controls(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('classes', wintypes.DWORD)]

        init = _bind(self.comctl32, 'InitCommonControlsEx', wintypes.BOOL, ctypes.POINTER(Controls))
        controls = Controls(ctypes.sizeof(Controls), 0x20)  # ICC_PROGRESS_CLASS
        if not init(ctypes.byref(controls)):
            raise ctypes.WinError(ctypes.get_last_error())


class UpdateWindow:
    """A visible update lifecycle, independent of the host application's window."""

    def __init__(self):
        # Intentionally do not load DLLs or create native UI before show().
        self._lock = threading.Lock()
        self._status = dict(state='waiting_exit', version='', message='', progress=0)
        self._api = None
        self._hwnd = None
        self._controls = {}
        self._fonts = []
        self._owner = None
        self._wndproc = None
        self._class_name = None
        self._instance = None
        self._running = False
        self._worker = None

    def update(self, status):
        """Merge a status snapshot; safe before show() and from a worker thread."""
        change = {key: status[key] for key in ('state', 'version', 'message', 'progress') if key in status}
        if 'state' in change and change['state'] not in ('waiting_exit', 'installing', 'done', 'error'):
            raise ValueError('不支持的更新窗口状态：' + str(change['state']))
        if 'progress' in change:
            value = float(change['progress'] or 0)
            if not math.isfinite(value):
                raise ValueError('更新进度必须是有限数值。')
            change['progress'] = max(0, min(100, int(value)))
        for field in ('version', 'message'):
            if field in change:
                change[field] = str(change[field] or '')
        with self._lock:
            self._status.update(change)
            if self._status['state'] == 'done':
                self._status['progress'] = 100
            hwnd, api = self._hwnd, self._api
        if hwnd and api:
            api.PostMessage(hwnd, WM_UPDATE, 0, 0)

    def _snapshot(self):
        with self._lock:
            return dict(self._status)

    @staticmethod
    def _presentation(status):
        state = status['state']
        version = status['version'].lstrip('vV')
        suffix = (' V' + version) if version else ''
        titles = dict(waiting_exit='安装包已准备好' + suffix,
                      installing='正在安装' + suffix,
                      done='更新完成' + suffix, error='更新未完成')
        explanations = dict(
            waiting_exit='请完全退出字字动画。退出后，本窗口将自动开始安装，无需再次点击更新。',
            installing='正在更新插件文件，请稍候。安装完成前请不要重新打开字字动画。',
            done='可以重新打开字字动画。授权、激活、设置和个人文件已保留。',
            error='请重新打开插件查看更新状态，按提示重试。')
        active = state not in TERMINAL_STATES
        return dict(title=titles[state], message=status['message'] or explanations[state],
                    progress=status['progress'],
                    note=('可最小化本窗口；关闭按钮也只会最小化，更新会继续。' if active else
                          '安装已结束，可以关闭此窗口。'),
                    button='最小化' if active else '关闭')

    def _assert_owner(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError('更新窗口必须在创建它的主线程操作。')

    def show(self):
        """Create and show the window on the main thread; raise on any failure."""
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError('请在主线程创建更新助手窗口。')
        if self._hwnd:
            self._make_visible()
            return
        self._owner = threading.get_ident()
        api = self._api = _Win32()
        self._instance = api.GetModuleHandle(None)
        self._class_name = 'LingyunUpdateWindow_' + str(os.getpid()) + '_' + str(id(self))
        self._wndproc = api.WNDPROC(self._on_message)  # Keep alive for the full native lifetime.
        cls = api.WNDCLASS()
        cls.lpfnWndProc = self._wndproc
        cls.hInstance = self._instance
        cls.hCursor = api.LoadCursor(None, 32512)
        cls.hIcon = api.LoadIcon(None, 32516)
        cls.hbrBackground = 6  # COLOR_WINDOW + 1
        cls.lpszClassName = self._class_name
        if not api.RegisterClass(ctypes.byref(cls)):
            raise ctypes.WinError(ctypes.get_last_error())
        scale = lambda value: round(value * api.dpi / 96)
        style = 0x00C00000 | 0x00080000 | 0x00020000  # Caption, system menu, minimize.
        rect = wintypes.RECT(0, 0, scale(560), scale(300))
        api.AdjustWindowRect(ctypes.byref(rect), style, False)
        width, height = rect.right - rect.left, rect.bottom - rect.top
        try:
            hwnd = api.CreateWindow(0x00040000, self._class_name, '凌云插件更新助手', style,
                                    max(0, (api.GetSystemMetrics(0) - width) // 2),
                                    max(0, (api.GetSystemMetrics(1) - height) // 2),
                                    width, height, None, None, self._instance, None)
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            with self._lock:
                self._hwnd = hwnd
            for size, weight in ((21, 600), (14, 400), (12, 400)):
                font = api.CreateFont(-scale(size), 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 5, 0,
                                      'Microsoft YaHei UI')
                if not font:
                    raise ctypes.WinError(ctypes.get_last_error())
                self._fonts.append(font)
            items = (
                ('title', 'STATIC', 26, 23, 508, 34, 0, 0),
                ('message', 'STATIC', 26, 72, 508, 80, 0, 1),
                ('progress', 'msctls_progress32', 26, 167, 445, 20, 1, 1),
                ('percent', 'STATIC', 478, 165, 56, 24, 2, 1),
                ('note', 'STATIC', 26, 210, 508, 27, 0, 2),
                ('button', 'BUTTON', 436, 250, 98, 32, 0, 1),
            )
            for index, (key, kind, x, y, w, h, extra, font_index) in enumerate(items, 100):
                handle = api.CreateWindow(0, kind, '', 0x40000000 | 0x10000000 | extra,
                                          scale(x), scale(y), scale(w), scale(h), hwnd, index,
                                          self._instance, None)
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                self._controls[key] = handle
                api.SendMessage(handle, 0x0030, self._fonts[font_index], 1)  # WM_SETFONT
            api.SendMessage(self._controls['progress'], 0x0406, 0, 100)  # PBM_SETRANGE32
            self._render()
            self._make_visible()
        except BaseException:
            if self._hwnd:
                api.DestroyWindow(self._hwnd)
            self._release_resources()
            raise

    def _make_visible(self):
        self._assert_owner()
        api, hwnd = self._api, self._hwnd
        api.ShowWindow(hwnd, 5)
        # Windows may override the FIRST ShowWindow call with STARTUPINFO's
        # SW_HIDE (e.g. a helper launched without a console). A successful HWND
        # alone therefore does not prove that the user can see the assistant.
        if not api.IsWindowVisible(hwnd):
            api.ShowWindow(hwnd, 5)
        if api.IsIconic(hwnd):
            api.ShowWindow(hwnd, 9)  # SW_RESTORE also handles minimized startup.
        if not api.IsWindowVisible(hwnd) or api.IsIconic(hwnd):
            raise OSError('更新助手窗口未能显示，请重新打开插件后重试更新。')
        api.UpdateWindow(hwnd)
        api.SetForegroundWindow(hwnd)

    def _render(self):
        self._assert_owner()
        if not self._hwnd or not self._controls:
            return
        view = self._presentation(self._snapshot())
        for name in ('title', 'message', 'note', 'button'):
            self._api.SetWindowText(self._controls[name], view[name])
        self._api.SetWindowText(self._controls['percent'], str(view['progress']) + '%')
        self._api.SendMessage(self._controls['progress'], 0x0402, view['progress'], 0)  # PBM_SETPOS

    def _on_message(self, hwnd, message, wparam, lparam):
        if message == WM_UPDATE:
            self._render()
            return 0
        if message == WM_CLOSE or (message == WM_COMMAND and (wparam & 0xFFFF) == 105):
            if self._snapshot()['state'] in TERMINAL_STATES:
                self._api.DestroyWindow(hwnd)
            else:
                self._api.ShowWindow(hwnd, SW_MINIMIZE)
            return 0
        if message == WM_DESTROY:
            with self._lock:
                self._hwnd = None
            self._api.PostQuitMessage(0)
            return 0
        return self._api.DefWindowProc(hwnd, message, wparam, lparam)

    def _run_worker(self, worker):
        try:
            worker()
        except BaseException as exc:
            self.update(dict(state='error', message='更新未完成：' + str(exc) +
                             '\n请重新打开插件查看更新状态后重试。'))

    def run(self, worker):
        """Start worker() once and keep this window open until the user closes it."""
        self._assert_owner()
        if not self._hwnd:
            raise RuntimeError('请先调用 show() 显示更新助手窗口。')
        if self._running:
            raise RuntimeError('更新助手已经在运行。')
        self._running = True
        self._worker = threading.Thread(target=self._run_worker, args=(worker,),
                                        name='LingyunUpdateInstaller', daemon=True)
        self._worker.start()
        message = wintypes.MSG()
        try:
            while True:
                result = self._api.GetMessage(ctypes.byref(message), None, 0, 0)
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if result == 0:
                    break
                self._api.TranslateMessage(ctypes.byref(message))
                self._api.DispatchMessage(ctypes.byref(message))
        finally:
            self._release_resources()

    def _release_resources(self):
        for font in self._fonts:
            self._api.DeleteObject(font)
        self._fonts.clear()
        if self._class_name:
            self._api.UnregisterClass(self._class_name, self._instance)
            self._class_name = None
