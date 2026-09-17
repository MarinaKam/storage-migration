"""Match manifest IDs to local filenames offline; presence is not integrity proof."""
import csv
import datetime
import html
import json
from collections import Counter, defaultdict
from pathlib import Path
import re
import stat

EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff', '.heic'}


def reconcile(manifest, images):
    if manifest.is_symlink() or images.is_symlink() or not images.is_dir():
        raise ValueError('Use a regular manifest and a real image directory')
    if manifest.stat().st_size > 100_000_000:
        raise ValueError('Manifest exceeds 100 MB limit')
    local = defaultdict(list)
    for n, path in enumerate(images.iterdir()):
        if n >= 250_000:
            raise ValueError('Image directory exceeds entry limit')
        if path.suffix.lower() in EXTENSIONS:
            info = path.lstat()
            local[path.stem].append((path.name, info.st_size, stat.S_ISREG(info.st_mode)))
    rows = []
    with manifest.open(newline='', encoding='utf-8-sig') as stream:
        reader = csv.DictReader(stream)
        if not {'image_id', 's3_url'}.issubset(reader.fieldnames or []):
            raise ValueError('Manifest requires image_id and s3_url columns')
        for n, row in enumerate(reader):
            if n >= 100_000:
                raise ValueError('Manifest exceeds row limit')
            ident = (row.get('image_id') or '').strip()
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', ident):
                raise ValueError(f'Invalid image_id at CSV row {n+2}')
            rows.append({'image_id': ident, 'has_source_url': bool((row.get('s3_url') or '').strip())})
    frequencies = Counter(row['image_id'] for row in rows)
    for row in rows:
        files = local.get(row['image_id'], [])
        if frequencies[row['image_id']] > 1:
            status = 'duplicate_manifest_id'
        elif len(files) > 1:
            status = 'ambiguous_local_files'
        elif not files:
            status = 'missing'
        elif not files[0][2]:
            status = 'not_regular_file'
        elif files[0][1] == 0:
            status = 'empty_file'
        else:
            status = 'present_unverified'
        row.update(status=status, local_files=[f[0] for f in files])
    return {'rows': rows, 'counts': dict(Counter(r['status'] for r in rows)),
            'manifest_rows': len(rows), 'unique_ids': len(frequencies),
            'rows_without_source_url': sum(not r['has_source_url'] for r in rows),
            'local_ids_outside_manifest': sorted(set(local) - set(frequencies))}


def main():
    project = Path(__file__).resolve().parents[1]
    try:
        config = json.loads((project / '.local/dataset.json').read_text())
        manifest, images = Path(config['manifest']), Path(config['images'])
        if not manifest.is_absolute() or not images.is_absolute():
            raise ValueError('Configured paths must be absolute')
        result = reconcile(manifest, images)
    except (OSError, ValueError, KeyError, TypeError, csv.Error) as exc:
        print('Reconciliation stopped: ' + str(exc))
        return 1
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    dest = project / 'reports' / ('reconcile-' + stamp)
    dest.mkdir(parents=True, mode=0o700)
    (dest / 'reconcile.json').write_text(json.dumps(result, indent=2))
    labels = {'missing': 'Отсутствуют', 'present_unverified': 'Есть локально, содержимое не проверено',
              'empty_file': 'Пустые файлы', 'not_regular_file': 'Ссылки или специальные файлы',
              'ambiguous_local_files': 'Несколько файлов для одного ID', 'duplicate_manifest_id': 'Повторяющийся ID в CSV'}
    summary = ''.join(f'<tr><td>{labels[k]}</td><td>{v}</td></tr>' for k,v in result['counts'].items())
    detail = ''.join('<tr><td>'+html.escape(r['image_id'])+'</td><td>'+labels[r['status']]+'</td></tr>' for r in result['rows'] if r['status'] != 'present_unverified')
    page = '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Сверка фотографий</title><style>body{font:17px/1.6 system-ui;max-width:1000px;margin:40px auto;padding:0 24px;color:#203343}table{border-collapse:collapse;width:100%}td{padding:8px;border-bottom:1px solid #ddd}</style><h1>Сверка манифеста с локальными фотографиями</h1><p>Сопоставление по image_id и имени файла, независимо от расширения. Проверены только наличие, тип и ненулевой размер. Содержимое фото не читалось. Сеть не использовалась.</p><table>'''+summary+'''</table><p>Строк CSV: '''+str(result['manifest_rows'])+'''; уникальных ID: '''+str(result['unique_ids'])+'''; строк без s3_url: '''+str(result['rows_without_source_url'])+'''; локальных ID вне манифеста: '''+str(len(result['local_ids_outside_manifest']))+'''.</p><h2>Границы результата</h2><p>Наличие файла не доказывает правильность формата, полноту или совпадение с источником. Список отсутствующих — кандидаты на скачивание, а не разрешение на перенос. Перед повторным использованием локальных файлов нужна проверка содержимого. Никакие данные не удалены и не переданы в облако.</p><p>Источники: '''+html.escape(str(manifest))+'; '+html.escape(str(images))+'''.</p><p><a href="reconcile.json">Полный локальный результат</a> — хранить вне публичного Git.</p><h2>Отсутствующие и требующие разбора</h2><table>'''+detail+'</table></html>'
    (dest / 'reconcile.html').write_text(page)
    print(f"Manifest: {result['manifest_rows']} rows; {result['unique_ids']} unique IDs")
    for key in labels:
        print(f'{key}: {result["counts"].get(key, 0)}')
    print(f"Local IDs outside manifest: {len(result['local_ids_outside_manifest'])}")
    print(f"Rows without source URL: {result['rows_without_source_url']}")
    print('Report: ' + str(dest / 'reconcile.html'))
    print('Offline filename match only. No image-content checks, downloads or uploads.')
    problems = {'duplicate_manifest_id', 'ambiguous_local_files', 'empty_file', 'not_regular_file'}
    return 2 if problems.intersection(result['counts']) or result['rows_without_source_url'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
