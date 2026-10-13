"""Close through exposed modal backdrop only, never through page controls."""
from playwright.sync_api import Error as PlaywrightError


BACKDROP_POINTS = r'''e => {
  const points=[];
  let target=e;
  for(let ancestor=0;target && ancestor<8;ancestor++,target=target.parentElement) {
    if(target.tagName==='BODY' || target.tagName==='HTML') break;
    if(!target.matches('.modal,.reader-modal,[role="dialog"]') && getComputedStyle(target).position!=='fixed') continue;
    const r=target.getBoundingClientRect();
    const left=Math.max(r.left,0),right=Math.min(r.right,innerWidth);
    const top=Math.max(r.top,0),bottom=Math.min(r.bottom,innerHeight);
    if(right-left<20 || bottom-top<20) continue;
    for(const x of [left+8,right-8]) {
      for(const fraction of [0.5,0.35,0.65]) {
        const y=top+(bottom-top)*fraction;
        // A click is eligible only when it hits the backdrop itself. Book
        // pages, iframe content, arrows and underlying site controls are out.
        if(document.elementFromPoint(x,y)!==target) continue;
        const contents=[...target.querySelectorAll('.modal-content,.reader-container,.modal-dialog')];
        if(contents.some(c=>{const b=c.getBoundingClientRect();return x>=b.left && x<=b.right && y>=b.top && y<=b.bottom;})) continue;
        points.push({ancestor,x:x-r.left,y:y-r.top});
        break;
      }
    }
    if(points.length) break;
  }
  return points;
}'''


def close_by_backdrop(modal):
    """Try left, then right; completion requires the original body to hide."""
    points = modal.evaluate(BACKDROP_POINTS)
    for point in points:
        target = modal
        for _ in range(point['ancestor']):
            target = target.locator('xpath=..')
        try:
            # Recheck the hit immediately before acting in case layout changed.
            if not target.evaluate('''(e,p)=>{const r=e.getBoundingClientRect();
                return document.elementFromPoint(r.left+p.x,r.top+p.y)===e;}''', point):
                continue
            target.click(position={'x': point['x'], 'y': point['y']}, timeout=5000)
            modal.wait_for(state='hidden', timeout=5000)
            return True
        except PlaywrightError:
            if not modal.is_visible():
                return True
    return False
