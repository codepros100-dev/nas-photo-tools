"""Build a curated HTML photo album from photo_analysis.db.

Selects ~200 diverse, high-quality photos:
  * Greedy by quality score, capped per (year, month, geo-bucket)
  * Skips near-duplicates (perceptual hash Hamming < 8)
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

ImageFile.LOAD_TRUNCATED_IMAGES = True

from nas_config import PHOTO_LIBRARY_ROOT, PHOTO_DB_DIR

DB_PATH = PHOTO_DB_DIR / "photo_analysis.db"
LIBRARY = PHOTO_LIBRARY_ROOT
ALBUMS_ROOT = Path.home() / 'PhotoAlbums'
THUMB_SIZE = (520, 520)
LARGE_SIZE = (1920, 1920)
MAX_PHOTOS = 200
PER_BUCKET_CAP = 3
PHASH_THRESHOLD = 8


def log(msg):
    print(f'{datetime.now():%H:%M:%S} {msg}', flush=True)


def hamming(a, b):
    if not a or not b:
        return 64
    return bin(int(a, 16) ^ int(b, 16)).count('1')


def get_optional_columns(conn):
    cols = {row[1] for row in conn.execute('PRAGMA table_info(photos)')}
    return 'face_count' in cols, 'sharpness' in cols


def select(conn, target=MAX_PHOTOS):
    has_faces, has_sharp = get_optional_columns(conn)
    extra = []
    if has_faces:
        extra.append('face_count')
    else:
        extra.append('NULL AS face_count')
    if has_sharp:
        extra.append('sharpness')
    else:
        extra.append('NULL AS sharpness')
    extra_sql = ', ' + ', '.join(extra)
    cur = conn.execute(f'''
        SELECT nas_path, file_size, width, height, taken_at, taken_year, taken_month,
               gps_lat, gps_lon, camera_make, camera_model, phash, quality_score
               {extra_sql}
        FROM photos
        WHERE error IS NULL AND is_video = 0
          AND quality_score IS NOT NULL AND quality_score > 0
    ''')
    cands = [dict(zip([d[0] for d in cur.description], row)) for row in cur]
    log(f'{len(cands)} candidates  (faces:{has_faces} sharp:{has_sharp})')

    # Score = quality * face_bonus * sharpness_bonus
    def score(c):
        q = c['quality_score'] or 0
        # Face bonus
        if has_faces and c['face_count'] is not None and c['face_count'] >= 0:
            n = c['face_count']
            if n == 0:
                q *= 0.85
            elif n <= 4:
                q *= 1.5
            elif n <= 10:
                q *= 1.2
            else:
                q *= 1.0
        # Sharpness bonus (Laplacian variance)
        if has_sharp and c['sharpness'] is not None:
            s = c['sharpness']
            if s < 30:
                q *= 0.4   # very blurry — almost never pick
            elif s < 100:
                q *= 0.8
            elif s < 300:
                q *= 1.0
            elif s < 1000:
                q *= 1.1
            else:
                q *= 1.15
        return q

    cands.sort(key=score, reverse=True)

    # Group by year to enforce year coverage. Each year that has photos
    # gets a guaranteed slice; remaining slots fill greedily.
    by_year = {}
    for c in cands:
        by_year.setdefault(c['taken_year'] or 0, []).append(c)
    years = sorted(by_year.keys(), reverse=True)
    log(f'years available: {years}')

    # Per-year guaranteed minimum (proportional to share of total) and cap
    n_years = len(years)
    if n_years == 0:
        return []
    base_per_year = max(2, target // (n_years * 2))    # at least 2 per year
    cap_per_year = max(base_per_year * 4, target // 3)  # don't let one year dominate

    selected, phashes = [], []
    year_counts = {y: 0 for y in years}
    bucket_counts = {}

    def try_pick(c):
        if c['phash']:
            if any(hamming(c['phash'], p) < PHASH_THRESHOLD for p in phashes):
                return False
        lat_g = round(c['gps_lat'], 1) if c['gps_lat'] is not None else None
        lon_g = round(c['gps_lon'], 1) if c['gps_lon'] is not None else None
        bk = (c['taken_year'], c['taken_month'], lat_g, lon_g)
        if bucket_counts.get(bk, 0) >= PER_BUCKET_CAP:
            return False
        bucket_counts[bk] = bucket_counts.get(bk, 0) + 1
        if c['phash']:
            phashes.append(c['phash'])
        selected.append(c)
        year_counts[c['taken_year'] or 0] += 1
        return True

    # Pass 1: round-robin pick top photos from each year up to base_per_year
    for slot in range(base_per_year):
        for y in years:
            if len(selected) >= target:
                break
            if year_counts[y] > slot:
                continue
            for c in by_year[y]:
                if c in selected:
                    continue
                if try_pick(c):
                    break

    # Pass 2: greedy fill remaining slots from global ranking, capped per year
    for c in cands:
        if len(selected) >= target:
            break
        if c in selected:
            continue
        y = c['taken_year'] or 0
        if year_counts[y] >= cap_per_year:
            continue
        try_pick(c)

    log(f'selected {len(selected)} after diversity+dedupe (years: {sorted(set(year_counts) & set(c["taken_year"] or 0 for c in selected))})')
    return selected


def make_resized(src, dest, size, quality=85, retries=3):
    import time
    for attempt in range(retries):
        try:
            with Image.open(src) as img:
                img = ImageOps.exif_transpose(img)
                img = img.convert('RGB')
                img.thumbnail(size, Image.Resampling.LANCZOS)
                img.save(dest, 'JPEG', quality=quality, optimize=True, progressive=True)
            return True
        except OSError as e:
            if attempt < retries - 1:
                time.sleep(2 + attempt * 5)
                continue
            log(f'resize fail {src.name}: {e}')
            return False
        except Exception as e:
            log(f'resize fail {src.name}: {e}')
            return False
    return False


def make_thumb(src, dest, size=THUMB_SIZE):
    return make_resized(src, dest, size, quality=85)


def make_large(src, dest, size=LARGE_SIZE):
    return make_resized(src, dest, size, quality=88)


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
nav{display:flex;justify-content:center;gap:6px;padding:14px;background:rgba(19,19,26,0.92);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100;backdrop-filter:blur(10px);flex-wrap:wrap}
nav button{color:var(--muted);background:transparent;border:1px solid var(--border);padding:8px 18px;border-radius:24px;font-size:13px;cursor:pointer;font-weight:500;transition:all 0.15s}
nav button:hover{color:#fff;border-color:#444}
nav button.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.year-nav{display:flex;justify-content:center;gap:4px;padding:10px 14px;background:rgba(13,13,17,0.92);border-bottom:1px solid var(--border);position:sticky;top:60px;z-index:99;backdrop-filter:blur(10px);flex-wrap:wrap}
.year-nav a{color:var(--muted);text-decoration:none;padding:4px 10px;border-radius:12px;font-size:12px;transition:all 0.15s;font-variant-numeric:tabular-nums}
.year-nav a:hover{color:#fff;background:var(--bg2)}
.tile-badge{position:absolute;top:8px;right:8px;background:rgba(124,92,255,0.92);color:#fff;font-size:10px;font-weight:600;padding:2px 7px;border-radius:10px;letter-spacing:0.04em;backdrop-filter:blur(4px)}
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
  <button id="slideshow-btn">▶ Slideshow</button>
</nav>
__YEAR_NAV__
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
  const parts=[p.date,p.location,p.camera];
  if(p.faces!=null&&p.faces>0)parts.push(p.faces+' face'+(p.faces>1?'s':''));
  lbInfo.textContent=parts.filter(Boolean).join(' · ')||'Photo '+(i+1);
  lb.classList.add('open');
}
let slideshowTimer=null;
function toggleSlideshow(){
  const btn=document.getElementById('slideshow-btn');
  if(slideshowTimer){
    clearInterval(slideshowTimer);
    slideshowTimer=null;
    btn.textContent='▶ Slideshow';
    return;
  }
  if(!lb.classList.contains('open'))showLb(0);
  btn.textContent='⏸ Pause';
  slideshowTimer=setInterval(()=>showLb((lbIdx+1)%PHOTOS.length),3500);
}
document.getElementById('slideshow-btn').addEventListener('click',toggleSlideshow);
document.querySelectorAll('.tile').forEach(t=>{
  t.addEventListener('click',()=>showLb(parseInt(t.dataset.idx)));
});
lb.addEventListener('click',e=>{
  const a=e.target.dataset.act;
  if(a==='close'||e.target===lb){
    lb.classList.remove('open');
    if(slideshowTimer){clearInterval(slideshowTimer);slideshowTimer=null;document.getElementById('slideshow-btn').textContent='▶ Slideshow';}
  } else if(a==='prev')showLb((lbIdx-1+PHOTOS.length)%PHOTOS.length);
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


def build(name=None, target=MAX_PHOTOS):
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
    album_dir = ALBUMS_ROOT / name
    thumb_dir = album_dir / 'thumbs'
    large_dir = album_dir / 'large'
    thumb_dir.mkdir(parents=True, exist_ok=True)
    large_dir.mkdir(parents=True, exist_ok=True)
    log(f'Building album at {album_dir}')

    def safe_exists(p, retries=3):
        import time
        for attempt in range(retries):
            try:
                return p.exists()
            except OSError:
                if attempt < retries - 1:
                    time.sleep(1 + attempt * 2)
                else:
                    return False
        return False

    photos_data = []
    skipped = 0
    for i, p in enumerate(photos):
        src = LIBRARY / p['nas_path']
        name_jpg = f'{i:04d}.jpg'
        thumb_path = thumb_dir / name_jpg
        large_path = large_dir / name_jpg
        if not safe_exists(thumb_path):
            if not make_thumb(src, thumb_path):
                skipped += 1
                continue
        if not safe_exists(large_path):
            make_large(src, large_path)
            # If large failed, lightbox falls back to thumb path
        if (i + 1) % 25 == 0 or i == len(photos) - 1:
            log(f'  rendered {i + 1}/{len(photos)}')
        date_str = p['taken_at'][:10] if p['taken_at'] else (str(p['taken_year']) if p['taken_year'] else '')
        loc = ''
        if p['gps_lat'] is not None and p['gps_lon'] is not None:
            loc = f'{p["gps_lat"]:.3f}, {p["gps_lon"]:.3f}'
        camera = ' '.join(filter(None, [p.get('camera_make'), p.get('camera_model')])).strip()
        face_n = p.get('face_count')
        full_url = f'large/{name_jpg}' if large_path.exists() else f'thumbs/{name_jpg}'
        photos_data.append({
            'idx': len(photos_data),
            'thumb': f'thumbs/{name_jpg}',
            'full': full_url,
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
    sorted_years = sorted(by_year, reverse=True)
    for yr in sorted_years:
        label = str(yr) if yr else 'Undated'
        anchor = f'y{yr}' if yr else 'yundated'
        grid.append(f'<div class="year-group" id="{anchor}">')
        grid.append(f'<div class="year-label"><span class="yr">{label}</span><span>{len(by_year[yr])} photos</span></div>')
        grid.append('<div class="grid">')
        for p in by_year[yr]:
            badge = ''
            if p.get('faces') and 1 <= p['faces'] <= 6:
                # Show small badge for "people-centric" shots
                noun = 'face' if p['faces'] == 1 else 'faces'
                badge = f'<div class="tile-badge">{p["faces"]} {noun}</div>'
            grid.append(
                f'<div class="tile" data-idx="{p["idx"]}">'
                f'<img src="{p["thumb"]}" loading="lazy" alt="">'
                f'{badge}'
                f'<div class="tile-meta"><span>{p["date"]}</span><span>{p["camera"][:18]}</span></div>'
                f'</div>'
            )
        grid.append('</div></div>')

    if len(sorted_years) > 1:
        year_links = []
        for yr in sorted_years:
            label = str(yr) if yr else 'Undated'
            anchor = f'y{yr}' if yr else 'yundated'
            year_links.append(f'<a href="#{anchor}">{label}</a>')
        year_nav = '<div class="year-nav">' + ''.join(year_links) + '</div>'
    else:
        year_nav = ''

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
    html = html.replace('__YEAR_NAV__', year_nav)
    html = html.replace('__GRID__', '\n'.join(grid))
    html = html.replace('__PHOTOS_JSON__', json.dumps(photos_data, separators=(',', ':')))

    out = album_dir / 'index.html'
    out.write_text(html, encoding='utf-8')
    log(f'Wrote {out}')
    log(f'Open: file:///{str(out).replace(chr(92), "/")}')
    conn.close()
    return out


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', help='album folder name (default Auto-YYYY-MM-DD)')
    parser.add_argument('--target', type=int, default=MAX_PHOTOS, help='target photo count')
    args = parser.parse_args()
    build(name=args.name, target=args.target)
