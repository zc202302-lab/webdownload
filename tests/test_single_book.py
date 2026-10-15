"""Manual starts, completed-title/ID dedupe and already-open reader use."""
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch
from PIL import Image
sys.path.insert(0,str(Path('ebook_collector').resolve()))
try:import playwright.sync_api
except ModuleNotFoundError:
    sys.modules['playwright']=types.ModuleType('playwright')
    sys.modules['playwright.sync_api']=types.SimpleNamespace(sync_playwright=None,Error=Exception)
import book_collector as books
from collector import build_pdf
from library_manager import file_hash

def item(title,id):return {'title':title,'source':f'https://www.easteat.com/magazine_detail.php?id={id}&type=detail'}
settings={'chapter_wait_seconds':60,'keep_page_images':True}
with tempfile.TemporaryDirectory() as folder:
    root=Path(folder);store=books.BookStore(root/'output',1)
    done=item('《完成书》（上）',905)
    pdf,work=store.paths(done)
    image=root/'image.jpg';Image.new('RGB',(10,20),'white').save(image)
    build_pdf([image],pdf)
    store.update(done,'已完成',pdf=pdf.name,sha256=file_hash(pdf),pages=1)
    assert books.completed_title(store,' 完成书 (上) ')
    assert not books.completed_title(store,'完成书（下）')
    partial=item('原有书名',904);store.update(partial,'下载失败已跳过')
    answers=iter([' 完成书 (上) ','别名重复','   ','新输入名称','退出'])
    trace=[]
    def ask(prompt):
        answer=next(answers);trace.append(('ask',answer));return answer
    readers=[('page',done['source'],('frame','1/1','title')),
             ('page',partial['source'],('frame','1/1','title'))]
    def batch(context,page,items,book_store,options,opened_reader=None):
        trace.append(('download',items[0]['title']))
        assert items==[partial] and opened_reader==('frame','1/1','title')
    with patch.object(books,'manual_reader',side_effect=readers) as locate,patch.object(books,'batch_books',side_effect=batch):
        books.manual_session(None,store,settings,ask)
    assert locate.call_count==2
    assert trace==[('ask',' 完成书 (上) '),('ask','别名重复'),('ask','   '),
                   ('ask','新输入名称'),('download','原有书名'),('ask','退出')]
    # Missing/changed completed PDF is eligible again.
    saved=pdf.read_bytes();pdf.write_bytes(b'changed')
    assert not books.completed_title(store,done['title']);pdf.write_bytes(saved)
    class Page:
        def goto(self,*args,**kwargs):raise AssertionError('must use already-open book')
        def locator(self,selector):
            assert selector=='#readerModal'
            return types.SimpleNamespace(count=lambda:0)
    new=item('单本新书',903);out,work=store.paths(new)
    work.mkdir(parents=True)
    def collect(*args):out.write_bytes(saved);return out,work
    with patch.object(books,'collect_book',side_effect=collect),patch.object(books,'find_reader',side_effect=AssertionError('must not reopen reader')):
        books.batch_books(None,Page(),[new],store,settings,opened_reader=(object(),'1/1','reader'))
    assert store.done(new)
    # No hidden automatic action when lookup fails; ask again, then exit.
    answers=iter(['bad','退出'])
    with patch.object(books,'manual_reader',side_effect=RuntimeError('多个阅读器')),patch.object(books,'batch_books') as batch:
        books.manual_session(None,store,settings,lambda prompt:next(answers))
    assert not batch.called
    # Resolve the current reader through its own detail URL or actual opener;
    # refuse multiple readers and unrelated pages rather than guessing an ID.
    class ReaderPage:
        def __init__(self,url,opener=None):self.url=url;self.parent=opener;self.frames=[]
        def is_closed(self):return False
        def wait_for_timeout(self,ms):pass
        def opener(self):return self.parent
    class ReaderFrame:
        def __init__(self,page):self.page=page
        def is_detached(self):return False
        def locator(self,selector):return types.SimpleNamespace(count=lambda:1,is_visible=lambda:True)
    detail=ReaderPage(done['source']);frame=ReaderFrame(detail);detail.frames=[frame]
    context=types.SimpleNamespace(pages=[detail])
    with patch.object(books,'find_reader',return_value=(frame,'1/1','title')):
        assert books.manual_reader(context,settings)[1]==done['source']
        detail.frames.append(ReaderFrame(detail))
        try:books.manual_reader(context,settings)
        except RuntimeError as exc:assert '一本' in str(exc)
        else:raise AssertionError('multiple open books accepted')
        detail.frames=[frame]
        detail.url='https://example.test/reader';detail.parent=ReaderPage(done['source'])
        assert books.manual_reader(context,settings)[1]==done['source']
        detail.parent=None
        try:books.manual_reader(context,settings)
        except RuntimeError as exc:assert '详情链接' in str(exc)
        else:raise AssertionError('unknown book identity accepted')
print('PASS: manual per-book start, title/ID dedupe, partial identity, damaged PDF retry and no reader reopening')
