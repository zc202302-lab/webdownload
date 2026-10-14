import sys
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path('ebook_collector').resolve()))
from download_paths import category_root

with tempfile.TemporaryDirectory() as folder:
    root=Path(folder)
    output=root/'output'
    legacy=output/'电子书'
    legacy.mkdir(parents=True)
    (legacy/'progress.json').write_text('preserved')
    target=category_root(output,'电子书')
    assert target==root/'电子书' and (target/'progress.json').read_text()=='preserved' and not legacy.exists()
    assert category_root(output,'爆款菜谱')==root/'爆款菜谱'
    assert category_root(output,'未来分类')==root/'未来分类'
    legacy.mkdir()
    (legacy/'older.pdf').write_bytes(b'preserve')
    assert category_root(output,'电子书')==target and (legacy/'older.pdf').exists()
print('PASS: sibling category paths, legacy progress move and conflicting directories preserved')
