import os
import json
import logging
import re
import uuid
import urllib.request
import urllib.parse

logger = logging.getLogger(__name__)
from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, Header, Request, Response, Cookie
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from dotenv import load_dotenv

from sqlalchemy import inspect as sa_inspect, text
from database import engine, get_db, Base
from models import Song, Vote
from schemas import SongCreate, SongUpdate, LyricsUpdate, ChordsUpdate, SongOut

load_dotenv()

ADMIN_TOKEN  = os.getenv("ADMIN_TOKEN", "rockNroll2024")

# ── Bootstrap DB ─────────────────────────────────────────────────────────────

def _migrate_db():
    """Ajoute les colonnes manquantes sans toucher aux données existantes."""
    existing = [c["name"] for c in sa_inspect(engine).get_columns("songs")]
    with engine.connect() as conn:
        if "chords" not in existing:
            conn.execute(text("ALTER TABLE songs ADD COLUMN chords TEXT"))
            conn.commit()

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _migrate_db()
    db = next(get_db())
    if db.query(Song).count() == 0:
        seed = [
            Song(title="Johnny B. Goode", artist="Chuck Berry", position=0),
            Song(title="Whole Lotta Love", artist="Led Zeppelin", position=1),
        ]
        db.add_all(seed)
        db.commit()
    db.close()
    yield

app = FastAPI(title="PlayThatOne", lifespan=lifespan)

# ── WebSocket Manager ─────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        message = json.dumps(data)
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.remove(ws)

manager = ConnectionManager()

# ── Helpers ───────────────────────────────────────────────────────────────────

def check_admin(authorization: str = Header(None)):
    if authorization != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(status_code=401, detail="Non autorisé")

def songs_with_votes(db: Session) -> List[dict]:
    songs = db.query(Song).order_by(Song.position).all()
    result = []
    for s in songs:
        count = db.query(func.count(Vote.id)).filter(Vote.song_id == s.id).scalar()
        result.append({
            "id": s.id,
            "title": s.title,
            "artist": s.artist,
            "lyrics": s.lyrics,
            "chords": s.chords,
            "position": s.position,
            "votes": count,
        })
    return result

# ── Public routes ─────────────────────────────────────────────────────────────

@app.get("/songs", response_model=List[SongOut])
def get_songs(db: Session = Depends(get_db)):
    return songs_with_votes(db)


@app.get("/my-votes")
def get_my_votes(db: Session = Depends(get_db), session_id: str = Cookie(default=None)):
    if not session_id:
        return {"voted_ids": []}
    voted = db.query(Vote.song_id).filter(Vote.session_id == session_id).all()
    return {"voted_ids": [v[0] for v in voted]}


@app.post("/vote/{song_id}", status_code=201)
async def vote(
    song_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    session_id: str = Cookie(default=None)
):
    # Créer un cookie de session si absent
    if not session_id:
        session_id = str(uuid.uuid4())

    song = db.query(Song).filter(Song.id == song_id).first()
    if not song:
        raise HTTPException(status_code=404, detail="Canción no encontrada")

    # Vérifier si cette session a déjà voté pour cette chanson spécifique
    existing = db.query(Vote).filter(
        Vote.session_id == session_id,
        Vote.song_id == song_id
    ).first()
    if existing:
        resp = JSONResponse(
            status_code=409,
            content={"ok": False, "already_voted": True, "voted_for": song_id}
        )
        resp.set_cookie("session_id", session_id, max_age=86400 * 30, httponly=True, samesite="lax")
        return resp

    db.add(Vote(song_id=song_id, session_id=session_id))
    db.commit()
    await manager.broadcast({"event": "votes_update", "songs": songs_with_votes(db)})

    resp = JSONResponse(status_code=201, content={"ok": True})
    resp.set_cookie("session_id", session_id, max_age=86400 * 30, httponly=True, samesite="lax")
    return resp


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, db: Session = Depends(get_db)):
    await manager.connect(ws)
    await ws.send_text(json.dumps({"event": "votes_update", "songs": songs_with_votes(db)}))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)

# ── Admin routes ──────────────────────────────────────────────────────────────

@app.get("/admin/songs")
def admin_get_songs(db: Session = Depends(get_db), _=Depends(check_admin)):
    return songs_with_votes(db)


