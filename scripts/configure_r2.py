import getpass
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

root = Path(__file__).resolve().parents[1] / '.local'
root.mkdir(mode=0o700, exist_ok=True)
if root.is_symlink():
    raise SystemExit('Refusing symlink directory')
os.chmod(root, 0o700)
(root / '.gitignore').write_text('*\n')
target = root / 'credentials.json'
if target.exists():
    raise SystemExit('Credentials already saved; nothing overwritten.')
print('Paste each value, then press Enter. Input is hidden.')
endpoint = getpass.getpass('S3 endpoint (https://...): ').strip().rstrip('/')
p = urlsplit(endpoint)
if (p.scheme != 'https' or not p.hostname or not p.hostname.endswith('.r2.cloudflarestorage.com')
        or p.username or p.password or p.port or p.path or p.query or p.fragment):
    raise SystemExit('Invalid R2 S3 endpoint. Nothing saved.')
bucket = getpass.getpass('Bucket name: ').strip()
if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', bucket):
    raise SystemExit('Invalid bucket name. Nothing saved.')
access = getpass.getpass('Access Key ID: ').strip()
secret = getpass.getpass('Secret Access Key: ').strip()
if not access or not secret:
    raise SystemExit('Empty value. Nothing saved.')
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
fd = os.open(target, flags, 0o600)
with os.fdopen(fd, 'w') as f:
    json.dump({'endpoint': endpoint, 'access_key_id': access, 'secret_access_key': secret,
               'bucket': bucket, 'region': 'auto'}, f)
    f.flush()
    os.fsync(f.fileno())
print('Saved locally. No cloud connection or upload was made.')
