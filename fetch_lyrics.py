"""Fetch lyrics and chords from the internet and add them through the app's admin API.

Usage:
    python fetch_lyrics.py --fill-missing                  # songs in the catalog without lyrics
    python fetch_lyrics.py --add-file songs.txt            # new songs, one "Artist - Title" per line
    python fetch_lyrics.py --fill-chords                   # songs without chords (Ultimate Guitar)
    python fetch_lyrics.py --ug-top 100                    # 100 new songs from UG's most viewed chords
    python fetch_lyrics.py --fill-missing --dry-run        # print without writing

Options:
    --base-url URL   app to update (default http://127.0.0.1:8080)
    ADMIN_TOKEN      env var, sent as the Bearer token (not needed with DISABLE_AUTH=1)

Sources are in enrich.py (lyrics: LRCLIB, then lyrics.ovh; chords: the most voted
Ultimate Guitar "Chords" tab). Songs added here are also filled in by the app in
the background (photo, style/year/language, and anything else still missing).
--ug-top skips songs whose title is already in the catalog, even by another artist
(one "Hallelujah" is enough).
"""
import argparse
import os
import re

from enrich import find_chords, find_lyrics, http_json, norm, title_key, ug_fetch_tab, ug_top


def nicer_title(ug_title, track):
    # UG drops apostrophes ("Cant Help Falling In Love"); use the lyrics source's
    # spelling when it is the same title apart from punctuation and case
    if track:
        track = re.sub(r"\s*[\(\[].*$|\s+-\s+.*$", "", track).strip()
        if track and norm(track) == norm(ug_title):
            return track
    return ug_title


def parse_song_file(path):
    songs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            artist, sep, title = line.partition(" - ")
            if not sep:
                print(f"  skipped (expected 'Artist - Title'): {line}")
                continue
            songs.append((artist.strip(), title.strip()))
    return songs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fill-missing", action="store_true")
    parser.add_argument("--add-file")
    parser.add_argument("--fill-chords", action="store_true")
    parser.add_argument("--ug-top", type=int, metavar="N")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not (args.fill_missing or args.add_file or args.fill_chords or args.ug_top):
        parser.error("use --fill-missing, --add-file, --fill-chords and/or --ug-top N")

    base, token = args.base_url.rstrip("/"), os.getenv("ADMIN_TOKEN")
    catalog = http_json(base + "/admin/songs", token=token)
    found = missed = 0

    if args.fill_missing:
        print("== Filling missing lyrics")
        for song in [s for s in catalog if not s["lyrics"]]:
            lyrics, source, _, _ = find_lyrics(song["artist"], song["title"])
            if not lyrics:
                print(f"  ✗ {song['title']} — {song['artist']}: not found")
                missed += 1
                continue
            print(f"  ✓ {song['title']} — {song['artist']} ({source}, {len(lyrics.splitlines())} lines)")
            found += 1
            if not args.dry_run:
                http_json(f"{base}/admin/songs/{song['id']}/lyrics", "POST", {"lyrics": lyrics}, token)

    if args.add_file:
        print("== Adding new songs")
        existing = {(norm(s["artist"]), norm(s["title"])) for s in catalog}
        position = max((s["position"] or 0 for s in catalog), default=-1) + 1
        for artist, title in parse_song_file(args.add_file):
            if (norm(artist), norm(title)) in existing:
                print(f"  = {title} — {artist}: already in catalog")
                continue
            lyrics, source, _, _ = find_lyrics(artist, title)
            status = f"{source}, {len(lyrics.splitlines())} lines" if lyrics else "no lyrics found, added anyway"
            print(f"  + {title} — {artist} ({status})")
            found += bool(lyrics)
            missed += not lyrics
            if not args.dry_run:
                http_json(base + "/admin/songs", "POST",
                          {"title": title, "artist": artist, "lyrics": lyrics, "position": position}, token)
                position += 1
                existing.add((norm(artist), norm(title)))

    if args.fill_chords:
        print("== Filling missing chords")
        catalog = http_json(base + "/admin/songs", token=token)   # includes songs added above
        for song in [s for s in catalog if not s["chords"]]:
            try:
                chords, tab = find_chords(song["artist"], song["title"])
            except Exception as exc:
                print(f"  ✗ {song['title']} — {song['artist']}: error ({exc})")
                missed += 1
                continue
            if not chords:
                print(f"  ✗ {song['title']} — {song['artist']}: not found")
                missed += 1
                continue
            print(f"  ✓ {song['title']} — {song['artist']} (UG #{tab['id']}, {tab['votes']} votes, {len(chords.splitlines())} lines)")
            found += 1
            if not args.dry_run:
                http_json(f"{base}/admin/songs/{song['id']}/chords", "POST", {"chords": chords}, token)

    if args.ug_top:
        print(f"== Adding {args.ug_top} songs from UG's most viewed chords")
        catalog = http_json(base + "/admin/songs", token=token)
        titles = {title_key(s["title"]) for s in catalog}
        position = max((s["position"] or 0 for s in catalog), default=-1) + 1
        added, page = 0, 1
        while added < args.ug_top and page <= 20:
            for tab in ug_top(page):
                if added >= args.ug_top:
                    break
                artist, title = tab["artist_name"], tab["song_name"]
                if title_key(title) in titles:
                    continue
                titles.add(title_key(title))
                chords = ug_fetch_tab(tab["url"])
                if not chords:
                    print(f"  ✗ {title} — {artist}: chords not found")
                    continue
                lyrics, source, track, _ = find_lyrics(artist, title)
                title = nicer_title(title, track)
                added += 1
                found += bool(lyrics)
                missed += not lyrics
                print(f"  + {added:3}. {title} — {artist} (UG #{tab['id']}, "
                      f"{source + ' lyrics' if lyrics else 'no lyrics'})")
                if not args.dry_run:
                    http_json(base + "/admin/songs", "POST", {"title": title, "artist": artist,
                              "lyrics": lyrics, "chords": chords, "position": position}, token)
                    position += 1
            page += 1

    print(f"\n{'(dry-run) ' if args.dry_run else ''}found: {found}, not found: {missed}")


if __name__ == "__main__":
    main()
