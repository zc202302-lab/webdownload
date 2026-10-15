"""Offline book scan, arrow navigation, PDF/resume and batch failure checks."""
import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path('ebook_collector').resolve()))
try:
    import playwright.sync_api
except ModuleNotFoundError:
    sys.modules['playwright'] = types.ModuleType('playwright')
    sys.modules['playwright.sync_api'] = types.SimpleNamespace(sync_playwright=None, Error=Exception)
import book_collector as books
from library_manager import validate_pdf

ITEM = {'title': '<测试书>', 'source': 'https://www.easteat.com/magazine_detail.php?id=905&type=detail'}
SECOND = {'title': '下一本', 'source': 'https://www.easteat.com/magazine_detail.php?id=904&type=detail'}
SETTINGS = {'chapter_wait_seconds': 10, 'keep_page_images': False}


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def wait(self, ms):
        self.now += ms/1000


class ListPage:
    def __init__(self, clock, mode):
        self.clock, self.mode, self.scrolls = clock, mode, 0

    def evaluate(self, script):
        self.scrolls += 1

    def wait_for_timeout(self, ms):
        self.clock.wait(ms)

    def snapshot(self):
        ready = self.clock.now >= 40 and self.mode in ('complete','jitter')
        return {'items': [ITEM, ITEM, SECOND] if ready else [ITEM],
                'height': (2000+int(self.clock.now*2)%7 if self.mode=='jitter' else 2000) if ready else 1000, 'bottom': self.scrolls > 0,
                'more_present': self.mode != 'missing', 'more': not ready,
                'loading': not ready and self.mode == 'complete',
                'error': self.mode == 'error'}


def expect_failure(action, message):
    try:
        action()
    except (RuntimeError, ValueError) as exc:
        assert message in str(exc), str(exc)
    else:
        raise AssertionError('Expected failure: ' + message)


class Field:
    def __init__(self, frame):
        self.frame = frame

    def input_value(self):
        pages = self.frame.pages()
        label = str(pages[0]) if len(pages) == 1 else f'{pages[0]}-{pages[-1]}'
        return f'{label}/{self.frame.total}'

    def fill(self, value):
        self.value = int(value)

    def press(self, key):
        assert key == 'Enter'
        self.frame.seeks.append(self.value)
        self.frame.current = self.value


class Arrow:
    def __init__(self, frame):
        self.frame = frame

    def count(self):
        return 1

    def nth(self, index):
        return self

    def is_visible(self):
        return True

    def click(self, **kwargs):
        self.frame.clicks += 1
        self.frame.current = self.frame.pages()[-1]+1


class Frame:
    url = 'https://reader.easteat.com/book/mobile/index.html'

    def __init__(self, clock, total=3, double=False, missing=False):
        self.clock, self.total, self.double, self.missing = clock, total, double, missing
        self.current, self.clicks = 1, 0
        self.seeks=[]

    def pages(self):
        if self.double and self.current == 2:
            return [2, 3]
        return [self.current]

    def locator(self, selector):
        if selector == '#currentPageIndexTextField':
            return Field(self)
        if selector == '#nextPageButton':
            return Arrow(self)
        raise AssertionError(selector)

    def wait_for_timeout(self, ms):
        self.clock.wait(ms)

    def images(self):
        return [{'src': f'https://reader.easteat.com/book/files/large/{n}.jpg',
                 'sources': [], 'ready': not self.missing} for n in self.pages()]


