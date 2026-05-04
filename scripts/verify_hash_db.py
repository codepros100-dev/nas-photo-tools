"""Verify hash DB integrity against the NAS.

For each entry in the SHA256 hash JSON:
  - Check the recorded remote path still exists on the NAS.
  - Print and prune entries pointing at files that no longer exist.

Then scan the NAS Library for files NOT in the DB (orphans) and re-hash
them so they are tracked. Keeps the dedupe DB accurate so the photo guard
never re-uploads something already there.
"""
import argparse
import hashlib
import io
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from nas_config import (
    NAS_SHARE, PHOTO_DB_DIR, smb_connect,
)

HASH_DB_DEFAULT = PHOTO_DB_DIR / 'nas_photo_hashes.json'
LOG_FILE = PHOTO_DB_DIR / 'verify_hash_db.log'

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


def remote_exists(conn, remote_path):
    try:
        conn.getAttributes(NAS_SHARE, remote_path)
        return True
    except Exception:
        return False


def list_recursive(conn, path):
    try:
        entries = conn.listPath(NAS_SHARE, path)
    except Exception as e:
        log(f'List failed on {path}: {e}')
        return
    for e in entries:
        if e.filename in ('.', '..'):
            continue
        full = f'{path}/{e.filename}' if path else e.filename
        if e.isDirectory:
            yield from list_recursive(conn, full)
        else:
            yield full, e.file_size


def hash_remote_file(conn, remote_path):
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hash-db', type=Path, default=HASH_DB_DEFAULT)
    parser.add_argument('--library-prefix', default='Library',
                        help='top-level dir under share to scan for orphans')
    args = parser.parse_args()

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log('=' * 60)
    log('Hash DB integrity verification')

    if not args.hash_db.exists():
        log(f'ERROR: {args.hash_db} not found')
        sys.exit(1)

    db = json.loads(args.hash_db.read_text(encoding='utf-8'))
    log(f'DB has {len(db)} entries')

    log('Connecting to NAS...')
    conn = smb_connect()
    log('Connected.')

    orphaned = []
    paths_in_db = set()
    for i, (h, remote) in enumerate(db.items(), 1):
        if i % 1000 == 0:
            log(f'Checked {i}/{len(db)}...')
        paths_in_db.add(remote)
        if not remote_exists(conn, remote):
            orphaned.append(h)
    log(f'Orphaned DB entries: {len(orphaned)}')
    for h in orphaned:
        del db[h]

    log(f'Scanning NAS {args.library_prefix} for untracked files...')
    untracked = []
    for remote, _size in list_recursive(conn, args.library_prefix):
        if Path(remote).suffix.lower() not in ALL_EXTS:
            continue
        if remote not in paths_in_db:
            untracked.append(remote)
    log(f'Untracked files: {len(untracked)}')

    added = 0
    for i, remote in enumerate(untracked, 1):
        try:
            h = hash_remote_file(conn, remote)
            if h not in db:
                db[h] = remote
                added += 1
            if i % 100 == 0:
                log(f'Hashed {i}/{len(untracked)} (added {added})')
        except Exception as e:
            log(f'Hash error on {remote}: {e}')
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(5)
            try:
                conn = smb_connect()
                log('Reconnected')
            except Exception as e2:
                log(f'Reconnect failed: {e2}')
                break

    args.hash_db.write_text(json.dumps(db), encoding='utf-8')
    try:
        conn.close()
    except Exception:
        pass

    log('=' * 60)
    log(f'VERIFY DONE: pruned={len(orphaned)} added={added} db_size={len(db)}')


if __name__ == '__main__':
    main()
