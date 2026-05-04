# nas-photo-tools

A small toolkit for self-hosting your photo library on a home NAS — dedupe,
date-organize, analyze, and turn 19,000 photos into a polished web album.

Built and battle-tested on a vintage ZyXEL NSA320 over SMB1 with a 188 GB
photo collection. The pieces are independent, so you can use whichever ones
fit your setup.

## What's in here

| Tool | What it does |
|---|---|
| `analyze_photos.py` | Walks your library, extracts EXIF (date, GPS, camera), dimensions, perceptual hash, quality score → SQLite |
| `detect_faces.py` | Counts faces per photo using OpenCV YuNet (ONNX, ~340KB, CPU). Adds `face_count` column |
| `build_album.py` | Picks ~200 diverse, high-quality photos and renders a single-file HTML album with grid, lightbox, and clustered Leaflet map |
| `verify_hash_db.py` | Compares your SHA256 hash DB against the NAS — prunes dead entries, hashes orphaned files |
| `photo_guard.ps1` | Windows scheduled task that watches an Incoming folder, hashes new files, dedupes, files into `Library\YYYY\MM\` |

## Quick start

```bash
# 1. Install
git clone https://github.com/codepros100-dev/nas-photo-tools.git
cd nas-photo-tools
python -m pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# edit .env — at minimum, set PHOTO_LIBRARY_ROOT to where your photos live

# 3. Analyze (one-time, resumable)
python scripts/analyze_photos.py

# 4. Add face counts (optional, adds ~30min for 19k photos)
python scripts/detect_faces.py

# 5. Build the album
python scripts/build_album.py
# Opens at ~/PhotoAlbums/Auto-YYYY-MM-DD/index.html
```

## How the album curation works

The album is *opinionated*. From an arbitrary number of analyzed photos it
greedy-picks ~200 by descending score, where:

```
score = quality_score(width × height × bytes/pixel)
      × face_bonus(0.85 if no faces, 1.5 for 1-4 faces, 1.2 for 5-10, 1.0 for 11+)
```

Then enforces diversity: at most 3 photos per `(year, month, lat_bucket, lon_bucket)`
slot, and skips near-duplicates (perceptual hash Hamming distance < 8 bits).

The result skews toward **photos of people in distinct places and times**,
which lines up with what most people want when they say "best of". For pure
landscape/scenery libraries, lower the face bonus or set it to `1.0` flat.

## Architecture

```
            ┌──────────────┐
            │  Photo NAS   │
            └──────┬───────┘
                   │ SMB (locally mounted as P:)
                   ▼
   analyze_photos.py ─→ photo_analysis.db ◄── detect_faces.py
                              │
                              ▼
                       build_album.py ─→ HTML + thumbs

   photo_guard.ps1 (FileSystemWatcher) ─→ moves new files into Library
   verify_hash_db.py ─→ keeps SHA256 dedupe DB in sync
```

The SQLite is the single source of truth. Run any tool against any NAS;
they all read/write the same schema.

## Why this exists

Stock NAS bundled apps tend to be slow web UIs that don't survive firmware
EOL. These scripts live on your laptop, talk to the NAS over plain SMB, and
keep working long after the vendor has stopped shipping updates. The output
is static HTML — copyable, archivable, viewable in any browser, forever.

## License

MIT
