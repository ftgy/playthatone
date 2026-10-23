"""Import the old PTO catalog (legacy/) into the PlayThatOne database.

Usage:
    python import_legacy.py                     # enabled songs only
    python import_legacy.py --include-disabled  # all songs
    python import_legacy.py --dry-run           # print without writing

Uses DATABASE_URL like the app does (local SQLite by default).
Idempotent: a song already present (same title + artist) is skipped.
"""
import argparse
import csv
import json
from pathlib import Path

from database import engine, SessionLocal, Base
from models import Song, Vote

LEGACY      = Path(__file__).parent / "legacy"
CATALOG     = LEGACY / "frontend/public/catalog/catalog.json"
CATALOG_CSV = LEGACY / "backend/core/catalog_postgres.csv"
SONGS_DIR   = LEGACY / "songs"

# Text the old script generated when nothing was found
PLACEHOLDERS = ("Letra no encontrada", "Letra simulada", "Acordes aún no", "Acordes simulados")

# Demo songs created on the app's first start
DEMO_SONGS = {("Johnny B. Goode", "Chuck Berry"), ("Whole Lotta Love", "Led Zeppelin")}


def read_text(path: Path, title: str, artist: str):
    """Return the cleaned-up content, or None if missing / placeholder."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text or text.startswith(PLACEHOLDERS):
        return None
    # Strip the title / artist header (the app already shows them)
    lines = text.splitlines()
    header = {title.lower(), artist.lower()}
    while lines and (not lines[0].strip() or lines[0].strip().lower() in header):
        lines.pop(0)
    return "\n".join(line.rstrip() for line in lines) or None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-disabled", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    with CATALOG_CSV.open(encoding="utf-8") as f:
        enabled = {r["id"]: r["enabled"] == "Y" for r in csv.DictReader(f, delimiter=";")}

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        # Remove the demo songs if nobody voted for them
        for song in db.query(Song).all():
            if (song.title, song.artist) in DEMO_SONGS and not db.query(Vote).filter(Vote.song_id == song.id).count():
                print(f"- demo removed: {song.title}")
                db.delete(song)
        db.flush()

        existing = {(s.title.lower(), s.artist.lower()) for s in db.query(Song).all()}
        position = (max((s.position or 0 for s in db.query(Song).all()), default=-1)) + 1
        added = skipped = 0

        for entry in catalog:
            sid, title, artist = entry["id"], entry["title"], entry["artist"]
            if not args.include_disabled and not enabled.get(sid, True):
                print(f"  disabled   : {title}")
                skipped += 1
                continue
            if (title.lower(), artist.lower()) in existing:
                print(f"  exists     : {title}")
                skipped += 1
                continue

            lyrics = read_text(SONGS_DIR / "lyrics" / f"{sid}.txt", title, artist)
            chords = read_text(SONGS_DIR / "tabs" / f"TAB{sid}.txt", title, artist)
            db.add(Song(title=title, artist=artist, lyrics=lyrics, chords=chords, position=position))
            position += 1
            added += 1
            flags = ("lyrics" if lyrics else "") + (" + chords" if chords else "")
            print(f"+ {title} — {artist}  [{flags.strip(' +') or 'no content'}]")

        if args.dry_run:
            db.rollback()
            print(f"\n(dry-run) {added} to add, {skipped} skipped")
        else:
            db.commit()
            print(f"\n{added} added, {skipped} skipped")
    finally:
        db.close()


if __name__ == "__main__":
    main()
