# backend/api/ingest.py
import os, re, csv, json, urllib.parse, urllib.request
from flask import Blueprint, request, jsonify, send_from_directory

ingest_bp = Blueprint("ingest_bp", __name__)

# Rutas base
BASE_DIR   = os.path.dirname(os.path.dirname(__file__))  # .../backend
SONGS_DIR  = os.path.join(BASE_DIR, "songs")
LYRICS_DIR = os.path.join(SONGS_DIR, "lyrics")
TABS_DIR   = os.path.join(SONGS_DIR, "tabs")
CORE_DIR   = os.path.join(BASE_DIR, "core")
CATALOG_CSV = os.path.join(CORE_DIR, "catalog_postgres.csv")
META_CACHE_PATH = os.path.join(CORE_DIR, "metadata_cache.json")
FRONTEND_DIR = os.path.join(os.path.dirname(BASE_DIR), "frontend")
PUBLIC_DIR   = os.path.join(FRONTEND_DIR, "public")
PUB_TABS_DIR   = os.path.join(PUBLIC_DIR, "songs", "tabs")
PUB_LYRICS_DIR = os.path.join(PUBLIC_DIR, "songs", "lyrics")
os.makedirs(PUB_TABS_DIR, exist_ok=True)
os.makedirs(PUB_LYRICS_DIR, exist_ok=True)
os.makedirs(LYRICS_DIR, exist_ok=True)
os.makedirs(TABS_DIR, exist_ok=True)
os.makedirs(CORE_DIR, exist_ok=True)
if not os.path.exists(META_CACHE_PATH):
    with open(META_CACHE_PATH, "w", encoding="utf-8") as f: f.write("{}")

# --- utilidades ---
CHORD_TOKEN = r"(?:[A-G][#b]?m?(?:aj|sus|add|dim|aug|°|\+|-)?\d{0,2})"
CHORD_IN_BRACKETS = re.compile(r"\[([A-G][#b]?[^]]{0,6})\]")
ONLY_CHORDS_LINE  = re.compile(rf"^\s*(?:{CHORD_TOKEN}|\||/|–|-|\.)+(?:\s+{CHORD_TOKEN}|\s*[-/|.])*?\s*$")
TIME_SIG_LINE     = re.compile(r"\b\d+/\d+\b")
TEMPO_HINTS       = re.compile(r"(?:tempo|bpm|half|double|intro|verse|chorus|bridge)", re.I)

