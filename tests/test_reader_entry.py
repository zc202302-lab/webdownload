import sys
import types
from pathlib import Path
sys.path.insert(0,str(Path('ebook_collector').resolve()))
sys.modules['playwright']=types.ModuleType('playwright')
sys.modules['playwright.sync_api']=types.SimpleNamespace(sync_playwright=None,Error=Exception)
import collector as c

class Locator:
    def __init__(self,count=0,text='',click=None):
        self.number,self.text,self.action=count,text,click
    def count(self): return self.number
    def is_visible(self): return bool(self.number)
    def filter(self,**kwargs): return self
    def nth(self,index): return self
    def inner_text(self,**kwargs): return self.text
    def click(self,**kwargs): self.action()
    def input_value(self,**kwargs): return '1/3'
    def text_content(self,**kwargs): return 'Test Book'

class Frame:
    def __init__(self,here=True,ready=False,text='Sorry, flash player unavailable. Click here for mobile.'):
        self.here,self.ready,self.text,self.clicks=here,ready,text,0
    def click(self):
        self.clicks+=1;self.ready=True;self.here=False
    def is_detached(self): return False
    def locator(self,selector):
        if selector=='#currentPageIndexTextField': return Locator(int(self.ready))
        if selector=='a': return Locator(int(self.here),click=self.click)
        if selector=='body': return Locator(1,self.text)
        if selector=='title': return Locator(1,'Test Book')
        raise AssertionError(selector)
class Page:
    def __init__(self,frame): self.frames=[frame]
    def is_closed(self): return False
    def wait_for_timeout(self,ms): pass
for here,ready in ((True,False),(False,True),(True,True)):
    frame=Frame(here,ready)
    page=Page(frame)
    found,value,title=c.find_reader(types.SimpleNamespace(pages=[page]),timeout=1)
    assert found is frame and value=='1/3'
    assert frame.clicks==(1 if here and not ready else 0)
frame=Frame(text='Some unrelated here link')
assert not c.try_mobile_link(frame) and frame.clicks==0
frame=Frame(here=True,ready=False)
page=Page(frame);page.context=types.SimpleNamespace(pages=[page]);frame.page=page
assert c.recover_reader(frame,{},None) is frame and frame.clicks==1
print('PASS: optional here entry, direct reader, existing reader priority and unrelated link exclusion')
