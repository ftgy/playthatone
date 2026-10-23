"""Download a photo for each catalog artist that has none in static/images/artist/.

Usage:
    python fetch_artist_photos.py              # artists without a photo
    python fetch_artist_photos.py --dry-run    # print without downloading

Options:
    --base-url URL   app to read the catalog from (default http://127.0.0.1:8080)

Source: Deezer's artist picture, only when Deezer's artist name matches exactly.
Photos are saved as "<artist>.jpg" (the name the audience page looks for),
resized to 300x300 with ImageMagick's `convert` when it is installed.
"""
import argparse
import os
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

from fetch_lyrics import USER_AGENT, http_json, norm

PHOTO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "images", "artist")

# Artists Deezer lists under another name
DEEZER_NAMES = {
    "Bob Marley": "Bob Marley & The Wailers",
    "Cat Stevens": "Yusuf / Cat Stevens",
}


def deezer_picture(artist):
    artist = DEEZER_NAMES.get(artist, artist)
    q = urllib.parse.urlencode({"q": artist, "limit": 10})
    # Exact name only: avoids joint credits ("Elvis Presley, Chuck Berry, ...")
    # and tribute acts; an empty hash ("/artist//") is Deezer's placeholder.
    # Several acts can share a name ("Queen"): take the one with the most fans.
    hits = [h for h in http_json("https://api.deezer.com/search/artist?" + q).get("data", [])
            if norm(h["name"]) == norm(artist) and "/artist//" not in h.get("picture_big", "/artist//")]
    return max(hits, key=lambda h: h.get("nb_fan", 0))["picture_big"] if hits else None


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as res, open(dest, "wb") as f:
        if not res.headers.get("Content-Type", "").startswith("image/jpeg"):
            raise ValueError(f"not a JPEG ({res.headers.get('Content-Type')})")
        shutil.copyfileobj(res, f)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    artists = sorted({s["artist"] for s in http_json(args.base_url.rstrip("/") + "/songs")})
    missing = [a for a in artists if not os.path.exists(os.path.join(PHOTO_DIR, a + ".jpg"))]
    has_convert = shutil.which("convert") is not None
    print(f"== {len(missing)} of {len(artists)} artists without a photo")
    found = 0
    with tempfile.TemporaryDirectory() as tmp:
        for artist in missing:
            if "/" in artist:
                print(f"  ✗ {artist}: '/' can't be in a file name")
                continue
            time.sleep(0.3)   # be gentle with Deezer
            try:
                url = deezer_picture(artist)
            except Exception as exc:
                print(f"  ✗ {artist}: error ({exc})")
                continue
            if not url:
                print(f"  ✗ {artist}: not found")
                continue
            found += 1
            print(f"  ✓ {artist}")
            if args.dry_run:
                continue
            raw, dest = os.path.join(tmp, "photo.jpg"), os.path.join(PHOTO_DIR, artist + ".jpg")
            try:
                download(url, raw)
                if has_convert:
                    subprocess.run(["convert", raw, "-strip", "-resize", "300x300", "-quality", "82", dest], check=True)
                else:
                    shutil.copy(raw, dest)
            except Exception as exc:
                print(f"    (download failed: {exc})")
                found -= 1
    print(f"\n{'(dry-run) ' if args.dry_run else ''}photos: {found}, missing: {len(missing) - found}")


if __name__ == "__main__":
    main()
