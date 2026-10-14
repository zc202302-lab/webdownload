"""Offline recipe batch, access checks, integrity and export regression checks."""
import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path('ebook_collector').resolve()))
sys.modules['playwright'] = types.ModuleType('playwright')
sys.modules['playwright.sync_api'] = types.SimpleNamespace(sync_playwright=None, Error=Exception)
import recipe_collector as r
import collector as c
from PIL import Image

item = {'title': '<带鱼>', 'source': 'https://www.easteat.com/book_list.php?action=cookbook_detail&id=141299'}
assert r.recipe_id(item['source']) == '141299'
for url in ('https://evil.com/book_list.php?action=cookbook_detail&id=1',
            'https://www.easteat.com/book_list.php?action=cookbook_list&id=1'):
    try: r.recipe_id(url)
    except ValueError: pass
    else: raise AssertionError('invalid recipe URL accepted')

class Page:
    url = r.LIST_URL
    def wait_for_timeout(self, value): clock[0] += value / 1000
    def goto(self, url, **kwargs): self.url = url; return types.SimpleNamespace(ok=True, status=200)
    def close(self): self.closed = True
class Context:
    def new_page(self):
        p = Page(); self.pages.append(p); return p
    def __init__(self): self.pages = []
clock = [0]
with patch.object(r, 'body_snapshot', return_value={'locked': True}):
    try: r.wait_body(Page(), {})
    except PermissionError: pass
    else: raise AssertionError('locked recipe accepted')
snapshot = {'locked': False, 'has_steps': True, 'title': '<带鱼>',
            'html': '<p>制作步骤：煎制</p><img src="https://cdn.easteat.com/a.jpg">',
            'sections': [{'heading': '制作步骤', 'text': '煎制'}],
            'ingredients': [{'category': '主料', 'items': [{'name': '带鱼', 'amount': '1千克'}]}],
            'sources': [{'src': 'https://cdn.easteat.com/a.jpg', 'dom_src': 'https://cdn.easteat.com/a.jpg', 'sources': [], 'ready': True}]}
polls = [0]
def slow_body(page):
    polls[0] += 1
    result = json.loads(json.dumps(snapshot)); result['sources'][0]['ready'] = polls[0] > 4
    return result
with patch.object(r, 'body_snapshot', side_effect=slow_body), patch.object(r.time, 'monotonic', side_effect=lambda: clock[0]):
    assert r.wait_body(Page(), {})['has_steps'] and clock[0] >= 3
clock[0] = 0
with patch.object(r, 'body_snapshot', return_value=None), patch.object(r.time, 'monotonic', side_effect=lambda: clock[0]):
    try: r.wait_body(Page(), {'chapter_wait_seconds': 10})
    except RuntimeError: assert clock[0] >= 60
    else: raise AssertionError('incomplete recipe accepted')

with tempfile.TemporaryDirectory() as temp:
    store = r.RecipeStore(Path(temp), 1)
    def download(context, image, referer, path, storage):
        Image.new('RGB', (100, 200), 'white').save(path, format='JPEG')
        return (100, 200), image['src']
    def export(context, body, mapping, path):
        assert '&lt;带鱼&gt;' in body['html'] and '制作步骤' in body['html']
        c.build_pdf(list(mapping.values())[:1], path)
    with patch.object(r, 'wait_body', return_value=snapshot), patch.object(r, 'download_best', side_effect=download), patch.object(r, 'print_body', side_effect=export):
        r.collect_recipe(None, Page(), item, store, {'keep_page_images': True})
        assert store.done(item)
        # Corrupt final data triggers re-export, but valid cached original is reused.
        pdf, data = store.paths(item); data.write_text('changed')
        assert not store.done(item)
        with patch.object(r, 'download_best', side_effect=AssertionError('cached image should be reused')):
            r.collect_recipe(None, Page(), item, store, {'keep_page_images': False})
        assert store.done(item) and not (store.root / '_work' / '141299').exists()
        assert json.loads(data.read_text())['ingredients'][0]['items'][0]['amount'] == '1千克'
        assert '&lt;带鱼&gt;' in (store.root / '菜谱目录.html').read_text()
    second = dict(item, title='第二道', source=item['source'].replace('141299', '141298'))
    third = dict(item, title='第三道', source=item['source'].replace('141299', '141297'))
    context = Context(); called = []
    def fail(context, page, task, *args): called.append(task['title']); raise RuntimeError('download failed')
    with patch.object(r, 'list_snapshot', return_value={'items': [item, second, third], 'current': '1', 'next': None}), patch.object(r, 'collect_recipe', side_effect=fail):
        try: r.batch_recipes(context, Page(), store, {})
        except RuntimeError: pass
        else: raise AssertionError('batch failure did not stop')
    assert called == ['第二道'] and all(p.closed for p in context.pages)
    assert store.db['recipes']['141298']['status'] == '未完成已终止'
    # Successful pagination visits page two and commits scan completion only at the end.
    context = Context(); listing = Page(); called = []
    def complete(context, page, task, store, settings): called.append(task['title'])
    with patch.object(r, 'list_snapshot', side_effect=[
            {'items': [item], 'current': '1', 'next': r.LIST_URL + '&page=2'},
            {'items': [third], 'current': '2', 'next': None}]), patch.object(r, 'collect_recipe', side_effect=complete):
        r.batch_recipes(context, listing, store, {})
    assert called == ['第三道'] and listing.url.endswith('page=2') and store.db['scan_complete']
    # Repeated list after pagination must stop rather than loop or claim completion.
    with patch.object(r, 'list_snapshot', return_value={'items': [item], 'current': '1', 'next': r.LIST_URL + '&page=2'}):
        try: r.batch_recipes(Context(), Page(), store, {})
        except RuntimeError as exc: assert '重复' in str(exc)
        else: raise AssertionError('pagination loop accepted')
    assert not store.db['scan_complete']
print('PASS: recipe locks, lazy images, minimum wait, export, cached images, checksums, stop-on-failure and pagination loop')

