# Storage migration

A small Python 3.14 project for preparing verified object-storage migrations.
Current functionality: local configuration, bucket access checks, metadata inventory,
manifest reconciliation and resumable local image downloads with live progress.
Direct R2 migration with read-back verification is available. Database restore
commands are not implemented yet.

## Quick start

Install Python **3.14** if it is not already available, then open a terminal in
this repository's directory:

```sh
make setup
make deps
make check
make configure
make check-r2
```

- `make setup` creates **`.venv/` inside this project** using `python3.14`.
- `make check` checks the interpreter and Python syntax, without cloud access.
- `make configure` saves connection settings through hidden terminal prompts.
- `make deps` installs dependencies declared in `pyproject.toml` into `.venv/`.
- `make check-r2` makes a HeadBucket request to the configured R2 endpoint with SDK retries disabled. It does not list or transfer objects. R2 operation billing rules still apply.
- `make help` lists these commands.

Run `make deps` to install boto3, Pillow and their dependencies into `.venv/`.
This downloads packages from the configured Python package index; it does not contact R2. Setup does not modify the system Python or install
anything globally. `.venv` points to the existing base Python installation;
it is not a portable copy to commit or transfer to another machine.

Python itself is a prerequisite: `make setup` does not download it. Install it
from [python.org](https://www.python.org/downloads/) or your normal package manager.
If your Python 3.14 executable has a different path:

```sh
make setup PYTHON=/path/to/python3.14
```

Without `make`:

```sh
python3.14 -m venv .venv
.venv/bin/python scripts/configure_r2.py
```

## PyCharm

Open the cloned project folder. In Python Interpreter settings, select an
existing local environment and choose **`<project>/.venv/bin/python`**.
Run `make configure` in the **Terminal**, not a non-interactive Run console.
No shell activation is required for the Makefile commands.

## Configure R2

Create an R2 credential restricted to the intended bucket. Its name is stored
only in your local configuration. Existing saved settings are preserved.

Run `make configure`, then paste each value and press Enter:

1. **S3 endpoint**: `https://<account-id>.r2.cloudflarestorage.com`, or the
   appropriate jurisdiction-specific S3 endpoint. Do not append a bucket name.
2. **Bucket name**.
3. **Access Key ID**.
4. **Secret Access Key**, not the separate Cloudflare API token value.

Input is hidden; no characters or stars appear. On completion:

```text
Saved locally. No cloud connection or upload was made.
```

Settings are saved in **`.local/credentials.json` inside this project**.
The directory has mode 0700 and the file 0600. The file is plaintext protected by
filesystem permissions, not an encrypted vault. Keep a backup in a password
manager. Existing settings are not overwritten. The command does not check cloud
connectivity, upload data, or validate the permissions of the supplied key.

Do not paste credentials into chat, command arguments, screenshots, or issues.
If hidden-input fallback is reported, stop and use an interactive terminal.

## Public repository boundary

Intended public files:

- `README.md`
- `pyproject.toml`, `.python-version`, `Makefile`, `.gitignore`
- `scripts/` source code

Git uses a default-deny file list: only the public files above and direct
`scripts/*.py` files are eligible. Everything else is ignored by default.
Review every change before publication; newly added Python files are also eligible.

Local-only paths excluded from Git:

| Path | Contents |
| --- | --- |
| `.venv/` | Project interpreter environment |
| `.local/`, `.env*` (except `.env.example`) | Credentials and machine settings |
| `.idea/`, `.vscode/` | Local editor configuration |
| `manifests/` | Private source paths and file inventories |
| `plans/` | Internal migration plans |
| `logs/`, `reports/` | Execution journals and retained results |

Do not put source images, database dumps or retained experiments into the public
source tree. Keep them outside the repository or in an explicitly ignored local
location. `.gitignore` is not a secret scanner: it does not remove already tracked
files and can be bypassed by force-add. Before each publication, review the staged
file list and diff. No remote or push is configured by the setup commands.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `python3.14` not found | Install Python 3.14 or pass its path with `PYTHON=...` |
| No interpreter in PyCharm | Run `make setup`, then select `.venv/bin/python` |
| Wrong interpreter after changing Python | Recreate the disposable `.venv`; preserve `.local` |
| `make` missing | Use the Python commands above |
| Invalid endpoint | Copy the R2 S3 endpoint, not the dashboard browser URL |
| Credentials already saved | Existing settings are preserved; arrange deliberate replacement |

## Future migration commands

Consistent database restore is not implemented yet. Upload requires an exact approved source list,
permission to store those files remotely and a bounded budget. Deletion requires
separate approval of an exact list after verification.

## R2 access check

After configuration, run `make deps` and then `make check-r2`. A successful check
prints `R2 connection OK`. This proves bucket access only, not write
permissions, completeness of a migration, or hash integrity. Authentication errors
are reported without secret values.

API reference: https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/head_bucket.html

## Local inventory

Run `make inventory`. Configure source directories in ignored `.local/sources.json`:

```json
{"roots": ["/absolute/path/to/source"]}
```

The command reads directory entries and file metadata, not source contents. It
does not follow symlinks, hash images, open databases, identify writers or contact
the cloud. Hidden entries, environment/dependency directories, and names carrying
sealed/holdout/secret markers are excluded. Name filtering alone does not certify
that every sensitive dataset has been classified. Bounds: 250,000 entries and
120 seconds across all roots. Errors or a reached bound give a partial report
and nonzero exit code; they are never reported as complete.

Results go to `reports/inventory-<UTC timestamp>/inventory.html` and
`inventory.json`; both stay outside public Git. Byte totals exclude symlinks and
may count hard links repeatedly. File counts are not unique-content counts.
Do not use them as deletion approval.

## Match existing images

Run `make reconcile` to match the full manifest to an existing image directory.
Ignored `.local/dataset.json` configures absolute `manifest` and `images` paths.
The command reads the CSV and image directory metadata only, without downloading
or uploading. It does not import an evaluation package or require its environment.
It matches image IDs to filename stems across supported image extensions.
Nonempty regular files are `present_unverified`, not verified reusable copies.
Duplicates, empty files, links and ambiguous matches are reported separately.
Malformed input stops the command; missing images are a normal result.
Limits: 100 MB CSV, 100,000 rows, 250,000 local directory entries.
Reports are private `reports/reconcile-<timestamp>/reconcile.html` and JSON.
The command does not verify image content, source equality or remote copies.

## Download a bounded batch of missing images

Run `make deps`, then `make download`. The default batch is 20 missing images.
The operator runs this command: it makes source HTTPS GET requests and stores
photos in the `images` directory configured in ignored `.local/dataset.json`.
No R2 calls occur. Existing photos, tags, captions, grouping and manifests are
unchanged. Configure `allowed_source_hosts` locally; redirects, proxies, HTTP,
URL credentials and query strings are not accepted. No source URLs are logged.

Original bytes are preserved, with `<image_id>.jpg` filenames matching the
existing evaluation tool, regardless of actual image format. New files must
decode and pass a local SHA-256 read-back before atomic no-overwrite publication.
Temporary files are hidden; interrupted transfers are not final images. Re-run
the command to continue; failed requests remain missing. Connection setup failures receive up to three attempts with 2- and 4-second
backoff. HTTP errors, TLS errors, response-body failures and local failures are
not retried automatically. Retry counts and sanitized error categories are logged. A forced process termination may leave hidden `.part` files.
An exclusive lock prevents concurrent invocations from this project. Concurrent
external writers to the image directory are outside this tool's threat boundary.

Bounds: four workers, 30-second socket timeout, 25 MiB per object; at most
204 selected objects per invocation (5 GiB worst-case response-body budget).
`make download LIMIT=100` changes the batch size. HTTP/library overhead is not
part of the body budget. Source service billing may apply.
Private journals and HTML summaries appear in `reports/download-*/`.
Existing files are only present-unverified; these checks do not prove their
integrity, source immutability or remote R2 restore. No automatic sorting occurs.

Download progress updates after each completion and once per second while waiting.
The percentage is processed files in the selected batch, including failures.
Saved MB counts completed successful files, not bytes of in-flight responses.
An interactive terminal uses one updating line; redirected output uses plain lines.

## Configure a local dataset

Create `.local/dataset.json` with your own absolute paths and source hostname:

```json
{
  "manifest": "/path/to/dataset/manifest.csv",
  "images": "/path/to/dataset/images",
  "allowed_source_hosts": ["images.example.com"]
}
```

The existing image directory must already exist. CSV columns `image_id` and
`s3_url` are required. IDs must contain only letters, digits, underscores or
hyphens. Configuration examples are placeholders, not working source addresses.
These local settings are never required in the public repository.

## Download all remaining images

Run `make download-all` for one pass over every missing image in the configured
manifest. This explicitly selects the full remaining dataset rather than a
limited batch: the 5 GiB worst-case batch selection cap does not apply.
The 25 MiB per-image limit, four workers, connection retries, source-host checks
and no-overwrite behavior remain in effect. Total transfer size is unknown in
advance. No R2 calls occur. Progress covers all images selected for this run.

Only four tasks are submitted at a time. An unsuccessful image is recorded and
the command continues to later images; it does not get stuck repeating the same
failed batch. Failures remaining after the bounded connection retries produce
exit code 2 at the end. Re-run the same command to attempt remaining files.
Ctrl+C stops scheduling new images; active requests may take time to finish.
Already saved images stay in place and are skipped on the next invocation.

## Direct transfer to R2

`make r2-plan` reads the local manifest and metadata and prints the destination
and source counts without network calls. `make r2-transfer` then processes the
whole dataset in one invocation. The operator runs both commands.

Connection settings remain in `.local/credentials.json`. Dataset paths and
source-host restrictions remain in `.local/dataset.json`. Add ignored
`.local/r2-transfer.json` with this structure, using your own absolute paths:

```json
{
  "prefix": "dataset",
  "metadata_files": ["/path/to/dataset/manifest.csv", "/path/to/dataset/tags.json"]
}
```

The explicit metadata list must include the exact dataset manifest. Objects use
`<prefix>/metadata/<filename>` and `<prefix>/images/<image_id><source-extension>`.
Metadata is copied byte-for-byte. Tags and group assignments are not regenerated.
URLs in metadata remain the original URLs; consumers must map image IDs to the
new keys. Original bytes are preserved, including local images named `.jpg` that
actually contain another supported image format. Content-Type comes from decoding.

Existing local image files are checked for stable reads and decoded. They are
operator-controlled inputs: decoding does not prove equality with source S3.
Missing local images are fetched into bounded memory, never saved as local photos.
Each image is limited to 25 MiB; metadata files to 100 MiB; four images run at once.
Decoded images can use more memory than compressed response bytes. There is no
total dataset byte limit or enforced currency cap; source GET and R2 operations
are billed according to the configured services. No compute service is deployed.

An existing remote object's bytes must match; conflicting objects are never
overwritten. New objects use conditional `If-None-Match: *` writes, then a full
remote GET and SHA-256 comparison. ETags and user-supplied remote hash metadata
are not accepted as proof. Support is documented in the
[Cloudflare S3 compatibility table](https://developers.cloudflare.com/r2/api/s3/api/).

Private receipts under `.local/r2-state/` bind the manifest/metadata snapshot,
destination, key, source-URL digest, content hash and size. Resume re-reads remote
bytes against those receipts; it does not fetch the original URL again for an
already verified object. Therefore resume verifies the recorded snapshot, not
that a mutable original URL still serves the same content today. Receipts and
local sources are trusted operator-owned state. A changed local file, changed
remote object or metadata conflict is not silently accepted. Keep the receipts.
A lost receipt causes the source to be read again and compared with R2.

Ctrl+C stops scheduling new work and lets active requests finish or time out.
Repeated Ctrl+C prints the stopping message without a traceback. No files are
deleted. JSONL journals stay under `logs/`; no HTML/PDF is generated or opened.
Exit 2 means some image transfers failed; retry the same command. Metadata failure
stops before photos. All failures require review before declaring a full migration.
Live R2 upload, actual service behavior and downstream application consumption
have not been exercised by the preparation checks. A verified stored copy is not
a database restore, retention guarantee, or permission to delete local sources.

Run `make test-r2` for offline tests using generated fixtures and an in-memory
S3 double. They cover plan-to-metadata-to-image transfer, resume, byte corruption,
conditional-write races, lost upload acknowledgements, access denial, invalid
images, source restrictions and duplicate identities. They make no network calls.

Source URLs without a filename extension are supported: their R2 key is
`<prefix>/images/<image_id>` without an invented suffix. The image content is
still decoded and Content-Type comes from its actual format.

## Continuous integration

GitHub Actions runs on pushes to main, pull requests and manual dispatch.
The single Python 3.14 job checks dependency consistency, public file scope,
syntax, Ruff lint, Ruff formatting and offline tests. It has read-only repository
permissions, uses no private checkout or user secrets, and does not upload data.
Dependency installation requires network access; test socket connections are
blocked and fixtures are generated in temporary directories.

Run the same checks locally:

```sh
make dev
make ci
```

Ruff installs into this project's .venv. Public-file checks reject unexpected
tracked files, personal absolute paths, Cyrillic content and private-key blocks.
They are a limited working-tree check, not a complete secret scanner.
The workflow becomes active after it is committed and pushed. Requiring the
`Python 3.14 checks` status before merge is a separate repository ruleset setting.
