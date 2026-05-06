"""Process every file in P:\\Incoming and move it into the Library.

For each file:
  - Hash it
  - If hash is already in nas_photo_hashes.json -> move to Duplicates/
  - Otherwise:
      - Read EXIF date (or file mtime) to determine YYYY/MM
      - Move to Library/YYYY/MM/Photos or .../Videos
      - Add hash -> path to the JSON

Sanitizes non-ASCII filenames before move. Runs locally over SMB; resilient
to dropped connections.
"""
import argparse
import hashlib
import json
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from nas_config import PHOTO_LIBRARY_ROOT, PHOTO_DB_DIR, smb_connect, NAS_SHARE

INCOMING = PHOTO_LIBRARY_ROOT.parent / 'Incoming'
LIBRARY = PHOTO_LIBRARY_ROOT
DUPLICATES = PHOTO_LIBRARY_ROOT.parent / 'Duplicates'
HASH_DB = PHOTO_DB_DIR / 'nas_photo_hashes.json'
LOG_FILE = PHOTO_DB_DIR / 'process_incoming.log'

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


def safe_op(fn, *args, retries=4, delay=3, **kwargs):
    last = None
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except OSError as e:
            last = e
            if attempt < retries:
                time.sleep(delay * (2 ** attempt))
    raise last


def file_hash(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def get_taken_date(path):
    try:
        from PIL import Image, ImageFile
        from PIL.ExifTags import TAGS
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        with Image.open(path) as img:
            exif = img._getexif() if hasattr(img, '_getexif') else None
            if exif:
                tags = {TAGS.get(k, k): v for k, v in exif.items()}
                dt = tags.get('DateTimeOriginal') or tags.get('DateTime')
                if dt:
                    return datetime.strptime(str(dt), '%Y:%m:%d %H:%M:%S')
    except Exception:
        pass
    return datetime.fromtimestamp(path.stat().st_mtime)


def sanitize_name(name):
    p = Path(name)
    stem = re.sub(r'[^\x20-\x7e]', '_', p.stem)
    stem = re.sub(r'_+', '_', stem).strip('_')
    if not stem:
        stem = 'renamed'
    return f'{stem}{p.suffix}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=None,
                        help='cap files processed this run')
    args = parser.parse_args()

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log('=' * 60)
    log('Process incoming starting')

    if not INCOMING.exists():
        log(f'ERROR: {INCOMING} not accessible')
        sys.exit(1)

    db = json.loads(HASH_DB.read_text(encoding='utf-8'))
    log(f'Hash DB: {len(db)} entries')

    files = []
    for f in INCOMING.rglob('*'):
        try:
            if f.is_file() and f.suffix.lower() in ALL_EXTS:
                files.append(f)
        except OSError:
            continue
    log(f'{len(files)} files in Incoming')
    if args.limit:
        files = files[:args.limit]
        log(f'Limited to {len(files)}')

    DUPLICATES.mkdir(exist_ok=True)

    filed = duplicates = errors = 0
    start = time.time()
    for i, f in enumerate(files, 1):
        try:
            h = safe_op(file_hash, f)
            if h in db:
                # Duplicate
                ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                dst = DUPLICATES / f'dupe_{ts}_{sanitize_name(f.name)}'
                cnt = 1
                while dst.exists():
                    dst = DUPLICATES / f'dupe_{ts}_{cnt}_{sanitize_name(f.name)}'
                    cnt += 1
                safe_op(f.rename, dst)
                duplicates += 1
            else:
                d = get_taken_date(f)
                kind = 'Videos' if f.suffix.lower() in VIDEO_EXTS else 'Photos'
                year_dir = LIBRARY / f'{d.year:04d}' / f'{d.month:02d}' / kind
                safe_op(year_dir.mkdir, parents=True, exist_ok=True)
                clean_name = sanitize_name(f.name)
                dst = year_dir / clean_name
                cnt = 1
                while dst.exists():
                    p = Path(clean_name)
                    dst = year_dir / f'{p.stem}_{cnt}{p.suffix}'
                    cnt += 1
                safe_op(f.rename, dst)
                rel = str(dst.relative_to(LIBRARY.parent)).replace('\\', '/')
                db[h] = rel
                filed += 1
        except Exception as e:
            errors += 1
            safe = str(f).encode('ascii', 'replace').decode('ascii')
            log(f'ERR {safe}: {str(e)[:120]}')

        if i % 50 == 0:
            elapsed = time.time() - start
            rate = i / elapsed if elapsed else 0
            eta_min = (len(files) - i) / rate / 60 if rate else 0
            log(f'  [{i}/{len(files)}] {rate:.1f}/s  filed={filed} dup={duplicates} err={errors}  ETA {eta_min:.0f}min')
            HASH_DB.write_text(json.dumps(db), encoding='utf-8')

    HASH_DB.write_text(json.dumps(db), encoding='utf-8')
    log(f'DONE: filed={filed} duplicates={duplicates} errors={errors}')
    log(f'Hash DB now {len(db)} entries')


if __name__ == '__main__':
    main()
