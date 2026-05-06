"""Hash every photo/video on the laptop, check it's on the NAS.

For each missing file, copy it to P:\\Incoming so PhotoGuard organizes it.
Won't create duplicates: each file is only copied if its hash isn't on the
NAS, and PhotoGuard re-checks hashes when it picks them up.

Use --upload to actually copy missing files; default is dry-run report.
"""
import argparse
import hashlib
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from nas_config import PHOTO_LIBRARY_ROOT, PHOTO_DB_DIR, smb_connect, NAS_SHARE

NAS_HASH_DB = PHOTO_DB_DIR / 'nas_photo_hashes.json'
LOG_FILE = PHOTO_DB_DIR / 'verify_laptop_complete.log'
INCOMING = PHOTO_LIBRARY_ROOT.parent / 'Incoming'

SOURCE_DIRS = [
    r'C:\Users\chaim\Desktop',
    r'C:\Users\chaim\Pictures',
    r'C:\Users\chaim\Documents',
    r'C:\Users\chaim\Downloads',
    r'C:\Users\chaim\OneDrive',
]

PHOTO_EXTS = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.gif', '.bmp',
              '.tiff', '.tif', '.webp', '.raw', '.cr2', '.nef', '.arw', '.dng'}
VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.m4v', '.3gp', '.wmv'}
ALL_EXTS = PHOTO_EXTS | VIDEO_EXTS


def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'{ts} {msg}'
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode('ascii', 'replace').decode('ascii'), flush=True)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def file_hash(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def scan_laptop():
    files = []
    for src in SOURCE_DIRS:
        p = Path(src)
        if not p.exists():
            continue
        log(f'Scanning {src} ...')
        for f in p.rglob('*'):
            try:
                if f.is_file() and f.suffix.lower() in ALL_EXTS:
                    files.append(f)
            except OSError:
                continue
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upload', action='store_true',
                        help='actually copy missing files to P:\\Incoming')
    parser.add_argument('--max-uploads', type=int, default=None,
                        help='cap on how many files to upload this run')
    args = parser.parse_args()

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log('=' * 60)
    log('Laptop coverage verification starting')

    if not NAS_HASH_DB.exists():
        log(f'ERROR: hash DB not found at {NAS_HASH_DB}')
        sys.exit(1)
    db = json.loads(NAS_HASH_DB.read_text(encoding='utf-8'))
    nas_hashes = set(db.keys())
    log(f'NAS hash DB has {len(nas_hashes)} entries')

    laptop_files = scan_laptop()
    log(f'Found {len(laptop_files)} media files on laptop')

    missing = []
    seen_hashes = set()  # to skip dup hashes we encounter on the laptop too
    duplicates_on_laptop = 0
    start = time.time()
    for i, f in enumerate(laptop_files, 1):
        try:
            h = file_hash(f)
        except Exception as e:
            safe = str(f).encode('ascii', 'replace').decode('ascii')
            log(f'  hash-error {safe}: {e}')
            continue
        if h in nas_hashes:
            continue  # already on NAS
        if h in seen_hashes:
            duplicates_on_laptop += 1
            continue  # earlier laptop file with same content; don't double-count
        seen_hashes.add(h)
        missing.append((f, h))
        if i % 500 == 0:
            elapsed = time.time() - start
            rate = i / elapsed if elapsed else 0
            log(f'  hashed {i}/{len(laptop_files)} ({rate:.0f}/s)  missing={len(missing)}')
    elapsed = time.time() - start
    log(f'Hashing done in {elapsed:.0f}s. Missing: {len(missing)}; '
        f'laptop self-duplicates: {duplicates_on_laptop}')

    if not missing:
        log('All laptop photos already on NAS.')
        return

    # Show first few
    log('First 20 missing files:')
    for f, h in missing[:20]:
        safe = str(f).encode('ascii', 'replace').decode('ascii')
        log(f'  {safe}  [{h[:12]}]')

    if not args.upload:
        log('Dry-run: not uploading. Re-run with --upload to copy to P:\\Incoming.')
        log(f'Total missing: {len(missing)}')
        return

    # Upload missing files to P:\Incoming
    if not INCOMING.exists():
        log(f'ERROR: {INCOMING} not accessible')
        sys.exit(1)

    cap = args.max_uploads or len(missing)
    log(f'Copying up to {cap} files to {INCOMING} ...')
    copied = 0
    errors = 0
    for f, h in missing[:cap]:
        # Use sanitized name to avoid weird-filename SMB1 issues; PhotoGuard
        # will rename via EXIF date.
        dest = INCOMING / f.name
        suffix_n = 1
        while dest.exists():
            dest = INCOMING / f'{f.stem}_{suffix_n}{f.suffix}'
            suffix_n += 1
        try:
            shutil.copy2(str(f), str(dest))
            copied += 1
            if copied % 25 == 0:
                log(f'  copied {copied}/{cap}')
        except Exception as e:
            errors += 1
            safe = str(f).encode('ascii', 'replace').decode('ascii')
            log(f'  copy-error {safe}: {e}')
    log(f'DONE: copied={copied} errors={errors}')
    log('PhotoGuard will pick these up from P:\\Incoming and dedupe + organize them.')


if __name__ == '__main__':
    main()
