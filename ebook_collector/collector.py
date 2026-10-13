import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from PIL import Image
from playwright.sync_api import sync_playwright
from playwright.sync_api import Error as PlaywrightError
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

ROOT = Path(__file__).resolve().parent
from library_manager import Library, StorageFull, book_id, file_hash, read_settings, validate_pdf
from chapter_reader import collect_chapters, request_image


def page_number(url):
    match = re.search(r'/([0-9]+)\.(?:jpg|jpeg|png)$', urlparse(url).path, re.I)
    return int(match.group(1)) if match else None


def page_matches(displayed, number):
    # Some readers leave just the entered page number in the textbox.
    match = re.match(r'^\s*(\d+)(?:\s*[-–]\s*(\d+))?(?:\s*/\s*\d+)?\s*$', displayed)
    if not match:
        return False
    first = int(match.group(1))
    last = int(match.group(2) or first)
    return first <= number <= last


def verified(path, expected_hash=None):
    try:
        with Image.open(path) as im:
            im.load()
        with Image.open(path) as im:
            im.verify()
        return not expected_hash or hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash
    except Exception:
        return False


def save_json(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def build_pdf(paths, output, outline=None):
    temporary = output.with_suffix('.tmp.pdf')
    pdf = canvas.Canvas(str(temporary))
    pdf.setTitle(output.stem)
    for number, path in enumerate(paths, 1):
        for index, (title, target) in enumerate(outline or []):
            if target == number:
                destination = f'chapter_{index}'
                pdf.bookmarkPage(destination)
                pdf.addOutlineEntry(title, destination, level=0)
        with Image.open(path) as im:
            width, height = im.size
            page_width = 420
            page_height = page_width * height / width
            pdf.setPageSize((page_width, page_height))
            pdf.drawImage(str(path), 0, 0, page_width, page_height)
            pdf.showPage()
    pdf.save()
    validate_pdf(temporary, len(paths))
    temporary.replace(output)


def find_reader(context, timeout=90, pages=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for page in list(pages if pages is not None else context.pages):
            if page.is_closed():
                continue
            try:
                # input() blocks the synchronous event loop; this browser call
                # pumps pending navigation events before taking a frame snapshot.
                page.wait_for_timeout(200)
                for frame in list(page.frames):
                    try:
                        if frame.is_detached():
                            continue
                        field = frame.locator('#currentPageIndexTextField')
                        if field.count() and field.is_visible():
                            value = field.input_value(timeout=2000)
                            if re.search(r'/\s*\d+', value):
                                title = frame.locator('title').text_content(timeout=2000) or '电子书'
                                return frame, value, title
                    except PlaywrightError:
                        # A reader iframe may be replaced during initialization.
                        continue
            except PlaywrightError:
                continue
        time.sleep(0.2)
    raise RuntimeError('未找到已加载的移动版阅读器。请保持书页打开，确认能看到1/152样式的页码，然后重试。')


def activate_mobile_reader(context, pages=None):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        for page in list(pages if pages is not None else context.pages):
            for frame in list(page.frames):
                try:
                    if frame.is_detached():
                        continue
                    body = frame.locator('body').inner_text(timeout=1500)
                    if 'Sorry' in body and 'flash player' in body.lower():
                        mobile = frame.locator('a').filter(has_text=re.compile(r'^\s*here\s*$', re.I))
                        if mobile.count():
                            print('检测到旧版Flash提示，正在打开网站提供的移动版阅读器……')
                            mobile.first.click(timeout=5000)
                            page.wait_for_timeout(500)
                            return
                    if frame.locator('#currentPageIndexTextField').count():
                        return
                except PlaywrightError:
                    continue
        time.sleep(0.25)


def close_reader(page):
    # The site's reader is presented in a modal above the purchased-book list.
    for label in ('关闭', 'Close'):
        button = page.get_by_role('button', name=re.compile(label, re.I))
        try:
            if button.count() and button.last.is_visible():
                button.last.click(timeout=3000)
                page.wait_for_timeout(300)
                return
        except PlaywrightError:
            continue


def catalogue_rows(page, expected_year):
    rows = page.locator('a').filter(has_text=re.compile('查看目录')).evaluate_all("""es => es.map(e => {
        let p=e.parentElement;
        while(p && !p.querySelector('h3')) p=p.parentElement;
        return {source:e.href,title:p?.querySelector('h3')?.textContent?.trim() || ''};
    })""")
    result = {}
    for row in rows:
        year = re.search(r'(19\d{2}|20\d{2})年', row['title'])
        if year and year.group(1) == str(expected_year):
            if urlparse(row['source']).hostname != urlparse(page.url).hostname:
                continue
            result[row['source']] = dict(row, year=int(expected_year), id=book_id(row['source']))
    return list(result.values())


def discover_years(page, settings):
    labels = page.locator('a,button').all_text_contents()
    years = sorted({int(m.group(1)) for value in labels if (m := re.fullmatch(r'\s*((?:19|20)\d{2})年\s*', value))}, reverse=True)
    if settings['years'] != 'all':
        requested = set(map(int, settings['years']))
        years = [y for y in years if y in requested]
    if settings.get('start_year') is not None:
        years = [y for y in years if y <= settings['start_year']]
    if not years:
        raise RuntimeError('没有找到符合设置的年份。请检查栏目页年份标签、years和start_year设置。')
    return years


def load_year(page, year):
    label = page.get_by_text(f'{year}年', exact=True)
    label.first.click(timeout=10000)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if catalogue_rows(page, year):
            break
        page.wait_for_timeout(250)
    rows = catalogue_rows(page, year)
    # Clicking a year may update cards through AJAX. Wait for actual card growth.
    for _ in range(100):
        more = page.get_by_text('加载更多', exact=True)
        if not more.count() or not more.last.is_visible():
            break
        before = len(rows)
        more.last.click(timeout=5000)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            rows = catalogue_rows(page, year)
            if len(rows) > before or not more.count() or not more.last.is_visible():
                break
            page.wait_for_timeout(250)
        if len(rows) <= before:
            break
    return rows


def batch_all_years(context, list_page, settings, library):
    years = discover_years(list_page, settings)
    print(f'发现 {len(years)} 个年份：{years}')
    print(f'资料库：{library.root}；磁盘可用 {library.check_space()/1e9:.1f}GB，预留 {library.reserve/1e9:.0f}GB')
    for year in years:
        library.check_space()
        print(f'\n===== {year} 年 =====')
        tasks = load_year(list_page, year)
        library.db['years'][str(year)] = {'found': len(tasks), 'updated': time.strftime('%Y-%m-%d %H:%M:%S')}
        for task in tasks:
            library.register(task)
        library.flush()
        print(f'发现 {len(tasks)} 本书。')
        for position, task in enumerate(tasks, 1):
            if library.done(task):
                print(f'[{position}/{len(tasks)}] 已完成，跳过：{task["title"]}')
                continue
            library.check_space()
            library.update(task, '处理中', reason='')
            work_page = context.new_page()
            baseline = set(context.pages)
            try:
                response = work_page.goto(task['source'], wait_until='domcontentloaded', timeout=45000)
                if response and response.status in (401, 403, 429):
                    raise PermissionError(f'网站返回 {response.status}，暂停以便检查登录或访问限制。')
                if settings.get('reading_mode') == 'chapters':
                    collect_chapters(context,work_page,task,library,settings,build_pdf)
                    continue
                immediate = work_page.get_by_text('立即阅读', exact=True)
                try:
                    immediate.first.wait_for(state='visible', timeout=5000)
                except PlaywrightError:
                    raise RuntimeError('详情页未显示立即阅读，当前书未完成。')
                print(f'[{position}/{len(tasks)}] 阅读：{task["title"]}')
                immediate.first.click(timeout=10000)
                work_page.wait_for_timeout(300)
                reader_pages = [work_page] + [p for p in context.pages if p not in baseline]
                try:
                    activate_mobile_reader(context, pages=reader_pages)
                    reader_pages = [work_page] + [p for p in context.pages if p not in baseline]
                    frame, text, reader_title = find_reader(context, pages=reader_pages)
                except RuntimeError:
                    print('整本阅读器未加载，改用杂志目录正文采集……')
                    for popup in list(context.pages):
                        if popup not in baseline and not popup.is_closed():
                            popup.close()
                    work_page.goto(task['source'],wait_until='domcontentloaded',timeout=45000)
                    collect_chapters(context,work_page,task,library,settings,build_pdf)
                    continue
                match = re.search(r'/\s*(\d+)', text)
                if not match:
                    raise RuntimeError(f'无法识别页数：{text}')
                collect(context, frame, int(match.group(1)), task, library, settings)
            except (StorageFull, PermissionError) as exc:
                library.update(task, '已暂停', reason=str(exc))
                raise
            except Exception as exc:
                library.update(task, '未完成已终止', reason=str(exc)[:500])
                raise RuntimeError(f'《{task["title"]}》未完成，整批采集已终止，进度保留：{exc}') from exc
            finally:
                for popup in list(context.pages):
                    if popup is work_page or popup not in baseline:
                        if not popup.is_closed():
                            popup.close()
    library.flush()
    counts = {}
    for book in library.db['books'].values():
        counts[book['status']] = counts.get(book['status'], 0) + 1
    print(f'批量结束：{counts}。请打开“书库目录.html”查看。')


def collect(context, frame, total, task, library, settings):
    output, folder = library.paths(task)
    folder.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    recorded_hash = library.register(task).get('sha256')
    if output.exists() and (not recorded_hash or file_hash(output) == recorded_hash):
        # Recover a PDF committed just before an interrupted metadata write.
        try:
            validate_pdf(output, total)
            library.complete(task, output, total)
            if not settings['keep_page_images']:
                shutil.rmtree(folder)
            print(f'已恢复完成记录：{output}')
            return
        except Exception:
            pass
    manifest_path = folder / 'progress.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {'reader': frame.url, 'total': total, 'pages': {}}
    if manifest['total'] != total:
        raise RuntimeError('页数与原进度不一致，请核查书籍。')
    field = frame.locator('#currentPageIndexTextField')
    for number in range(1, total + 1):
        library.check_space()
        path = folder / f'{number:04d}.jpg'
        existing = manifest['pages'].get(str(number))
        if existing and verified(path, existing['sha256']):
            print(f'[{number}/{total}] 已保存，跳过')
            continue
        last_error = None
        for attempt in range(3):
            try:
                if not page_matches(field.input_value(), number):
                    field.fill(str(number))
                    field.press('Enter')
                deadline = time.monotonic() + 25
                candidates = []
                while time.monotonic() < deadline:
                    displayed = field.input_value()
                    imgs = frame.locator('img').evaluate_all('(es) => es.filter(e => e.complete && e.naturalHeight >= 500).map(e => ({src:e.src,w:e.naturalWidth,h:e.naturalHeight}))')
                    candidates = [im for im in imgs if page_number(im['src']) == number]
                    if page_matches(displayed, number) and candidates:
                        break
                    time.sleep(0.3)
                else:
                    save_json(folder / 'diagnostic.json', {'requested_page': number, 'page_field': displayed, 'loaded_images': imgs})
                    raise RuntimeError(f'跳页或图片加载超时（页码框：{displayed}；已加载大图：{len(imgs)}，详情见diagnostic.json）')
                candidate = max(candidates, key=lambda im: im['w'] * im['h'])
                resource = urljoin(frame.url, candidate['src'])
                if urlparse(resource).hostname != urlparse(frame.url).hostname:
                    raise RuntimeError('图片来自其他域名，暂停以便核查。')
                response = request_image(context,resource,frame.url)
                if response.status in (401, 403, 429):
                    raise PermissionError(f'网站返回 {response.status}，停止采集，请在浏览器检查访问状态。')
                if not response.ok:
                    raise RuntimeError(f'下载失败 HTTP {response.status}')
                temporary = path.with_suffix('.part')
                data = response.body()
                library.check_space(len(data) * 2)
                temporary.write_bytes(data)
                if not verified(temporary):
                    raise RuntimeError('返回内容不是有效图片')
                with Image.open(temporary) as im:
                    size = list(im.size)
                temporary.replace(path)
                manifest['pages'][str(number)] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'size': size}
                save_json(manifest_path, manifest)
                print(f'[{number}/{total}] 已保存 {size[0]}×{size[1]}')
                time.sleep(0.5)
                break
            except (PermissionError, StorageFull):
                raise
            except Exception as exc:
                last_error = exc
                print(f'第{number}页重试 {attempt + 1}/3：{exc}')
                time.sleep(1)
        else:
            raise RuntimeError(f'第{number}页失败，进度已保留：{last_error}')
    paths = [folder / f'{n:04d}.jpg' for n in range(1, total + 1)]
    if not all(verified(p, manifest['pages'][str(n)]['sha256']) for n, p in enumerate(paths, 1)):
        raise RuntimeError('完整性检查失败，未生成PDF。')
    hashes = {}
    for n, p in enumerate(paths, 1):
        hashes.setdefault(hashlib.sha256(p.read_bytes()).hexdigest(), []).append(n)
    duplicates = [nums for nums in hashes.values() if len(nums) > 1]
    save_json(folder / 'check.json', {'expected_pages': total, 'saved_pages': len(paths), 'identical_page_groups': duplicates})
    library.check_space(sum(p.stat().st_size for p in paths) * 2 + 64 * 1024 * 1024)
    build_pdf(paths, output)
    library.complete(task, output, total)
    if duplicates:
        library.update(task, '已完成', duplicate_page_groups=duplicates, reason='存在相同页面，请人工核查')
    print(f'完成：{output}')
    if duplicates:
        print(f'发现内容完全相同的页，请人工核查（可能是空白页）：{duplicates}')
    if not settings['keep_page_images']:
        # Only successful, validated PDFs permit removal of their own temporary pages.
        shutil.rmtree(folder)


def launch_browser(playwright):
    for channel in ('msedge', 'chrome', None):
        try:
            options = dict(headless=False, viewport={'width': 1440, 'height': 1000}, accept_downloads=True, chromium_sandbox=True)
            if channel:
                options['channel'] = channel
            profile = ROOT / ('browser_profile_' + channel if channel else 'browser_profile')
            context = playwright.chromium.launch_persistent_context(str(profile), **options)
            print(f'已打开：{channel or "Chromium"}')
            return context
        except PlaywrightError as exc:
            message = str(exc).lower()
            if not any(s in message for s in ("executable doesn't exist", 'distribution', 'executable does not exist')):
                raise
    raise RuntimeError('未找到 Edge、Chrome 或已安装的 Chromium。请安装 Edge/Chrome，或运行 install_browser.bat。')


def prompt_start_year(settings, settings_path):
    current = settings.get('start_year')
    print('下载顺序：从起始年份往更早年份。已完成PDF自动跳过，未完成页继续保存。')
    while True:
        value = input(f'输入起始年份（例如2020）；直接回车保留“{current or "全部年份"}”；输入all恢复全部：').strip()
        if not value:
            break
        if value.lower() == 'all' or value == '全部':
            current = None
            break
        if re.fullmatch(r'(19|20)\d{2}', value):
            current = int(value)
            break
        print('请输入四位年份，例如2020，或输入all。')
    settings['start_year'] = current
    # Preserve existing output location, disk reserve and other user settings.
    raw = json.loads(settings_path.read_text(encoding='utf-8')) if settings_path.exists() else {}
    raw['start_year'] = current
    save_json(settings_path, raw)
    print(f'本次范围：{current}年及更早年份。' if current else '本次范围：全部年份。')
    if settings['years'] != 'all':
        print(f'注意：years设置仍限制为{settings["years"]}，本次取这两个范围的交集。')


def prompt_reading_mode(settings, settings_path):
    print('1：直接从杂志目录进入正文，逐章采集（默认）')
    print('2：先打开立即阅读，失败后再转杂志目录')
    while True:
        choice = input('选择采集方式，直接回车使用1：').strip()
        if choice in ('', '1', '2'):
            break
        print('请输入1或2。')
    settings['reading_mode'] = 'auto' if choice == '2' else 'chapters'
    raw = json.loads(settings_path.read_text(encoding='utf-8')) if settings_path.exists() else {}
    raw['reading_mode'] = settings['reading_mode']
    save_json(settings_path, raw)
    print('本次方式：直接从杂志目录进入。' if choice != '2' else '本次方式：先使用立即阅读。')


def main():
    settings = read_settings(ROOT)
    prompt_start_year(settings, ROOT / 'settings.json')
    prompt_reading_mode(settings, ROOT / 'settings.json')
    library = Library(settings['output_path'], settings['reserve_gb'])
    library.check_space()
    library.flush()
    print('浏览器即将打开。请登录，并选择想下载的杂志栏目（能看到年份标签）。')
    with sync_playwright() as p:
        context = launch_browser(p)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto('https://www.easteat.com/book_list.php')
        input('栏目页显示年份标签后，回到此窗口按回车开始按起始年份采集：')
        # The user may select a magazine category in a newly opened tab.
        for candidate in reversed(context.pages):
            try:
                candidate.wait_for_timeout(200)
                discover_years(candidate, settings)
                page = candidate
                break
            except (PlaywrightError, RuntimeError):
                continue
        try:
            batch_all_years(context, page, settings, library)
        finally:
            context.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n已停止，重新运行可继续。')
    except Exception as exc:
        print(f'\n未完成：{exc}\n已保存的图片和进度不会删除。')
    input('按回车退出。')
