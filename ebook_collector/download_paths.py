"""All additional download categories live beside the configured magazine root."""
from pathlib import Path
import shutil


def category_root(output,name):
    output=Path(output)
    if Path(name).name!=name or name in ('','.', '..'):
        raise ValueError('下载分类名称无效')
    target=output.parent/name
    legacy=output/name
    if legacy.exists() and not target.exists():
        # Move the whole old category, including progress and relative indexes.
        shutil.move(str(legacy),str(target))
    elif legacy.exists():
        print(f'新旧分类目录同时存在，使用{target}；原{legacy}保留，请核查后手动合并。')
    return target
