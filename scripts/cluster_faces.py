"""Cluster face embeddings into people.

Reads all faces from the `faces` table, normalizes embeddings, and runs
agglomerative clustering with cosine distance. Each resulting cluster_id
is one identified person. Updates faces.cluster_id in place.
"""
import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

from nas_config import PHOTO_DB_DIR

DB_PATH = PHOTO_DB_DIR / "photo_analysis.db"
LOG_FILE = PHOTO_DB_DIR / "cluster_faces.log"

# SFace recommended threshold: cosine similarity >= 0.363 = same person.
# Distance = 1 - cosine_sim; threshold = 1 - 0.363 = 0.637.
DEFAULT_DISTANCE = 0.55  # tighter than 0.637 — fewer mis-merges


def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'{ts} {msg}'
    print(line, flush=True)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def load_embeddings(conn):
    rows = conn.execute('SELECT id, embedding FROM faces').fetchall()
    if not rows:
        return [], np.zeros((0, 128), dtype=np.float32)
    ids = [r[0] for r in rows]
    embs = np.zeros((len(rows), 128), dtype=np.float32)
    for i, (_, blob) in enumerate(rows):
        embs[i] = np.frombuffer(blob, dtype=np.float32)
    # L2-normalize for cosine
    norms = np.linalg.norm(embs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    embs = embs / norms
    return ids, embs


def cluster_agglomerative(embs, distance):
    """Greedy complete-linkage clustering. A point joins an existing cluster
    only if its distance to *every* member is below the threshold (no chaining).
    Returns array of cluster ids (0..n_clusters-1)."""
    n = len(embs)
    if n == 0:
        return np.array([], dtype=int)

    # Cluster membership: list of arrays of indices
    clusters = []           # list of np.ndarray of int (member indices)
    labels = np.full(n, -1, dtype=int)

    for i in range(n):
        if i % 500 == 0 and i > 0:
            log(f'  clustering... {i}/{n}')
        if not clusters:
            clusters.append(np.array([i]))
            labels[i] = 0
            continue
        # cosine distance from point i to each existing cluster — find one
        # where MAX distance to any member is < threshold (complete linkage)
        sims_all = embs[i] @ embs.T  # (n,) but we'll mask
        best_cluster = -1
        best_max_dist = float('inf')
        for ci, members in enumerate(clusters):
            # Max distance to all members
            sims = sims_all[members]
            max_dist = float(1.0 - sims.min())
            if max_dist < distance and max_dist < best_max_dist:
                best_max_dist = max_dist
                best_cluster = ci
        if best_cluster >= 0:
            clusters[best_cluster] = np.append(clusters[best_cluster], i)
            labels[i] = best_cluster
        else:
            labels[i] = len(clusters)
            clusters.append(np.array([i]))

    return labels


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--distance', type=float, default=DEFAULT_DISTANCE,
                        help='cosine distance threshold (lower = stricter)')
    args = parser.parse_args()

    log('=' * 60)
    log(f'Clustering with distance={args.distance}')

    conn = sqlite3.connect(str(DB_PATH))
    ids, embs = load_embeddings(conn)
    log(f'Loaded {len(ids)} face embeddings')
    if not ids:
        log('No faces to cluster')
        return

    labels = cluster_agglomerative(embs, args.distance)
    n_clusters = int(labels.max()) + 1 if len(labels) else 0
    log(f'Formed {n_clusters} clusters')

    # Distribution
    sizes = defaultdict(int)
    for l in labels:
        sizes[int(l)] += 1
    top = sorted(sizes.items(), key=lambda x: -x[1])[:10]
    log('Top clusters by size:')
    for cid, n in top:
        log(f'  cluster {cid}: {n} faces')
    singletons = sum(1 for n in sizes.values() if n == 1)
    log(f'Singletons: {singletons}/{n_clusters}')

    # Update DB
    conn.executemany(
        'UPDATE faces SET cluster_id = ? WHERE id = ?',
        [(int(l), int(fid)) for l, fid in zip(labels, ids)]
    )
    conn.commit()
    conn.close()
    log('Done')


if __name__ == '__main__':
    main()
