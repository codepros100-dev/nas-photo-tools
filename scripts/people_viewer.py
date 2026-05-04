"""Generate people.html — one face crop per identified person cluster.

For each cluster (>= MIN_CLUSTER_SIZE faces), pick the highest-quality face,
crop it from the source photo, and emit an HTML grid with include/exclude
checkboxes. Choices are saved to localStorage; a button downloads them as
`excluded_people.json`. Drop that file in NAS_DIR and re-run build_album.py.
"""
import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

from nas_config import PHOTO_LIBRARY_ROOT, PHOTO_DB_DIR

LIBRARY = PHOTO_LIBRARY_ROOT
DB_PATH = PHOTO_DB_DIR / "photo_analysis.db"
OUT_DIR = Path.home() / 'PhotoAlbums' / 'People'
THUMB_DIR = OUT_DIR / 'faces'
FACE_THUMB_SIZE = 220  # output px (square)
MIN_CLUSTER_SIZE = 2   # ignore singletons by default; stranger noise


def log(msg):
    print(f'{datetime.now():%H:%M:%S} {msg}', flush=True)


def fetch_clusters(conn):
    """Return dict cluster_id -> list of (face_id, photo_id, bbox, score, quality, taken_at)."""
    cur = conn.execute('''
        SELECT f.id, f.cluster_id, f.photo_id,
               f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h, f.score,
               p.quality_score, p.taken_at, p.nas_path,
               p.gps_lat, p.gps_lon, p.width, p.height
        FROM faces f
        JOIN photos p ON p.id = f.photo_id
        WHERE f.cluster_id IS NOT NULL
    ''')
    by_cluster = defaultdict(list)
    for row in cur:
        by_cluster[row[1]].append({
            'face_id': row[0],
            'photo_id': row[2],
            'bbox': (row[3], row[4], row[5], row[6]),
            'score': row[7] or 0,
            'quality': row[8] or 0,
            'taken_at': row[9],
            'nas_path': row[10],
            'gps_lat': row[11], 'gps_lon': row[12],
            'p_w': row[13], 'p_h': row[14],
        })
    return by_cluster


