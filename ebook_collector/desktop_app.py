"""Windowless desktop application with graphical tasks and verified self-update."""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from pathlib import Path
import json
import time

from app_runtime import VERSION, app_root
from update_checker import check_update, download_update
from auto_updater import prepare_update, launch_updater, apply_update, process_options
from task_window import TaskWindow


def run_collection(mode):
    root = tk.Tk()
    TaskWindow(root, mode)
    root.mainloop()


def self_test():
    # Frozen smoke test: imports and browser driver must survive packaging.
    import collector
    import recipe_collector
    import book_collector
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        assert playwright.chromium.name == 'chromium'
    assert collector.ROOT == app_root()
    assert recipe_collector.ROOT == app_root()
    assert book_collector.ROOT == app_root()
    import tempfile
    from pathlib import Path
    from PIL import Image
    from library_manager import validate_pdf
    with tempfile.TemporaryDirectory() as folder:
        image = Path(folder) / 'test.jpg'
        pdf = Path(folder) / 'test.pdf'
        Image.new('RGB', (100, 150), 'white').save(image)
        collector.build_pdf([image], pdf)
        assert validate_pdf(pdf, 1) == 1
    root = tk.Tk()
    root.withdraw()
    app = App(root)
    root.update_idletasks()
    assert len(app.start_buttons) == 4
    root.destroy()
    # Verify GUI prompts, log routing and completion without console stdin.
    result = []
    root = tk.Tk()
    root.withdraw()
    def task():
        result.append(input('输入测试值'))
        print('图形任务日志测试')
    window = TaskWindow(root, 'single-book', runner=task)
    assert window.submit_button.cget('text')=='开始下载'
    deadline = __import__('time').monotonic() + 15
    def advance():
        if window.pending:
            window.entry.insert(0, 'GUI answer')
            window.submit()
        if window.finished or __import__('time').monotonic() > deadline:
            root.quit()
        else:
            root.after(100, advance)
    root.after(100, advance)
    root.mainloop()
    assert result == ['GUI answer'] and window.finished
    assert '图形任务日志测试' in window.log.get('1.0', 'end')
    window.thread.join(timeout=5)
    root.destroy()
    # Real owned subprocess cancellation, including a blocked operation.
    from task_process import TaskProcess
    event_queue=queue.Queue()
    cancellation=threading.Event()
    def test_command(port,token):
        base=[sys.executable] if getattr(sys,'frozen',False) else [sys.executable,str(app_root()/'desktop_app.py')]
        return base+['--task-worker-test','block',str(port),token]
    controller=TaskProcess('magazines',event_queue,cancellation,test_command)
    thread=threading.Thread(target=controller.run)
    thread.start()
    try:
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            try: kind,value=event_queue.get(timeout=0.2)
            except queue.Empty: continue
            if kind=='log' and 'block started' in value: break
        else: raise RuntimeError('Worker did not start')
    finally:
        cancellation.set()
        thread.join(timeout=10)
    assert not thread.is_alive() and controller.process.poll() is not None
    # Validate the actual main-loop acknowledgement used by restart checking.
    from auto_updater import wait_startup
    with tempfile.TemporaryDirectory() as folder:
        marker=Path(folder)/'startup.json'
        base=[sys.executable] if getattr(sys,'frozen',False) else [sys.executable,str(app_root()/'desktop_app.py')]
        process=subprocess.Popen(base+['--startup-test','--startup-result',str(marker)],**process_options())
        try:
            wait_startup(process,marker,app_root()/'WebDownload.exe',timeout=20)
            assert process.wait(timeout=10)==0
        finally:
            if process.poll() is None: process.kill();process.wait(timeout=5)
    if os.name == 'nt' and getattr(sys, 'frozen', False):
        import ctypes
        assert not ctypes.windll.kernel32.GetConsoleWindow(), 'EXE unexpectedly has a console'
    if '--self-test-result' in sys.argv:
        index = sys.argv.index('--self-test-result')
        Path(sys.argv[index + 1]).write_text(json.dumps({'version': VERSION, 'root': str(app_root()), 'console': False}), encoding='utf-8')
    print('EXE self-test passed:', VERSION)