with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    store = books.BookStore(root/'output', 1)
    assert store.root == root/'电子书'
    clock = Clock()
    page = ListPage(clock, 'complete')
    with patch.object(books.time, 'monotonic', clock.monotonic), patch.object(books, 'list_snapshot', lambda p: p.snapshot()):
        items = books.discover_books(page, store, SETTINGS)
    assert len(items) == 2 and clock.now >= 70 and store.db['scan_complete']
    assert page.scrolls > 1
    clock=Clock();page=ListPage(clock,'jitter')
    with patch.object(books.time,'monotonic',clock.monotonic),patch.object(books,'list_snapshot',lambda p:p.snapshot()):
        assert len(books.discover_books(page,store,SETTINGS))==2
    assert clock.now<100 and store.db['scan_complete']
    for mode, message in [('error', '加载失败'), ('stalled', '至少60秒'), ('missing', '至少60秒')]:
        clock = Clock()
        page = ListPage(clock, mode)
        with patch.object(books.time, 'monotonic', clock.monotonic), patch.object(books, 'list_snapshot', lambda p: p.snapshot()):
            expect_failure(lambda: books.discover_books(page, store, SETTINGS), message)
        assert not store.db['scan_complete']

    assert books.displayed_pages('2-3/3', 3) == [2,3]
    for value in ['0/3', '4/3', '1/4', '1-3/3', 'bad']:
        expect_failure(lambda: books.displayed_pages(value, 3), '页码')
    expect_failure(lambda: books.book_identifier('https://evil.com/magazine_detail.php?id=905&type=detail'), '链接异常')
    downloads = []

    def download(context, image, referer, part, storage):
        number = books.page_number(image['src'])
        downloads.append(number)
        # Include a small blank page: resolution/content must not exclude it.
        Image.new('RGB', (50,75), 'white' if number == 2 else 'blue').save(part, format='JPEG')
        return (50,75), image['src']

    for double in (False, True):
        clock = Clock()
        frame = Frame(clock, double=double)
        with patch.object(books.time, 'monotonic', clock.monotonic), patch.object(books, 'reader_images', lambda f: f.images()), patch.object(books, 'download_best', download):
            pdf, work = books.collect_book(None, frame, 3, ITEM, store, SETTINGS)
        assert validate_pdf(pdf, 3) == 3
        assert frame.clicks == (0 if double else 2)
        assert work.exists() and not store.done(ITEM)  # Not committed until close.
    assert downloads == [1,2,3]  # Second run reused checksummed original pages.
    _, work = store.paths(ITEM)
    (work/'0002.jpg').write_bytes(b'corrupt')
    clock = Clock()
    frame = Frame(clock)
    with patch.object(books.time, 'monotonic', clock.monotonic), patch.object(books, 'reader_images', lambda f: f.images()), patch.object(books, 'download_best', download):
        books.collect_book(None, frame, 3, ITEM, store, SETTINGS)
    assert downloads == [1,2,3,2]
    assert frame.seeks==[2] and frame.clicks==0
    progress=json.loads((work/'progress.json').read_text())
    del progress['pages']['3']
    (work/'progress.json').write_text(json.dumps(progress))
    (work/'0003.jpg').unlink()
    clock=Clock();frame=Frame(clock)
    with patch.object(books.time,'monotonic',clock.monotonic),patch.object(books,'reader_images',lambda f:f.images()),patch.object(books,'download_best',download):
        books.collect_book(None,frame,3,ITEM,store,SETTINGS)
    assert frame.seeks==[3] and frame.clicks==0 and downloads==[1,2,3,2,3]
    clock = Clock()
    frame = Frame(clock, missing=True)
    with patch.object(books.time, 'monotonic', clock.monotonic), patch.object(books, 'reader_images', lambda f: f.images()):
        expect_failure(lambda: books.wait_pages(frame, 1, 3, SETTINGS, work), '至少60秒')
    assert clock.now >= 60 and (work/'diagnostic.json').exists()

    class NoArrow:
        def locator(self, selector):
            return types.SimpleNamespace(count=lambda: 0)
        def get_by_role(self, *args, **kwargs):
            return types.SimpleNamespace(count=lambda: 0)

    expect_failure(lambda: books.next_arrow(NoArrow()), '无法唯一识别')
    clock = Clock()
    frame = Frame(clock)
    frame.current = 3
    with patch.object(books.time, 'monotonic', clock.monotonic), patch.object(books, 'reader_images', lambda f: f.images()):
        expect_failure(lambda: books.wait_pages(frame, 2, 3, SETTINGS, work), '跳过')
    assert clock.now < 60 and (work/'diagnostic.json').exists()

    class Button:
        def filter(self, **kwargs):
            return self
        def wait_for(self, **kwargs):
            pass
        def click(self, **kwargs):
            pass

    class Detail:
        def __init__(self):
            self.visited = []
        def goto(self, url, **kwargs):
            self.visited.append(url)
            return None
        def locator(self, selector):
            return Button()

    page = Detail()
    with patch.object(books, 'find_reader', return_value=(Frame(Clock()), '1/3', '测试书')), patch.object(books, 'collect_book', return_value=(pdf,work)), patch.object(books, 'close_book_reader', side_effect=RuntimeError('关闭失败')):
        books.batch_books(None, page, [ITEM,SECOND], store, SETTINGS)
    assert page.visited == [ITEM['source'],SECOND['source']] and not store.done(ITEM) and work.exists()
    log=(store.root/'缺失记录.txt').read_text(encoding='utf-8-sig')
    assert '关闭失败' in log and ITEM['title'] in log and SECOND['title'] in log
    # Ordinary first-book failure does not prevent the second book succeeding.
    saved=json.loads((work/'progress.json').read_text())
    saved.update(active_page=3,phase='下载页面原图')
    (work/'progress.json').write_text(json.dumps(saved))
    page=Detail()
    with patch.object(books,'find_reader',return_value=(Frame(Clock()),'1/3','测试书')),patch.object(books,'collect_book',side_effect=[RuntimeError('页图加载失败'),(pdf,work)]),patch.object(books,'close_book_reader'):
        books.batch_books(None,page,[ITEM,SECOND],store,{**SETTINGS,'keep_page_images':True})
    assert page.visited==[ITEM['source'],SECOND['source']] and store.done(SECOND) and not store.done(ITEM)
    log=(store.root/'缺失记录.txt').read_text(encoding='utf-8-sig')
    assert '页图加载失败' in log and '第3页' in log
    # Explicit access/storage failures and a user's stop are not ordinary skips.
    for error in (PermissionError('网站返回403'),books.StorageFull('空间不足'),KeyboardInterrupt()):
        page=Detail()
        with patch.object(books,'find_reader',return_value=(Frame(Clock()),'1/3','测试书')),patch.object(books,'collect_book',side_effect=error):
            try:books.batch_books(None,page,[ITEM,SECOND],store,SETTINGS)
            except type(error):pass
            else:raise AssertionError('pause/stop was swallowed')
        assert page.visited==[ITEM['source']]
    page = Detail()
    with patch.object(books, 'find_reader', return_value=(Frame(Clock()), '1/3', '测试书')), patch.object(books, 'collect_book', return_value=(pdf,work)), patch.object(books, 'close_book_reader') as close:
        books.batch_books(None, page, [ITEM], store, SETTINGS)
    assert close.call_count == 1 and store.done(ITEM) and not work.exists()
    page=Detail()
    books.batch_books(None,page,[ITEM],store,SETTINGS)
    assert not page.visited  # Completed PDF is checked before entering details.
    # A new run removes old log history from both JSON and text, so flushes
    # cannot resurrect it. PDFs and completion/resume metadata remain intact.
    pdf_before=pdf.read_bytes()
    records_before={key:{k:v for k,v in rec.items() if k!='failures'} for key,rec in store.db['books'].items()}
    store.begin_run()
    store.flush()
    new_log=(store.root/'缺失记录.txt').read_text(encoding='utf-8-sig')
    assert '本轮暂无缺失记录' in new_log and '关闭失败' not in new_log and '页图加载失败' not in new_log
    assert pdf.read_bytes()==pdf_before and store.done(ITEM)
    assert store.db['books']==records_before
    reopened=books.BookStore(root/'output',1)
    reopened.flush()
    assert (store.root/'缺失记录.txt').read_text(encoding='utf-8-sig')==new_log
    # New failures appear normally after clearing the preceding run.
    store.db['books'][books.book_identifier(ITEM['source'])]['failures']=[{
        'time':'2026-10-15 20:00:00','stage':'翻页','position':'阅读器第2页','reason':'本轮新错误'}]
    store.flush()
    assert '本轮新错误' in (store.root/'缺失记录.txt').read_text(encoding='utf-8-sig')
    store.begin_run()
    assert '本轮新错误' not in (store.root/'缺失记录.txt').read_text(encoding='utf-8-sig')
    assert '&lt;测试书&gt;' in (store.root/'电子书目录.html').read_text(encoding='utf-8')
    pdf.write_bytes(b'corrupt')
    assert not store.done(ITEM)

print('PASS: discovery, page checks, direct verified resume, completed-book skip, failure log/continue, access/storage pause and cancellation')
