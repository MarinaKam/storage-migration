"""Bounded metadata inventory; never follow links or open source file contents."""
import datetime
import html
import json
import os
from pathlib import Path
import stat
import time

SKIP_DIRS = {'node_modules', '__pycache__', 'venv', 'env'}
RESTRICTED = ('holdout', 'heldout', 'held-out', 'sealed', 'secret', 'credential')
IMAGES = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff', '.bmp', '.heic', '.gif'}


def excluded(name):
    return name.startswith('.') or name in SKIP_DIRS or any(x in name.lower() for x in RESTRICTED)


def scan(root, deadline, remaining):
    result = {'root': str(root), 'groups': {}, 'entries': [], 'excluded_count': 0,
              'errors': [], 'complete': True}
    if root.is_symlink() or not root.is_dir():
        result['errors'].append('Root missing, not a directory, or a symlink')
        result['complete'] = False
        return result
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as items:
                for item in items:
                    if time.monotonic() > deadline or remaining[0] <= 0:
                        result['complete'] = False
                        result['errors'].append('Inventory bound reached')
                        return result
                    remaining[0] -= 1
                    if excluded(item.name):
                        result['excluded_count'] += 1
                        continue
                    path = Path(item.path)
                    rel = path.relative_to(root).as_posix()
                    try:
                        info = item.stat(follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(path)
                            continue
                        group = result['groups'].setdefault(rel.split('/')[0],
                            {'files': 0, 'bytes': 0, 'images': 0, 'symlinks': 0})
                        if stat.S_ISLNK(info.st_mode):
                            group['symlinks'] += 1
                            result['entries'].append({'path': rel, 'kind': 'symlink',
                                'target': os.readlink(path)})
                        elif stat.S_ISREG(info.st_mode):
                            group['files'] += 1
                            group['bytes'] += info.st_size
                            group['images'] += path.suffix.lower() in IMAGES
                            result['entries'].append({'path': rel, 'kind': 'file',
                                'bytes': info.st_size, 'mtime_ns': info.st_mtime_ns,
                                'device': info.st_dev, 'inode': info.st_ino, 'links': info.st_nlink})
                    except OSError:
                        result['errors'].append('Metadata unavailable: ' + rel)
                        result['complete'] = False
        except OSError:
            result['errors'].append('Directory unavailable: ' + str(directory.relative_to(root)))
            result['complete'] = False
    return result


def main():
    project = Path(__file__).resolve().parents[1]
    try:
        source_config = json.loads((project / '.local/sources.json').read_text())
        sources = source_config['roots']
        if not isinstance(sources, list) or not sources or any(
            not isinstance(x, str) or not Path(x).is_absolute() for x in sources
        ):
            raise ValueError('Invalid roots')
    except (OSError, ValueError, KeyError, TypeError):
        print('Missing/invalid .local/sources.json; see README. No inventory run.')
        return 1
    start = time.monotonic()
    deadline = start + 120
    remaining = [250000]
    results = [scan(Path(source), deadline, remaining) for source in sources]
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    dest = project / 'reports' / ('inventory-' + stamp)
    dest.mkdir(parents=True, mode=0o700)
    report = {'generated_at_utc': stamp, 'elapsed_seconds': round(time.monotonic()-start, 3),
        'method': 'metadata only; no source contents, no symlink traversal, no hashes',
        'limit_entries': 250000, 'limit_seconds': 120, 'sources': results}
    (dest / 'inventory.json').write_text(json.dumps(report, indent=2))
    rows=[]
    for result in results:
        for group, counts in sorted(result['groups'].items(), key=lambda x: -x[1]['bytes']):
            rows.append('<tr>' + ''.join('<td>'+html.escape(str(x))+'</td>' for x in (
                Path(result['root']).name, group, counts['files'], counts['images'],
                counts['symlinks'], round(counts['bytes']/1e9, 4))) + '</tr>')
        totals = {k: sum(g[k] for g in result['groups'].values()) for k in ('files','bytes','images','symlinks')}
        status = 'scanned' if result['complete'] else 'PARTIAL'
        print(f"{Path(result['root']).name}: {totals['bytes']/1e9:.3f} GB; "
              f"{totals['images']} image files; {totals['symlinks']} symlinks; {status}")
    limits = ''.join('<li>'+html.escape(Path(x['root']).name)+': исключено '+str(x['excluded_count'])+
        ', ошибок '+str(len(x['errors']))+', '+('обход завершён' if x['complete'] else 'ЧАСТИЧНО')+'</li>' for x in results)
    page='''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Инвентаризация данных</title><style>body{font:16px/1.6 system-ui;margin:32px auto;max-width:1100px;padding:0 20px;color:#223743}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:10px;border-bottom:1px solid #ddd;overflow-wrap:anywhere}th{background:#edf3f5}</style><h1>Инвентаризация данных</h1><p>Собраны только метаданные: пути, размеры и символические ссылки. Содержимое изображений, баз и результатов не открывалось; обращений к облаку нет.</p><p><b>Ограничение:</b> это количество обычных файлов, не уникальных изображений. Символические ссылки не обходились и в объём байтов не включены; жёсткие ссылки могут увеличивать сумму. Скрытые каталоги, окружения и имена с holdout/heldout/held-out/sealed/secret/credential исключены. Проверка по имени не доказывает полную классификацию закрытых данных. Активные писатели и права внешнего хранения здесь не проверяются. Эти числа не являются списком для удаления.</p><table><tr><th>Источник</th><th>Каталог</th><th>Файлов</th><th>Изображений</th><th>Ссылок</th><th>ГБ</th></tr>'''+''.join(rows)+'''</table><h2>Полнота обхода</h2><ul>'''+limits+'''</ul><p><a href="inventory.json">Полные локальные метаданные</a>. Хранить отчёт вне публичного Git.</p></html>'''
    (dest/'inventory.html').write_text(page)
    print('Report: ' + str(dest/'inventory.html'))
    print('No cloud calls, source-content reads, hashes or deletions. Counts are not deduplicated.')
    return 0 if all(x['complete'] for x in results) else 2


if __name__ == '__main__':
    raise SystemExit(main())
