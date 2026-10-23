import psycopg2
from collections import defaultdict

# Configuración
DB_NAME = "pto"
DB_USER = "postgres"
DB_PASSWORD = "P2727YES!"
DB_HOST = "localhost"
DB_PORT = "5433"

ALLOWED_FIELDS = [
    "artist", "language", "genre", "mood"
]

def get_grouped_songs(group_by="artist"):
    if group_by not in ALLOWED_FIELDS:
        raise ValueError(f"Invalid group field: {group_by}")

    conn = psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT
    )
    cursor = conn.cursor()

    cursor.execute(f"SELECT {group_by}, name FROM songs WHERE {group_by} IS NOT NULL ORDER BY {group_by}, name;")
    results = cursor.fetchall()
    cursor.close()
    conn.close()

    grouped = defaultdict(list)
    for group, name in results:
        grouped[group].append(name)

    return grouped

if __name__ == "__main__":
    group_field = input("Group by (artist/language/genre/mood): ").strip() or "artist"
    try:
        grouped_songs = get_grouped_songs(group_field)
        for group, songs in grouped_songs.items():
            print(f"{group}:")
            for title in songs:
                print(f"  {title}")
            print()
    except ValueError as e:
        print(f"Error: {e}")
