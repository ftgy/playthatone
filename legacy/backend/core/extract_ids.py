import os
import psycopg2

# Configura tu conexión
DB_NAME = "pto"
DB_USER = "postgres"
DB_PASSWORD = "P2727YES!"
DB_HOST = "localhost"
DB_PORT = "5433"

LYRICS_FOLDER = "D:/PTO/songs/lyrics"

def get_existing_ids(cursor):
    cursor.execute("SELECT id FROM songs;")
    return {row[0] for row in cursor.fetchall()}

def insert_new_ids(new_ids, cursor):
    for song_id in new_ids:
        cursor.execute("INSERT INTO songs (id) VALUES (%s) ON CONFLICT DO NOTHING;", (song_id,))

def main():
    conn = psycopg2.connect(
        dbname=DB_NAME, user=DB_USER,
        password=DB_PASSWORD, host=DB_HOST, port=DB_PORT
    )
    cursor = conn.cursor()

    existing_ids = get_existing_ids(cursor)

    all_files = os.listdir(LYRICS_FOLDER)
    new_ids = {
        filename[:-4] for filename in all_files
        if filename.endswith(".txt") and filename[:-4] not in existing_ids
    }

    insert_new_ids(new_ids, cursor)
    conn.commit()
    print(f"{len(new_ids)} new song(s) added.")

    cursor.close()
    conn.close()

if __name__ == "__main__":
    main()
