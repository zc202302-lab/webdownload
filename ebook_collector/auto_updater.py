"""Detached windowless updater: verify, wait, replace one EXE, rollback, restart."""
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def process_options():
    return {'env': dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1'),
            'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL,
            'creationflags': subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0}


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def prepare_update(candidate, expected, target=None, parent_pid=None, smoke_test=False):
    target = Path(target or sys.executable).resolve()
    candidate = Path(candidate).resolve()
    if candidate.parent.parent != target.parent / '.updates' or candidate.name != 'WebDownload.exe':
        raise ValueError('更新暂存位置不正确')
    if digest(candidate) != expected:
        raise ValueError('更新程序校验失败')
    # Run the actual candidate before asking the original GUI to exit.
    subprocess.run([str(candidate), '--self-test'], check=True, timeout=120, **process_options())
    helper = candidate.parent / 'updater.exe'
    # The verified candidate carries the newest updater and launch handshake.
    shutil.copy2(candidate, helper)
    plan = candidate.parent / 'plan.json'
    data = {'target': str(target), 'candidate': str(candidate), 'sha256': expected,
            'parent_pid': os.getpid() if parent_pid is None else parent_pid, 'smoke_test': smoke_test}
    plan.write_text(json.dumps(data), encoding='utf-8')
    return helper, plan


def launch_updater(helper, plan):
    return subprocess.Popen([str(helper), '--apply-update', str(plan)], cwd=Path(plan).parent, **process_options())


def wait_parent(pid):
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # Already exited / no such PID.
            return
        raise OSError('无法等待旧程序退出，更新取消。')
    try:
        if kernel.WaitForSingleObject(handle, 90000) != 0:
            raise RuntimeError('旧程序尚未退出，更新取消。')
    finally:
        kernel.CloseHandle(handle)


def replace_retry(source, destination, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def install_candidate(target, candidate, backup, expected):
    if digest(candidate) != expected:
        raise ValueError('安装前校验失败，原程序未修改。')
    replace_retry(target, backup)
    try:
        replace_retry(candidate, target)
    except BaseException:
        replace_retry(backup, target)
        raise


def wait_startup(process,marker,target,timeout=45):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if marker.exists():
            try:
                data=json.loads(marker.read_text(encoding='utf-8'))
                if data.get('ready') and Path(data['root']).resolve()==target.parent.resolve():
                    if process.poll() is None:
                        return
            except (ValueError,KeyError,OSError):
                pass
        if process.poll() is not None:
            raise RuntimeError(f'新版启动后退出，退出码：{process.returncode}')
        time.sleep(0.2)
    raise RuntimeError('新版未在45秒内确认主窗口启动。')


def apply_update(plan_path):
    plan_path = Path(plan_path).resolve()
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    target, candidate = Path(plan['target']).resolve(), Path(plan['candidate']).resolve()
    if (plan_path.name != 'plan.json' or candidate.parent != plan_path.parent
            or candidate.parent.parent != target.parent / '.updates'
            or candidate.name != 'WebDownload.exe' or target.suffix.lower() != '.exe'):
        raise ValueError('更新计划路径无效')
    backup = plan_path.parent / 'previous.exe'
    if plan['parent_pid'] > 0:
        wait_parent(plan['parent_pid'])
    elif not plan.get('smoke_test'):
        raise ValueError('旧程序进程信息无效')
    install_candidate(target, candidate, backup, plan['sha256'])
    restarted=None
    try:
        marker=plan_path.parent/'startup.json'
        marker.unlink(missing_ok=True)
        args = ['--updated','--startup-result',str(marker)]
        if plan.get('smoke_test'):
            args = ['--self-test', '--self-test-result', str(plan_path.parent / 'restarted.json')]
        restarted = subprocess.Popen([str(target), *args], cwd=target.parent, **process_options())
        if plan.get('smoke_test'):
            if restarted.wait(timeout=120) != 0:
                raise RuntimeError('新版启动测试失败')
        else:
            wait_startup(restarted,marker,target)
    except BaseException as exc:
        try:
            (plan_path.parent/'error.txt').write_text(str(exc),encoding='utf-8')
        except OSError:
            pass
        if restarted and restarted.poll() is None:
            from task_process import kill_tree
            # This updater child is a dedicated owned process on Windows.
            if os.name=='nt': kill_tree(restarted)
            else: restarted.kill();restarted.wait(timeout=5)
        replace_retry(backup, target)
        if not plan.get('smoke_test'):
            subprocess.Popen([str(target)],cwd=target.parent,**process_options())
        raise
    # Leave previous.exe available for recovery. It never contains user data.
    return target

