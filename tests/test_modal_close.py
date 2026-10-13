"""Regression: missing cross falls back only to a proven modal backdrop."""
import json
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path('ebook_collector').resolve()))
try:
    from playwright.sync_api import Error
except ModuleNotFoundError:
    Error = type('PWError', (Exception,), {})
    sys.modules['playwright'] = types.ModuleType('playwright')
    sys.modules['playwright.sync_api'] = types.SimpleNamespace(sync_playwright=None, Error=Error)
import modal_close as mc
import chapter_reader as chapters
import book_collector as books

# Run the actual DOM geometry JavaScript with overlapping-content fixtures.
script = '''
const inspect = INSPECT;
global.innerWidth=1000;global.innerHeight=800;
global.getComputedStyle=e=>({position:e.fixed?'fixed':'static'});
const rect=(left,right,top,bottom)=>({left,right,top,bottom});
const body={tagName:'BODY'};
const content={getBoundingClientRect:()=>rect(200,800,100,700)};
const backdrop={tagName:'DIV',parentElement:body,matches:()=>true,fixed:true,
  getBoundingClientRect:()=>rect(0,1000,0,800),querySelectorAll:()=>[content]};
const article={tagName:'DIV',parentElement:backdrop,matches:()=>false,fixed:false};
global.document={elementFromPoint:(x,y)=>x<200 || x>800 ? backdrop : content};
let points=inspect(article);
if(points.length!==2 || points[0].ancestor!==1 || points[0].x!==8 || points[1].x!==992) throw Error('backdrop not selected');
document.elementFromPoint=()=>content;
if(inspect(article).length) throw Error('page content selected');
document.elementFromPoint=()=>body;
if(inspect(article).length) throw Error('underlying site selected');
document.elementFromPoint=()=>backdrop;
content.getBoundingClientRect=()=>rect(0,1000,0,800);
if(inspect(article).length) throw Error('reader container selected');
console.log('PASS JS: actual backdrop hit, left/right points, content/iframe/container and underlying page exclusion');
'''.replace('INSPECT', '('+mc.BACKDROP_POINTS+')')
subprocess.run(['node','-e',script], check=True)


class Empty:
    def count(self):
        return 0


class Modal:
    def __init__(self, closes='left', hit=True, points=True):
        self.closes, self.hit, self.has_points = closes, hit, points
        self.visible, self.clicks = True, []
    def evaluate(self, script, point=None):
        if script == mc.BACKDROP_POINTS:
            return [{'ancestor':0,'x':8,'y':400},{'ancestor':0,'x':992,'y':400}] if self.has_points else []
        return self.hit
    def click(self, position=None, **kwargs):
        side = 'left' if position['x']==8 else 'right'
        self.clicks.append(side)
        if self.closes == side:
            self.visible = False
    def wait_for(self, **kwargs):
        assert kwargs['state'] == 'hidden'
        if self.visible:
            raise Error('still visible')
    def is_visible(self):
        return self.visible
    def locator(self, selector):
        if selector=='xpath=..':
            return types.SimpleNamespace(evaluate=lambda script:'BODY')
        return Empty()
    def get_by_text(self, *args, **kwargs):
        return Empty()


for closes, expected in [('left',['left']),('right',['left','right']),('neither',['left','right'])]:
    modal=Modal(closes)
    assert mc.close_by_backdrop(modal)==(closes!='neither')
    assert modal.clicks==expected
for options in ({'hit':False},{'points':False}):
    modal=Modal(**options)
    assert not mc.close_by_backdrop(modal) and not modal.clicks
modal=Modal('right')
chapters.close_chapter(modal)
assert not modal.visible and modal.clicks==['left','right']
modal=Modal('neither')
try:
    chapters.close_chapter(modal)
except RuntimeError as exc:
    assert '仍未确认关闭' in str(exc)
else:
    raise AssertionError('unclosed magazine accepted')


class Button:
    def __init__(self, modal, present=True):
        self.modal, self.present, self.clicked = modal, present, False
    def count(self):
        return int(self.present)
    def is_visible(self):
        return self.present
    def click(self, **kwargs):
        self.clicked=True
        self.modal.visible=False


class Page:
    def __init__(self, modal, present=True):
        self.modal=modal
        self.button=Button(modal,present)
    def locator(self, selector):
        return self.modal if selector=='#readerModal' else self.button


page=Page(Modal())
with patch.object(mc,'close_by_backdrop',side_effect=AssertionError('cross must take priority')):
    books.close_book_reader(page)
assert page.button.clicked and not page.modal.visible
page=Page(Modal('right'),False)
books.close_book_reader(page)
assert not page.modal.visible and page.modal.clicks==['left','right']
page=Page(Modal('neither'),False)
try:
    books.close_book_reader(page)
except RuntimeError as exc:
    assert '仍未确认关闭' in str(exc)
else:
    raise AssertionError('unclosed reader accepted')

print('PASS: cross priority, missing cross fallback, left/right retry, hit recheck, confirmed hiding and failure stops both modes')