@app.post("/admin/songs", status_code=201)
async def admin_add_song(body: SongCreate, db: Session = Depends(get_db), _=Depends(check_admin)):
    song = Song(**body.model_dump())
    db.add(song)
    db.commit()
    db.refresh(song)
    await manager.broadcast({"event": "votes_update", "songs": songs_with_votes(db)})
    return song


@app.patch("/admin/songs/{song_id}")
async def admin_update_song(song_id: int, body: SongUpdate, db: Session = Depends(get_db), _=Depends(check_admin)):
    song = db.query(Song).filter(Song.id == song_id).first()
    if not song:
        raise HTTPException(status_code=404, detail="Canción no encontrada")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(song, field, value)
    db.commit()
    await manager.broadcast({"event": "votes_update", "songs": songs_with_votes(db)})
    return song


@app.delete("/admin/songs/{song_id}", status_code=204)
async def admin_delete_song(song_id: int, db: Session = Depends(get_db), _=Depends(check_admin)):
    song = db.query(Song).filter(Song.id == song_id).first()
    if not song:
        raise HTTPException(status_code=404, detail="Canción no encontrada")
    db.query(Vote).filter(Vote.song_id == song_id).delete()
    db.delete(song)
    db.commit()
    await manager.broadcast({"event": "votes_update", "songs": songs_with_votes(db)})


@app.post("/admin/songs/{song_id}/chords")
async def admin_update_chords(song_id: int, body: ChordsUpdate, db: Session = Depends(get_db), _=Depends(check_admin)):
    song = db.query(Song).filter(Song.id == song_id).first()
    if not song:
        raise HTTPException(status_code=404, detail="Canción no encontrada")
    song.chords = body.chords
    db.commit()
    await manager.broadcast({"event": "votes_update", "songs": songs_with_votes(db)})
    return {"ok": True}


@app.post("/admin/songs/{song_id}/lyrics")
async def admin_update_lyrics(song_id: int, body: LyricsUpdate, db: Session = Depends(get_db), _=Depends(check_admin)):
    song = db.query(Song).filter(Song.id == song_id).first()
    if not song:
        raise HTTPException(status_code=404, detail="Canción no encontrada")
    song.lyrics = body.lyrics
    db.commit()
    await manager.broadcast({"event": "votes_update", "songs": songs_with_votes(db)})
    return {"ok": True}


@app.post("/admin/reset")
async def admin_reset_votes(db: Session = Depends(get_db), _=Depends(check_admin)):
    db.query(Vote).delete()
    db.commit()
    # Broadcast avec reset_token pour que les clients effacent leur cookie de vote
    reset_token = str(uuid.uuid4())
    await manager.broadcast({
        "event": "votes_reset",
        "reset_token": reset_token,
        "songs": songs_with_votes(db)
    })
    return {"ok": True, "reset_token": reset_token}


@app.get("/admin/stats")
def admin_stats(db: Session = Depends(get_db), _=Depends(check_admin)):
    total_votes = db.query(func.count(Vote.id)).scalar()
    connected   = len(manager.active)
    return {
        "total_votes": total_votes,
        "connected_clients": connected,
        "songs": songs_with_votes(db),
    }





def search_chartlyrics(artist: str, title: str) -> str:
    """Search ChartLyrics API - free, no token needed."""
    try:
        params = urllib.parse.urlencode({"lyricsartist": artist, "lyricssong": title})
        url = f"https://api.chartlyrics.com/apiv1.asmx/SearchLyricDirect?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            xml = r.read().decode("utf-8", errors="ignore")
        # Extract lyrics from XML
        match = re.search("<Lyric>(.*?)</Lyric>", xml, re.DOTALL)
        if match:
            lyrics = match.group(1).strip()
            if len(lyrics) > 50:
                return lyrics
    except Exception:
        pass
    return ""


def search_chartlyrics_list(query: str) -> list:
    """Search ChartLyrics for song suggestions."""
    try:
        params = urllib.parse.urlencode({"lyricText": query})
        url = f"https://api.chartlyrics.com/apiv1.asmx/SearchLyric?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            xml = r.read().decode("utf-8", errors="ignore")
        results = []
        artists = re.findall("<Artist>(.*?)</Artist>", xml)
        songs_found = re.findall("<Song>(.*?)</Song>", xml)
        ids = re.findall("<LyricId>(.*?)</LyricId>", xml)
        for i in range(min(len(artists), len(songs_found), len(ids), 8)):
            if artists[i] and songs_found[i] and ids[i]:
                results.append({
                    "title": songs_found[i].strip(),
                    "artist": artists[i].strip(),
                    "chartlyrics_id": ids[i].strip(),
                })
        return results[:8]
    except Exception:
        return []




