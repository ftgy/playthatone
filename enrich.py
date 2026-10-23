"""Find a song's lyrics, chords, style/year/language and artist photo online.

Used by the app (songs added in the admin panel are filled in the background)
and by the fetch_*.py scripts. Standard library only; every call blocks, so the
app runs them in threads.

Sources:
    lyrics    LRCLIB (lrclib.net), then lyrics.ovh
    chords    Ultimate Guitar (the JSON embedded in its web pages)
    year      MusicBrainz: earliest recording, single or album of that title
    style     the artist's most voted MusicBrainz genre, mapped to STYLE_RULES
    language  guessed from the lyrics
    photo     Deezer's artist picture
A result is only kept when its artist matches the requested one.
"""
import html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("playthatone.enrich")

USER_AGENT = "PlayThatOne/1.0 (https://github.com/mathieumllt/playthatone)"
PHOTO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "images", "artist")


# ── Helpers ───────────────────────────────────────────────────────────────────

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
    # "Fito y Fitipaldis" = "Fito & Fitipaldis", "Simon and Garfunkel" = "Simon & Garfunkel"
    s = re.sub(r"&|\b(?:and|y|et)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def same_artist(a, b):
    a, b = norm(a), norm(b)
    return bool(a) and (a == b or a in b or b in a)


def accent_count(s):
    return sum(unicodedata.combining(c) > 0 for c in unicodedata.normalize("NFD", s))


def better_spelling(typed, canonical):
    """The source's spelling of a name typed carelessly (all lowercase/uppercase), or None.

    Never replaces a name typed with capitals, nor one that would lose its accents
    ("Me gustas tú" stays, even if the source says "Me Gustas Tu").
    """
    if not canonical or canonical == typed or norm(canonical) != norm(typed):
        return None
    if typed not in (typed.lower(), typed.upper()) or accent_count(canonical) < accent_count(typed):
        return None
    return canonical


def title_key(title):
    # "Knocking" and "Knockin'" are the same song
    return norm(re.sub(r"ing\b", "in", title.lower()))


class Throttle:
    """At most one call every `interval` seconds, across threads."""

    def __init__(self, interval):
        self.interval, self.last, self.lock = interval, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            time.sleep(max(0.0, self.interval - (time.time() - self.last)))
            self.last = time.time()


# ── Lyrics ────────────────────────────────────────────────────────────────────

_lyrics_throttle = Throttle(0.5)


def clean(lyrics):
    lines = [line.rstrip() for line in lyrics.strip().splitlines()]
    # lyrics.ovh sometimes prepends "Paroles de la chanson X par Y"
    if lines and lines[0].lower().startswith("paroles de la chanson"):
        lines = lines[1:]
    text = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", text) or None


def from_lrclib(artist, title):
    """Returns (lyrics, track name, artist name) as LRCLIB spells them."""
    q = urllib.parse.urlencode({"artist_name": artist, "track_name": title})
    try:
        hit = http_json("https://lrclib.net/api/get?" + q)
        if hit and hit.get("plainLyrics") and same_artist(hit["artistName"], artist):
            return clean(hit["plainLyrics"]), hit["trackName"], hit["artistName"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    # Exact lookup failed: search and take the first result by the same artist
    q = urllib.parse.urlencode({"q": f"{artist} {title}"})
    for hit in http_json("https://lrclib.net/api/search?" + q) or []:
        if hit.get("plainLyrics") and same_artist(hit["artistName"], artist) and norm(title) in norm(hit["trackName"]):
            return clean(hit["plainLyrics"]), hit["trackName"], hit["artistName"]
    return None, None, None


def from_lyrics_ovh(artist, title):
    url = "https://api.lyrics.ovh/v1/{}/{}".format(urllib.parse.quote(artist), urllib.parse.quote(title))
    try:
        data = http_json(url)
    except urllib.error.HTTPError:
        return None, None, None
    lyrics = (data or {}).get("lyrics") or ""
    return (clean(lyrics) if len(lyrics) > 20 else None), None, None


def find_lyrics(artist, title):
    """Returns (lyrics, source name, track name, artist name); names as the source spells them."""
    for name, source in (("lrclib", from_lrclib), ("lyrics.ovh", from_lyrics_ovh)):
        _lyrics_throttle.wait()
        try:
            lyrics, track, artist_name = source(artist, title)
        except Exception as exc:
            logger.warning("%s error for '%s - %s': %s", name, artist, title, exc)
            continue
        if lyrics:
            return lyrics, name, track, artist_name
    return None, None, None, None


# ── Chords (Ultimate Guitar) ──────────────────────────────────────────────────
# The mobile API (api.ultimate-guitar.com) now rejects requests, so we read the
# JSON that the website embeds in its pages (<div class="js-store" data-content=...>).

UG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
UG_TAB_URL = re.compile(r"^https://tabs\.ultimate-guitar\.com/tab/[\w\-/]+$")
_ug_throttle = Throttle(1.0)


def ug_page_data(url):
    _ug_throttle.wait()
    req = urllib.request.Request(url, headers=UG_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        page = r.read().decode("utf-8")
    m = re.search(r'class="js-store" data-content="([^"]*)"', page)
    if not m:
        raise RuntimeError("UG page format changed (no js-store)")
    return json.loads(html.unescape(m.group(1)))["store"]["page"]["data"]


def ug_search(artist, title):
    """Search Ultimate Guitar for chord tabs, most voted first.

    Raises:
        RuntimeError: if UG is unreachable or its page format changed.
    """
    query = urllib.parse.urlencode({"search_type": "title", "value": f"{artist} {title}"})
    try:
        data = ug_page_data(f"https://www.ultimate-guitar.com/search.php?{query}")
    except Exception as exc:
        logger.error("UG search error for '%s %s': %s", artist, title, exc)
        raise RuntimeError(f"Ultimate Guitar unreachable: {exc}") from exc
    tabs = [t for t in data.get("results", []) if t.get("type") == "Chords"]
    tabs.sort(key=lambda t: t.get("votes") or 0, reverse=True)
    return [{
        "id": t.get("id"),
        "url": t.get("tab_url", ""),
        "song_name": t.get("song_name", ""),
        "artist_name": t.get("artist_name", ""),
        "rating": t.get("rating", 0),
        "votes": t.get("votes", 0),
        "tonality": t.get("tonality_name", ""),
        "difficulty": t.get("difficulty", ""),
    } for t in tabs[:8]]


def ug_top(page=1):
    """UG's most viewed chord tabs of all time, 50 per page."""
    data = ug_page_data(f"https://www.ultimate-guitar.com/explore?type[]=Chords&order=hitstotal_desc&page={page}")
    return [{
        "id": t.get("id"),
        "url": t.get("tab_url", ""),
        "song_name": t.get("song_name", ""),
        "artist_name": t.get("artist_name", ""),
        "votes": t.get("votes", 0),
    } for t in data.get("data", {}).get("tabs", []) if t.get("type") == "Chords"]


def ug_fetch_tab(url):
    """Fetch a UG tab page's content and clean up its proprietary format."""
    try:
        data = ug_page_data(url)
        content = data.get("tab_view", {}).get("wiki_tab", {}).get("content", "")
    except Exception as exc:
        logger.error("UG fetch error for %s: %s", url, exc)
        return ""
    # Clean up the UG format: [ch]Am[/ch] → Am, [tab]...[/tab] → ...
    # Some tab authors type HTML entities ("Music &amp; Lyrics")
    content = html.unescape(content.replace("\r\n", "\n"))
    content = re.sub(r"\[ch\](.*?)\[/ch\]", r"\1", content)
    content = re.sub(r"\[/?tab\]", "", content)
    return content.strip()


def find_chords(artist, title):
    """Returns (chords, tab) for the most voted matching "Chords" tab, or (None, None)."""
    for tab in ug_search(artist, title):
        if same_artist(tab["artist_name"], artist) and title_key(title) in title_key(tab["song_name"]):
            chords = ug_fetch_tab(tab["url"])
            if chords:
                return chords, tab
    return None, None


# ── Style, year, language ─────────────────────────────────────────────────────

MB = "https://musicbrainz.org/ws/2"
# MusicBrainz allows about 1 request per second and answers 503 when busy
_mb_throttle = Throttle(1.2)

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


def mb(path, **params):
    params["fmt"] = "json"
    url = f"{MB}/{path}?{urllib.parse.urlencode(params)}"
    for delay in (3, 8, 20, 40, None):
        _mb_throttle.wait()
        try:
            return http_json(url)
        except urllib.error.HTTPError as e:
            if e.code != 503 or delay is None:
                raise
            time.sleep(delay)


def lucene(s):
    return re.sub(r'([+\-!(){}\[\]^"~*?:\\/&|])', r"\\\1", s)


def artist_terms(artist):
    # Every word, not the exact phrase: "Fito y Fitipaldis" must find "Fito & Fitipaldis"
    words = [w for w in re.findall(r"\w+", artist.lower()) if w not in ("and", "y", "et")]
    return "(" + " AND ".join(lucene(w) for w in words) + ")" if words else f'"{lucene(artist)}"'


def lookup_recording(artist, title):
    """Returns (earliest year, MusicBrainz artist id) for the song."""
    base_title = re.sub(r"\s*\(.*\)$", "", title)
    keys = (title_key(title), title_key(base_title))
    query = f'recording:"{lucene(base_title)}" AND artist:{artist_terms(artist)}'
    years, artist_ids = [], []
    for r in mb("recording", query=query, limit=100).get("recordings", []):
        credits = [c for c in r.get("artist-credit", []) if isinstance(c, dict) and same_artist(c["name"], artist)]
        if not credits or title_key(r["title"]) not in keys:
            continue
        artist_ids.append(credits[0]["artist"]["id"])
        date = r.get("first-release-date") or ""
        if re.match(r"\d{4}", date):
            years.append(int(date[:4]))
    artist_id = max(set(artist_ids), key=artist_ids.count) if artist_ids else None
    # Famous songs have hundreds of recordings and the search may miss the original:
    # the single (or album) of the same name usually carries the original date
    query = f'releasegroup:"{lucene(base_title)}" AND artist:{artist_terms(artist)}'
    for rg in mb("release-group", query=query, limit=50).get("release-groups", []):
        credits = [c for c in rg.get("artist-credit", []) if isinstance(c, dict) and same_artist(c["name"], artist)]
        date = rg.get("first-release-date") or ""
        if credits and title_key(rg["title"]) in keys and re.match(r"\d{4}", date):
            years.append(int(date[:4]))
    return (min(years) if years else None), artist_id


_genres_cache = {}


def artist_genres(artist_id):
    if artist_id not in _genres_cache:
        data = mb(f"artist/{artist_id}", inc="genres+tags")
        ranked = sorted(data.get("genres") or data.get("tags") or [], key=lambda g: -g.get("count", 0))
        _genres_cache[artist_id] = [g["name"].lower() for g in ranked]
    return _genres_cache[artist_id]


def find_year_and_style(artist, title):
    """Returns (year, style); either can be None."""
    year, artist_id = lookup_recording(artist, title)
    return year, (style_for(artist_genres(artist_id)) if artist_id else None)


# ── Artist photos ─────────────────────────────────────────────────────────────

# Artists Deezer lists under another name
DEEZER_NAMES = {
    "Bob Marley": "Bob Marley & The Wailers",
    "Cat Stevens": "Yusuf / Cat Stevens",
}


def photo_path(artist):
    """Where the audience page looks for the artist's photo (None if the name can't be a file)."""
    if not artist or "/" in artist or "\\" in artist or artist.startswith("."):
        return None
    return os.path.join(PHOTO_DIR, artist + ".jpg")


def deezer_picture(artist):
    artist = DEEZER_NAMES.get(artist, artist)
    q = urllib.parse.urlencode({"q": artist, "limit": 10})
    # Exact name only: avoids joint credits ("Elvis Presley, Chuck Berry, ...")
    # and tribute acts; an empty hash ("/artist//") is Deezer's placeholder.
    # Several acts can share a name ("Queen"): take the one with the most fans.
    hits = [h for h in http_json("https://api.deezer.com/search/artist?" + q).get("data", [])
            if norm(h["name"]) == norm(artist) and "/artist//" not in h.get("picture_big", "/artist//")]
    return max(hits, key=lambda h: h.get("nb_fan", 0))["picture_big"] if hits else None


def save_artist_photo(artist):
    """Download the artist's photo to PHOTO_DIR (300x300 if ImageMagick is installed).

    Returns True if saved, False if no photo was found.
    """
    dest = photo_path(artist)
    if not dest:
        return False
    url = deezer_picture(artist)
    if not url:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        raw = os.path.join(tmp, "photo.jpg")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as res, open(raw, "wb") as f:
            if not res.headers.get("Content-Type", "").startswith("image/jpeg"):
                raise ValueError(f"not a JPEG ({res.headers.get('Content-Type')})")
            shutil.copyfileobj(res, f)
        os.makedirs(PHOTO_DIR, exist_ok=True)
        if shutil.which("convert"):
            subprocess.run(["convert", raw, "-strip", "-resize", "300x300", "-quality", "82", dest], check=True)
        else:
            shutil.copy(raw, dest)
    return True
