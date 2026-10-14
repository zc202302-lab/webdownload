"""Empty chapters skip; missing images and unresolved loading never do."""
import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path('ebook_collector').resolve()))
sys.modules['playwright'] = types.ModuleType('playwright')
sys.modules['playwright.sync_api'] = types.SimpleNamespace(sync_playwright=None, Error=type('PWError', (Exception,), {}))
import chapter_reader as cr
import collector as c
from library_manager import Library
from PIL import Image
from pypdf import PdfReader

clock = [0]
class Page:
    url = 'https://example.com/book'
    def get_by_text(self, *args, **kwargs): return self
    def count(self): return 0
    @property
    def first(self): return self
    def click(self, **kwargs): pass
    def wait_for_timeout(self, ms): clock[0] += ms / 1000
class Modal:
    def inner_text(self): return 'Chapter'
blank = {'body_identified': True, 'text_length': 0, 'html': '<p></p>', 'image_urls': []}

def run_wait(snapshot, sources, should_pass):
    clock[0] = 0
    with patch.object(cr.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(cr, 'visible_next'), patch.object(cr, 'modal_body', return_value=Modal()), patch.object(cr, 'body_snapshot', return_value=snapshot), patch.object(cr, 'image_sources', return_value=sources), patch.object(cr, 'advance_scroll', return_value={'bottom': True, 'height': '1000'}):
        try: result = cr.wait_chapter(Page(), 'Chapter', None, {'chapter_wait_seconds': 10})
        except (RuntimeError, PermissionError):
            assert not should_pass
        else:
            assert should_pass
            return result

assert run_wait(blank, [], True)[0] == [] and clock[0] >= 60
for change in ({'pending': True}, {'restricted': True}, {'unsupported': True}, {'body_identified': False}, {'image_urls': ['missing.jpg']}):
    run_wait(dict(blank, **change), [], False)
run_wait(blank, [{'src': 'a', 'width': 0, 'height': 0, 'ready': False}], False)
# Short real text is content, not a blank or a lost caption.
run_wait(dict(blank, text_length=3, html='<p>三字文</p>'), [], True)
assert clock[0] < 60
# A chapter blank for 59 seconds must still wait for a late image, not skip it.
clock[0] = 0
def lazy_images(modal):
    return [] if clock[0] < 59 else [{'src': 'late', 'width': 0, 'height': 0, 'ready': False}]
with patch.object(cr.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(cr, 'visible_next'), patch.object(cr, 'modal_body', return_value=Modal()), patch.object(cr, 'body_snapshot', return_value=blank), patch.object(cr, 'image_sources', side_effect=lazy_images), patch.object(cr, 'advance_scroll', return_value={'bottom': True, 'height': '1000'}):
    try: cr.wait_chapter(Page(), 'Chapter', None, {})
    except RuntimeError: pass
    else: raise AssertionError('late pending image was skipped')

with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    lib = Library(root / 'output', 1)
    task = {'id': 'mixed', 'title': '《Test》2024年第1期', 'source': Page.url, 'year': 2024}
    image = {'src': 'https://example.com/image.jpg', 'dom_src': 'https://example.com/image.jpg', 'sources': []}
    state = {'title': None}
    def waiting(page, title, *args):
        state['title'] = title
        return ([] if title == 'Blank' else [image]), 'signature', object()
    def snapshot(*args):
        return blank if state['title'] == 'Blank' else {'html': '<img>', 'text_length': 0}
    def download(context, item, referer, path, library):
        Image.new('RGB', (100, 200), 'white').save(path, format='JPEG')
        return (100, 200), item['src']
    with patch.object(cr, 'catalog_entries', return_value=['First', 'Blank', 'Third']), patch.object(cr, 'wait_chapter', side_effect=waiting), patch.object(cr, 'body_snapshot', side_effect=snapshot), patch.object(cr, 'close_chapter'), patch.object(cr, 'download_best', side_effect=download):
        cr.collect_chapters(None, Page(), task, lib, {'keep_page_images': True}, c.build_pdf)
        pdf, work = lib.paths(task)
        reader = PdfReader(pdf)
        assert len(reader.pages) == 2
        assert [b.title for b in reader.outline] == ['First', 'Third']
        assert reader.get_destination_page_number(reader.outline[1]) == 1
        assert lib.done(task) and lib.db['books']['mixed']['blank_entries'] == ['Blank']
        assert not (work / 'chapter_0002.pdf').exists()
        with patch.object(cr, 'wait_chapter', side_effect=AssertionError('cached entries should not reopen')):
            cr.collect_chapters(None, Page(), task, lib, {'keep_page_images': True}, c.build_pdf)
    empty_task = dict(task, id='allblank')
    with patch.object(cr, 'catalog_entries', return_value=['Blank']), patch.object(cr, 'wait_chapter', side_effect=waiting), patch.object(cr, 'body_snapshot', side_effect=snapshot), patch.object(cr, 'close_chapter'):
        cr.collect_chapters(None, Page(), empty_task, lib, {'keep_page_images': False}, c.build_pdf)
        pdf, work = lib.paths(empty_task)
        assert not pdf.exists() and lib.done(empty_task)
        record = json.loads((work / 'chapters.json').read_text())
        assert record['completed_entries']['0']['empty']
        (work / 'chapters.json').write_text('changed')
        assert not lib.done(empty_task)
    failed_task = dict(task, id='closefailure')
    with patch.object(cr, 'catalog_entries', return_value=['Blank']), patch.object(cr, 'wait_chapter', side_effect=waiting), patch.object(cr, 'body_snapshot', side_effect=snapshot), patch.object(cr, 'close_chapter', side_effect=RuntimeError('close failed')):
        try: cr.collect_chapters(None, Page(), failed_task, lib, {}, c.build_pdf)
        except RuntimeError: pass
        else: raise AssertionError('close failure ignored')
        assert not lib.done(failed_task)
print('PASS: blank-only skip, 60-second confirmation, blocked/pending safety, late images, short text, bookmarks, resume and all-empty books')

