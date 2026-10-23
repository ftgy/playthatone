print(">> main.py iniciado")

import sys
import os
import json
import csv
from pathlib import Path
from backend.api.ingest import ingest_bp

# Para imports tipo `backend.*`
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import eventlet
eventlet.monkey_patch()

from flask import Flask, send_from_directory, jsonify, request, Blueprint, session
print(">> Flask importado")

from backend.api.websockets import socketio
print(">> socketio importado")

from backend.services.vote_logic import load_votes, count_votes, save_votes, load_states

# --- import robusto de rutas de persistencia ---
try:
    from backend.core.config import VOTES_FILE, SONG_STATES_FILE
except Exception:
    try:
        from .core.config import VOTES_FILE, SONG_STATES_FILE  # type: ignore
    except Exception:
        CORE_DIR = os.environ.get("CORE_DIR", "/opt/render/project/src/backend/core")
        if not os.path.isdir(CORE_DIR):
            CORE_DIR = os.path.join(os.path.dirname(__file__), "core")
        VOTES_FILE = os.path.join(CORE_DIR, "votes.json")
        SONG_STATES_FILE = os.path.join(CORE_DIR, "song_states.json")

# Ruta absoluta a frontend/public
PROJECT_ROOT = Path(__file__).resolve().parents[1]      # .../src
PUBLIC_DIR   = PROJECT_ROOT / "frontend" / "public"
print(f">> PUBLIC_DIR={PUBLIC_DIR}")
# =========================
# Proposals (suggested songs)
# =========================
import time, re
from collections import defaultdict

# Carpeta backend/proposals
BASE_DIR = os.path.dirname(__file__)
PROPOSALS_DIR = os.path.join(BASE_DIR, "proposals")
os.makedirs(PROPOSALS_DIR, exist_ok=True)
PROPOSALS_PATH = os.path.join(PROPOSALS_DIR, "proposals.json")

_ip_last_ts = defaultdict(lambda: 0)