def crop_face(photo_path, bbox, p_w, p_h, out_path, margin=0.4):
    """Crop a square region around the bbox from the original photo.
    bbox values are in the DETECT_LONG_EDGE-scaled space, so we scale them
    back up to the original. Saves a square JPG of FACE_THUMB_SIZE."""
    try:
        with Image.open(photo_path) as img:
            img = ImageOps.exif_transpose(img)
            ow, oh = img.size
            x, y, w, h = bbox
            # Scale bbox from detection space (max edge ~= 800 set in recognize) to original
            # We don't know exact detection scale, but we know the source w/h. Re-derive.
            if p_w and p_h and (p_w != ow or p_h != oh):
                # Stored p_w/p_h is the original PIL size; bbox came from a downscaled detect.
                # Detection was done at max-edge=800. Scale bbox by ow/detected_w.
                pass
            # Pragmatic: assume bbox is in detection space where long edge = min(800, max(ow,oh)).
            detect_long = min(800, max(ow, oh))
            scale = max(ow, oh) / detect_long
            x, y, w, h = int(x * scale), int(y * scale), int(w * scale), int(h * scale)
            cx, cy = x + w // 2, y + h // 2
            side = int(max(w, h) * (1 + margin))
            left = max(0, cx - side // 2)
            top = max(0, cy - side // 2)
            right = min(ow, left + side)
            bottom = min(oh, top + side)
            face = img.crop((left, top, right, bottom)).convert('RGB')
            face = face.resize((FACE_THUMB_SIZE, FACE_THUMB_SIZE), Image.Resampling.LANCZOS)
            face.save(out_path, 'JPEG', quality=85, optimize=True)
        return True
    except Exception as e:
        log(f'crop fail {photo_path.name}: {e}')
        return False


HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>People in your library</title>
<style>
:root{--bg:#0a0a0c;--bg2:#13131a;--fg:#e8e8ec;--muted:#8b8b95;--accent:#7c5cff;--danger:#ff5577;--border:#23232b}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--fg);min-height:100vh}
header{padding:32px 24px 24px;text-align:center;background:radial-gradient(circle at 50% 0%,#2a1f4a 0%,var(--bg) 70%);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:50;backdrop-filter:blur(10px);background-color:rgba(10,10,12,0.92)}
header h1{font-size:24px;font-weight:700;letter-spacing:-0.02em}
.help{color:var(--muted);margin-top:6px;font-size:13px;max-width:680px;margin-left:auto;margin-right:auto;line-height:1.5}
.actions{display:flex;justify-content:center;gap:8px;margin-top:18px;flex-wrap:wrap}
.btn{padding:10px 18px;border-radius:6px;border:1px solid var(--border);background:var(--bg2);color:var(--fg);font-size:13px;cursor:pointer;font-weight:500;transition:all 0.15s}
.btn:hover{background:#22222a}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.danger{color:var(--danger);border-color:#552233}
.summary{padding:12px 16px;color:var(--muted);font-size:13px;text-align:center}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;padding:24px}
.person{position:relative;background:var(--bg2);border:2px solid var(--border);border-radius:12px;overflow:hidden;cursor:pointer;transition:all 0.15s}
.person:hover{border-color:#444}
.person.excluded{border-color:var(--danger);opacity:0.55}
.person.excluded::after{content:'EXCLUDED';position:absolute;top:8px;right:8px;background:var(--danger);color:#fff;font-size:10px;font-weight:700;padding:3px 8px;border-radius:8px;letter-spacing:0.05em}
.person img{width:100%;aspect-ratio:1;object-fit:cover;display:block}
.person .meta{padding:8px 10px;font-size:11px;color:var(--muted);display:flex;justify-content:space-between}
.person .meta b{color:var(--fg);font-weight:600}
</style>
</head>
<body>
<header>
  <h1>People in your library</h1>
  <p class="help">Click a person to <b>exclude</b> them from the album. Excluded people turn red.<br>
  When you're done, click <b>Save exclusions</b> — a JSON file downloads. Save it to <code>C:\Users\chaim\NAS\excluded_people.json</code> and re-run <code>build_album.py</code>.</p>
  <div class="actions">
    <button class="btn primary" id="save">💾 Save exclusions</button>
    <button class="btn" id="clear">Clear all</button>
    <button class="btn" id="all">Exclude all</button>
  </div>
  <div class="summary" id="summary"></div>
</header>
<div class="grid" id="grid">__GRID__</div>
<script>
const CLUSTERS = __CLUSTERS__;
const KEY = 'excluded_people_v1';
let excluded = new Set(JSON.parse(localStorage.getItem(KEY) || '[]'));
function render(){
  let n = 0;
  document.querySelectorAll('.person').forEach(el=>{
    const cid = parseInt(el.dataset.cluster);
    if (excluded.has(cid)) { el.classList.add('excluded'); n++; }
    else el.classList.remove('excluded');
  });
  document.getElementById('summary').textContent =
    `${CLUSTERS.length} people clustered  ·  ${n} excluded  ·  ${CLUSTERS.length - n} included`;
}
document.querySelectorAll('.person').forEach(el=>{
  el.addEventListener('click',()=>{
    const cid = parseInt(el.dataset.cluster);
    if (excluded.has(cid)) excluded.delete(cid); else excluded.add(cid);
    localStorage.setItem(KEY, JSON.stringify([...excluded]));
    render();
  });
});
document.getElementById('save').addEventListener('click',()=>{
  const data = {generated_at: new Date().toISOString(), excluded_clusters: [...excluded].sort((a,b)=>a-b)};
  const blob = new Blob([JSON.stringify(data, null, 2)], {type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'excluded_people.json';
  a.click();
});
document.getElementById('clear').addEventListener('click',()=>{
  excluded.clear(); localStorage.setItem(KEY,'[]'); render();
});
document.getElementById('all').addEventListener('click',()=>{
  excluded = new Set(CLUSTERS.map(c=>c.id));
  localStorage.setItem(KEY, JSON.stringify([...excluded]));
  render();
});
render();
</script>
</body>
</html>
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--min-cluster', type=int, default=MIN_CLUSTER_SIZE,
                        help='ignore clusters smaller than this (default 2)')
    parser.add_argument('--top', type=int, default=200,
                        help='show only top N clusters by size')
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    by_cluster = fetch_clusters(conn)
    log(f'Loaded {len(by_cluster)} clusters total')

    # Filter by size, sort descending
    clusters = [(cid, faces) for cid, faces in by_cluster.items()
                if len(faces) >= args.min_cluster]
    clusters.sort(key=lambda x: -len(x[1]))
    clusters = clusters[:args.top]
    log(f'Showing {len(clusters)} clusters (min size {args.min_cluster}, top {args.top})')

    cluster_data = []
    tile_html = []
    for cid, faces in clusters:
        # Pick the face with highest detection score × source quality
        best = max(faces, key=lambda f: (f.get('score') or 0) * (f.get('quality') or 1))
        face_jpg = THUMB_DIR / f'{cid:04d}.jpg'
        if not face_jpg.exists():
            crop_face(LIBRARY / best['nas_path'], best['bbox'],
                      best.get('p_w'), best.get('p_h'), face_jpg)

        n_photos = len({f['photo_id'] for f in faces})
        cluster_data.append({'id': cid, 'n': len(faces), 'n_photos': n_photos})
        tile_html.append(
            f'<div class="person" data-cluster="{cid}">'
            f'<img src="faces/{cid:04d}.jpg" alt="cluster {cid}" loading="lazy">'
            f'<div class="meta"><b>Person #{cid}</b><span>{len(faces)} faces · {n_photos} photos</span></div>'
            f'</div>'
        )

    out = OUT_DIR / 'index.html'
    html = HTML.replace('__GRID__', '\n'.join(tile_html))
    html = html.replace('__CLUSTERS__', json.dumps(cluster_data))
    out.write_text(html, encoding='utf-8')

    log(f'Wrote {out}')
    log(f'Open: {out.as_uri()}')


if __name__ == '__main__':
    main()
