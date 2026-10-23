import psycopg2

# Configuración
DB_NAME = "pto"
DB_USER = "postgres"
DB_PASSWORD = "tu_contraseña"
DB_HOST = "localhost"
DB_PORT = "5433"

def detect_encoding_issues(order_by="artist"):
    conn = psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT
    )
    cursor = conn.cursor()

    cursor.execute(f"SELECT * FROM songs ORDER BY {order_by} NULLS LAST;")
    results = cursor.fetchall()
    columns = [desc[0] for desc in cursor.description]

    cursor.close()
    conn.close()

    for row in results:
        record = dict(zip(columns, row))
        try:
            line = f"{record['id']} → {record.get(order_by)}"
            print(line)
        except UnicodeDecodeError as e:
            print(f"[ERROR] id: {record['id']} caused: {e}")

if __name__ == "__main__":
    detect_encoding_issues()
