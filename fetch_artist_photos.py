"""Download a photo for each catalog artist that has none in static/images/artist/.

Usage:
    python fetch_artist_photos.py              # artists without a photo
    python fetch_artist_photos.py --dry-run    # print without downloading

Options:
    --base-url URL   app to read the catalog from (default http://127.0.0.1:8080)

Source: Deezer's artist picture (see enrich.py), saved as "<artist>.jpg" (the name
the audience page looks for). Songs added in the admin panel get their artist's
photo automatically; this is for catching up.
"""
import argparse
import os

from enrich import deezer_picture, http_json, photo_path, save_artist_photo


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    artists = sorted({s["artist"] for s in http_json(args.base_url.rstrip("/") + "/songs")})
    missing = [a for a in artists if not (photo_path(a) and os.path.exists(photo_path(a)))]
    print(f"== {len(missing)} of {len(artists)} artists without a photo")
    found = 0
    for artist in missing:
        if not photo_path(artist):
            print(f"  ✗ {artist}: can't be a file name")
            continue
        try:
            ok = bool(deezer_picture(artist)) if args.dry_run else save_artist_photo(artist)
        except Exception as exc:
            print(f"  ✗ {artist}: error ({exc})")
            continue
        print(f"  {'✓' if ok else '✗'} {artist}{'' if ok else ': not found'}")
        found += ok
    print(f"\n{'(dry-run) ' if args.dry_run else ''}photos: {found}, missing: {len(missing) - found}")


if __name__ == "__main__":
    main()
