import json
import sys
from pathlib import Path

CATALOG_PATH = Path('frontend/public/catalog/catalog.json')
VALID_STATES = {'now_playing', 'played', 'deselected'}

def update_state(song_id, new_state):
    if new_state not in VALID_STATES:
        print(f"❌ Invalid state: {new_state}")
        return

    with CATALOG_PATH.open(encoding='utf-8') as f:
        catalog = json.load(f)

    updated = False
    for song in catalog:
        if song.get('id') == song_id:
            if new_state == 'deselected':
                song.pop('state', None)
            else:
                song['state'] = new_state
            updated = True
            break

    if not updated:
        print(f"❌ Song with id '{song_id}' not found.")
        return

    with CATALOG_PATH.open('w', encoding='utf-8') as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    print(f"✅ State of '{song_id}' updated to '{new_state}'.")

def reset_all_states():
    with CATALOG_PATH.open(encoding='utf-8') as f:
        catalog = json.load(f)

    for song in catalog:
        song.pop('state', None)

    with CATALOG_PATH.open('w', encoding='utf-8') as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    print(f"✅ All song states reset.")

if __name__ == '__main__':
    if len(sys.argv) == 2 and sys.argv[1] == 'reset':
        reset_all_states()
    elif len(sys.argv) == 3:
        update_state(sys.argv[1], sys.argv[2])
    else:
        print("Usage:")
        print("  python update_states.py <song_id> <state>")
        print("  python update_states.py reset")
