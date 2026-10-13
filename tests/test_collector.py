import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path('ebook_collector').resolve()))
sys.modules['playwright'] = types.ModuleType('playwright')
sys.modules['playwright.sync_api'] = types.SimpleNamespace(sync_playwright=None, Error=type('PlaywrightError', (Exception,), {}))
import collector as c
from library_manager import Library, StorageFull, book_id, validate_pdf, read_settings
from PIL import Image, ImageDraw


def run():
    root = Path('tmp/pdfs/all_years_test').resolve()
    root.mkdir(parents=True, exist_ok=True)
    settings = {'keep_page_images': False}
    library = Library(root, reserve_gb=1)
    # A true two-page PDF from JPEG assets: readable PDF structure and unchanged aspect ratios.
    task = {'id': book_id('https://example.com/magazine_detail.php?id=1'), 'source': 'https://example.com/magazine_detail.php?id=1', 'title': '《Test Journal》2024年第1期', 'year': 2024}
    pdf, work = library.paths(task)
    pdf.parent.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    paths = []
    for n, size in enumerate([(506,900), (675,1200)], 1):
        path = work / f'{n:04d}.jpg'
        im = Image.new('RGB', size, '#f3f6fa')
        d = ImageDraw.Draw(im)
        d.rectangle((35,35,size[0]-35,150), fill='#164e63')
        d.text((55,70), f'Integration Test - Page {n}', fill='white')
        for row in range(8): d.text((40,200+row*55), f'Content line {row+1}', fill='black')
        im.save(path)
        paths.append(path)
    c.build_pdf(paths,pdf)
    assert validate_pdf(pdf,2)==2
    library.complete(task,pdf,2)
    assert library.done(task)
    assert 'href=' in (root/'书库目录.html').read_text()
    original=pdf.read_bytes()
    pdf.write_bytes(original+b'changed')
    assert not library.done(task)
    pdf.write_bytes(original)
    library.complete(task,pdf,2)
    with patch('library_manager.shutil.disk_usage',return_value=types.SimpleNamespace(free=999_000_000)):
        try:library.check_space()
        except StorageFull:pass
        else:raise AssertionError('disk reserve not enforced')
    # Source identity is stable through fragment changes.
    assert book_id(task['source']+'#part')==task['id']
    class Frame: url='https://example.com/book/mobile/index.html'
    # Existing PDF recovery validates, writes metadata, and does not invoke browser.
    c.collect(None,Frame(),2,task,library,{'keep_page_images':True})
    assert library.done(task)
    # A failed PDF validation preserves image bytes and does not commit the bad result.
    invalid_task=dict(task,id='badpdf',title='《Test Journal》2024年第2期')
    badout,badwork=library.paths(invalid_task);badout.parent.mkdir(parents=True,exist_ok=True)
    with patch('collector.validate_pdf',side_effect=ValueError('validation failure')):
        try:c.build_pdf(paths,badout)
        except ValueError:pass
        else:raise AssertionError('validation not checked')
    assert not badout.exists() and all(p.exists() for p in paths)
    # Full collect path with resumed images cleans temporary pages only after validation.
    import shutil
    cleanup_task=dict(task,id='cleanup',title='《Test Journal》2023年第1期',year=2023)
    cleanupout,cleanupwork=library.paths(cleanup_task);cleanupwork.mkdir(parents=True,exist_ok=True)
    pages={}
    for n,source in enumerate(paths,1):
        dst=cleanupwork/f'{n:04d}.jpg';shutil.copyfile(source,dst)
        pages[str(n)]={'sha256':c.hashlib.sha256(dst.read_bytes()).hexdigest()}
    c.save_json(cleanupwork/'progress.json',{'total':2,'pages':pages})
    class CachedFrame:
        url=Frame.url
        def locator(self,*args):return object()
    c.collect(None,CachedFrame(),2,cleanup_task,library,settings)
    assert not cleanupwork.exists() and validate_pdf(cleanupout,2)==2
    assert library.done(cleanup_task)
        # Newest-first year discovery and explicit subset.
    class Labels:
        def all_text_contents(self):return ['全部','2024年','1990年','2026年','2024年']
    class Catalog:
        def locator(self,*args):return Labels()
    assert c.discover_years(Catalog(),{'years':'all'})==[2026,2024,1990]
    assert c.discover_years(Catalog(),{'years':[2024]})==[2024]
    # Batch failures continue; completed files skip before opening a reader.
    tasks=[task,dict(task,id='fails',title='fails'),dict(task,id='next',title='next')]
    class Immediate:
        @property
        def first(self):return self
        def wait_for(self,**kw):pass
        def click(self,**kw):pass
    class Page:
        def __init__(self,context):self.context=context;self.closed=False
        def goto(self,*args,**kwargs):return types.SimpleNamespace(status=200)
        def get_by_text(self,*args,**kwargs):return Immediate()
        def wait_for_timeout(self,*args):pass
        def is_closed(self):return self.closed
        def close(self):self.closed=True;self.context.pages.remove(self)
    class Context:
        def __init__(self):self.pages=[]
        def new_page(self):
            page=Page(self);self.pages.append(page);return page
    calls=[]
    def fake_collect(context,frame,total,task,library,settings):
        calls.append(task['id'])
        if task['id']=='fails':raise ValueError('network interrupted')
        library.update(task,'已完成')
    context=Context()
    with patch.object(c,'discover_years',return_value=[2024]),patch.object(c,'load_year',return_value=tasks),patch.object(c,'activate_mobile_reader'),patch.object(c,'find_reader',return_value=(Frame(),'1/2','title')),patch.object(c,'collect',side_effect=fake_collect):
        try:
            c.batch_all_years(context,None,{'years':'all'},library)
        except RuntimeError as exc:
            assert '整批采集已终止' in str(exc)
        else:
            raise AssertionError('failed book did not stop batch')
    assert calls==['fails'] and library.db['books']['fails']['status']=='未完成已终止'
    assert not context.pages
    print('PASS: PDF validation, index links, checksums, disk reserve, resume, year ordering, stop-on-incomplete and tab cleanup')
    print(pdf)

run()