def _slug_underscore(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[’´`'“”\"(){}\[\],.?!:;]", "", s)
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "untitled"

def _guess_year(lines):
    for ln in lines[:10]:
        m = re.search(r"\b(19\d{2}|20\d{2})\b", ln)
        if m: return m.group(1)
    return ""

def _strip_chords_to_lyrics(text: str) -> str:
    t = CHORD_IN_BRACKETS.sub("", text)
    out = []
    for raw in t.splitlines():
        ln = raw.rstrip()
        if ONLY_CHORDS_LINE.match(ln): continue
        if TIME_SIG_LINE.search(ln) and len(ln) < 40: continue
        if TEMPO_HINTS.search(ln) and len(ln) < 80: continue
        out.append(ln)
    txt = "\n".join(out)
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
    return txt

def _parse_paste(text: str):
    lines = [l.rstrip() for l in text.splitlines()]
    non_empty = [l for l in lines if l.strip()]
    title  = non_empty[0].strip() if non_empty else "Untitled"
    artist = non_empty[1].strip() if len(non_empty) > 1 else ""

    sample = "\n".join(non_empty[:50]).lower()
    if re.search(r"[ñáéíóúü]", sample):
        language = "Spanish"
    elif re.search(r"[àèìòùç]", sample):
        language = "French/Italian?"
    else:
        language = "English"

    year = _guess_year(non_empty)
    song_id = _slug_underscore(title)
    lyrics_guess = _strip_chords_to_lyrics(text)

    return {
        "id": song_id,
        "title": title,
        "artist": artist,
        "year": year,
        "language": language,
        "genre": "",
        "lyrics_guess": lyrics_guess,
    }

def _load_meta_cache():
    try:
        with open(META_CACHE_PATH, "r", encoding="utf-8") as f: return json.load(f)
    except Exception:
        return {}

def _save_meta_cache(d):
    with open(META_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

def _http_json(url, headers=None, timeout=7):
    req = urllib.request.Request(url, headers=headers or {"User-Agent":"PTO/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))

def _score_match(qt, qa, it_title, it_artist):
    t = (it_title or "").lower()
    a = (it_artist or "").lower()
    s = 0
    if qt in t: s += 3
    if qa and qa in a: s += 2
    if t == qt: s += 2
    if qa and a == qa: s += 1
    return s

def _write_text(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def _append_catalog_row(row):
    """
    Escribe SIEMPRE en backend/core/catalog_postgres.csv
    - separador ';'
    - cabecera exacta de 12 columnas PTO
      id;name;artist;year;language;genre;popularity;duration;mood;key;tempo;enabled
    """
    header = ["id","name","artist","year","language","genre","popularity","duration","mood","key","tempo","enabled"]
    delim = ";"

    # Si no existe, crea con cabecera
    is_new = not os.path.exists(CATALOG_CSV)
    if is_new:
        os.makedirs(os.path.dirname(CATALOG_CSV), exist_ok=True)
        with open(CATALOG_CSV, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter=delim)
            w.writerow(header)

    # Construye la fila en el orden exacto de la cabecera
    values = {
        "id":        row.get("id",""),
        "name":      row.get("name",""),
        "artist":    row.get("artist",""),
        "year":      row.get("year",""),
        "language":  row.get("language",""),
        "genre":     row.get("genre",""),
        "popularity":row.get("popularity",""),
        "duration":  row.get("duration",""),
        "mood":      row.get("mood",""),
        "key":       row.get("key",""),
        "tempo":     row.get("tempo",""),
        "enabled":   row.get("enabled","Y"),
    }

    with open(CATALOG_CSV, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=delim)
        w.writerow([values.get(k, "") for k in header])


# ---------- RUTAS ----------

@ingest_bp.route("/songs/ingest/parse", methods=["POST"])
def ingest_parse():
    payload = request.get_json(silent=True) or {}
    pasted = (payload.get("pasted") or "").strip()
    if not pasted:
        return jsonify({"ok": False, "error": "empty_paste"}), 400

    info = _parse_paste(pasted)
    q = re.sub(r"\s+", "+", (info["title"] + " " + info.get("artist","")).strip())
    links = {
        "lyrics_google": f"https://www.google.com/search?q={q}+lyrics",
        "lyrics_letras": f"https://www.letras.com/?q={q}",
        "chords_google": f"https://www.google.com/search?q={q}+chords",
        "youtube":       f"https://www.youtube.com/results?search_query={q}+chords",
    }
    return jsonify({"ok": True, "info": info, "links": links})

@ingest_bp.route("/songs/ingest/fetch_meta", methods=["POST"])
def ingest_fetch_meta():
    payload = request.get_json(silent=True) or {}
    title  = (payload.get("title")  or "").strip()
    artist = (payload.get("artist") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "missing_title"}), 400

    key = "|".join([title.lower(), artist.lower()])
    cache = _load_meta_cache()
    if key in cache:
        c = cache[key]
        return jsonify({"ok": True, "source": c.get("source","cache"), "year": c.get("year",""), "genre": c.get("genre","")})

    qt = title.lower()
    qa = artist.lower()

    # 1) iTunes
    try:
        q = urllib.parse.quote_plus((title + " " + artist).strip())
        url = f"https://itunes.apple.com/search?term={q}&entity=song&limit=5"
        data = _http_json(url)
        best = None; best_score = -1
        for it in data.get("results", []):
            sc = _score_match(qt, qa, it.get("trackName",""), it.get("artistName",""))
            if sc > best_score: best, best_score = it, sc
        if best:
            year = (best.get("releaseDate","") or "")[:4]
            genre = best.get("primaryGenreName","") or ""
            cache[key] = {"source":"itunes","year":year,"genre":genre}
            _save_meta_cache(cache)
            return jsonify({"ok": True, "source": "itunes", "year": year, "genre": genre})
    except Exception:
        pass

    # 2) MusicBrainz
    try:
        if artist: q = f'recording:"{title}" AND artist:"{artist}"'
        else:      q = f'recording:"{title}"'
        url = "https://musicbrainz.org/ws/2/recording/?query=" + urllib.parse.quote(q) + "&fmt=json&limit=5"
        data = _http_json(url, headers={"User-Agent":"PTO/1.0 (playthatone)"})
        recs = data.get("recordings", []) or []
        year = ""; genre = ""
        if recs:
            recs.sort(key=lambda r: int(r.get("score", 0)), reverse=True)
            r0 = recs[0]
            year = (r0.get("first-release-date","") or "")[:4]
            if not year and r0.get("releases"):
                for rel in r0["releases"]:
                    d = rel.get("date","")
                    if d and len(d)>=4: year = d[:4]; break
            tags = r0.get("tags") or []
            if tags:
                tags.sort(key=lambda t: int(t.get("count",1)), reverse=True)
                genre = tags[0].get("name","")
        cache[key] = {"source":"musicbrainz","year":year,"genre":genre}
        _save_meta_cache(cache)
        return jsonify({"ok": True, "source": "musicbrainz", "year": year, "genre": genre})
    except Exception:
        pass

    cache[key] = {"source":"none","year":"","genre":""}
    _save_meta_cache(cache)
    return jsonify({"ok": True, "source": "none", "year": "", "genre": ""})

@ingest_bp.route("/songs/ingest/create", methods=["POST"])
def ingest_create():
    payload = request.get_json(silent=True) or {}
    pasted = (payload.get("pasted") or "").strip()
    info   = payload.get("info") or {}
    if not pasted or not info:
        return jsonify({"ok": False, "error": "missing_data"}), 400

    # id técnico (slug) y título visible
    sid    = info.get("id") or _slug_underscore(info.get("title",""))
    title  = info.get("title","").strip() or "Untitled"
    artist = info.get("artist","").strip()
    year   = info.get("year","").strip()
    language = info.get("language","").strip()
    genre    = info.get("genre","").strip()
    lyrics_g = info.get("lyrics_guess","")

    # rutas de ficheros
    tab_path = os.path.join(PUB_TABS_DIR,   f"TAB{sid}.txt")
    lyr_path = os.path.join(PUB_LYRICS_DIR, f"{sid}.txt")
    if os.path.exists(tab_path) or os.path.exists(lyr_path):
        return jsonify({"ok": False, "error": "id_exists", "id": sid}), 409

    # guardar tablatura y letra
    _write_text(tab_path, pasted)
    _write_text(lyr_path, lyrics_g)

    # fila de catálogo (solo 'name', no 'title')
    row = {
        "id": sid,
        "name": title,
        "artist": artist,
        "year": year,
        "language": language,
        "genre": genre,
        "enabled": "Y",
    }
    _append_catalog_row(row)

    # intentar regenerar catálogo automáticamente
    updated = False
    try:
        from backend.core.gen_catalog import generate_catalog  # si existe
        generate_catalog()
        updated = True
    except Exception:
        pass

    return jsonify({"ok": True, "id": sid, "updated_catalog": updated, "csv_path": CATALOG_CSV})
    
@ingest_bp.route("/songs/ingest/debug_catalog", methods=["GET"])
def ingest_debug_catalog():
    info = {
        "path": CATALOG_CSV,
        "exists": os.path.exists(CATALOG_CSV),
        "size": os.path.getsize(CATALOG_CSV) if os.path.exists(CATALOG_CSV) else 0,
        "head": "",
        "tail": "",
    }
    try:
        with open(CATALOG_CSV, "r", encoding="utf-8") as f:
            lines = f.readlines()
        info["head"] = "".join(lines[:2]).strip()   # cabecera + 1a fila
        info["tail"] = "".join(lines[-2:]).strip()  # últimas 2 filas
    except Exception as e:
        info["error"] = str(e)
    return jsonify(info)
    
# Servir TABs y Lyrics desde backend/songs/*
@ingest_bp.route("/songs/tabs/<path:filename>", methods=["GET"])
def serve_tab_file(filename):
    return send_from_directory(TABS_DIR, filename)

@ingest_bp.route("/songs/lyrics/<path:filename>", methods=["GET"])
def serve_lyrics_file(filename):
    return send_from_directory(LYRICS_DIR, filename)

from flask import jsonify  # arriba ya tienes imports de Flask; si falta, déjalo aquí

# ¿Existe el par TAB/lyrics para un ID?
@ingest_bp.route("/api/ingest/debug_file/<id>", methods=["GET"])
def ingest_debug_file_api(id):
    tab_path_pub = os.path.join(PUB_TABS_DIR,   f"TAB{id}.txt")
    lyr_path_pub = os.path.join(PUB_LYRICS_DIR, f"{id}.txt")
    return jsonify({
        "id": id,
        "tabs_path": tab_path_pub,
        "tabs_exists": os.path.exists(tab_path_pub),
        "lyrics_path": lyr_path_pub,
        "lyrics_exists": os.path.exists(lyr_path_pub),
        "tabs_url": f"/songs/tabs/TAB{id}.txt",
        "lyrics_url": f"/songs/lyrics/{id}.txt",
    })

# Lista rápida de archivos creados en la carpeta pública
@ingest_bp.route("/api/ingest/list_files", methods=["GET"])
def ingest_list_files_api():
    try:
        tabs = sorted([f for f in os.listdir(PUB_TABS_DIR) if f.endswith(".txt")])
    except Exception:
        tabs = []
    try:
        lyrics = sorted([f for f in os.listdir(PUB_LYRICS_DIR) if f.endswith(".txt")])
    except Exception:
        lyrics = []
    return jsonify({
        "tabs_dir": PUB_TABS_DIR,
        "lyrics_dir": PUB_LYRICS_DIR,
        "tabs_tail": tabs[-10:],      # últimas 10 por si hay muchas
        "lyrics_tail": lyrics[-10:]
    })
