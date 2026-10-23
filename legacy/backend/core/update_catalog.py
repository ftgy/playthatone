import csv
import psycopg2

# Configuración de conexión
DB_NAME = "pto"
DB_USER = "postgres"
DB_PASSWORD = "P2727YES!"
DB_HOST = "localhost"
DB_PORT = "5433"

CSV_PATH = "D:/PTO/backend/core/catalog_postgres.csv"

def upsert_song(row, cursor):
    cursor.execute("""
        INSERT INTO songs (id, name, artist, year, language, genre, popularity, duration, mood, key, tempo)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            name = COALESCE(EXCLUDED.name, songs.name),
            artist = COALESCE(EXCLUDED.artist, songs.artist),
            year = COALESCE(EXCLUDED.year, songs.year),
            language = COALESCE(EXCLUDED.language, songs.language),
            genre = COALESCE(EXCLUDED.genre, songs.genre),
            popularity = COALESCE(EXCLUDED.popularity, songs.popularity),
            duration = COALESCE(EXCLUDED.duration, songs.duration),
            mood = COALESCE(EXCLUDED.mood, songs.mood),
            key = COALESCE(EXCLUDED.key, songs.key),
            tempo = COALESCE(EXCLUDED.tempo, songs.tempo);
    """, row)

def main():
    conn = psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT
    )
    cursor = conn.cursor()

    with open(CSV_PATH, newline='', encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile, delimiter=';')
        for raw_row in reader:
            row = {k.lower(): v.strip() if isinstance(v, str) else v for k, v in raw_row.items()}
            values = (
                row.get("id"),
                row.get("name") or None,
                row.get("artist") or None,
                int(row["year"]) if row.get("year") else None,
                row.get("language") or None,
                row.get("genre") or None,
                int(row["popularity"]) if row.get("popularity") else None,
                int(row["duration"]) if row.get("duration") else None,
                row.get("mood") or None,
                row.get("key") or None,
                row.get("tempo") or None,
            )
            upsert_song(values, cursor)

    conn.commit()
    cursor.close()
    conn.close()
    print("Catalog updated successfully.")

if __name__ == "__main__":
    main()
