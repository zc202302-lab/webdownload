"""Run the existing collector prompts and logs in a GUI, without stdin/CMD."""
import builtins
import io
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText


class QueueWriter(io.TextIOBase):
    def __init__(self, events):
        self.events = events

    def writable(self):
        return True

    def write(self, value):
        self.events.put(('log', value))
        return len(value)

    def flush(self):
        pass


class TaskWindow:
    def __init__(self, root, mode, runner=None):
        self.root, self.mode = root, mode
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.pending = None
        self.finished = False
        self.closing = False
        root.title({'recipes': '爆款菜谱下载', 'books': '电子书下载', 'single-book': '单本电子书下载', 'magazines': '电子杂志下载'}[mode])
        root.geometry('800x600')
        root.minsize(650, 480)
        frame = ttk.Frame(root, padding=20)
        frame.pack(fill='both', expand=True)
        self.status = tk.StringVar(value='正在准备……')
        ttk.Label(frame, textvariable=self.status, font=('Microsoft YaHei', 12, 'bold')).pack(anchor='w', pady=(0, 12))
        self.log = ScrolledText(frame, wrap='word', state='disabled', font=('Microsoft YaHei', 10))
        self.log.pack(fill='both', expand=True)
        self.prompt = tk.StringVar(value='')
        ttk.Label(frame, textvariable=self.prompt, wraplength=750).pack(anchor='w', pady=12)
        row = ttk.Frame(frame)
        row.pack(fill='x')
        self.entry = ttk.Entry(row)
        self.entry.pack(side='left', fill='x', expand=True)
        self.entry.bind('<Return>', lambda event: self.submit())
        self.submit_button = ttk.Button(row, text='开始下载' if mode=='single-book' else '确认 / 继续', command=self.submit)
        self.submit_button.pack(side='left', padx=8)
        self.submit_button.state(['disabled'])
        self.stop_button = ttk.Button(row, text='停止下载', command=self.stop)
        self.stop_button.pack(side='right')
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.thread = threading.Thread(target=self.work, args=(runner,), daemon=True)
        self.thread.start()
        root.after(100, self.poll)

    def input(self, prompt=''):
        event, answer = threading.Event(), []
        self.events.put(('prompt', (str(prompt), event, answer)))
        while not event.wait(0.1):
            if self.cancel.is_set():
                raise KeyboardInterrupt
        if self.cancel.is_set():
            raise KeyboardInterrupt
        return answer[0]

    def trace(self, frame, event, arg):
        if self.cancel.is_set():
            raise KeyboardInterrupt
        return self.trace

    def work(self, runner):
        if runner is None:
            from task_process import TaskProcess
            try:
                TaskProcess(self.mode,self.events,self.cancel).run()
            except Exception as exc:
                self.events.put(('log',f'\n下载进程异常：{exc}\n'))
                self.events.put(('done','下载已停止，已保存的进度保留。'))
            return
        original_input = builtins.input
        original_out, original_err = sys.stdout, sys.stderr
        builtins.input = self.input
        sys.stdout = sys.stderr = QueueWriter(self.events)
        try:
            sys.settrace(self.trace)
            if runner is not None:
                runner()
            elif self.mode == 'recipes':
                from recipe_collector import main
                main()
            elif self.mode == 'books':
                from book_collector import main
                main()
            else:
                from collector import main
                main()
            self.events.put(('done', '任务结束。请打开资料库查看完成情况。'))
        except KeyboardInterrupt:
            self.events.put(('done', '已停止，下载进度已保留。'))
        except Exception as exc:
            self.events.put(('log', f'\n未完成：{exc}\n下载进度已保留。\n'))
            self.events.put(('done', '任务未完成，已停止。详情见上方记录。'))
        finally:
            sys.settrace(None)
            builtins.input = original_input
            sys.stdout, sys.stderr = original_out, original_err

    def submit(self):
        if self.pending:
            _, event, answer = self.pending
            value = self.entry.get()
            answer.append(value)
            self.entry.delete(0, 'end')
            event.set()
            self.pending = None
            self.prompt.set('')
            self.submit_button.state(['disabled'])
            self.status.set('正在处理……')

    def stop(self):
        if not self.finished:
            self.cancel.set()
            self.stop_button.state(['disabled'])
            self.submit_button.state(['disabled'])
            self.status.set('正在停止下载；保留已保存进度，长时间等待将结束下载进程。')

    def close(self):
        if self.finished:
            self.root.destroy()
        else:
            self.closing = True
            self.stop()

    def poll(self):
        # Bound each pass so large log output cannot starve the UI.
        for _ in range(200):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'log':
                self.log.configure(state='normal')
                self.log.insert('end', value)
                if int(self.log.index('end-1c').split('.')[0]) > 3000:
                    self.log.delete('1.0', '1000.0')
                self.log.see('end')
                self.log.configure(state='disabled')
            elif kind == 'prompt':
                if self.cancel.is_set():
                    continue
                self.pending = value
                self.prompt.set(value[0])
                self.status.set('等待你的操作')
                self.submit_button.state(['!disabled'])
                self.entry.focus_set()
            elif kind == 'done':
                self.finished = True
                self.status.set(value)
                self.pending = None
                self.prompt.set('')
                self.submit_button.state(['disabled'])
                self.stop_button.configure(text='关闭窗口', command=self.close)
                self.stop_button.state(['!disabled'])
        if self.closing and self.finished and not self.thread.is_alive():
            self.root.destroy()
            return
        self.root.after(100, self.poll)

