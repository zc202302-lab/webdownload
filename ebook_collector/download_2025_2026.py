"""Download only the requested 2025 and 2026 electronic-journal collections."""
from collector import main as collect


def main():
    collect(target_url='https://www.easteat.com/book_list.php?category=dianzikan&year=2025', target_years=[2026, 2025])

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('已停止，重新运行本入口可继续。')
    except Exception as exc:
        print(f'未完成：{exc}；进度已保留。')
        raise SystemExit(1)