# ── Ultimate Guitar (chords) ─────────────────────────────────────────────────

UG_HEADERS = {
    "User-Agent": "UGT_ANDROID/5.10.12 (10)",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

def ug_search(artist: str, title: str) -> list:
    """Recherche les chord tabs sur Ultimate Guitar.

    Returns:
        list: résultats trouvés.
    Raises:
        RuntimeError: si l'API UG est injoignable ou renvoie une erreur.
    """
    query = urllib.parse.quote(f"{artist} {title}")
    url = (
        f"https://api.ultimate-guitar.com/api/v1/tab/search"
        f"?q={query}&type[]=Chords&page=1&official[]=0"
    )
    req = urllib.request.Request(url, headers=UG_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode("utf-8")
            data = json.loads(raw)
    except Exception as exc:
        logger.error("UG API error for '%s %s': %s", artist, title, exc)
        raise RuntimeError(f"UG API injoignable : {exc}") from exc
    tabs = data.get("tabs", [])
    logger.info(
        "UG search '%s %s' → %d tabs, types: %s",
        artist, title, len(tabs),
        list({t.get("type") for t in tabs})
    )
    results = []
    for t in tabs[:8]:
        if t.get("type") == "Chords":
            results.append({
                "id": t.get("id"),
                "song_name": t.get("song_name", ""),
                "artist_name": t.get("artist_name", ""),
                "rating": t.get("rating", 0),
                "votes": t.get("votes", 0),
                "tonality": t.get("tonality_name", ""),
                "difficulty": t.get("difficulty", ""),
            })
    logger.info("UG search '%s %s' → %d chords results", artist, title, len(results))
    return results


def ug_fetch_tab(tab_id: int) -> str:
    """Récupère le contenu d'un tab UG et nettoie le format propriétaire."""
    try:
        url = f"https://api.ultimate-guitar.com/api/v1/tab/info?id={tab_id}"
        req = urllib.request.Request(url, headers=UG_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        content = (
            data.get("tab_view", {})
                .get("wiki_tab", {})
                .get("content", "")
        )
        if not content:
            return ""
        # Nettoyer le format UG : [ch]Am[/ch] → Am, [tab]...[/tab] → ...
        content = re.sub(r"\[ch\](.*?)\[/ch\]", r"\1", content)
        content = re.sub(r"\[tab\]|\[/tab\]", "", content)
        content = re.sub(r"\[/?[a-z]+\]", "", content)  # autres balises éventuelles
        return content.strip()
    except Exception:
        return ""


@app.get("/admin/chords/search")
def chords_search(artist: str, title: str, _=Depends(check_admin)):
    if not artist or not title:
        raise HTTPException(status_code=400, detail="artist et title requis")
    try:
        results = ug_search(artist, title)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if not results:
        raise HTTPException(status_code=404, detail="Aucun résultat sur Ultimate Guitar")
    return results


@app.get("/admin/chords/fetch")
def chords_fetch(tab_id: int, _=Depends(check_admin)):
    content = ug_fetch_tab(tab_id)
    if not content:
        raise HTTPException(status_code=404, detail="Contenu introuvable")
    return {"chords": content}


# ── ChartLyrics API (free fallback) ──────────────────────────────────────────

@app.get("/admin/chartlyrics/search")
def chartlyrics_search(q: str, _=Depends(check_admin)):
    if not q or len(q) < 2:
        return []
    results = search_chartlyrics_list(q)
    if not results:
        raise HTTPException(status_code=404, detail="Sin resultados en ChartLyrics")
    return results


@app.get("/admin/chartlyrics/lyrics")
def chartlyrics_lyrics(artist: str, title: str, _=Depends(check_admin)):
    lyrics = search_chartlyrics(artist, title)
    if not lyrics:
        raise HTTPException(status_code=404, detail="Paroles introuvables sur ChartLyrics")
    return {"lyrics": lyrics}


# ── Static / SPA ──────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root():
    return FileResponse("static/index.html")

@app.get("/admin-panel")
def admin_panel():
    return FileResponse("static/admin.html")