def self_test_update():
    """Exercise the real detached updater, replacement and restart on Windows."""
    import tempfile
    import shutil
    from auto_updater import digest
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder).resolve()
        target = root / 'WebDownload.exe'
        shutil.copy2(sys.executable, target)
        staged = root / '.updates' / 'test'
        staged.mkdir(parents=True)
        candidate = staged / 'WebDownload.exe'
        shutil.copy2(sys.executable, candidate)
        preserved = [root / 'settings.json', root / 'output' / '_work' / 'progress.json',
                     root / 'browser_profile_msedge' / 'state', root / 'output' / 'saved.pdf']
        for path in preserved:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'preserve-user-data')
        helper, plan = prepare_update(candidate, digest(candidate), target=target, parent_pid=0, smoke_test=True)
        parent = subprocess.Popen([str(target), '--update-parent-test'], **process_options())
        data = json.loads(plan.read_text())
        data['parent_pid'] = parent.pid
        plan.write_text(json.dumps(data), encoding='utf-8')
        process = launch_updater(helper, plan)
        code = process.wait(timeout=180)
        error = staged / 'error.txt'
        assert code == 0, error.read_text() if error.exists() else 'updater helper failed'
        assert parent.wait(timeout=30) == 0
        marker = json.loads((staged / 'restarted.json').read_text())
        assert marker['version'] == VERSION and Path(marker['root']).resolve() == root and not marker['console'], (marker, str(root))
        assert all(path.read_bytes() == b'preserve-user-data' for path in preserved)
        assert (staged / 'previous.exe').exists() and target.exists()
    if '--self-test-result' in sys.argv:
        index = sys.argv.index('--self-test-result')
        Path(sys.argv[index + 1]).write_text(json.dumps({'update': True, 'data_preserved': True, 'console': False}), encoding='utf-8')


class App:
    def __init__(self, root):
        self.root = root
        self.tasks = []
        self.events = queue.Queue()
        self.updating = False
        root.title('东方美食资料下载工具')
        root.geometry('560x540')
        root.minsize(520, 520)
        style = ttk.Style(root)
        if 'vista' in style.theme_names():
            style.theme_use('vista')
        frame = ttk.Frame(root, padding=28)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='东方美食资料下载工具', font=('Microsoft YaHei', 18, 'bold')).pack(anchor='w')
        ttk.Label(frame, text=f'版本 {VERSION} · Windows 便携版', padding=(0, 8)).pack(anchor='w')
        ttk.Label(frame, text='选择任务后，会打开浏览器和下载窗口。\n请自行登录网站，再按下载窗口提示开始。', padding=(0, 10)).pack(anchor='w')
        self.start_buttons = []
        for text, mode in [('下载电子杂志', 'magazines'), ('下载电子书', 'books'), ('单本电子书', 'single-book'), ('下载爆款菜谱', 'recipes')]:
            button = ttk.Button(frame, text=text, command=lambda m=mode: self.start(m))
            button.pack(fill='x', pady=6)
            self.start_buttons.append(button)
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=12)
        ttk.Button(row, text='打开资料库', command=self.open_output).pack(side='left')
        ttk.Button(row, text='打开下载目录', command=self.open_download_root).pack(side='left',padx=6)
        ttk.Button(row, text='打开程序文件夹', command=lambda: os.startfile(app_root())).pack(side='left', padx=8)
        self.update_button = ttk.Button(row, text='检查更新', command=self.update)
        self.update_button.pack(side='right')
        self.status = tk.StringVar(value=f'已自动更新至 {VERSION}。' if '--updated' in sys.argv else '就绪。任务未完成时会保留下载进度。')
        ttk.Label(frame, textvariable=self.status, wraplength=480).pack(anchor='w', pady=6)
        ttk.Label(frame, text='点击“检查更新”后，有新版将自动下载安装并重启。\n设置、登录状态和下载资料会保留。', wraplength=480).pack(anchor='w')
        root.after(250, self.poll)

    def start(self, mode):
        if self.updating:
            return
        self.tasks = [task for task in self.tasks if task.poll() is None]
        if self.tasks:
            messagebox.showinfo('任务正在运行', '请先完成或关闭当前任务窗口，避免同时使用同一个浏览器登录目录。')
            return
        command = ([sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, str(app_root() / 'desktop_app.py')])
        try:
            self.tasks.append(subprocess.Popen(command + ['--collect', mode], cwd=app_root(),
                                              **process_options()))
            self.status.set('下载窗口已打开，可在该窗口点击“停止下载”。')
        except OSError as exc:
            messagebox.showerror('启动失败', str(exc))

    def open_output(self):
        from library_manager import read_settings
        try:
            output = read_settings(app_root())['output_path']
            output.mkdir(parents=True, exist_ok=True)
            os.startfile(output)
        except Exception as exc:
            messagebox.showerror('无法打开资料库', str(exc))

    def open_download_root(self):
        from library_manager import read_settings
        try:
            directory=read_settings(app_root())['output_path'].parent
            directory.mkdir(parents=True,exist_ok=True)
            os.startfile(directory)
        except Exception as exc:
            messagebox.showerror('无法打开下载目录',str(exc))

    def update(self):
        self.tasks = [task for task in self.tasks if task.poll() is None]
        if self.tasks:
            messagebox.showinfo('任务正在运行', '请先停止下载并关闭下载窗口，再更新软件。')
            return
        if self.updating:
            return
        if not getattr(sys, 'frozen', False):
            messagebox.showinfo('检查更新', '自动更新适用于Windows EXE版本。源码版本请使用Git更新。')
            return
        self.updating = True
        self.update_button.state(['disabled'])
        self.status.set('正在检查更新……')
        def worker():
            try:
                release = check_update()
                if not release['available']:
                    self.events.put(('latest', None))
                    return
                self.events.put(('progress', f'发现新版 {release["version"]}，正在下载……'))
                last_progress = [-1, 0.0]
                def progress(received, total):
                    percent = received * 100 // total
                    now = time.monotonic()
                    if percent != last_progress[0] and (percent == 100 or now - last_progress[1] >= 0.3):
                        last_progress[:] = [percent, now]
                        self.events.put(('progress', f'正在下载更新：{percent}%'))
                candidate, expected = download_update(release, app_root(), progress)
                self.events.put(('progress', '下载已校验，正在验证新版程序……'))
                helper, plan = prepare_update(candidate, expected)
                self.events.put(('install', (helper, plan)))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        previous = bool(self.tasks)
        self.tasks = [task for task in self.tasks if task.poll() is None]
        for button in self.start_buttons:
            button.state(['disabled'] if self.tasks or self.updating else ['!disabled'])
        if previous and not self.tasks:
            self.status.set('下载窗口已关闭。完成情况请查看资料库目录。')
        try:
            kind, value = self.events.get_nowait()
            if kind in ('error', 'latest'):
                self.updating = False
                self.update_button.state(['!disabled'])
            if kind == 'error':
                self.status.set('更新检查失败，下载功能不受影响。')
                messagebox.showwarning('检查更新', value)
            elif kind == 'progress':
                self.status.set(value)
            elif kind == 'install':
                try:
                    launch_updater(*value)
                except Exception as exc:
                    self.updating = False
                    self.update_button.state(['!disabled'])
                    messagebox.showerror('无法安装更新', str(exc))
                else:
                    self.root.destroy()
                    return
            elif kind == 'latest':
                self.status.set(f'当前已是最新版本（{VERSION}）。')
                messagebox.showinfo('检查更新', f'当前已是最新版本（{VERSION}）。')
        except queue.Empty:
            pass
        self.root.after(250, self.poll)


