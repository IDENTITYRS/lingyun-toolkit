"""Signed update packages and recoverable installation; Python standard library only."""
from __future__ import annotations

import ast
import atexit
import base64
import contextlib
import ctypes
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import time
import zipfile

REPOSITORY = 'IDENTITYRS/lingyun-toolkit'
PRODUCT = 'lingyun_practical_toolkit'
DOMAIN = b'LINGYUN-RELEASE-V1\x00'
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
PUBLIC_FILES = ('lingyun_features/license_public.json', 'lingyun_update_public.json')


def io(path):
    value = os.path.abspath(os.fspath(path))
    if os.name == 'nt' and not value.startswith('\\\\?\\'):
        value = '\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value
    return Path(value)


def read_json(path, default=None):
    try:
        return json.loads(io(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return default


def atomic_bytes(path, data):
    path = io(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='upd-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path, value):
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8'))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value):
        raise ValueError('更新版本号无效。')
    return tuple(map(int, value.split('.')))


def current_version(root):
    value = read_json(Path(root) / 'plugin.json')['version']
    version(value)
    return value


def state_dir(root):
    identity = os.path.normcase(str(Path(root).resolve()))
    base = Path(os.environ.get('LOCALAPPDATA', tempfile.gettempdir()))
    return base / 'LingyunUpdater' / digest(identity.encode('utf-8'))[:24]


def code_name(name):
    """Allow only program files; never replace user configuration or licenses."""
    if not isinstance(name, str) or "\\" in name or ":" in name or "\x00" in name:
        return False
    parts = PurePosixPath(name).parts
    if not parts or PurePosixPath(name).as_posix() != name or name.startswith('/') or any(p in ('.', '..') or p.endswith(('.', ' ')) for p in parts):
        return False
    if name in ('plugin.json', 'README.md', 'CHANGELOG.md', '安装使用说明.txt', *PUBLIC_FILES):
        return True
    if len(parts) == 1:
        return bool(re.fullmatch(r'(main|lingyun_[a-z0-9_]+)\.py', name))
    if len(parts) == 2 and parts[0] == 'lingyun_features':
        return bool(re.fullmatch(r'[a-z_]+\.py', parts[1]))
    return len(parts) == 2 and parts[0] == 'ui' and bool(re.fullmatch(r'[A-Za-z0-9_-]+\.(html|css|js)', parts[1]))


def safe_target(root, name):
    if not code_name(name):
        raise ValueError('更新包含有非程序文件或不安全路径：' + str(name))
    root = Path(root).resolve()
    target = root.joinpath(*PurePosixPath(name).parts)
    for part in [target, *target.parents]:
        if part == root:
            break
        if io(part).is_symlink() or (hasattr(io(part), 'is_junction') and io(part).is_junction()):
            raise ValueError('更新目标不允许链接或目录联接：' + name)
    if not target.resolve().is_relative_to(root):
        raise ValueError('更新目标越过插件目录。')
    return target


def verify_manifest(raw, signature, public):
    if len(raw) > 256 * 1024 or len(signature) > 4096:
        raise ValueError('更新清单过大。')
    n, e = int(public['n'], 16), int(public['e'])
    sig = base64.b64decode(signature.strip(), validate=True)
    width = (n.bit_length() + 7) // 8
    if len(sig) != width or int.from_bytes(sig, 'big') >= n or width < 256:
        raise ValueError('更新签名无效。')
    hashed = hashlib.sha256(DOMAIN + raw).digest()
    encoded = bytes.fromhex('3031300d060960864801650304020105000420') + hashed
    expected = b'\x00\x01' + b'\xff' * (width - len(encoded) - 3) + b'\x00' + encoded
    actual = pow(int.from_bytes(sig, 'big'), e, n).to_bytes(width, 'big')
    if not hmac.compare_digest(actual, expected):
        raise ValueError('更新签名验证失败，未安装任何文件。')
    manifest = json.loads(raw)
    if manifest.get('schema') != 1 or manifest.get('product') != PRODUCT:
        raise ValueError('更新清单不属于本插件。')
    release_version = manifest.get('version')
    version(release_version)
    expected_url = f'https://github.com/{REPOSITORY}/releases/download/v{release_version}/lingyun-toolkit-{release_version}.zip'
    if manifest.get('url') != expected_url or manifest.get('archive_root') != 'plugin':
        raise ValueError('更新下载地址无效。')
    size = manifest.get('size')
    if type(size) is not int or not 0 < size <= MAX_ARCHIVE:
        raise ValueError('更新包大小无效。')
    files = manifest.get('files')
    if not isinstance(files, dict) or not 1 <= len(files) <= 200:
        raise ValueError('更新文件清单无效。')
    hashes = [manifest.get('sha256'), *files.values()]
    if any(not isinstance(h, str) or not re.fullmatch('[0-9a-f]{64}', h) for h in hashes):
        raise ValueError('更新哈希值无效。')
    if any(not code_name(name) for name in files) or len({n.casefold() for n in files}) != len(files):
        raise ValueError('更新包含重复路径或个人数据路径。')
    required = {'main.py', 'lingyun_main.py', 'plugin.json', 'ui/index.html', 'lingyun_update_core.py', 'lingyun_update.py', 'lingyun_update_helper.py', 'lingyun_update_launch.py', 'lingyun_update_window.py', 'ui/host-bridge.js', 'ui/update-panel.js', 'lingyun_features/__init__.py', 'lingyun_features/license_manager.py', 'lingyun_features/sections.py', 'lingyun_features/assets.py', 'lingyun_features/mapper.py', *PUBLIC_FILES}
    if not required.issubset(files):
        raise ValueError('更新包缺少必要程序。')
    return manifest


def unpack(root, job, manifest):
    raw = io(Path(job) / 'package.zip').read_bytes()
    if len(raw) != manifest['size'] or digest(raw) != manifest['sha256']:
        raise ValueError('更新包大小或 SHA-256 不匹配。')
    payload = {}
    with zipfile.ZipFile(io(Path(job) / 'package.zip')) as archive:
        entries = archive.infolist()
        if len(entries) != len(manifest['files']) or sum(e.file_size for e in entries) > MAX_EXPANDED:
            raise ValueError('更新 ZIP 文件数量或展开大小异常。')
        for entry in entries:
            mode = entry.external_attr >> 16
            if entry.is_dir() or stat.S_ISLNK(mode) or entry.flag_bits & 1 or not entry.filename.startswith('plugin/'):
                raise ValueError('更新 ZIP 包结构无效。')
            name = entry.filename[len('plugin/'):]
            if name in payload or name not in manifest['files']:
                raise ValueError('更新 ZIP 含有重复或未声明的文件。')
            safe_target(root, name)
            data = archive.read(entry)
            if digest(data) != manifest['files'][name]:
                raise ValueError('更新程序校验失败：' + name)
            if name.endswith('.py'):
                ast.parse(data.decode('utf-8-sig'), filename=name)
            payload[name] = data
    info = json.loads(payload['plugin.json'])
    if info.get('version') != manifest['version'] or info.get('name') != 'lingyun_practical_toolkit':
        raise ValueError('更新包内的插件标识或版本不一致。')
    for name in PUBLIC_FILES:
        if payload[name] != io(Path(root) / name).read_bytes():
            raise ValueError('更新试图更换授权或更新公钥，已停止：' + name)
    if payload['main.py'] != io(Path(root) / 'main.py').read_bytes():
        raise ValueError('更新不能替换稳定启动器，请联系发布者提供兼容的更新包。')
    for name, data in payload.items():
        atomic_bytes(safe_target(Path(job) / 'payload', name), data)
    return payload


class FileLock:
    def __init__(self, path):
        self.path, self.file = Path(path), None

    def acquire(self, timeout=0):
        io(self.path.parent).mkdir(parents=True, exist_ok=True)
        f = io(self.path).open('a+b')
        if f.seek(0, 2) == 0:
            f.write(b'0')
            f.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                f.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.file = f
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    f.close()
                    raise TimeoutError('另一个更新或插件进程正在使用此目录，请稍后重试。') from None
                time.sleep(0.1)

    def close(self):
        if self.file:
            self.file.close()
            self.file = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def job_dir(root, job_id):
    if not isinstance(job_id, str) or not re.fullmatch('[0-9a-f]{32}', job_id):
        raise ValueError('更新任务编号无效。')
    return state_dir(root) / 'jobs' / job_id


def recover(root):
    """Caller must hold the installation gate. Restore an uncommitted transaction."""
    state = state_dir(root)
    transaction = read_json(state / 'transaction.json')
    if not transaction:
        return False
    job = job_dir(root, transaction['job'])
    for name, old in transaction['originals'].items():
        target = safe_target(root, name)
        if old is None:
            io(target).unlink(missing_ok=True)
        else:
            data = io(safe_target(job / 'backup', name)).read_bytes()
            if digest(data) != old:
                raise ValueError('更新恢复备份校验失败：' + name)
            atomic_bytes(target, data)
    clear_bytecode(root, transaction['originals'])
    io(state / 'transaction.json').unlink()
    write_json(state / 'status.json', dict(ok=False, state='error', running=False,
               message='上次更新未完成，已恢复原版本，可以重新检查更新。'))
    return True


def clear_bytecode(root, names):
    for name in names:
        if not name.endswith('.py'):
            continue
        target = safe_target(root, name)
        cache = target.parent / '__pycache__'
        if io(cache).is_symlink() or (hasattr(io(cache), 'is_junction') and io(cache).is_junction()):
            raise ValueError('插件字节码缓存目录不能是链接。')
        for item in io(cache).glob(target.stem + '.*.pyc'):
            item.unlink(missing_ok=True)


def startup(root):
    """Register a process lease before loading any other plugin modules."""
    state = state_dir(root)
    with FileLock(state / 'install.lock').acquire(timeout=120):
        recover(root)
        registry = getattr(sys, '_lingyun_update_process_leases', None)
        if registry is None:
            registry = {}
            sys._lingyun_update_process_leases = registry
        existing = registry.get(str(state))
        if existing is not None and existing.file is not None and not existing.file.closed:
            return existing
        lease = FileLock(state / 'leases' / f'{os.getpid()}-{time.time_ns()}.lock').acquire()
        registry[str(state)] = lease
        atexit.register(lease.close)
    return lease


def busy_leases(root):
    for path in io(state_dir(root) / 'leases').glob('*.lock'):
        try:
            with FileLock(path).acquire():
                pass
        except TimeoutError:
            return True
    return False


def host_root(root):
    for part in Path(root).resolve().parents:
        if part.name == '_internal':
            return part.parent
    return None


def process_images():
    """Read executable paths with Windows APIs; never inspect browser credentials."""
    if os.name != 'nt':
        return []
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    pids, needed = (w.DWORD * 65536)(), w.DWORD()
    if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
        raise OSError('无法确认宿主是否已退出，请重试。')
    result = []
    for pid in pids[:needed.value // ctypes.sizeof(w.DWORD)]:
        if not pid:
            continue
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            continue
        try:
            size, buffer = w.DWORD(32768), ctypes.create_unicode_buffer(32768)
            if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                result.append((int(pid), buffer.value))
        finally:
            kernel.CloseHandle(handle)
    return result


def host_busy(root, exclude_pid=None):
    host = host_root(root)
    if not host:
        return False
    prefix = os.path.normcase(str(host)) + os.sep
    return any(pid != exclude_pid and os.path.normcase(path).startswith(prefix)
               for pid, path in process_images())


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == 'nt':
        return any(item[0] == pid for item in process_images())
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def install(root, job_id, after_replace=None, on_progress=None):
    """Caller holds gate, and has established that no host/lease is active."""
    root, job = Path(root).resolve(), job_dir(root, job_id)
    raw = io(job / 'update.json').read_bytes()
    manifest = verify_manifest(raw, io(job / 'update.sig').read_bytes(), read_json(root / 'lingyun_update_public.json'))
    if version(manifest['version']) <= version(current_version(root)):
        raise ValueError('该更新不高于当前版本，请重新检查更新。')
    payload = unpack(root, job, manifest)
    state = state_dir(root)
    originals = {}
    for name in payload:
        target = safe_target(root, name)
        if io(target).exists():
            old = io(target).read_bytes()
            atomic_bytes(safe_target(job / 'backup', name), old)
            originals[name] = digest(old)
        else:
            originals[name] = None
    write_json(state / 'transaction.json', dict(job=job_id, originals=originals, version=manifest['version']))
    try:
        # Commit metadata last; startup guard serializes concurrent launches.
        ordered = sorted(payload, key=lambda n: (n == 'plugin.json', n == 'main.py', n))
        for completed, name in enumerate(ordered, 1):
            atomic_bytes(safe_target(root, name), payload[name])
            if after_replace:
                after_replace(name)
            if on_progress:
                on_progress(completed, len(ordered))
        clear_bytecode(root, payload)
        for name, data in payload.items():
            if digest(io(safe_target(root, name)).read_bytes()) != digest(data):
                raise OSError('安装后文件校验失败：' + name)
        io(state / 'transaction.json').unlink()
    except Exception:
        recover(root)
        raise
    write_json(state / 'status.json', dict(ok=True, state='done', running=False,
               current_version=manifest['version'], message=f"已更新到 V{manifest['version']}，可以重新打开字字动画。"))
    return manifest['version']
