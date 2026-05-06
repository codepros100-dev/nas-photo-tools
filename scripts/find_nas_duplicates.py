"""Walk the NAS Library and find files not tracked in nas_photo_hashes.json.
Each untracked file is hashed:
  - If hash is in the JSON: it's a duplicate; move it to Duplicates/.
  - If hash is NEW: add it to the JSON (a previously-untracked unique file).

Idempotent. Uses pysmb directly so it survives the NSA320's flaky SMB1.
"""
import argparse
import hashlib
import io
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from nas_config import PHOTO_LIBRARY_ROOT, PHOTO_DB_DIR, smb_connect, NAS_SHARE
from smb.SMBConnection import SMBConnection

# NAS credentials loaded from env via nas_config

LIBRARY_PREFIX = 'Library'
DUPLICATES_PREFIX = 'Duplicates'

HASH_DB = PHOTO_DB_DIR / 'nas_photo_hashes.json'
LOG_FILE = PHOTO_DB_DIR / 'find_nas_duplicates.log'

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




def list_recursive(conn, path):
    try:
        entries = conn.listPath(NAS_SHARE, path)
    except Exception as e:
        log(f'  list-fail {path}: {e}')
        return
    for e in entries:
        if e.filename in ('.', '..'):
            continue
        full = f'{path}/{e.filename}' if path else e.filename
        if e.isDirectory:
            yield from list_recursive(conn, full)
        else:
            yield full, e.file_size


def hash_remote(conn, remote_path):
    h = hashlib.sha256()
    buf = io.BytesIO()
    conn.retrieveFile(NAS_SHARE, remote_path, buf)
    buf.seek(0)
    while True:
        chunk = buf.read(1024 * 1024)
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()


def ensure_dir(conn, path):
    parts = [p for p in path.split('/') if p]
    cur = ''
    for part in parts:
        cur = f'{cur}/{part}' if cur else part
        try:
            conn.createDirectory(NAS_SHARE, cur)
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='detect only; do not move duplicates')
    args = parser.parse_args()

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log('=' * 60)
    log('NAS duplicate scan starting')

    db = json.loads(HASH_DB.read_text(encoding='utf-8'))
    canonical_paths = set(db.values())
    log(f'DB has {len(db)} hashes, {len(canonical_paths)} unique paths')

    conn = smb_connect()
    log('Connected; walking NAS Library...')

    all_paths = []
    for path, size in list_recursive(conn, LIBRARY_PREFIX):
        if Path(path).suffix.lower() not in ALL_EXTS:
            continue
        all_paths.append((path, size))
    log(f'Found {len(all_paths)} media files on NAS')

    untracked = [p for p in all_paths if p[0] not in canonical_paths]
    log(f'Untracked file paths (not in DB.values): {len(untracked)}')

    duplicates = []
    new_orphans = []
    errors = 0
    for i, (path, size) in enumerate(untracked, 1):
        try:
            h = hash_remote(conn, path)
            if h in db:
                duplicates.append((path, db[h]))
            else:
                new_orphans.append((path, h))
                db[h] = path
                canonical_paths.add(path)
        except Exception as e:
            errors += 1
            log(f'  hash-fail {path}: {e}')
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(5)
            try:
                conn = smb_connect()
                log('  reconnected')
            except Exception as e2:
                log(f'  FATAL reconnect failed: {e2}')
                break
        if i % 25 == 0:
            log(f'  hashed {i}/{len(untracked)} (dups={len(duplicates)} orphans={len(new_orphans)})')

    log(f'Duplicates found: {len(duplicates)}')
    log(f'New orphans tracked: {len(new_orphans)}')

    if duplicates and not args.dry_run:
        log(f'Moving {len(duplicates)} duplicates to {DUPLICATES_PREFIX}/...')
        ensure_dir(conn, DUPLICATES_PREFIX)
        moved = 0
        for path, canon in duplicates:
            base = Path(path).name
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            dst = f'{DUPLICATES_PREFIX}/dupe_{ts}_{base}'
            try:
                conn.rename(NAS_SHARE, path, dst)
                moved += 1
                log(f'  MOVED {path} -> {dst} (canonical: {canon})')
            except Exception as e:
                log(f'  rename-fail {path}: {e}')
        log(f'Moved {moved}/{len(duplicates)}')

    if new_orphans or duplicates:
        HASH_DB.write_text(json.dumps(db), encoding='utf-8')
        log(f'Updated hash DB to {len(db)} entries')

    try:
        conn.close()
    except Exception:
        pass

    log('=' * 60)
    log(f'DONE: nas_files={len(all_paths)} untracked={len(untracked)} '
        f'duplicates={len(duplicates)} new_orphans={len(new_orphans)} errors={errors}')


if __name__ == '__main__':
    main()