if __name__ == '__main__':
    # Windowed PyInstaller sets standard streams to None. Libraries still need
    # writable streams; task windows replace these with their graphical logger.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w', encoding='utf-8')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w', encoding='utf-8')
    if len(sys.argv)==5 and sys.argv[1] in ('--task-worker','--task-worker-test'):
        from task_process import run_worker
        run_worker(sys.argv[2],sys.argv[3],sys.argv[4],test=sys.argv[1]=='--task-worker-test')
    elif '--update-parent-test' in sys.argv:
        import time
        time.sleep(5)
    elif '--self-test' in sys.argv or '--self-test-update' in sys.argv:
        try:
            self_test_update() if '--self-test-update' in sys.argv else self_test()
        except Exception:
            # Report to build jobs without displaying a blocking crash dialog.
            import traceback
            if '--self-test-result' in sys.argv:
                index = sys.argv.index('--self-test-result')
                Path(sys.argv[index + 1]).write_text(traceback.format_exc(), encoding='utf-8')
            raise SystemExit(1)
    elif len(sys.argv) == 3 and sys.argv[1] == '--apply-update':
        try:
            apply_update(sys.argv[2])
        except Exception as exc:
            try:
                testing = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8')).get('smoke_test')
            except (OSError, ValueError):
                testing = False
            if testing:
                Path(sys.argv[2]).with_name('error.txt').write_text(str(exc), encoding='utf-8')
            else:
                messagebox.showerror('自动更新失败', f'{exc}\n原程序或备份已保留，请重新打开原程序。')
            raise SystemExit(1)
    elif len(sys.argv) == 3 and sys.argv[1] == '--collect' and sys.argv[2] in ('magazines', 'recipes', 'books', 'single-book'):
        run_collection(sys.argv[2])
    else:
        root = tk.Tk()
        App(root)
        if '--startup-test' in sys.argv:
            root.withdraw()
            root.after(2000,root.destroy)
        if '--startup-result' in sys.argv:
            marker=Path(sys.argv[sys.argv.index('--startup-result')+1])
            def startup_ready():
                temporary=marker.with_suffix('.tmp')
                temporary.write_text(json.dumps({'ready':True,'version':VERSION,'root':str(app_root()),'pid':os.getpid()}),encoding='utf-8')
                temporary.replace(marker)
            root.after(100,startup_ready)
        root.mainloop()

