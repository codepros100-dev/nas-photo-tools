"""Detect faces in NAS photos using OpenCV's YuNet model.

Adds face_count column to the photos table. Resumable: skips photos
already processed. Downloads YuNet ONNX model (~340KB) once on first run.
"""
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
LOG_FILE = PHOTO_DB_DIR / "detect_faces.log"
MODEL_PATH = PHOTO_DB_DIR / "face_detection_yunet_2023mar.onnx"
MODEL_URL = 'https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx'

DETECT_LONG_EDGE = 640
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


def ensure_model():
    if MODEL_PATH.exists():
        return
    log(f'Downloading YuNet model -> {MODEL_PATH}')
    urllib.request.urlretrieve(MODEL_URL, str(MODEL_PATH))
    log('Downloaded')


def add_columns(conn):
    cols = {row[1] for row in conn.execute('PRAGMA table_info(photos)')}
    if 'face_count' not in cols:
        conn.execute('ALTER TABLE photos ADD COLUMN face_count INTEGER')
    if 'face_detected_at' not in cols:
        conn.execute('ALTER TABLE photos ADD COLUMN face_detected_at TEXT')
    if 'sharpness' not in cols:
        conn.execute('ALTER TABLE photos ADD COLUMN sharpness REAL')
    conn.commit()


def compute_sharpness(bgr):
    """Laplacian variance — higher = sharper. Typical range 5-3000+.
    Below 50 is generally blurry; 100-300 is OK; >500 is crisp."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def load_image_rgb(path):
    """Read via PIL (handles HEIC, weird formats), convert to BGR ndarray."""
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        img = img.convert('RGB')
        if max(img.size) > DETECT_LONG_EDGE:
            scale = DETECT_LONG_EDGE / max(img.size)
            new_size = (int(img.size[0] * scale), int(img.size[1] * scale))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
        arr = np.array(img)
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def detect_faces(detector, bgr):
    h, w = bgr.shape[:2]
    detector.setInputSize((w, h))
    _, faces = detector.detect(bgr)
    if faces is None:
        return 0
    return int(faces.shape[0])


def main():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log('=' * 60)
    log('Face detection starting')

    if not LIBRARY.exists():
        log('ERROR: P: drive not mapped')
        sys.exit(1)

    ensure_model()

    conn = sqlite3.connect(str(DB_PATH))
    add_columns(conn)

    rows = conn.execute('''
        SELECT id, nas_path FROM photos
        WHERE is_video = 0 AND error IS NULL
          AND (face_count IS NULL OR sharpness IS NULL)
    ''').fetchall()
    log(f'{len(rows)} photos pending face/sharpness detection')

    if not rows:
        log('Nothing to do')
        return

    detector = cv2.FaceDetectorYN.create(
        str(MODEL_PATH), '', (320, 320),
        score_threshold=SCORE_THRESHOLD, nms_threshold=0.3, top_k=500)

    start = time.time()
    processed = 0
    errors = 0
    for i, (pid, rel) in enumerate(rows, 1):
        path = LIBRARY / rel
        try:
            bgr = load_image_rgb(path)
            n_faces = detect_faces(detector, bgr)
            sharp = compute_sharpness(bgr)
            conn.execute(
                'UPDATE photos SET face_count = ?, sharpness = ?, face_detected_at = ? WHERE id = ?',
                (n_faces, sharp, datetime.now().isoformat(), pid))
            processed += 1
        except Exception as e:
            errors += 1
            conn.execute(
                'UPDATE photos SET face_count = -1, face_detected_at = ? WHERE id = ?',
                (datetime.now().isoformat(), pid))
            if errors <= 10:
                safe = str(path).encode('ascii', 'replace').decode('ascii')
                log(f'ERR {safe}: {str(e)[:120]}')

        if i % 100 == 0:
            conn.commit()
            elapsed = time.time() - start
            rate = i / elapsed if elapsed else 0
            eta_min = (len(rows) - i) / rate / 60 if rate else 0
            log(f'  [{i}/{len(rows)}] {rate:.1f}/s  ETA {eta_min:.0f}min  errors={errors}')

    conn.commit()
    n_with_faces = conn.execute(
        'SELECT COUNT(*) FROM photos WHERE face_count > 0').fetchone()[0]
    n_total = conn.execute(
        'SELECT COUNT(*) FROM photos WHERE face_count IS NOT NULL AND face_count >= 0').fetchone()[0]
    log(f'DONE: processed={processed} errors={errors}')
    log(f'  {n_with_faces}/{n_total} photos contain faces')
    conn.close()


if __name__ == '__main__':
    main()
