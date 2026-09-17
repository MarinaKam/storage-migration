"""Download missing manifest images locally. Never contact R2 or overwrite files."""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import csv
import datetime
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import tempfile
import sys
import time
import urllib.parse
import urllib.request
import warnings

from reconcile import reconcile

MAX_BYTES = 25 * 1024 * 1024
MAX_RUN_BYTES = 5 * 1024**3


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_url(url, hosts):
    u = urllib.parse.urlsplit(url)
    if (u.scheme != 'https' or u.hostname not in hosts or u.username or u.password
            or u.port not in (None, 443) or u.query or u.fragment):
        raise ValueError('Source URL outside configured HTTPS boundary')
    return url


def image_check(path):
    from PIL import Image
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            im.load()


def fetch_one(ident, url, images, opener=None):
    # Directory and manifest are operator-controlled; concurrent hostile local writers are out of scope.
    target = images / (ident + '.jpg')  # Same filename contract as the evaluation consumer.
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    temp = None
    try:
        fd, name = tempfile.mkstemp(prefix='.' + ident + '-', suffix='.part', dir=images)
        temp = Path(name)
        with os.fdopen(fd, 'wb') as out:
            with opener.open(url, timeout=30) as response:
                if response.status != 200:
                    raise ValueError('Unexpected source status')
                expected = response.headers.get('Content-Length')
                expected = int(expected) if expected is not None else None
                if expected is not None and not 0 < expected <= MAX_BYTES:
                    raise ValueError('Invalid source length')
                digest = hashlib.sha256()
                count = 0
                while chunk := response.read(128 * 1024):
                    count += len(chunk)
                    if count > MAX_BYTES:
                        raise ValueError('Image exceeds size bound')
                    out.write(chunk)
                    digest.update(chunk)
                if count == 0 or (expected is not None and count != expected):
                    raise ValueError('Incomplete source response')
            out.flush()
            os.fsync(out.fileno())
        image_check(temp)
        if hashlib.sha256(temp.read_bytes()).hexdigest() != digest.hexdigest():
            raise ValueError('Local read-back mismatch')
        # Atomic create without replacing an existing file, including a symlink.
        os.link(temp, target)
        return {'image_id': ident, 'status': 'downloaded', 'bytes': count,
                'sha256': digest.hexdigest(), 'filename': target.name}
    except Exception as exc:
        return {'image_id': ident, 'status': 'failed', 'error_type': type(exc).__name__}
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100000:
        parser.error('limit must be 1..100000')
    try:
        import PIL  # noqa: F401
    except ImportError:
        print('Run make deps first to install the image decoder into this project .venv.')
        return 1
    project = Path(__file__).resolve().parents[1]
    lock = (project / '.local/download.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('Another download command is already running.')
        return 1
    try:
        config = json.loads((project / '.local/dataset.json').read_text())
        manifest, images = Path(config['manifest']), Path(config['images'])
        hosts = config['allowed_source_hosts']
        if not manifest.is_absolute() or not images.is_absolute() or not isinstance(hosts, list) or not hosts:
            raise ValueError('Invalid local dataset configuration')
        state = reconcile(manifest, images)
        if set(state['counts']) - {'missing', 'present_unverified'} or state['rows_without_source_url']:
            raise ValueError('Resolve reconciliation problems before download')
        with manifest.open(newline='', encoding='utf-8-sig') as stream:
            sources = {r['image_id'].strip(): validate_url(r['s3_url'].strip(), hosts)
                       for r in csv.DictReader(stream)}
        missing = [r['image_id'] for r in state['rows'] if r['status'] == 'missing']
        chosen = missing[:args.limit]
        # Worst-case traffic bound: each selected request can consume MAX_BYTES.
        if len(chosen) * MAX_BYTES > MAX_RUN_BYTES:
            raise ValueError('Use at most 204 images per invocation (5 GiB worst-case bound)')
    except (OSError, ValueError, KeyError, TypeError, csv.Error):
        print('Configuration/manifest rejected. Run make reconcile and check .local/dataset.json.')
        return 1
    print(f'Downloading {len(chosen)} of {len(missing)} missing images to {images}', flush=True)
    print('Existing files unchanged. No R2 calls. Source GET requests begin now.', flush=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    dest = project / 'reports' / ('download-' + stamp)
    dest.mkdir(parents=True, mode=0o700)
    results = []
    with (dest / 'journal.jsonl').open('x') as journal:
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = {pool.submit(fetch_one, ident, sources[ident], images) for ident in chosen}
            started = time.monotonic()

            def show_progress():
                completed = len(results)
                good_now = sum(r['status'] == 'downloaded' for r in results)
                failed_now = completed - good_now
                mb = sum(r.get('bytes', 0) for r in results) / 1_000_000
                elapsed = int(time.monotonic() - started)
                percent = 100 * completed / len(chosen) if chosen else 100
                bar = '#' * int(percent / 5) + '-' * (20 - int(percent / 5))
                line = (f"[{bar}] {percent:5.1f}% | processed {completed}/{len(chosen)}"
                        f" | saved {good_now} | failed {failed_now}"
                        f" | saved {mb:.2f} MB | elapsed {elapsed // 60:02d}:{elapsed % 60:02d}")
                if sys.stdout.isatty():
                    print('\r' + line + '    ', end='', flush=True)
                else:
                    print(line, flush=True)

            show_progress()
            try:
                while pending:
                    done, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
                    for future in done:
                        row = future.result()
                        results.append(row)
                        journal.write(json.dumps(row) + '\n')
                        journal.flush()
                        os.fsync(journal.fileno())
                    show_progress()
            finally:
                if sys.stdout.isatty():
                    print(flush=True)
    good = sum(r['status'] == 'downloaded' for r in results)
    failed = len(results) - good
    page = '<!doctype html><html lang="en"><meta charset="utf-8"><title>Image download</title><h1>Image download</h1>'
    page += f'<p>Downloaded: {good}. Failed: {failed}. Selected: {len(chosen)}. Not selected: {len(missing)-len(chosen)}.</p>'
    page += '<p>New files were decoded and their local read-back hashes matched the received bytes. This does not prove source immutability or remote R2 integrity. Existing files were not verified or overwritten. Original bytes are preserved; filenames use the .jpg convention regardless of format.</p><p>Reruns skip existing files. A crash after publication but before journaling can leave an existing unverified file. Hidden temporary files are not treated as completed images. No original files are deleted.</p><p><a href="journal.jsonl">Local journal</a></p><table>'
    page += ''.join('<tr><td>'+html.escape(r['image_id'])+'</td><td>'+html.escape(r['status'])+'</td></tr>' for r in results)
    (dest / 'download.html').write_text(page + '</table></html>')
    print(f'Downloaded: {good}; failed: {failed}; remaining at least: {len(missing)-good}')
    print('Report: ' + str(dest / 'download.html'))
    return 2 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
