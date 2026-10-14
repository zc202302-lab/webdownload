"""Private loopback IPC and bounded cancellation of an owned collector tree."""
import builtins
import io
import json
import os
import queue
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from app_runtime import app_root
from auto_updater import process_options


def kill_tree(process):
    if process.poll() is not None:
        return
    if os.name=='nt':
        subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],timeout=5,
                       **process_options())
    else:
        try: os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError: pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


class TaskProcess:
    def __init__(self,mode,events,cancel,command_factory=None):
        self.mode,self.events,self.cancel=mode,events,cancel
        self.command_factory=command_factory
        self.process=None

    def run(self):
        token=secrets.token_hex(24)
        server=socket.socket()
        server.bind(('127.0.0.1',0));server.listen(1);server.settimeout(0.2)
        port=server.getsockname()[1]
        args=['--task-worker',self.mode,str(port),token]
        command=([sys.executable] if getattr(sys,'frozen',False) else [sys.executable,str(app_root()/'desktop_app.py')])+args
        if self.command_factory:
            command=self.command_factory(port,token)
        options=process_options()
        if os.name!='nt': options['start_new_session']=True
        connection=None
        stopped=None
        sent_stop=False
        pending=None
        received_done=False
        buffer=b''
        start=time.monotonic()
        authenticated=False
        try:
            self.process=subprocess.Popen(command,cwd=app_root(),**options)
            while True:
                if self.cancel.is_set():
                    stopped=stopped or time.monotonic()
                    if connection and authenticated and not sent_stop:
                        try: connection.sendall(b'{"kind":"stop"}\n')
                        except OSError: pass
                        sent_stop=True
                    if time.monotonic()-stopped>=2:
                        kill_tree(self.process)
                        self.events.put(('done','已停止；已保存的进度保留，未完成内容将在下次重新检查。'))
                        return
                if connection is None and self.process.poll() is not None:
                    if not received_done:
                        self.events.put(('done','已停止，进度保留。' if self.cancel.is_set() else '下载进程退出，已保存的进度保留。请查看日志。'))
                    return
                if connection is None:
                    if time.monotonic()-start>20:
                        raise RuntimeError('下载进程启动超时。')
                    try:
                        connection,_=server.accept();connection.settimeout(0.2)
                    except socket.timeout: continue
                if pending and pending[0].is_set():
                    connection.sendall((json.dumps({'kind':'answer','value':pending[1][0]})+'\n').encode())
                    pending=None
                try: chunk=connection.recv(65536)
                except socket.timeout:
                    if self.process.poll() is not None:
                        raise RuntimeError('下载进程退出前未报告完成状态，请查看日志。')
                    continue
                except ConnectionResetError as exc:
                    raise RuntimeError('下载进程连接意外断开，已保存的进度保留。') from exc
                if not chunk:
                    self.process.wait(timeout=5)
                    if not received_done:
                        self.events.put(('done','下载进程已关闭，已保存的进度保留。'))
                    return
                buffer+=chunk
                while b'\n' in buffer:
                    line,buffer=buffer.split(b'\n',1)
                    message=json.loads(line)
                    if not authenticated:
                        if message.get('token')!=token: raise RuntimeError('下载进程连接验证失败。')
                        authenticated=True
                        continue
                    kind=message['kind']
                    if kind=='prompt':
                        event,answer=threading.Event(),[]
                        pending=(event,answer)
                        if not self.cancel.is_set():
                            self.events.put(('prompt',(message['value'],event,answer)))
                    elif kind in ('log','done'):
                        if kind=='done':
                            received_done=True
                            connection.sendall(b'{"kind":"finished"}\n')
                        self.events.put((kind,message['value']))
                        if kind=='done':
                            self.process.wait(timeout=5)
                            return
        finally:
            if connection: connection.close()
            server.close()
            if self.process and self.process.poll() is None:
                kill_tree(self.process)


def run_worker(mode,port,token,test=False):
    connection=socket.create_connection(('127.0.0.1',int(port)),timeout=10)
    connection.settimeout(None)
    lock=threading.Lock()
    cancelled=threading.Event()
    finished=threading.Event()
    answers=queue.Queue()
    def send(kind,value=None):
        with lock:
            connection.sendall((json.dumps({'kind':kind,'value':value})+'\n').encode())
    connection.sendall((json.dumps({'token':token})+'\n').encode())
    def read():
        try:
            with connection.makefile('r',encoding='utf-8') as incoming:
                for line in incoming:
                    message=json.loads(line)
                    if message['kind']=='stop': cancelled.set()
                    elif message['kind']=='answer': answers.put(message['value'])
                    elif message['kind']=='finished':
                        finished.set()
                        return
        except OSError:
            pass
        finally:
            cancelled.set()
    threading.Thread(target=read,daemon=True).start()
    class Writer(io.TextIOBase):
        def write(self,value): send('log',value);return len(value)
        def flush(self): pass
    def prompt(value=''):
        send('prompt',str(value))
        while not cancelled.is_set():
            try: return answers.get(timeout=0.1)
            except queue.Empty: pass
        raise KeyboardInterrupt
    owned={'collector','chapter_reader','recipe_collector','book_collector','library_manager','missing_records'}
    def trace(frame,event,arg):
        # Never interrupt Playwright/asyncio internals or checkpoint writes.
        if frame.f_globals.get('__name__') not in owned:
            return None
        if cancelled.is_set() and frame.f_code.co_name not in ('save_json','trace','send','write'):
            raise KeyboardInterrupt
        return trace
    builtins.input=prompt
    sys.stdout=sys.stderr=Writer()
    result='任务结束。请打开资料库查看完成情况。'
    try:
        sys.settrace(trace)
        if test:
            if mode=='block': print('block started');time.sleep(60)
            elif mode=='prompt': print('answer:',input('测试输入'))
            elif mode=='stop-prompt': input('等待停止')
        else:
            if mode=='magazines': from collector import main
            elif mode=='recipes': from recipe_collector import main
            elif mode=='books': from book_collector import main
            else: raise ValueError('下载模式无效')
            main()
    except KeyboardInterrupt:
        result='已停止，下载进度已保留。'
    except Exception as exc:
        print(f'未完成：{exc}\n下载进度已保留。')
        result='任务未完成，已停止。详情见上方记录。'
    finally:
        sys.settrace(None)
        send('done',result)
        finished.wait(timeout=2)
        try: connection.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        connection.close()


if __name__=='__main__':
    run_worker(sys.argv[2],sys.argv[3],sys.argv[4],test=sys.argv[1]=='--worker-test')
