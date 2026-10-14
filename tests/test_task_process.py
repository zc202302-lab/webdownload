"""Real subprocess prompt protocol and stop during an uninterruptible sleep."""
import queue
import sys
import threading
import time
from pathlib import Path
sys.path.insert(0,str(Path('ebook_collector').resolve()))
from task_process import TaskProcess


def exercise(mode,stop=False):
    events=queue.Queue();cancel=threading.Event()
    def command(port,token):
        return [sys.executable,str(Path('ebook_collector/task_process.py').resolve()),'--worker-test',mode,str(port),token]
    controller=TaskProcess('magazines',events,cancel,command)
    errors=[]
    def run():
        try: controller.run()
        except Exception as exc: errors.append(exc)
    thread=threading.Thread(target=run)
    thread.start()
    deadline=time.monotonic()+15
    logs=[];done=False;stopping=None
    while time.monotonic()<deadline and not done:
        try: kind,value=events.get(timeout=0.2)
        except queue.Empty: continue
        if kind=='log':
            logs.append(value)
            if stop and 'block started' in value:
                stopping=time.monotonic();cancel.set()
        elif kind=='prompt':
            if stop:
                stopping=time.monotonic();cancel.set()
            else:
                value[2].append('GUI answer');value[1].set()
        elif kind=='done': done=True
    thread.join(timeout=5)
    if thread.is_alive(): cancel.set();thread.join(timeout=10)
    assert not thread.is_alive() and not errors,errors
    assert done and controller.process.poll() is not None
    if stop:
        assert stopping and time.monotonic()-stopping<10
    else:
        assert 'GUI answer' in ''.join(logs)


exercise('prompt')
exercise('stop-prompt',True)
exercise('block',True)
print('PASS: real worker prompt/log IPC, cancellation while awaiting input and forced stop of blocked owned process within 10 seconds')
