import csv
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUT_CSV = PROJECT_ROOT / 'backend/core/catalog_postgres.csv'
OUTPUT_JSON = PROJECT_ROOT / 'frontend/public/catalog/catalog.json'

REQUIRED_FIELDS = {'id', 'name'}
OPTIONAL_FIELDS = {'artist', 'state', 'year', 'language', 'genre'}

def generate_catalog():
    catalog = []

    with INPUT_CSV.open(newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f, delimiter=';')  # Delimitador correcto
        for row in reader:
            if (row.get('enabled') or 'Y').strip().upper() == 'N':
                continue
            if not all(row.get(field) for field in REQUIRED_FIELDS):
                continue

            song = {
                'id': row['id'].strip(),
                'title': row['name'].strip()  # 'name' viene del CSV, 'title' es para el JSON
            }

            for field in OPTIONAL_FIELDS:
                value = row.get(field)
                if value:
                    song[field] = value.strip()

            catalog.append(song)

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_JSON.open('w', encoding='utf-8') as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    print(f'✅ catalog.json generated with {len(catalog)} songs.')

if __name__ == '__main__':
    generate_catalog()