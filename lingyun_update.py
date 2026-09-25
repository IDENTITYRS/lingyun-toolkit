"""Session update checks and background downloads, independent of account requests."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import build_opener, HTTPRedirectHandler, Request
import uuid

spec = importlib.util.spec_from_file_location('update_core', Path(__file__).with_name('lingyun_update_core.py'))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

launch_spec = importlib.util.spec_from_file_location('lingyun_update_launch', Path(__file__).with_name('lingyun_update_launch.py'))
launcher = importlib.util.module_from_spec(launch_spec)
launch_spec.loader.exec_module(launcher)


def release_notes(text, fallback=None):
    """Read cumulative Markdown logs, also accepting older unversioned manifests."""
    headings = list(re.finditer(r'^#{1,6}\s+[vV]?(\d+\.\d+\.\d+)(?:[^\S\r\n]+[^\r\n]*)?\s*$', str(text), re.M))
    releases = {}
    for index, heading in enumerate(headings):
        number = heading.group(1)
        try:
            core.version(number)
        except ValueError:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        releases.setdefault(number, str(text)[heading.end():end].strip())
    if not headings and str(text).strip() and fallback:
        core.version(fallback)
        releases[fallback] = str(text).strip()
    return releases


class GitHubRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.hostname not in (
                'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'):
            raise ValueError('更新下载跳转到了不受支持的地址。')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Manager:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.state = core.state_dir(self.root)
        # The host can re-import the plugin when switching panels. Keep the flag
        # on sys, scoped to this installation and this backend process lifetime.
        sessions = sys.__dict__.setdefault('_lingyun_update_sessions', {})
        self.session = sessions.setdefault(os.path.normcase(str(self.root)),
                                           dict(checked=False, lock=threading.Lock()))

    def history(self):
        releases = {}
        current = core.version(core.current_version(self.root))
        # Release archives append the complete changelog to README.md, a path
        # already accepted by the original signed updater.
        for name in ('CHANGELOG.md', 'README.md'):
            path = core.io(self.root / name)
            if path.is_file():
                releases = {v: n for v, n in release_notes(path.read_text(encoding='utf-8-sig')).items()
                            if core.version(v) <= current}
                if releases:
                    break
        cached = core.read_json(self.state / 'release-notes.json', {})
        if isinstance(cached, dict):
            for number, notes in cached.items():
                try:
                    core.version(number)
                except ValueError:
                    continue
                if isinstance(notes, str):
                    releases[number] = notes
        return releases

    def remember_notes(self, manifest):
        releases = self.history()
        # Migrate notes from older updater state before replacing that state.
        previous = core.read_json(self.state / 'status.json', {})
        if previous.get('latest_version') and previous.get('notes'):
            releases.update(release_notes(previous['notes'], previous['latest_version']))
        releases.update({v: n for v, n in release_notes(manifest.get('notes', ''), manifest['version']).items()
                         if core.version(v) <= core.version(manifest['version'])})
        core.write_json(self.state / 'release-notes.json', releases)

    def status(self):
        value = core.read_json(self.state / 'status.json', {})
        if value.get('state') == 'launching_helper' and value.get('job'):
            process = core.read_json(core.job_dir(self.root, value['job']) / 'helper-process.json', {})
            if process.get('pid'):
                value['helper_pid'] = process['pid']
        if value.get('running') and not core.pid_alive(value.get('helper_pid') or value.get('worker_pid')):
            value = dict(ok=False, state='error', running=False, message='上次更新进程已结束，请重新检查更新。')
        result = dict(ok=True, state='idle', running=False, message='点击检查更新。')
        result.update(value)
        result['current_version'] = core.current_version(self.root)
        history = self.history()
        result['releases'] = [dict(version=v, notes=history[v])
                              for v in sorted(history, key=core.version, reverse=True)]
        return result

    def save(self, **value):
        core.write_json(self.state / 'status.json', value)
        return self.status()

    def fetch(self, url, limit, destination=None, expected_size=None, release_version=None):
        opener = build_opener(GitHubRedirect())  # System proxy is allowed for GitHub.
        request = Request(url, headers={'User-Agent': 'Lingyun-Updater/1.0', 'Cache-Control': 'no-cache'})
        try:
            with opener.open(request, timeout=25) as response:
                total = response.headers.get('Content-Length')
                if total and int(total) > limit:
                    raise ValueError('更新下载超过大小限制。')
                chunks, count, last = [], 0, 0
                while True:
                    chunk = response.read(16 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    count += len(chunk)
                    if count > limit:
                        raise ValueError('更新下载超过大小限制。')
                    if destination and time.monotonic() - last > .2:
                        self.save(ok=True, state='downloading', running=True, worker_pid=os.getpid(),
                                  latest_version=release_version,
                                  downloaded_bytes=count, total_bytes=expected_size,
                                  progress=min(99, int(count * 100 / expected_size)) if expected_size else 0,
                                  message='正在下载更新包…')
                        last = time.monotonic()
                data = b''.join(chunks)
                if destination:
                    core.atomic_bytes(destination, data)
                    self.save(ok=True, state='verifying', running=True, worker_pid=os.getpid(),
                              latest_version=release_version, progress=100,
                              downloaded_bytes=count, total_bytes=expected_size,
                              message='下载完成，正在校验安装包…')
                return data
        except HTTPError as exc:
            if exc.code == 404:
                raise ValueError('尚未发布更新，或更新附件不完整，请稍后重试。') from None
            raise ValueError(f'GitHub 返回 HTTP {exc.code}，请稍后重试。') from None
        except (URLError, TimeoutError, OSError):
            raise ValueError('连接 GitHub 失败，请检查网络或系统代理后重试。') from None

    def latest(self):
        base = f'https://github.com/{core.REPOSITORY}/releases'
        raw = self.fetch(base + '/latest/download/update.json', 256 * 1024)
        claimed = json.loads(raw).get('version')
        core.version(claimed)
        signature = self.fetch(base + f'/download/v{claimed}/update.sig', 4096)
        manifest = core.verify_manifest(raw, signature, core.read_json(self.root / 'lingyun_update_public.json'))
        return manifest, raw, signature

    def wait_for_helper(self, process, job, timeout=20):
        """Never ask the user to exit until the independent window is visible."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready = core.read_json(job / 'helper-ready.json', {})
            if (ready.get('pid') == process.pid and ready.get('job') == job.name
                    and ready.get('window_ready') is True):
                return
            detail = core.read_json(self.state / 'status.json', {})
            if detail.get('job') == job.name and detail.get('state') == 'error':
                raise ValueError(detail.get('message') or '更新助手启动失败。')
            if process.poll() is not None:
                raise ValueError(detail.get('message') or '更新助手未能启动，请保持字字动画打开并重试。')
            time.sleep(.1)
        # The helper remains responsible for an eventual startup result; keeping
        # its running state prevents duplicate installers while it is starting.
        return

    def begin(self, install=False, automatic=False):
        with self.session['lock']:
            if automatic and self.session['checked']:
                return self.status()
            return self._begin(install)

    def _begin(self, install):
        status = self.status()
        if status.get('running'):
            self.session['checked'] = True
            return status
        try:
            operation = core.FileLock(self.state / 'operation.lock').acquire()
        except TimeoutError:
            return self.status()
        try:
            # Keep existing notes independently of transient checking/download states.
            if status.get('latest_version') and status.get('notes'):
                self.remember_notes(dict(version=status['latest_version'], notes=status['notes']))
            result = self.save(ok=True, state='checking', running=True, worker_pid=os.getpid(),
                               message='正在连接 GitHub 检查新版本，请稍候…网络较慢时可能需要一些时间。')
            self.session['checked'] = True
        except Exception:
            operation.close()
            raise

        def work():
            try:
                manifest, raw, signature = self.latest()
                self.remember_notes(manifest)
                newer = core.version(manifest['version']) > core.version(core.current_version(self.root))
                if not newer or not install:
                    self.save(ok=True, state='available' if newer else 'latest', running=False,
                              latest_version=manifest['version'], notes=str(manifest.get('notes', '')),
                              message=f"发现新版本 V{manifest['version']}" if newer else '当前已是最新版。')
                    return
                job_id = uuid.uuid4().hex
                job = core.job_dir(self.root, job_id)
                core.atomic_bytes(job / 'update.json', raw)
                core.atomic_bytes(job / 'update.sig', signature)
                self.save(ok=True, state='downloading', running=True, worker_pid=os.getpid(), progress=0,
                          latest_version=manifest['version'], downloaded_bytes=0, total_bytes=manifest['size'],
                          message='正在连接下载服务器…')
                self.fetch(manifest['url'], core.MAX_ARCHIVE, job / 'package.zip', manifest['size'], manifest['version'])
                core.unpack(self.root, job, manifest)
                # Use copies of the CURRENT trusted helper, not code from the download.
                for name in ('lingyun_update_core.py', 'lingyun_update_helper.py', 'lingyun_update_window.py'):
                    core.atomic_bytes(job / name, core.io(self.root / name).read_bytes())
                host = core.host_root(self.root)
                executable = host / '_internal' / 'python-3.12' / 'pythonw.exe' if host else None
                if not executable or not executable.is_file():
                    raise ValueError('未找到字字动画内置 Python，请在正式安装目录内更新。')
                args = [str(executable), '-B', str(job / 'lingyun_update_helper.py'), '--root', str(self.root), '--job', job_id]
                self.save(ok=True, state='launching_helper', running=True, worker_pid=os.getpid(),
                          latest_version=manifest['version'], job=job_id, progress=100,
                          message='校验完成，正在打开独立更新助手，请稍候…')
                # The host may terminate its entire process tree on exit. Console
                # detachment alone does not protect a child from that cleanup.
                helper = launcher.launch(args, job)
                try:
                    core.write_json(job / 'helper-process.json', dict(pid=helper.pid, launch_method='local-wmi'))
                    self.wait_for_helper(helper, job)
                finally:
                    helper.close()
            except Exception as exc:
                # No account payload, signed URL, or authentication headers enter logs.
                self.save(ok=False, state='error', running=False, operation='install' if install else 'check', message=str(exc))
            finally:
                operation.close()
        try:
            threading.Thread(target=work, daemon=True, name='Lingyun-update').start()
        except Exception:
            operation.close()
            self.save(ok=False, state='error', running=False, message='更新检查未能启动，请重试。')
            raise
        return result
