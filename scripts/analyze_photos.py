"""Scan the photo library, extract EXIF + file metadata into SQLite.

Reads the library through a local mount (PHOTO_LIBRARY_ROOT). File list is
optionally seeded from a hash DB JSON, which avoids an expensive recursive
SMB scan if you already have one (e.g. from a deduplicated copy run).

Resumable: re-run anytime; skips files already in the SQLite. Each filesystem
op retries on transient OSError because consumer NAS appliances often drop
SMB1/SMB2 connections under load.
"""
import argparse
import json
import math
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageFile
from PIL.ExifTags import TAGS, GPSTAGS

from nas_config import PHOTO_LIBRARY_ROOT, PHOTO_DB_DIR

ImageFile.LOAD_TRUNCATED_IMAGES = True

DB_PATH = PHOTO_DB_DIR / 'photo_analysis.db'
LOG_FILE = PHOTO_DB_DIR / 'analyze_photos.log'

PHOTO_EXTS = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.gif', '.bmp',
              '.tiff', '.tif', '.webp'}
VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.m4v', '.3gp', '.wmv'}

RETRY_DELAYS = [2, 5, 10, 20, 30]


def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'{ts} {msg}'
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode('ascii', 'replace').decode('ascii'), flush=True)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY,
            nas_path TEXT UNIQUE NOT NULL,
            file_size INTEGER,
            width INTEGER,
            height INTEGER,
            taken_at TEXT,
            taken_year INTEGER,
            taken_month INTEGER,
            gps_lat REAL,
            gps_lon REAL,
            camera_make TEXT,
            camera_model TEXT,
            orientation INTEGER,
            phash TEXT,
            quality_score REAL,
            is_video INTEGER DEFAULT 0,
            error TEXT,
            processed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_taken ON photos(taken_year, taken_month);
        CREATE INDEX IF NOT EXISTS idx_gps ON photos(gps_lat, gps_lon);
        CREATE INDEX IF NOT EXISTS idx_quality ON photos(quality_score);
        CREATE INDEX IF NOT EXISTS idx_video ON photos(is_video);
    ''')
    conn.commit()
    return conn


def with_retry(fn, *args, **kwargs):
    last_err = None
    for delay in [0] + RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            return fn(*args, **kwargs)
        except OSError as e:
            last_err = e
            continue
    raise last_err


def gps_to_decimal(coord, ref):
    if not coord:
        return None
    try:
        d, m, s = (float(x) for x in coord)
        val = d + m / 60.0 + s / 3600.0
        if ref in ('S', 'W'):
            val = -val
        return val
    except Exception:
        return None


def extract_exif(path):
    info = {'taken_at': None, 'gps_lat': None, 'gps_lon': None,
            'camera_make': None, 'camera_model': None,
            'width': None, 'height': None, 'orientation': None}
    try:
        with Image.open(path) as img:
            info['width'], info['height'] = img.size
            exif = img._getexif() if hasattr(img, '_getexif') else None
            if not exif:
                return info
            tags = {TAGS.get(k, k): v for k, v in exif.items()}
            info['camera_make'] = (str(tags.get('Make') or '').strip() or None)
            info['camera_model'] = (str(tags.get('Model') or '').strip() or None)
            info['orientation'] = tags.get('Orientation')
            dt = tags.get('DateTimeOriginal') or tags.get('DateTime')
            if dt:
                try:
                    info['taken_at'] = datetime.strptime(
                        str(dt), '%Y:%m:%d %H:%M:%S').isoformat()
                except Exception:
                    pass
            gps = tags.get('GPSInfo')
            if gps and isinstance(gps, dict):
                gd = {GPSTAGS.get(k, k): v for k, v in gps.items()}
                info['gps_lat'] = gps_to_decimal(gd.get('GPSLatitude'),
                                                  gd.get('GPSLatitudeRef'))
                info['gps_lon'] = gps_to_decimal(gd.get('GPSLongitude'),
                                                  gd.get('GPSLongitudeRef'))
    except Exception as e:
        info['_error'] = str(e)[:200]
    return info


def compute_phash(path):
    try:
        with Image.open(path) as img:
            small = img.convert('L').resize((8, 8), Image.Resampling.LANCZOS)
            pixels = list(small.getdata())
            avg = sum(pixels) / 64
            bits = ''.join('1' if p >= avg else '0' for p in pixels)
            return f'{int(bits, 2):016x}'
    except Exception:
        return None


def quality_score(width, height, file_size):
    if not width or not height or not file_size:
        return 0.0
    pixels = width * height
    if pixels == 0:
        return 0.0
    bytes_per_pixel = file_size / pixels
    return math.log10(pixels) * 10 + min(bytes_per_pixel, 5.0) * 100


def process(path, is_video, library):
    nas_rel = str(path.relative_to(library)).replace('\\', '/')
    row = {'nas_path': nas_rel, 'is_video': int(is_video),
           'processed_at': datetime.now().isoformat()}
    try:
        row['file_size'] = with_retry(lambda: path.stat().st_size)
    except Exception as e:
        row['error'] = f'stat: {str(e)[:200]}'
        return row
    if is_video:
        try:
            d = datetime.fromtimestamp(path.stat().st_mtime)
            row['taken_at'] = d.isoformat()
            row['taken_year'] = d.year
            row['taken_month'] = d.month
        except Exception:
            pass
        return row
    info = extract_exif(path)
    for k in ('width', 'height', 'taken_at', 'gps_lat', 'gps_lon',
              'camera_make', 'camera_model', 'orientation'):
        row[k] = info.get(k)
    if row.get('taken_at'):
        try:
            d = datetime.fromisoformat(row['taken_at'])
            row['taken_year'] = d.year
            row['taken_month'] = d.month
        except Exception:
            pass
    if not row.get('taken_year'):
        try:
            d = datetime.fromtimestamp(path.stat().st_mtime)
            row['taken_year'] = d.year
            row['taken_month'] = d.month
        except Exception:
            pass
    row['phash'] = compute_phash(path)
    row['quality_score'] = quality_score(row.get('width'), row.get('height'),
                                          row.get('file_size'))
    if '_error' in info:
        row['error'] = info['_error']
    return row


def build_todo_from_hashdb(hash_db_path, library, seen):
    """Yield (full_path, is_video) for files in a hash DB JSON."""
    with open(hash_db_path, 'r', encoding='utf-8') as f:
        hash_map = json.load(f)
    log(f'Hash DB has {len(hash_map)} files')
    todo = []
    for rel_path in hash_map.values():
        if not rel_path.lower().startswith('library/'):
            rel_under_lib = rel_path
        else:
            rel_under_lib = rel_path[len('library/'):]
        if rel_under_lib in seen:
            continue
        ext = Path(rel_under_lib).suffix.lower()
        if ext not in PHOTO_EXTS and ext not in VIDEO_EXTS:
            continue
        full = library / rel_under_lib
        todo.append((full, ext in VIDEO_EXTS))
    return todo


def build_todo_from_walk(library, seen):
    todo = []
    for f in library.rglob('*'):
        try:
            if not f.is_file():
                continue
        except OSError:
            continue
        ext = f.suffix.lower()
        if ext not in PHOTO_EXTS and ext not in VIDEO_EXTS:
            continue
        rel = str(f.relative_to(library)).replace('\\', '/')
        if rel in seen:
            continue
        todo.append((f, ext in VIDEO_EXTS))
    return todo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, default=PHOTO_LIBRARY_ROOT,
                        help='photo library root (default from env)')
    parser.add_argument('--hash-db', type=Path,
                        help='optional JSON {hash: relpath} to seed file list, '
                             'avoiding a recursive walk')
    args = parser.parse_args()

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log('=' * 60)
    log(f'Photo analysis starting (library={args.library})')

    if not args.library.exists():
        log(f'ERROR: {args.library} not accessible')
        sys.exit(1)

    conn = init_db()
    seen = {row[0] for row in conn.execute('SELECT nas_path FROM photos')}
    log(f'DB has {len(seen)} processed entries')

    if args.hash_db and args.hash_db.exists():
        log(f'Reading file list from {args.hash_db}')
        todo = build_todo_from_hashdb(args.hash_db, args.library, seen)
    else:
        log('Walking library...')
        todo = build_todo_from_walk(args.library, seen)
    log(f'New files to process: {len(todo)}')

    cols = ('nas_path', 'file_size', 'width', 'height', 'taken_at',
            'taken_year', 'taken_month', 'gps_lat', 'gps_lon',
            'camera_make', 'camera_model', 'orientation', 'phash',
            'quality_score', 'is_video', 'error', 'processed_at')
    placeholders = ','.join('?' for _ in cols)
    insert_sql = f'INSERT OR REPLACE INTO photos ({",".join(cols)}) VALUES ({placeholders})'

    start = time.time()
    consecutive_errors = 0
    for i, (f, is_video) in enumerate(todo, 1):
        try:
            row = process(f, is_video, args.library)
            conn.execute(insert_sql, tuple(row.get(c) for c in cols))
            consecutive_errors = 0
        except Exception as e:
            safe = str(f).encode('ascii', 'replace').decode('ascii')
            log(f'ERROR on {safe}: {str(e)[:150]}')
            try:
                rel = str(f.relative_to(args.library)).replace('\\', '/')
                conn.execute(
                    'INSERT OR REPLACE INTO photos (nas_path, is_video, error, processed_at) VALUES (?, ?, ?, ?)',
                    (rel, int(is_video), str(e)[:200], datetime.now().isoformat())
                )
            except Exception:
                pass
            consecutive_errors += 1
            if consecutive_errors >= 10:
                log('Too many consecutive errors; sleeping 30s')
                conn.commit()
                time.sleep(30)
                consecutive_errors = 0
        if i % 100 == 0:
            conn.commit()
            elapsed = time.time() - start
            rate = i / elapsed if elapsed else 0
            eta_min = (len(todo) - i) / rate / 60 if rate else 0
            log(f'  [{i}/{len(todo)}] {rate:.1f} files/s  ETA {eta_min:.0f}min')
    conn.commit()

    n = conn.execute('SELECT COUNT(*) FROM photos').fetchone()[0]
    n_gps = conn.execute('SELECT COUNT(*) FROM photos WHERE gps_lat IS NOT NULL').fetchone()[0]
    n_video = conn.execute('SELECT COUNT(*) FROM photos WHERE is_video=1').fetchone()[0]
    n_err = conn.execute('SELECT COUNT(*) FROM photos WHERE error IS NOT NULL').fetchone()[0]
    log(f'TOTAL {n}: photos={n - n_video} videos={n_video} gps={n_gps} errors={n_err}')
    conn.close()
    log('Done')


if __name__ == '__main__':
    main()
