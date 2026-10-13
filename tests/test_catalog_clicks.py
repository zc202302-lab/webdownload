import sys,types,tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path('ebook_collector').resolve()))
sys.modules['playwright']=types.ModuleType('playwright');sys.modules['playwright.sync_api']=types.SimpleNamespace(Error=type('PWError',(Exception,),{}),sync_playwright=None)
import chapter_reader as cr
import collector as c
from library_manager import Library
from PIL import Image
from pypdf import PdfReader
calls=[]
class Control:
 def __init__(self,title):self.title=title
 @property
 def first(self):return self
 def click(self,**kwargs):calls.append('open:'+self.title)
class Page:
 url='https://example.com/book'
 def get_by_text(self,title,**kwargs):return Control(title)
with tempfile.TemporaryDirectory() as temp:
 root=Path(temp);original=root/'original.jpg';Image.new('RGB',(550,739),'white').save(original)
 lib=Library(root/'output',reserve_gb=1)
 task={'id':'book','title':'《Test》2021年第1期','year':2021,'source':Page.url}
 settings={'keep_page_images':True}
 image={'src':'https://example.com/img.jpg','dom_src':'https://example.com/img.jpg'}
 def download(context,item,referer,path,library):
  path.write_bytes(original.read_bytes());return (550,739),item['src']
 with patch.object(cr,'catalog_entries',return_value=['One','Two']),patch.object(cr,'wait_chapter',return_value=([image],'signature',object())),patch.object(cr,'body_snapshot',return_value={'html':'<img>','text_length':0}),patch.object(cr,'download_best',side_effect=download),patch.object(cr,'close_chapter',side_effect=lambda modal:calls.append('close')),patch.object(cr,'visible_next',side_effect=AssertionError('Next must not be used')):
  cr.collect_chapters(None,Page(),task,lib,settings,c.build_pdf)
  assert calls==['open:One','close','open:Two','close'],calls
  assert lib.done(task)
  pdf,work=lib.paths(task);assert len(PdfReader(pdf).pages)==2
  calls.clear()
  cr.collect_chapters(None,Page(),task,lib,settings,c.build_pdf)
  assert calls==[],calls
print('PASS: catalogue open-close sequence, no Next clicks, low-resolution collection and verified entry resume')
