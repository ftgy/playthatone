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

Lyrics: LRCLIB (lrclib.net) first, then lyrics.ovh. Chords: Ultimate Guitar, via
the app's /admin/chords endpoints, taking the most voted "Chords" tab. A result is
only kept when its artist matches the requested one. --ug-top skips songs whose
title is already in the catalog, even by another artist (one "Hallelujah" is enough).
"""
import argparse
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "PlayThatOne/1.0 (https://github.com/mathieumllt/playthatone)"


def http_json(url, method="GET", body=None, token=None, timeout=20):
    headers = {"User-Agent": USER_AGENT}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    # Retry when a source is rate-limiting or briefly unavailable
    for delay in (2, 5, 10, None):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                raw = res.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503) or delay is None:
                raise
            time.sleep(delay)


def norm(s):
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"^the\s+", "", s)
    return re.sub(r"[^a-z0-9]+", "", s.replace("&", "and"))


def same_artist(a, b):
    a, b = norm(a), norm(b)
    return bool(a) and (a == b or a in b or b in a)


def clean(lyrics):
    lines = [line.rstrip() for line in lyrics.strip().splitlines()]
    # lyrics.ovh sometimes prepends "Paroles de la chanson X par Y"
    if lines and lines[0].lower().startswith("paroles de la chanson"):
        lines = lines[1:]
    text = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", text) or None


def from_lrclib(artist, title):
    q = urllib.parse.urlencode({"artist_name": artist, "track_name": title})
    try:
        hit = http_json("https://lrclib.net/api/get?" + q)
        if hit and hit.get("plainLyrics") and same_artist(hit["artistName"], artist):
            return clean(hit["plainLyrics"]), hit["trackName"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    # Exact lookup failed: search and take the first result by the same artist
    q = urllib.parse.urlencode({"q": f"{artist} {title}"})
    for hit in http_json("https://lrclib.net/api/search?" + q) or []:
        if hit.get("plainLyrics") and same_artist(hit["artistName"], artist) and norm(title) in norm(hit["trackName"]):
            return clean(hit["plainLyrics"]), hit["trackName"]
    return None, None


def from_lyrics_ovh(artist, title):
    url = "https://api.lyrics.ovh/v1/{}/{}".format(urllib.parse.quote(artist), urllib.parse.quote(title))
    try:
        data = http_json(url)
    except urllib.error.HTTPError:
        return None, None
    lyrics = (data or {}).get("lyrics") or ""
    return (clean(lyrics) if len(lyrics) > 20 else None), None


def fetch(artist, title):
    """Returns (lyrics, source name, track name as the source spells it)."""
    time.sleep(0.5)   # be gentle with the free APIs
    for name, source in (("lrclib", from_lrclib), ("lyrics.ovh", from_lyrics_ovh)):
        try:
            lyrics, track = source(artist, title)
        except Exception as exc:
            print(f"    ({name} error: {exc})")
            continue
        if lyrics:
            return lyrics, name, track
        time.sleep(0.3)
    return None, None, None


def nicer_title(ug_title, track):
    # UG drops apostrophes ("Cant Help Falling In Love"); use the lyrics source's
    # spelling when it is the same title apart from punctuation and case
    if track:
        track = re.sub(r"\s*[\(\[].*$|\s+-\s+.*$", "", track).strip()
        if track and norm(track) == norm(ug_title):
            return track
    return ug_title


def title_key(title):
    # "Knocking" and "Knockin'" are the same song
    return norm(re.sub(r"ing\b", "in", title.lower()))


def fetch_chords(base, token, artist, title):
    time.sleep(1)   # be gentle with UG
    q = urllib.parse.urlencode({"artist": artist, "title": title})
    try:
        results = http_json(f"{base}/admin/chords/search?{q}", token=token)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        raise
    for tab in results:
        if same_artist(tab["artist_name"], artist) and title_key(title) in title_key(tab["song_name"]):
            q = urllib.parse.urlencode({"url": tab["url"]})
            chords = http_json(f"{base}/admin/chords/fetch?{q}", token=token, timeout=30)["chords"]
            return chords, tab
    return None, None


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
            lyrics, source, _ = fetch(song["artist"], song["title"])
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
            lyrics, source, _ = fetch(artist, title)
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
                chords, tab = fetch_chords(base, token, song["artist"], song["title"])
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
            for tab in http_json(f"{base}/admin/chords/top?page={page}", token=token, timeout=30):
                if added >= args.ug_top:
                    break
                artist, title = tab["artist_name"], tab["song_name"]
                if title_key(title) in titles:
                    continue
                titles.add(title_key(title))
                time.sleep(1)   # be gentle with UG
                try:
                    q = urllib.parse.urlencode({"url": tab["url"]})
                    chords = http_json(f"{base}/admin/chords/fetch?{q}", token=token, timeout=30)["chords"]
                except Exception as exc:
                    print(f"  ✗ {title} — {artist}: chords error ({exc})")
                    continue
                lyrics, source, track = fetch(artist, title)
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
