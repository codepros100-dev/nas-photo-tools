"""Build a curated HTML photo album from photo_analysis.db.

Selects ~200 diverse, high-quality photos:
  * Boost photos with 1-4 faces (people-centric); slightly demote faceless ones
  * Cap per (year, month, geo-bucket) for diversity
  * Skip near-duplicates (perceptual hash Hamming < 8)

Renders a single-file HTML album with grid view, year grouping, lightbox,
and an interactive Leaflet map keyed off GPS data.
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageFile, ImageOps

from nas_config import PHOTO_LIBRARY_ROOT, PHOTO_DB_DIR

ImageFile.LOAD_TRUNCATED_IMAGES = True

DB_PATH = PHOTO_DB_DIR / 'photo_analysis.db'
DEFAULT_ALBUM_ROOT = Path.home() / 'PhotoAlbums'
THUMB_SIZE = (520, 520)
MAX_PHOTOS = 200
PER_BUCKET_CAP = 3
PHASH_THRESHOLD = 8


def log(msg):
    print(f'{datetime.now():%H:%M:%S} {msg}', flush=True)


def hamming(a, b):
    if not a or not b:
        return 64
    return bin(int(a, 16) ^ int(b, 16)).count('1')


def has_face_column(conn):
    cols = {row[1] for row in conn.execute('PRAGMA table_info(photos)')}
    return 'face_count' in cols


def select(conn, target=MAX_PHOTOS):
    has_faces = has_face_column(conn)
    face_select = ', face_count' if has_faces else ', NULL AS face_count'
    cur = conn.execute(f'''
        SELECT nas_path, file_size, width, height, taken_at, taken_year, taken_month,
               gps_lat, gps_lon, camera_make, camera_model, phash, quality_score
               {face_select}
        FROM photos
        WHERE error IS NULL AND is_video = 0
          AND quality_score IS NOT NULL AND quality_score > 0
    ''')
    cands = [dict(zip([d[0] for d in cur.description], row)) for row in cur]
    log(f'{len(cands)} photo candidates  (faces enriched: {has_faces})')

    def score(c):
        q = c['quality_score'] or 0
        if not has_faces or c['face_count'] is None or c['face_count'] < 0:
            return q
        n = c['face_count']
        if n == 0:
            return q * 0.85
        if n <= 4:
            return q * 1.5
        if n <= 10:
            return q * 1.2
        return q * 1.0

    cands.sort(key=score, reverse=True)

    selected, phashes = [], []
    bucket_counts = {}
    for c in cands:
        if len(selected) >= target:
            break
        lat_g = round(c['gps_lat'], 1) if c['gps_lat'] is not None else None
        lon_g = round(c['gps_lon'], 1) if c['gps_lon'] is not None else None
        bk = (c['taken_year'], c['taken_month'], lat_g, lon_g)
        if bucket_counts.get(bk, 0) >= PER_BUCKET_CAP:
            continue
        if c['phash']:
            if any(hamming(c['phash'], p) < PHASH_THRESHOLD for p in phashes):
                continue
            phashes.append(c['phash'])
        bucket_counts[bk] = bucket_counts.get(bk, 0) + 1
        selected.append(c)

    log(f'selected {len(selected)} after diversity+dedupe')
    return selected


def make_thumb(src, dest, size=THUMB_SIZE):
    try:
        with Image.open(src) as img:
            img = ImageOps.exif_transpose(img)
            img = img.convert('RGB')
            img.thumbnail(size, Image.Resampling.LANCZOS)
            img.save(dest, 'JPEG', quality=85, optimize=True, progressive=True)
        return True
    except Exception as e:
        log(f'thumb fail {src.name}: {e}')
        return False


HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css">
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css">
<style>
:root{--bg:#0a0a0c;--bg2:#13131a;--fg:#e8e8ec;--muted:#8b8b95;--accent:#7c5cff;--border:#23232b}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--fg);min-height:100vh}
header{padding:48px 24px 32px;text-align:center;background:radial-gradient(circle at 50% 0%,#2a1f4a 0%,var(--bg) 70%);border-bottom:1px solid var(--border)}
header h1{font-size:34px;font-weight:700;letter-spacing:-0.02em;background:linear-gradient(120deg,#fff,#a89bff);-webkit-background-clip:text;background-clip:text;color:transparent}
.subtitle{color:var(--muted);margin-top:8px;font-size:14px}
.stats{display:flex;justify-content:center;gap:48px;padding:24px 16px 0;flex-wrap:wrap}
.stats div{display:flex;flex-direction:column;align-items:center;gap:4px}
.stats strong{font-size:26px;font-weight:700;color:#fff}
.stats span{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.08em}
nav{display:flex;justify-content:center;gap:6px;padding:14px;background:rgba(19,19,26,0.92);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100;backdrop-filter:blur(10px)}
nav button{color:var(--muted);background:transparent;border:1px solid var(--border);padding:8px 18px;border-radius:24px;font-size:13px;cursor:pointer;font-weight:500;transition:all 0.15s}
nav button:hover{color:#fff;border-color:#444}
nav button.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.section{display:none;animation:fade 0.3s}
.section.active{display:block}
@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.year-group{margin:8px 0 32px}
.year-label{padding:24px 32px 12px;font-size:13px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:0.12em;display:flex;align-items:baseline;gap:12px}
.year-label .yr{color:#fff;font-size:22px;letter-spacing:-0.01em;text-transform:none}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:6px;padding:0 24px}
.tile{position:relative;aspect-ratio:1;overflow:hidden;border-radius:8px;cursor:pointer;background:var(--bg2);transition:transform 0.2s}
.tile:hover{transform:translateY(-2px)}
.tile img{width:100%;height:100%;object-fit:cover;transition:transform 0.4s}
.tile:hover img{transform:scale(1.06)}
.tile-meta{position:absolute;left:0;right:0;bottom:0;padding:14px 12px 10px;font-size:11px;color:#fff;background:linear-gradient(transparent,rgba(0,0,0,0.85));opacity:0;transition:opacity 0.2s;display:flex;justify-content:space-between}
.tile:hover .tile-meta{opacity:1}
#map{height:calc(100vh - 100px);width:100%}
.lightbox{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.96);z-index:1000;align-items:center;justify-content:center}
.lightbox.open{display:flex}
.lightbox img{max-width:100%;max-height:92vh;object-fit:contain}
.lightbox-info{position:absolute;bottom:24px;left:50%;transform:translateX(-50%);background:rgba(20,20,24,0.85);padding:10px 22px;border-radius:30px;font-size:13px;color:#ddd;backdrop-filter:blur(10px);border:1px solid var(--border)}
.close-btn,.nav-btn{position:absolute;color:#fff;font-size:32px;cursor:pointer;user-select:none;opacity:0.6;transition:opacity 0.15s;padding:16px 24px}
.close-btn{top:8px;right:16px}
.nav-btn{top:50%;transform:translateY(-50%);font-size:48px}
.nav-btn:hover,.close-btn:hover{opacity:1}
.nav-prev{left:8px}
.nav-next{right:8px}
@media(max-width:600px){.grid{grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}header h1{font-size:24px}.stats{gap:24px}.stats strong{font-size:20px}}
.empty{padding:80px;text-align:center;color:var(--muted)}
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <div class="subtitle">__SUBTITLE__</div>
  <div class="stats">__STATS__</div>
</header>
<nav>
  <button data-section="grid" class="active">Grid</button>
  <button data-section="map">Map</button>
</nav>
<main>
  <div id="grid" class="section active">__GRID__</div>
  <div id="map-section" class="section"><div id="map"></div></div>
</main>
<div class="lightbox" id="lightbox">
  <span class="close-btn" data-act="close">&times;</span>
  <span class="nav-btn nav-prev" data-act="prev">&#8249;</span>
  <span class="nav-btn nav-next" data-act="next">&#8250;</span>
  <img id="lightbox-img" alt="">
  <div class="lightbox-info" id="lightbox-info"></div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
<script>
const PHOTOS=__PHOTOS_JSON__;
let lbIdx=0;
const lb=document.getElementById('lightbox');
const lbImg=document.getElementById('lightbox-img');
const lbInfo=document.getElementById('lightbox-info');
function showLb(i){
  if(i<0||i>=PHOTOS.length)return;
  lbIdx=i;
  const p=PHOTOS[i];
  lbImg.src=p.full;
  lbInfo.textContent=[p.date,p.location,p.camera].filter(Boolean).join(' · ')||'Photo '+(i+1);
  lb.classList.add('open');
}
document.querySelectorAll('.tile').forEach(t=>{
  t.addEventListener('click',()=>showLb(parseInt(t.dataset.idx)));
});
lb.addEventListener('click',e=>{
  const a=e.target.dataset.act;
  if(a==='close'||e.target===lb)lb.classList.remove('open');
  else if(a==='prev')showLb((lbIdx-1+PHOTOS.length)%PHOTOS.length);
  else if(a==='next')showLb((lbIdx+1)%PHOTOS.length);
});
document.addEventListener('keydown',e=>{
  if(!lb.classList.contains('open'))return;
  if(e.key==='Escape')lb.classList.remove('open');
  else if(e.key==='ArrowLeft')showLb((lbIdx-1+PHOTOS.length)%PHOTOS.length);
  else if(e.key==='ArrowRight')showLb((lbIdx+1)%PHOTOS.length);
});
document.querySelectorAll('nav button').forEach(b=>{
  b.addEventListener('click',()=>{
    document.querySelectorAll('nav button').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');
    document.querySelectorAll('.section').forEach(s=>s.classList.remove('active'));
    const t=b.dataset.section==='map'?'map-section':b.dataset.section;
    document.getElementById(t).classList.add('active');
    if(b.dataset.section==='map'&&!window._map)initMap();
  });
});
function initMap(){
  const geo=PHOTOS.filter(p=>p.lat!=null&&p.lon!=null);
  if(!geo.length){
    document.getElementById('map').innerHTML='<div class="empty">No photos with GPS data.<br><small>Tip: phone photos usually have GPS; older or scanned photos do not.</small></div>';
    return;
  }
  const map=L.map('map',{worldCopyJump:true}).setView([geo[0].lat,geo[0].lon],3);
  window._map=map;
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{
    attribution:'&copy; OpenStreetMap &copy; CARTO',maxZoom:19
  }).addTo(map);
  const cluster=L.markerClusterGroup({chunkedLoading:true,maxClusterRadius:50});
  geo.forEach(p=>{
    const m=L.marker([p.lat,p.lon]);
    m.bindPopup('<a href="#" onclick="showLb('+p.idx+');return false"><img src="'+p.thumb+'" style="width:220px;border-radius:6px"></a><br><b>'+(p.date||'')+'</b>');
    cluster.addLayer(m);
  });
  map.addLayer(cluster);
  if(geo.length>1)map.fitBounds(cluster.getBounds().pad(0.1));
}
window.showLb=showLb;
</script>
</body>
</html>
'''


def build(name=None, target=MAX_PHOTOS, library=PHOTO_LIBRARY_ROOT,
          albums_root=DEFAULT_ALBUM_ROOT):
    if not DB_PATH.exists():
        log(f'No DB at {DB_PATH}; run analyze_photos.py first')
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH))
    photos = select(conn, target=target)
    if not photos:
        log('No photos selected; is the DB populated?')
        return None

    if not name:
        name = f'Auto-{datetime.now():%Y-%m-%d}'
    album_dir = albums_root / name
    thumb_dir = album_dir / 'thumbs'
    thumb_dir.mkdir(parents=True, exist_ok=True)
    log(f'Building album at {album_dir}')

    photos_data = []
    skipped = 0
    for i, p in enumerate(photos):
        src = library / p['nas_path']
        thumb_name = f'{i:04d}.jpg'
        thumb_path = thumb_dir / thumb_name
        if not thumb_path.exists() and not make_thumb(src, thumb_path):
            skipped += 1
            continue
        if (i + 1) % 25 == 0 or i == len(photos) - 1:
            log(f'  thumbs {i + 1}/{len(photos)}')
        date_str = p['taken_at'][:10] if p['taken_at'] else (str(p['taken_year']) if p['taken_year'] else '')
        loc = ''
        if p['gps_lat'] is not None and p['gps_lon'] is not None:
            loc = f'{p["gps_lat"]:.3f}, {p["gps_lon"]:.3f}'
        camera = ' '.join(filter(None, [p.get('camera_make'), p.get('camera_model')])).strip()
        face_n = p.get('face_count')
        photos_data.append({
            'idx': len(photos_data),
            'thumb': f'thumbs/{thumb_name}',
            'full': src.as_uri(),
            'date': date_str,
            'year': p['taken_year'],
            'month': p['taken_month'],
            'lat': p['gps_lat'],
            'lon': p['gps_lon'],
            'camera': camera,
            'location': loc,
            'faces': face_n if (face_n is not None and face_n >= 0) else None,
        })

    if skipped:
        log(f'skipped {skipped} (thumb failed)')

    by_year = {}
    for p in photos_data:
        by_year.setdefault(p['year'] or 0, []).append(p)
    grid = []
    for yr in sorted(by_year, reverse=True):
        label = str(yr) if yr else 'Undated'
        grid.append('<div class="year-group">')
        grid.append(f'<div class="year-label"><span class="yr">{label}</span><span>{len(by_year[yr])} photos</span></div>')
        grid.append('<div class="grid">')
        for p in by_year[yr]:
            grid.append(
                f'<div class="tile" data-idx="{p["idx"]}">'
                f'<img src="{p["thumb"]}" loading="lazy" alt="">'
                f'<div class="tile-meta"><span>{p["date"]}</span><span>{p["camera"][:18]}</span></div>'
                f'</div>'
            )
        grid.append('</div></div>')

    n_total = conn.execute('SELECT COUNT(*) FROM photos WHERE is_video=0').fetchone()[0]
    n_total_all = conn.execute('SELECT COUNT(*) FROM photos').fetchone()[0]
    n_videos = n_total_all - n_total
    n_gps = sum(1 for p in photos_data if p['lat'] is not None)
    years = sorted({p['year'] for p in photos_data if p['year']})
    span = f'{years[0]}–{years[-1]}' if len(years) > 1 else (str(years[0]) if years else '–')

    stats = (
        f'<div><strong>{len(photos_data)}</strong><span>selected</span></div>'
        f'<div><strong>{n_total:,}</strong><span>photos analyzed</span></div>'
        f'<div><strong>{n_videos:,}</strong><span>videos</span></div>'
        f'<div><strong>{n_gps}</strong><span>geotagged</span></div>'
        f'<div><strong>{span}</strong><span>spanning</span></div>'
    )

    html = HTML
    html = html.replace('__TITLE__', f'Photo Album · {name}')
    html = html.replace('__SUBTITLE__', f'Curated from {n_total:,} photos · generated {datetime.now():%B %d, %Y}')
    html = html.replace('__STATS__', stats)
    html = html.replace('__GRID__', '\n'.join(grid))
    html = html.replace('__PHOTOS_JSON__', json.dumps(photos_data, separators=(',', ':')))

    out = album_dir / 'index.html'
    out.write_text(html, encoding='utf-8')
    log(f'Wrote {out}')
    log(f'Open: {out.as_uri()}')
    conn.close()
    return out


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', help='album folder name (default Auto-YYYY-MM-DD)')
    parser.add_argument('--target', type=int, default=MAX_PHOTOS, help='target photo count')
    parser.add_argument('--library', type=Path, default=PHOTO_LIBRARY_ROOT)
    parser.add_argument('--albums-root', type=Path, default=DEFAULT_ALBUM_ROOT)
    args = parser.parse_args()
    build(name=args.name, target=args.target, library=args.library,
          albums_root=args.albums_root)