def _slug_title(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[’´`']", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:80] or "untitled"

def _load_proposals():
    if not os.path.exists(PROPOSALS_PATH):
        return []
    try:
        with open(PROPOSALS_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or []
    except Exception:
        return []

def _save_proposals(data):
    with open(PROPOSALS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# --- Blueprint ---
proposals_bp = Blueprint("proposals_bp", __name__)

@proposals_bp.route("/proposals", methods=["POST"])
def post_proposal():
    payload = request.get_json(silent=True) or {}
    title = (payload.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "empty_title"}), 400

    ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "?"
    now = time.time()
    if now - _ip_last_ts[ip] < 30:  # 1 propuesta / 30s por IP
        return jsonify({"ok": False, "error": "rate_limited"}), 429
    _ip_last_ts[ip] = now

    slug = _slug_title(title)
    data = _load_proposals()
    for p in data:
        if p["slug"] == slug:
            p["count"] += 1
            p["last_ts"] = int(now)
            _save_proposals(data)
            try:
                socketio.emit("proposal_added", p, broadcast=True)
            except Exception:
                pass
            return jsonify({"ok": True, "dedup": True, "proposal": p})

    item = {
        "slug": slug,
        "title": title,
        "count": 1,
        "first_ts": int(now),
        "last_ts": int(now),
    }
    data.append(item)
    _save_proposals(data)
    try:
        socketio.emit("proposal_added", item, broadcast=True)
    except Exception:
        pass
    return jsonify({"ok": True, "proposal": item})

@proposals_bp.route("/proposals", methods=["GET"])
def get_proposals():
    data = _load_proposals()
    data.sort(key=lambda p: (-p["count"], p["first_ts"]))
    return jsonify({"ok": True, "proposals": data})

@proposals_bp.route("/proposals/<slug>", methods=["DELETE"])
def delete_proposal(slug):
    data = _load_proposals()
    new_data = [p for p in data if p["slug"] != slug]
    _save_proposals(new_data)
    try:
        socketio.emit("proposal_removed", {"slug": slug}, broadcast=True)
    except Exception:
        pass
    return jsonify({"ok": True})
    
# =========================
# Fetch metadata (year, genre) from public APIs
# =========================
import urllib.parse, urllib.request

META_CACHE_PATH = os.path.join(os.path.dirname(__file__), "core", "metadata_cache.json")
os.makedirs(os.path.dirname(META_CACHE_PATH), exist_ok=True)
if not os.path.exists(META_CACHE_PATH):
    with open(META_CACHE_PATH, "w", encoding="utf-8") as f:
        f.write("{}")

def _load_meta_cache():
    try:
        with open(META_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_meta_cache(d):
    with open(META_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

def _http_json(url, headers=None, timeout=7):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))

def _score_match(qt, qa, it_title, it_artist):
    # coincidencias sencillas sin librerías extra
    t = it_title.lower()
    a = (it_artist or "").lower()
    s = 0
    if qt in t: s += 3
    if qa and qa in a: s += 2
    # bonus por igualdad (muy aproximado)
    if t == qt: s += 2
    if qa and a == qa: s += 1
    return s

def create_app():
    print(">> creando app")

    # Sirve estáticos desde la raíz del sitio
    app = Flask(__name__, static_folder=str(PUBLIC_DIR), static_url_path="")
    # Session config for admin auth overlay
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev")
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = True

    
    app.register_blueprint(ingest_bp)
    
    # Proposals API
    app.register_blueprint(proposals_bp)

    # Log para confirmar que index existe
    idx = PUBLIC_DIR / "index.html"
    print(f">> index.html existe? {idx.exists()}  ({idx})")

    socketio.init_app(app, cors_allowed_origins="*")

    @app.route("/debug-files")
def debug_files():
    import os
    result = {}
    thumbs = PUBLIC_DIR / "songs" / "images" / "artist_thumbs"
    artist = PUBLIC_DIR / "songs" / "images" / "artist"
    result["thumbs_exists"] = str(thumbs.exists())
    result["artist_exists"] = str(artist.exists())
    result["thumbs_files"] = os.listdir(str(thumbs)) if thumbs.exists() else []
    result["artist_files"] = os.listdir(str(artist)) if artist.exists() else []
    return jsonify(result)
    # ---- AUTH endpoints ----
    @app.post("/auth/login")
    def auth_login():
        data = request.get_json(silent=True) or {}
        pwd = str(data.get("password") or "")
        if pwd == os.environ.get("PTO_ADMIN_PASS"):
            session["is_admin"] = True
            return jsonify(ok=True)
        return jsonify(ok=False), 401

    @app.get("/auth/status")
    def auth_status():
        return jsonify(is_admin=bool(session.get("is_admin", False)))

    print(">> socketio registrado")

    # --- Asegurar CSV de catálogo en el Disk ---
    CORE_DIR = Path(__file__).resolve().parent / "core"  # backend/core (Disk)
    CATALOG_CSV = CORE_DIR / "catalog_postgres.csv"
    if not CATALOG_CSV.exists():
        CATALOG_CSV.parent.mkdir(parents=True, exist_ok=True)
        with open(CATALOG_CSV, "w", newline="", encoding="utf-8") as f:
            f.write("id;name;artist;year;language;genre;duration;mood;key;tempo;enabled\n")
        print(f">> creado {CATALOG_CSV}")
    else:
        print(f">> encontrado {CATALOG_CSV}")

    # --- Carpetas PERSISTENTES en el Disk para letras/tabs/imagenes ---
    LYRICS_DIR     = CORE_DIR / "songs" / "lyrics"
    TABS_DIR       = CORE_DIR / "songs" / "tabs"
    IMAGES_DIR     = CORE_DIR / "images"
    ARTIST_IMG_DIR = IMAGES_DIR / "artist"
    THUMBS_DIR = IMAGES_DIR / "artist_thumbs"
    # === Manifest de imágenes de artistas (clave: nombre artista; valor: filename con extensión) ===
    import json as _json, urllib.parse as _urlp

    ARTIST_MANIFEST = ARTIST_IMG_DIR / "manifest.json"

    def _load_artist_manifest():
        try:
            with open(ARTIST_MANIFEST, "r", encoding="utf-8") as f:
                return _json.load(f)
        except Exception:
            return {}

    def _save_artist_manifest(d):
        ARTIST_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        with open(ARTIST_MANIFEST, "w", encoding="utf-8") as f:
            _json.dump(d, f, ensure_ascii=False, indent=2)

    for d in [LYRICS_DIR, TABS_DIR, IMAGES_DIR, ARTIST_IMG_DIR, THUMBS_DIR]:
        d.mkdir(parents=True, exist_ok=True)
    print(f">> LYRICS_DIR={LYRICS_DIR}")
    print(f">> TABS_DIR={TABS_DIR}")
    print(f">> IMAGES_DIR={IMAGES_DIR}")
    # === Thumbs manifest (artist -> filename en artist_thumbs) ===
    import json as _json, urllib.parse as _urlp, shutil as _shutil

    THUMBS_MANIFEST = THUMBS_DIR / "manifest.json"

    def _load_thumbs_manifest():
        try:
            with open(THUMBS_MANIFEST, "r", encoding="utf-8") as f:
                return _json.load(f)
        except Exception:
            return {}

    def _save_thumbs_manifest(d):
        THUMBS_DIR.mkdir(parents=True, exist_ok=True)
        with open(THUMBS_MANIFEST, "w", encoding="utf-8") as f:
            _json.dump(d, f, ensure_ascii=False, indent=2)

    def _make_thumb(src_path: Path, dst_path: Path, max_w=400, max_h=300):
        """
        Crea una miniatura en THUMBS_DIR con extensión correcta.
        - Si Pillow está instalado: intenta .webp (quality=80). Si falla, usa la misma extensión del original.
        - Si Pillow NO está: copia el original con la MISMA extensión.
        Devuelve SIEMPRE el nombre de archivo final (con extensión).
        """
        import shutil as _shutil
        ext = src_path.suffix.lower() or ".jpg"  # extensión original (fallback jpg)

        # 1) Si no hay Pillow, copiar con extensión original
        try:
            from PIL import Image  # noqa
        except Exception:
            dst = dst_path.with_suffix(ext)
            dst.parent.mkdir(parents=True, exist_ok=True)
            _shutil.copyfile(src_path, dst)
            return dst.name

        # 2) Con Pillow: intentar WEBP primero
        from PIL import Image
        img = Image.open(src_path)
        if img.mode not in ("RGB", "L", "P"):
            img = img.convert("RGB")
        img.thumbnail((max_w, max_h))
        dst_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            dst_webp = dst_path.with_suffix(".webp")
            img.save(dst_webp, "WEBP", quality=80, method=6)
            return dst_webp.name
        except Exception:
            # Fallback: misma extensión que el original
            dst_ext = dst_path.with_suffix(ext)
            try:
                img.save(dst_ext, quality=85)
            except Exception:
                _shutil.copyfile(src_path, dst_ext)
            return dst_ext.name

    # ===== RUTAS ESTÁTICAS =====
    @app.route("/")
    def root():
        return app.send_static_file("index.html")

    @app.route("/catalog/<path:filename>")
    def catalog(filename):
        return send_from_directory(str(PUBLIC_DIR / "catalog"), filename)

    @app.route("/inter.html")
    def inter():
        return send_from_directory(str(PUBLIC_DIR), "inter.html")

    @app.route("/addsong.html")
    def addsong():
        return send_from_directory(str(PUBLIC_DIR), "addsong.html")
        
    @app.route("/update-song", methods=["POST"])
    def update_song():
        """Actualizar UN campo de una canción por ID (sin tocar letra/tab)."""
        try:
            data = request.get_json(force=True)
            song_id = (data.get("id") or "").strip()
            field   = (data.get("field") or "").strip()
            value   = data.get("value")
            # normaliza strings
            if isinstance(value, str):
                value = value.strip()

            allowed = ["name","artist","year","language","genre","duration","mood","key","tempo","enabled"]
            if not song_id or field not in allowed:
                return "Parámetros inválidos (id/field). Campos válidos: " + ", ".join(allowed), 400

            # leer CSV actual
            with open(CATALOG_CSV, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f, delimiter=";")
                fieldnames = reader.fieldnames or (["id"] + allowed)
                rows = list(reader)

            # aplicar cambio
            found = False
            for row in rows:
                if (row.get("id") or "").strip() == song_id:
                    row[field] = "" if value is None else str(value)
                    found = True
                    break

            if not found:
                return f"ID no encontrado: {song_id}", 404

            # escribir CSV
            tmp = Path(str(CATALOG_CSV) + ".tmp")
            with open(tmp, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
                w.writeheader()
                for r in rows:
                    w.writerow(r)

            os.replace(tmp, CATALOG_CSV)
            return f"✅ Actualizado '{field}' de '{song_id}'.", 200

        except Exception as e:
            return f"❌ Error al actualizar: {str(e)}", 500

    @app.route("/ran.html")
    def ran():
        return send_from_directory(str(PUBLIC_DIR), "ran.html")

    # ===== RUTAS SONGS (primero Disk, luego repo como fallback) =====
    @app.route("/songs/lyrics/<filename>")
    def serve_lyrics(filename):
        p = LYRICS_DIR / filename
        if p.exists():
            return send_from_directory(str(LYRICS_DIR), filename)
        # fallback a repo
        repo_dir = PROJECT_ROOT / "songs" / "lyrics"
        return send_from_directory(str(repo_dir), filename)

    @app.route("/songs/tabs/<filename>")
    def serve_tab(filename):
        p = TABS_DIR / filename
        if p.exists():
            return send_from_directory(str(TABS_DIR), filename)
        # fallback a repo
        repo_dir = PROJECT_ROOT / "songs" / "tabs"
        return send_from_directory(str(repo_dir), filename)

    # ==== Manifest JSON siempre disponible (aunque no exista archivo físico) ====
    @app.route("/songs/images/artist/manifest.json")
    def artist_manifest_json():
        try:
            data = _load_artist_manifest()   # devuelve {} si no hay archivo
            return jsonify(data)
        except Exception:
            return jsonify({}), 200

    # ==== Reconstruir manifest escaneando imágenes existentes (Disk + Repo) ====
    @app.get("/admin/rebuild-artist-manifest")
    def rebuild_artist_manifest():
        import os
        from collections import defaultdict
        import urllib.parse as _u

        # prioridad de extensiones (mejor → peor)
        ext_priority = {".webp": 5, ".jpg": 4, ".jpeg": 3, ".png": 2, ".bmp": 1}
        allow = set(ext_priority.keys())

        def scan_dir(base):
            found = defaultdict(lambda: ("", -1))  # artista -> (filename, score)
            if not os.path.isdir(base):
                return found
            for name in os.listdir(base):
                p = os.path.join(base, name)
                if not os.path.isfile(p):
                    continue
                root, ext = os.path.splitext(name)
                ext = ext.lower()
                if ext not in allow:
                    continue
                # clave “bonita”: decodifica %20 si existiera
                artist_key = _u.unquote(root)
                score = ext_priority.get(ext, 0)
                # si hay colisión, nos quedamos con el de mayor prioridad
                if score > found[artist_key][1]:
                    found[artist_key] = (name, score)
            return found

        # 1) Escanea disco persistente
        disk_map = scan_dir(str(ARTIST_IMG_DIR))
        # 2) Escanea repo (fallback)
        repo_artist_dir = PROJECT_ROOT / "songs" / "images" / "artist"
        repo_map = scan_dir(str(repo_artist_dir))

        # Fusiona: primero repo, luego sobrescribe disco (o al revés si prefieres)
        merged = dict(repo_map)
        merged.update(disk_map)

        manifest = {}
        for artist_key, (filename, _) in merged.items():
            manifest[artist_key] = _u.quote(filename)  # guardamos filename ya URL-encoded

        _save_artist_manifest(manifest)
        return jsonify({"ok": True, "count": len(manifest)})

    # ==== Manifest de thumbs (JSON) ====
    @app.route("/songs/images/artist_thumbs/manifest.json")
    def artist_thumbs_manifest_json():
        try:
            data = _load_thumbs_manifest()
            return jsonify(data)
        except Exception:
            return jsonify({}), 200

    # ==== Construir/Reconstruir thumbs + manifest a partir de imágenes existentes ====
    @app.get("/tools/build-artist-thumbs")
    def build_artist_thumbs():
        # Origen: imágenes de artista en disco y repo
        sources = []
        sources.append(ARTIST_IMG_DIR)  # backend/core/images/artist
        repo_artist_dir = PROJECT_ROOT / "songs" / "images" / "artist"
        sources.append(repo_artist_dir)

        allow = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
        manifest = {}

        for base in sources:
            if not base.exists():
                continue
            for name in os.listdir(base):
                p = base / name
                if not p.is_file():
                    continue
                root, ext = os.path.splitext(name)
                if ext.lower() not in allow:
                    continue
                # Clave = nombre "bonito" del artista (des-encode si venía %20)
                artist_key = _urlp.unquote(root)
                safe_artist = artist_key.strip().replace("/", "_").replace("\\", "_")
                # Destino thumb (queremos .webp si es posible)
                thumb_base = THUMBS_DIR / safe_artist
                out_name = _make_thumb(p, thumb_base)  # devuelve nombre final
                # Guardar en manifest (nombre ya URL-encoded para usar en URL)
                final_name = out_name or (thumb_base.name + ext.lower())
                manifest[artist_key] = _urlp.quote(final_name)

        _save_thumbs_manifest(manifest)
        return jsonify({"ok": True, "count": len(manifest)})

    @app.route("/songs/images/<path:filename>")
    def serve_images(filename):
        p = IMAGES_DIR / filename
        if p.exists():
            return send_from_directory(str(IMAGES_DIR), filename)
        # fallback a repo
        repo_dir = PROJECT_ROOT / "songs" / "images"
        return send_from_directory(str(repo_dir), filename)

    # ===== API =====
    @app.route("/core/votes.json")
    def votes():
        return send_from_directory(str(CORE_DIR), "votes.json")

    @app.route("/votes/counts.json")
    def vote_counts():
        raw_votes = load_votes()
        counts = count_votes(raw_votes)
        return jsonify(counts)

    @app.route("/votes/reset")
    def reset_votes():
        from backend.services.vote_logic import save_states
        save_votes({})
        save_states({})
        socketio.emit("update", {})
        socketio.emit("session_reset")
        return jsonify({"status": "ok", "message": "Votes reset"})

    @app.route("/songStates.js")
    def song_states_js():
        return send_from_directory(str(PUBLIC_DIR), "songStates.js")

    @app.route("/favicon.ico")
    def favicon():
        return "", 204

    # ======= AÑADIR/ACTUALIZAR CANCIÓN con detección de conflicto =======
    @app.route("/add-song", methods=["POST"])
    def add_song():
        try:
            data = request.get_json(force=True)
            song_id = (data.get("id") or "").strip()
            if not song_id:
                return "ID de canción no especificado", 400

            catalog_csv = CATALOG_CSV

            # ¿Existe ya este ID?
            existing = None
            if catalog_csv.exists():
                with open(catalog_csv, newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f, delimiter=";")
                    for row in reader:
                        if (row.get("id") or "").strip() == song_id:
                            existing = row
                            break

            # Si existe y NO viene 'overwrite', devolvemos conflicto con info (409)
            if existing and not data.get("overwrite"):
                return jsonify({
                    "status": "conflict",
                    "message": f"Ya existe el ID {song_id}",
                    "id": song_id,
                    "existing": {
                        "id": existing.get("id", ""),
                        "name": existing.get("name", ""),
                        "artist": existing.get("artist", "")
                    }
                }), 409

            fieldnames = ["id","name","artist","year","language","genre","duration","mood","key","tempo","enabled"]

            if existing and data.get("overwrite"):
                # Sobrescribir: reescribir el CSV sustituyendo la fila del ID
                with open(catalog_csv, newline="", encoding="utf-8") as f:
                    rows = list(csv.DictReader(f, delimiter=";"))

                # Fila nueva (si algún campo viene vacío, lo dejamos vacío)
                newrow = {k: (data.get(k) if data.get(k) not in [None, ""] else "") for k in fieldnames}
                newrow["id"] = song_id
                if not newrow.get("enabled"):
                    newrow["enabled"] = (existing.get("enabled") or "Y")

                with open(catalog_csv, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
                    w.writeheader()
                    for row in rows:
                        if (row.get("id") or "").strip() == song_id:
                            w.writerow({k: newrow.get(k, "") for k in fieldnames})
                        else:
                            w.writerow({k: row.get(k, "") for k in fieldnames})

                status_msg = "✅ Canción actualizada (sobrescrita)."

            elif not existing:
                # Añadir nueva fila
                with open(catalog_csv, "a", newline="", encoding="utf-8") as csvfile:
                    writer = csv.writer(csvfile, delimiter=";")
                    writer.writerow([
                        data.get("id", ""),
                        data.get("name", ""),
                        data.get("artist", ""),
                        data.get("year", ""),
                        data.get("language", ""),
                        data.get("genre", ""),
                        data.get("duration", ""),
                        data.get("mood", ""),
                        data.get("key", ""),
                        data.get("tempo", ""),
                        "Y",
                    ])
                status_msg = "✅ Canción añadida correctamente."

            else:
                # Caso teórico (existing sin overwrite ya devolvió 409)
                status_msg = "ℹ️ Nada que hacer."

            # Guardar letra/tab SOLO si añadimos o si se sobrescribe
            if not existing or data.get("overwrite"):
                if "lyrics" in data:
                    (LYRICS_DIR / f"{song_id}.txt").parent.mkdir(parents=True, exist_ok=True)
                    with open(LYRICS_DIR / f"{song_id}.txt", "w", encoding="utf-8") as f:
                        f.write((data.get("lyrics") or "").strip())
                if "tab" in data:
                    (TABS_DIR / f"TAB{song_id}.txt").parent.mkdir(parents=True, exist_ok=True)
                    with open(TABS_DIR / f"TAB{song_id}.txt", "w", encoding="utf-8") as f:
                        f.write((data.get("tab") or "").strip())

            return status_msg, 200

        except Exception as e:
            return f"❌ Error al añadir/actualizar canción: {str(e)}", 500

    # ===== VOTING / SOCKETS =====
    @app.route("/votes/now_playing", methods=["POST"])
    def set_now_playing():
        from backend.services.vote_logic import save_states
        data = request.get_json()
        new_id = data.get("songId")
        if not new_id:
            return jsonify({"status": "error", "message": "Missing songId"}), 400

        states = load_states()
        for sid in list(states):
            if states[sid] == "now_playing":
                states[sid] = "played"
        states[new_id] = "now_playing"
        save_states(states)

        votes = load_votes()
        counts = count_votes(votes)
        by_device = {}
        for dev, song_id in votes.items():
            by_device.setdefault(song_id, []).append(dev)

        socketio.emit("update", {
            "counts": counts,
            "byDevice": by_device,
            "states": states
        })
        return jsonify({"status": "ok", "now_playing": new_id})

    @socketio.on("vote")
    def handle_vote(data):
        device = data.get("deviceId")
        song = data.get("songId")
        if not device or not song:
            return
        votes = load_votes()
        votes[device] = song
        save_votes(votes)

        counts = count_votes(votes)
        by_device = {}
        for dev, song_id in votes.items():
            by_device.setdefault(song_id, []).append(dev)
        states = load_states()

        print(">> update:", {"counts": counts, "byDevice": by_device, "states": states})
        socketio.emit("update", {"counts": counts, "byDevice": by_device, "states": states})

    @socketio.on("update_request")
    def handle_update_request():
        votes = load_votes()
        counts = count_votes(votes)
        states = load_states()
        socketio.emit("update", {"counts": counts, "byDevice": votes, "states": states})

    # ===== CATALOGO =====
    @app.route("/refresh-catalog", methods=["POST"])
    def refresh_catalog():
        import subprocess
        try:
            script_path = Path(__file__).resolve().parent / "catalog" / "gen_catalog.py"
            result = subprocess.run(["python", str(script_path)], capture_output=True, text=True)
            if result.returncode == 0:
                return "✅ Catálogo actualizado correctamente."
            else:
                return f"❌ Error al actualizar catálogo:\n{result.stderr}", 500
        except Exception as e:
            return f"❌ Excepción al actualizar catálogo: {str(e)}", 500

    @app.route("/missing-artist-photos")
    def missing_artist_photos():
        try:
            catalog_path = PUBLIC_DIR / "catalog" / "catalog.json"

            with open(catalog_path, "r", encoding="utf-8") as f:
                catalog = json.load(f)

            missing = []
            for song in catalog:
                artist = (song.get("artist") or "").strip()
                if not artist:
                    continue
                # Busca primero en Disk, luego en repo
                has_image = any((ARTIST_IMG_DIR / f"{artist}{ext}").exists() for ext in [".jpg", ".jpeg", ".png"]) \
                            or any((PROJECT_ROOT / "songs" / "images" / "artist" / f"{artist}{ext}").exists()
                                   for ext in [".jpg", ".jpeg", ".png"])
                if not has_image and artist not in missing:
                    missing.append(artist)

            return jsonify(missing)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/edit-catalog")
    def edit_catalog():
        from flask import send_file
        catalog_csv = CATALOG_CSV
        return send_file(str(catalog_csv), as_attachment=False)

    @app.route("/upload-catalog", methods=["POST"])
    def upload_catalog():
        try:
            file = request.files["catalog"]
            if not file:
                return "No se recibió ningún archivo", 400
            # (Ahora mismo: sobrescribe archivo. Si quieres fusión, te paso el bloque cuando me digas)
            save_path = CATALOG_CSV
            file.save(str(save_path))
            return "✅ Catálogo actualizado correctamente."
        except Exception as e:
            return f"❌ Error al subir catálogo: {str(e)}", 500

    @app.route("/download-catalog")
    def download_catalog():
        from flask import send_file
        catalog_csv = CATALOG_CSV
        return send_file(str(catalog_csv), as_attachment=True)

    @app.route("/list-songs")
    def list_songs():
        try:
            catalog_csv = CATALOG_CSV
            songs = []
            with open(catalog_csv, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f, delimiter=";")
                for row in reader:
                    songs.append({"id": row["id"], "title": row["name"], "artist": row["artist"]})
            songs.sort(key=lambda x: x["title"].lower())
            return jsonify(songs)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/delete-songs", methods=["POST"])
    def delete_songs():
        try:
            data = request.get_json(force=True) or {}
            ids = set(data.get("ids") or [])
            if not ids:
                return "No has pasado IDs", 400

            # Rutas base
            BASE_DIR = os.path.dirname(__file__)
            CORE_DIR = os.path.join(BASE_DIR, "core")
            catalog_path = os.path.join(CORE_DIR, "catalog_postgres.csv")

            # 1) Detectar separador y cabecera reales
            with open(catalog_path, "r", encoding="utf-8", newline="") as f:
                header_line = f.readline().strip()
                delim = ";" if header_line.count(";") >= header_line.count(",") else ","
                header = [h.strip() for h in header_line.split(delim)]
                f.seek(0)
                reader = csv.DictReader(f, delimiter=delim)
                rows = []
                for r in reader:
                    # Quitar la clave None (campos extra) si aparece
                    if None in r:
                        r.pop(None, None)
                    # Mantener las filas cuyo id NO esté en la lista a borrar
                    if (r.get("id") or "").strip() not in ids:
                        rows.append(r)

            # 2) Reescribir CSV limpio (ignorando cualquier campo extra)
            with open(catalog_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=header, delimiter=delim, extrasaction="ignore")
                writer.writeheader()
                for r in rows:
                    writer.writerow(r)

            # 3) Borrar archivos públicos TAB/Lyrics
            FRONTEND_DIR = os.path.join(os.path.dirname(BASE_DIR), "frontend")
            PUB_TABS_DIR = os.path.join(FRONTEND_DIR, "public", "songs", "tabs")
            PUB_LYRICS_DIR = os.path.join(FRONTEND_DIR, "public", "songs", "lyrics")
            removed_files = []
            for sid in ids:
                for p in (
                    os.path.join(PUB_TABS_DIR,   f"TAB{sid}.txt"),
                    os.path.join(PUB_LYRICS_DIR, f"{sid}.txt"),
                ):
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                            removed_files.append(os.path.basename(p))
                    except Exception:
                        pass

            return f"OK. {len(ids)} canciones borradas. Archivos eliminados: {', '.join(removed_files) or '—'}"

        except Exception as e:
            print(">> /delete-songs error:", e)
            return f"Error al borrar canciones: {e}", 500

    @app.route("/get-song-status")
    def get_song_status():
        try:
            catalog_csv = CATALOG_CSV
            songs = []
            with open(catalog_csv, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f, delimiter=";")
                for row in reader:
                    songs.append({
                        "id": row["id"],
                        "title": row["name"],
                        "artist": row["artist"],
                        "enabled": (row.get("enabled") or "Y").strip().upper()
                    })
            return jsonify(songs)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/update-enabled", methods=["POST"])
    def update_enabled_status():
        try:
            data = request.get_json()
            updates = data.get("enabled", {})
            if not updates:
                return "No se recibieron datos para actualizar.", 400

            catalog_csv = CATALOG_CSV
            temp_file = Path(str(catalog_csv) + ".tmp")

            with open(catalog_csv, newline="", encoding="utf-8") as infile, open(temp_file, "w", newline="", encoding="utf-8") as outfile:
                reader = csv.DictReader(infile, delimiter=";")
                fieldnames = reader.fieldnames
                writer = csv.DictWriter(outfile, fieldnames=fieldnames, delimiter=";")
                writer.writeheader()
                for row in reader:
                    if row["id"] in updates:
                        row["enabled"] = "Y" if updates[row["id"]] else "N"
                    writer.writerow(row)

            os.replace(temp_file, catalog_csv)
            return "✅ Estado actualizado correctamente."
        except Exception as e:
            return f"❌ Error al actualizar estado: {str(e)}", 500

    @app.route("/catalog/fields.json")
    def catalog_fields():
        try:
            from collections import defaultdict
            csv_path = CATALOG_CSV

            fields = ["artist", "year", "language", "genre", "mood", "key"]
            values = defaultdict(set)
            with open(csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f, delimiter=";")
                for row in reader:
                    for field in fields:
                        val = row.get(field, "").strip()
                        if val:
                            values[field].add(val)
            sorted_fields = {k: sorted(values[k]) for k in fields}
            return jsonify(sorted_fields)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/upload-logo", methods=["POST"])
    def upload_logo():
        try:
            file = request.files.get("logo")
            if not file:
                return "No se recibió ningún archivo", 400
            ext = os.path.splitext(file.filename)[1].lower()
            if ext not in [".png", ".jpg", ".jpeg"]:
                return "Formato no permitido. Usa PNG o JPG.", 400
            save_path = IMAGES_DIR / ("event-logo" + ext)
            file.save(str(save_path))
            return "✅ Logo actualizado correctamente."
        except Exception as e:
            return f"❌ Error al subir logo: {str(e)}", 500

    @app.route("/upload-artist-photo/<artist>", methods=["POST"])
    def upload_artist_photo(artist):
        try:
            if "photo" not in request.files:
                return "No se recibió ningún archivo", 400
            file = request.files["photo"]
            if file.filename == "":
                return "Nombre de archivo vacío", 400

            ext = os.path.splitext(file.filename)[1].lower()
            if ext not in [".jpg", ".jpeg", ".png", ".bmp", ".webp"]:
                return "Formato no permitido", 400

            safe_artist = artist.strip().replace("/", "_").replace("\\", "_")
            original_name = safe_artist + ext
            save_path = ARTIST_IMG_DIR / original_name
            ARTIST_IMG_DIR.mkdir(parents=True, exist_ok=True)
            file.save(str(save_path))

            # Crear thumb y actualizar manifest de thumbs
            created = _make_thumb(save_path, THUMBS_DIR / safe_artist)  # intenta .webp
            final_name = created or original_name  # fallback

            manifest = _load_thumbs_manifest()
            manifest[artist] = _urlp.quote(final_name)
            _save_thumbs_manifest(manifest)

            return "✅ Imagen subida, miniatura creada y manifest actualizado.", 200
        except Exception as e:
            return f"❌ Error al subir imagen: {str(e)}", 500

    return app

# Instance pour Gunicorn
app = create_app()
