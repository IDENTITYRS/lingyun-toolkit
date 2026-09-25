"""Visible detached installer; waits for the host without terminating it."""
import argparse
import ctypes
import importlib.util
import os
from pathlib import Path
import time

spec = importlib.util.spec_from_file_location('update_core', Path(__file__).with_name('lingyun_update_core.py'))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


def create_window():
    spec = importlib.util.spec_from_file_location('update_window', Path(__file__).with_name('lingyun_update_window.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.UpdateWindow()


def run(root, job_id, notify=True, window_factory=None):
    root = Path(root).resolve()
    state, job = core.state_dir(root), core.job_dir(root, job_id)
    window, version = None, ''
    helper_lock = core.FileLock(state / 'helper.lock').acquire()

    def report(phase, message, **fields):
        value = dict(ok=phase != 'error', state=phase, running=phase not in ('done', 'error'),
                     helper_pid=os.getpid(), job=job_id, latest_version=version, message=message)
        value.update(fields)
        core.write_json(state / 'status.json', value)
        if window is not None:
            window.update(dict(value, version=version))

    def failed(exc):
        message = '更新未完成：' + str(exc) + '\n请关闭本窗口，重新打开字字动画后重试。'
        if core.io(state / 'transaction.json').exists():
            message += '\n恢复尚未完成；请保留更新目录：' + str(state)
        report('error', message)
        return message

    try:
        manifest = core.verify_manifest(core.io(job / 'update.json').read_bytes(),
                                        core.io(job / 'update.sig').read_bytes(),
                                        core.read_json(root / 'lingyun_update_public.json'))
        version = manifest['version']
        waiting = (f'V{version} 安装包已准备好，请完全退出字字动画。\n'
                   '退出后，本窗口会自动开始安装并显示进度。无需再次点击更新。')
        if notify and os.name == 'nt':
            window = (window_factory or create_window)()
            window.update(dict(state='waiting_exit', version=version, message=waiting, progress=100))
            window.show()
        report('waiting_exit', waiting, progress=100)
        core.write_json(job / 'helper-ready.json', dict(pid=os.getpid(), job=job_id,
                                                       window_ready=window is not None, version=version))
    except Exception as exc:
        message = failed(exc)
        helper_lock.close()
        if notify and os.name == 'nt':
            ctypes.windll.user32.MessageBoxW(None, message, '凌云插件更新助手未能启动', 0x10 | 0x10000)
        return

    def work():
        try:
            while True:
                with core.FileLock(state / 'install.lock').acquire(timeout=120):
                    if not core.host_busy(root, os.getpid()) and not core.busy_leases(root):
                        report('installing', f'正在安装 V{version}，请暂时不要打开字字动画…',
                               progress=0, installed_files=0, total_files=len(manifest['files']))
                        core.recover(root)

                        def progress(completed, total):
                            message = (f'正在安装 V{version}：已写入 {completed} / {total} 个程序文件。'
                                       if completed < total else '程序文件已写入，正在核对安装结果…')
                            report('installing', message, progress=min(99, completed * 100 // total),
                                   installed_files=completed, total_files=total)

                        installed = core.install(root, job_id, on_progress=progress)
                        report('done', f'已成功更新到 V{installed}。\n授权、激活和设置已保留。请关闭本窗口，再打开字字动画。',
                               progress=100, current_version=installed,
                               installed_files=len(manifest['files']), total_files=len(manifest['files']))
                        break
                time.sleep(1)
        except Exception as exc:
            failed(exc)
        finally:
            helper_lock.close()

    if window is not None:
        window.run(work)
    else:
        work()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--job', required=True)
    parser.add_argument('--no-notify', action='store_true')
    args = parser.parse_args()
    run(args.root, args.job, not args.no_notify)
