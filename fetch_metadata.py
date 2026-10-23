"""Fill each song's style, year and language through the app's admin API.

Usage:
    python fetch_metadata.py                 # only fills empty fields
    python fetch_metadata.py --overwrite     # recompute every field
    python fetch_metadata.py --dry-run       # print without writing

Options:
    --base-url URL   app to update (default http://127.0.0.1:8080)
    ADMIN_TOKEN      env var, sent as the Bearer token (not needed with DISABLE_AUTH=1)

Sources:
    year      earliest release date on MusicBrainz of a recording, single or album
              of that title by that artist
    style     the artist's most voted MusicBrainz genre, mapped to a short list of styles
    language  guessed from the lyrics (common words of each language)
Review the result: fix any value from the admin panel ("Info").
"""
import argparse
import os
import re
import time
import urllib.error
import urllib.parse

from fetch_lyrics import http_json, norm, same_artist, title_key

MB = "https://musicbrainz.org/ws/2"

# First rule matching a genre wins; genres are tried from most to least voted
STYLE_RULES = [
    ("Reggae", ["reggae", "ska", "dub"]),
    ("Gospel", ["worship", "gospel", "christian", "ccm"]),
    ("Soundtrack", ["soundtrack", "show tunes", "musical", "film score"]),
    ("Chanson", ["chanson", "variété française", "french pop"]),
    ("Latin", ["latin", "cumbia", "salsa", "bolero", "flamenco", "rumba", "bachata", "nueva canción", "reggaeton"]),
    ("Country", ["country", "americana", "bluegrass"]),
    ("Jazz & Swing", ["jazz", "swing", "traditional pop", "big band", "easy listening"]),
    ("Soul & R&B", ["soul", "r&b", "rhythm and blues", "funk", "motown", "doo-wop"]),
    ("Disco & Dance", ["disco", "house", "edm", "eurodance", "hi-nrg", "trance", "electro"]),
    ("Folk", ["folk", "singer-songwriter", "acoustic"]),
    ("Indie & Alternative", ["indie", "alternative", "grunge", "britpop", "emo", "post-punk", "shoegaze"]),
    ("Rock", ["rock", "metal", "punk", "blues", "new wave", "psychedelic"]),
    ("Pop", ["pop", "dance"]),
]

STOPWORDS = {
    "English": "the and you i me my to of it is in that your don't be we what all this on so",
    "Spanish": "el la los las que y de en mi tu no me un una por con es yo te se lo como pero más",
    "French": "le la les et je tu de des un une est pas que qui dans mon moi toi vous nous ne ce",
    "Portuguese": "o os as que e de não eu você um uma meu com em no é se mais",
    "Italian": "il che e di non un una io tu mi ti sono per con nel la ho ma",
    "Catalan": "el la els les i de que no em et un una amb per és jo tu del",
    "Tagalog": "ang ng sa mga ko mo ako ikaw at ay hindi kita lang ka pa",   # not "na": "na na na"
}
STOPWORDS = {lang: set(words.split()) for lang, words in STOPWORDS.items()}


def detect_language(lyrics):
    words = re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)?", re.sub(r"\[[^\]]*\]", " ", lyrics or "").lower())
    if len(words) < 20:
        return None
    scores = {lang: sum(w in stop for w in words) for lang, stop in STOPWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] >= 5 else None


def style_for(genres):
    for name in genres:
        for style, keywords in STYLE_RULES:
            if any(k in name for k in keywords):
                return style
    return None


_last_call = 0.0


def mb(path, **params):
    # MusicBrainz allows about 1 request per second and answers 503 when busy
    global _last_call
    params["fmt"] = "json"
    url = f"{MB}/{path}?{urllib.parse.urlencode(params)}"
    for delay in (3, 8, 20, 40, None):
        time.sleep(max(0.0, 1.2 - (time.time() - _last_call)))
        _last_call = time.time()
        try:
            return http_json(url)
        except urllib.error.HTTPError as e:
            if e.code != 503 or delay is None:
                raise
            time.sleep(delay)


def lucene(s):
    return re.sub(r'([+\-!(){}\[\]^"~*?:\\/&|])', r"\\\1", s)


def lookup_recording(artist, title):
    """Returns (earliest year, MusicBrainz artist id) for the song."""
    base_title = re.sub(r"\s*\(.*\)$", "", title)
    query = f'recording:"{lucene(base_title)}" AND artist:"{lucene(artist)}"'
    recordings = mb("recording", query=query, limit=100).get("recordings", [])
    years, artist_ids = [], []
    for r in recordings:
        credits = [c for c in r.get("artist-credit", []) if isinstance(c, dict) and same_artist(c["name"], artist)]
        if not credits or title_key(r["title"]) not in (title_key(title), title_key(base_title)):
            continue
        artist_ids.append(credits[0]["artist"]["id"])
        date = r.get("first-release-date") or ""
        if re.match(r"\d{4}", date):
            years.append(int(date[:4]))
    artist_id = max(set(artist_ids), key=artist_ids.count) if artist_ids else None
    # Famous songs have hundreds of recordings and the search may miss the original:
    # the single (or album) of the same name usually carries the original date
    query = f'releasegroup:"{lucene(base_title)}" AND artist:"{lucene(artist)}"'
    for rg in mb("release-group", query=query, limit=50).get("release-groups", []):
        credits = [c for c in rg.get("artist-credit", []) if isinstance(c, dict) and same_artist(c["name"], artist)]
        date = rg.get("first-release-date") or ""
        if credits and title_key(rg["title"]) in (title_key(title), title_key(base_title)) and re.match(r"\d{4}", date):
            years.append(int(date[:4]))
    return (min(years) if years else None), artist_id


_genres_cache = {}


def artist_genres(artist_id):
    if artist_id not in _genres_cache:
        data = mb(f"artist/{artist_id}", inc="genres+tags")
        ranked = sorted(data.get("genres") or data.get("tags") or [], key=lambda g: -g.get("count", 0))
        _genres_cache[artist_id] = [g["name"].lower() for g in ranked]
    return _genres_cache[artist_id]


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
                year, artist_id = lookup_recording(song["artist"], song["title"])
                if year and (args.overwrite or not song.get("year")):
                    update["year"] = year
                if artist_id and (args.overwrite or not song.get("genre")):
                    style = style_for(artist_genres(artist_id))
                    if style:
                        update["genre"] = style
            except Exception as exc:
                print(f"    (MusicBrainz error: {exc})")
        shown = " | ".join(str(update.get(k, "—")) for k in ("genre", "year", "language"))
        print(f"  {song['title'][:40]:40} {song['artist'][:24]:24} {shown}")
        if update and not args.dry_run:
            http_json(f"{base}/admin/songs/{song['id']}", "PATCH", update, token)


if __name__ == "__main__":
    main()
