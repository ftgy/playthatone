from flask import Flask, request
import os
import csv

app = Flask(__name__)

# Rutas fijas según estructura real del proyecto
LYRICS_DIR = "./songs/lyrics"
TABS_DIR = "./songs/tabs"
CATALOG_CSV = "./backend/core/catalog_postgres.csv"

@app.route("/add-song", methods=["POST"])
def add_song():
    data = request.get_json()
    song_id = data.get("id", "").strip()

    if not song_id:
        return "ID de canción no especificado", 400

    lyrics_path = os.path.join(LYRICS_DIR, f"{song_id}.txt")
    if os.path.exists(lyrics_path):
        return "Ya existe una canción con ese ID.", 400

    # Crear archivos
    os.makedirs(LYRICS_DIR, exist_ok=True)
    os.makedirs(TABS_DIR, exist_ok=True)
    with open(lyrics_path, "w", encoding="utf-8") as f:
        f.write(data.get("lyrics", "").strip())

    with open(os.path.join(TABS_DIR, f"TAB{song_id}.txt"), "w", encoding="utf-8") as f:
        f.write(data.get("tab", "").strip())

    # Añadir entrada al CSV
    fields = [
        data.get("id", ""),
        data.get("name", ""),
        data.get("artist", ""),
        data.get("year", ""),
        data.get("language", ""),
        data.get("genre", ""),
        data.get("duration", ""),
        data.get("mood", ""),
        data.get("key", ""),
        data.get("tempo", "")
    ]

    os.makedirs(os.path.dirname(CATALOG_CSV), exist_ok=True)
    with open(CATALOG_CSV, "a", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(fields)

    return "Canción añadida correctamente."

if __name__ == "__main__":
    app.run(debug=True)