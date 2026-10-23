"""Fill each song's style, year and language through the app's admin API.

Usage:
    python fetch_metadata.py                 # only fills empty fields
    python fetch_metadata.py --overwrite     # recompute every field
    python fetch_metadata.py --dry-run       # print without writing

Options:
    --base-url URL   app to update (default http://127.0.0.1:8080)
    ADMIN_TOKEN      env var, sent as the Bearer token (not needed with DISABLE_AUTH=1)

Sources are in enrich.py (year and style: MusicBrainz; language: the lyrics).
Songs added in the admin panel are filled automatically; this is for catching up.
Review the result: fix any value from the admin panel ("Info").
"""
import argparse
import os

from enrich import detect_language, find_year_and_style, http_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    base, token = args.base_url.rstrip("/"), os.getenv("ADMIN_TOKEN")

    songs = http_json(base + "/admin/songs", token=token)
    todo = [s for s in songs if args.overwrite or not (s.get("genre") and s.get("year") and s.get("language"))]
    print(f"== {len(todo)} songs to fill")
    for song in todo:
        update = {}
        if args.overwrite or not song.get("language"):
            lang = detect_language(song.get("lyrics"))
            if lang:
                update["language"] = lang
        if args.overwrite or not (song.get("year") and song.get("genre")):
            try:
                year, style = find_year_and_style(song["artist"], song["title"])
                if year and (args.overwrite or not song.get("year")):
                    update["year"] = year
                if style and (args.overwrite or not song.get("genre")):
                    update["genre"] = style
            except Exception as exc:
                print(f"    (MusicBrainz error: {exc})")
        shown = " | ".join(str(update.get(k, "—")) for k in ("genre", "year", "language"))
        print(f"  {song['title'][:40]:40} {song['artist'][:24]:24} {shown}")
        if update and not args.dry_run:
            http_json(f"{base}/admin/songs/{song['id']}", "PATCH", update, token)


if __name__ == "__main__":
    main()
