"""Windows portable desktop launcher, with collectors in separate consoles."""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser

from app_runtime import VERSION, app_root
from update_checker import check_update


def run_collection(mode):
    if mode == 'recipes':
        from recipe_collector import main
    else:
        from collector import main
    try:
        main()
    except KeyboardInterrupt:
        print('\n已停止，进度保留。')
    except Exception as exc:
        print(f'\n未完成：{exc}\n进度已保留。')
    input('按回车关闭任务窗口。')


def self_test():
    # Frozen smoke test: imports and browser driver must survive packaging.
    import collector
    import recipe_collector
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        assert playwright.chromium.name == 'chromium'
    assert collector.ROOT == app_root()
    assert recipe_collector.ROOT == app_root()
    print('EXE self-test passed:', VERSION)


class App:
    def __init__(self, root):
        self.root = root
        self.tasks = []
        self.events = queue.Queue()
        root.title('东方美食资料下载工具')
        root.geometry('560x420')
        root.minsize(520, 400)
        style = ttk.Style(root)
        if 'vista' in style.theme_names():
            style.theme_use('vista')
        frame = ttk.Frame(root, padding=28)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='东方美食资料下载工具', font=('Microsoft YaHei', 18, 'bold')).pack(anchor='w')
        ttk.Label(frame, text=f'版本 {VERSION} · Windows 便携版', padding=(0, 8)).pack(anchor='w')
        ttk.Label(frame, text='选择任务后，会打开浏览器和操作窗口。\n请自行登录网站，再按操作窗口提示开始。', padding=(0, 10)).pack(anchor='w')
        self.start_buttons = []
        for text, mode in [('下载电子杂志', 'magazines'), ('下载爆款菜谱', 'recipes')]:
            button = ttk.Button(frame, text=text, command=lambda m=mode: self.start(m))
            button.pack(fill='x', pady=6)
            self.start_buttons.append(button)
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=12)
        ttk.Button(row, text='打开资料库', command=self.open_output).pack(side='left')
        ttk.Button(row, text='打开程序文件夹', command=lambda: os.startfile(app_root())).pack(side='left', padx=8)
        self.update_button = ttk.Button(row, text='检查更新', command=self.update)
        self.update_button.pack(side='right')
        self.status = tk.StringVar(value='就绪。任务未完成时会保留下载进度。')
        ttk.Label(frame, textvariable=self.status, wraplength=480).pack(anchor='w', pady=6)
        ttk.Label(frame, text='升级方法：关闭软件，将新版EXE替换到原文件夹。\n保留 settings.json、output 和 browser_profile 开头的文件夹。', wraplength=480).pack(anchor='w')
        root.after(250, self.poll)

    def start(self, mode):
        if self.tasks:
            messagebox.showinfo('任务正在运行', '请先完成或关闭当前任务窗口，避免同时使用同一个浏览器登录目录。')
            return
        command = ([sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, str(app_root() / 'desktop_app.py')])
        try:
            self.tasks.append(subprocess.Popen(command + ['--collect', mode], cwd=app_root(),
                                              env=dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1'),
                                              creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0))
            self.status.set('任务窗口已打开；停止任务可在任务窗口按 Ctrl+C。')
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

    def update(self):
        self.update_button.state(['disabled'])
        self.status.set('正在检查更新……')
        def worker():
            try:
                self.events.put(('update', check_update()))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        previous = bool(self.tasks)
        self.tasks = [task for task in self.tasks if task.poll() is None]
        for button in self.start_buttons:
            button.state(['disabled'] if self.tasks else ['!disabled'])
        if previous and not self.tasks:
            self.status.set('任务窗口已关闭。完成情况请查看资料库目录。')
        try:
            kind, value = self.events.get_nowait()
            self.update_button.state(['!disabled'])
            if kind == 'error':
                self.status.set('更新检查失败，下载功能不受影响。')
                messagebox.showwarning('检查更新', value)
            elif value['available']:
                self.status.set(f'发现新版 {value["version"]}')
                if messagebox.askyesno('发现新版', f'当前版本：{VERSION}\n最新版本：{value["version"]}\n\n{value["notes"]}\n\n是否打开官方下载页？'):
                    webbrowser.open(value['url'])
            else:
                self.status.set(f'当前已是最新版本（{VERSION}）。')
                messagebox.showinfo('检查更新', f'当前已是最新版本（{VERSION}）。')
        except queue.Empty:
            pass
        self.root.after(250, self.poll)


if __name__ == '__main__':
    if '--self-test' in sys.argv:
        self_test()
    elif len(sys.argv) == 3 and sys.argv[1] == '--collect' and sys.argv[2] in ('magazines', 'recipes'):
        run_collection(sys.argv[2])
    else:
        if os.name == 'nt':
            # The console-enabled build allows child tasks to use input().
            # Hide only the GUI launcher's own console.
            import ctypes
            handle = ctypes.windll.kernel32.GetConsoleWindow()
            if handle:
                ctypes.windll.user32.ShowWindow(handle, 0)
        root = tk.Tk()
        App(root)
        root.mainloop()
