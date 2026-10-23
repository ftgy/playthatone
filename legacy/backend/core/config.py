# backend/core/config.py
import os, json

# Ruta del disk en Render (Starter). Si no existe, usa la carpeta local.
DEFAULT_DISK_CORE = "/opt/render/project/src/backend/core"
CORE_DIR = os.environ.get("CORE_DIR", DEFAULT_DISK_CORE)
if not os.path.isdir(CORE_DIR):
    CORE_DIR = os.path.dirname(__file__)  # fallback local
os.makedirs(CORE_DIR, exist_ok=True)

# Rutas usadas por el resto del código
VOTES_FILE = os.path.join(CORE_DIR, "votes.json")
SONG_STATES_FILE = os.path.join(CORE_DIR, "song_states.json")

# Inicializa si no existen (seguro para re-deploys)
def _init(path, default):
    try:
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default, f)
    except Exception:
        pass

_init(VOTES_FILE, {})
_init(SONG_STATES_FILE, {"now_playing": None, "played": []})

print(f"[config] CORE_DIR={CORE_DIR}", flush=True)
print(f"[config] VOTES_FILE={VOTES_FILE}", flush=True)
print(f"[config] SONG_STATES_FILE={SONG_STATES_FILE}", flush=True)
