"""Compute face embeddings (SFace) for photos that have at least one face.

Reads photos with face_count > 0 from photo_analysis.db, runs YuNet to get
bounding boxes, runs SFace for a 128-D embedding per face, stores in the
`faces` table. Resumable.

Optional --limit N processes the top N (by quality_score) so you can iterate.
"""
import argparse
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

from nas_config import PHOTO_LIBRARY_ROOT, PHOTO_DB_DIR

LIBRARY = PHOTO_LIBRARY_ROOT
DB_PATH = PHOTO_DB_DIR / "photo_analysis.db"
LOG_FILE = PHOTO_DB_DIR / "recognize_faces.log"

YUNET_PATH = PHOTO_DB_DIR / "face_detection_yunet_2023mar.onnx"
SFACE_PATH = PHOTO_DB_DIR / "face_recognition_sface_2021dec.onnx"
SFACE_URL = 'https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx'

DETECT_LONG_EDGE = 800
SCORE_THRESHOLD = 0.6


def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'{ts} {msg}'
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode('ascii', 'replace').decode('ascii'), flush=True)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def ensure_models():
    if not YUNET_PATH.exists():
        log(f'YuNet model missing at {YUNET_PATH}')
        sys.exit(1)
    if not SFACE_PATH.exists():
        log(f'Downloading SFace -> {SFACE_PATH}')
        urllib.request.urlretrieve(SFACE_URL, str(SFACE_PATH))
        log('Downloaded')


def init_faces_table(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS faces (
            id INTEGER PRIMARY KEY,
            photo_id INTEGER NOT NULL,
            face_idx INTEGER NOT NULL,
            bbox_x INTEGER, bbox_y INTEGER,
            bbox_w INTEGER, bbox_h INTEGER,
            score REAL,
            embedding BLOB NOT NULL,
            cluster_id INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY (photo_id) REFERENCES photos(id)
        );
        CREATE INDEX IF NOT EXISTS idx_faces_photo ON faces(photo_id);
        CREATE INDEX IF NOT EXISTS idx_faces_cluster ON faces(cluster_id);
    ''')
    cols = {row[1] for row in conn.execute('PRAGMA table_info(photos)')}
    if 'recognized_at' not in cols:
        conn.execute('ALTER TABLE photos ADD COLUMN recognized_at TEXT')
    conn.commit()


def load_image_bgr(path):
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        img = img.convert('RGB')
        if max(img.size) > DETECT_LONG_EDGE:
            scale = DETECT_LONG_EDGE / max(img.size)
            new_size = (int(img.size[0] * scale), int(img.size[1] * scale))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
        arr = np.array(img)
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=None,
                        help='process only top N face-containing photos')
    args = parser.parse_args()

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log('=' * 60)
    log('Face recognition starting')

    ensure_models()

    conn = sqlite3.connect(str(DB_PATH))
    init_faces_table(conn)

    sql = '''
        SELECT id, nas_path FROM photos
        WHERE is_video = 0 AND error IS NULL
          AND face_count IS NOT NULL AND face_count > 0
          AND recognized_at IS NULL
        ORDER BY quality_score DESC
    '''
    if args.limit:
        sql += f' LIMIT {int(args.limit)}'
    rows = conn.execute(sql).fetchall()
    log(f'{len(rows)} photos pending recognition')
    if not rows:
        return

    detector = cv2.FaceDetectorYN.create(
        str(YUNET_PATH), '', (320, 320),
        score_threshold=SCORE_THRESHOLD, nms_threshold=0.3, top_k=500)
    recognizer = cv2.FaceRecognizerSF.create(str(SFACE_PATH), '')

    start = time.time()
    n_faces = errors = 0
    now = datetime.now().isoformat()
    for i, (pid, rel) in enumerate(rows, 1):
        path = LIBRARY / rel
        try:
            bgr = load_image_bgr(path)
            h, w = bgr.shape[:2]
            detector.setInputSize((w, h))
            _, faces = detector.detect(bgr)
            if faces is not None:
                for fi, f in enumerate(faces):
                    aligned = recognizer.alignCrop(bgr, f)
                    feat = recognizer.feature(aligned)  # (1, 128) float32
                    bbox = f[0:4].astype(int)
                    score = float(f[14]) if f.shape[0] > 14 else None
                    conn.execute(
                        'INSERT INTO faces (photo_id, face_idx, bbox_x, bbox_y, bbox_w, bbox_h, score, embedding, created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                        (pid, fi, int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]),
                         score, feat.tobytes(), now)
                    )
                    n_faces += 1
            conn.execute(
                'UPDATE photos SET recognized_at = ? WHERE id = ?',
                (datetime.now().isoformat(), pid))
        except Exception as e:
            errors += 1
            if errors <= 10:
                safe = str(path).encode('ascii', 'replace').decode('ascii')
                log(f'ERR {safe}: {str(e)[:120]}')

        if i % 100 == 0:
            conn.commit()
            elapsed = time.time() - start
            rate = i / elapsed if elapsed else 0
            eta_min = (len(rows) - i) / rate / 60 if rate else 0
            log(f'  [{i}/{len(rows)}] {rate:.1f}/s  faces={n_faces}  ETA {eta_min:.0f}min  errors={errors}')

    conn.commit()
    n_total_faces = conn.execute('SELECT COUNT(*) FROM faces').fetchone()[0]
    log(f'DONE: photos_processed={len(rows)} new_faces={n_faces} total_faces={n_total_faces} errors={errors}')
    conn.close()


if __name__ == '__main__':
    main()
