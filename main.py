import os
import html
import json
import logging
import re
import time
import unicodedata
import uuid
import urllib.request
import urllib.parse

logger = logging.getLogger(__name__)
from collections import defaultdict
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
from models import Song, Vote, Proposal
from schemas import SongCreate, SongUpdate, LyricsUpdate, ChordsUpdate, SongOut, ProposalCreate

load_dotenv()

ADMIN_TOKEN  = os.getenv("ADMIN_TOKEN", "rockNroll2024")
# Votes each audience member can have on songs not yet played (a vote comes back
# when its song is played)
VOTES_PER_SESSION = int(os.getenv("VOTES_PER_SESSION", "3"))
# DISABLE_AUTH=1: admin routes are open (local dev only)
AUTH_DISABLED = os.getenv("DISABLE_AUTH") == "1"
if AUTH_DISABLED:
    logger.warning("DISABLE_AUTH=1 — admin authentication disabled")

# ── Bootstrap DB ─────────────────────────────────────────────────────────────

def _migrate_db():
    """Add missing columns without touching existing data."""
    existing = [c["name"] for c in sa_inspect(engine).get_columns("songs")]
    with engine.connect() as conn:
        if "chords" not in existing:
            conn.execute(text("ALTER TABLE songs ADD COLUMN chords TEXT"))
            conn.commit()
        if "state" not in existing:
            conn.execute(text("ALTER TABLE songs ADD COLUMN state VARCHAR(20)"))
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
        # Iterate over a copy: a client can disconnect (and remove itself) mid-broadcast
        for ws in list(self.active):
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

manager = ConnectionManager()

# ── Helpers ───────────────────────────────────────────────────────────────────

def check_admin(authorization: str = Header(None)):
    if AUTH_DISABLED:
        return
    if authorization != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")

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
            "state": s.state,
            "votes": count,
        })
    return result

# ── Public routes ─────────────────────────────────────────────────────────────

@app.get("/songs", response_model=List[SongOut])
def get_songs(db: Session = Depends(get_db)):
    return songs_with_votes(db)


def active_vote_count(db: Session, session_id: str) -> int:
    """Votes of this session on songs not yet played (the ones that count toward the limit)."""
    return (
        db.query(func.count(Vote.id))
          .join(Song, Song.id == Vote.song_id)
          .filter(Vote.session_id == session_id, Song.state.is_(None))
          .scalar()
    )


@app.get("/my-votes")
def get_my_votes(db: Session = Depends(get_db), session_id: str = Cookie(default=None)):
    if not session_id:
        return {"voted_ids": [], "limit": VOTES_PER_SESSION}
    voted = db.query(Vote.song_id).filter(Vote.session_id == session_id).all()
    return {"voted_ids": [v[0] for v in voted], "limit": VOTES_PER_SESSION}


@app.post("/vote/{song_id}", status_code=201)
async def vote(
    song_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    session_id: str = Cookie(default=None)
):
    # Create a session cookie if missing
    if not session_id:
        session_id = str(uuid.uuid4())

    song = db.query(Song).filter(Song.id == song_id).first()
    if not song:
        raise HTTPException(status_code=404, detail="Canción no encontrada")
    if song.state in ("now_playing", "played"):
        raise HTTPException(status_code=409, detail="Canción ya tocada")

    # Check whether this session has already voted for this specific song
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

    if active_vote_count(db, session_id) >= VOTES_PER_SESSION:
        resp = JSONResponse(
            status_code=409,
            content={"ok": False, "limit_reached": True, "limit": VOTES_PER_SESSION}
        )
        resp.set_cookie("session_id", session_id, max_age=86400 * 30, httponly=True, samesite="lax")
        return resp

    db.add(Vote(song_id=song_id, session_id=session_id))
    db.commit()
    await manager.broadcast({"event": "votes_update", "songs": songs_with_votes(db)})

    resp = JSONResponse(status_code=201, content={"ok": True})
    resp.set_cookie("session_id", session_id, max_age=86400 * 30, httponly=True, samesite="lax")
    return resp


@app.delete("/vote/{song_id}", status_code=204)
async def unvote(song_id: int, db: Session = Depends(get_db), session_id: str = Cookie(default=None)):
    """Take a vote back (only while the song has not been played)."""
    if not session_id:
        raise HTTPException(status_code=404, detail="No vote to remove")
    song = db.query(Song).filter(Song.id == song_id).first()
    if not song:
        raise HTTPException(status_code=404, detail="Canción no encontrada")
    if song.state in ("now_playing", "played"):
        raise HTTPException(status_code=409, detail="Canción ya tocada")
    vote = db.query(Vote).filter(Vote.session_id == session_id, Vote.song_id == song_id).first()
    if not vote:
        raise HTTPException(status_code=404, detail="No vote to remove")
    db.delete(vote)
    db.commit()
    await manager.broadcast({"event": "votes_update", "songs": songs_with_votes(db)})
    return Response(status_code=204)


_proposal_last_ts = defaultdict(float)   # IP → last submission (anti-spam)

def _slug_title(s: str) -> str:
    s = unicodedata.normalize("NFD", s.strip().lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:200] or "untitled"


@app.post("/proposals", status_code=201)
async def create_proposal(body: ProposalCreate, request: Request, db: Session = Depends(get_db)):
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Título vacío")

    # 1 suggestion / 30 s per IP
    ip = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip() or request.client.host
    now = time.time()
    if now - _proposal_last_ts[ip] < 30:
        raise HTTPException(status_code=429, detail="Espera un poco antes de sugerir otra")
    _proposal_last_ts[ip] = now

    # Same title already suggested → increment its count
    slug = _slug_title(title)
    proposal = db.query(Proposal).filter(Proposal.slug == slug).first()
    if proposal:
        proposal.count += 1
    else:
        proposal = Proposal(title=title, slug=slug, count=1)
        db.add(proposal)
    db.commit()
    # Content-free signal: the interpreter view reloads the list with its token
    await manager.broadcast({"event": "proposals_update"})
    return {"ok": True}


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
    # Broadcast with reset_token so clients clear their vote cookie
    reset_token = str(uuid.uuid4())
    await manager.broadcast({
        "event": "votes_reset",
        "reset_token": reset_token,
        "songs": songs_with_votes(db)
    })
    return {"ok": True, "reset_token": reset_token}


# ── Now playing ───────────────────────────────────────────────────────────────

def _finish_now_playing(db: Session):
    """The song currently playing becomes "played"."""
    db.query(Song).filter(Song.state == "now_playing").update({"state": "played"})


@app.post("/admin/songs/{song_id}/now-playing")
async def admin_set_now_playing(song_id: int, db: Session = Depends(get_db), _=Depends(check_admin)):
    song = db.query(Song).filter(Song.id == song_id).first()
    if not song:
        raise HTTPException(status_code=404, detail="Canción no encontrada")
    _finish_now_playing(db)
    song.state = "now_playing"
    db.commit()
    await manager.broadcast({"event": "now_playing", "songs": songs_with_votes(db)})
    return {"ok": True}


@app.post("/admin/now-playing/stop")
async def admin_stop_now_playing(db: Session = Depends(get_db), _=Depends(check_admin)):
    _finish_now_playing(db)
    db.commit()
    await manager.broadcast({"event": "now_playing", "songs": songs_with_votes(db)})
    return {"ok": True}


@app.post("/admin/played/reset")
async def admin_reset_played(db: Session = Depends(get_db), _=Depends(check_admin)):
    """New show: every song can be voted on again."""
    db.query(Song).update({"state": None})
    db.commit()
    await manager.broadcast({"event": "now_playing", "songs": songs_with_votes(db)})
    return {"ok": True}


# ── Proposals (admin) ─────────────────────────────────────────────────────────

@app.get("/admin/proposals")
def admin_get_proposals(db: Session = Depends(get_db), _=Depends(check_admin)):
    proposals = db.query(Proposal).order_by(Proposal.count.desc(), Proposal.created_at).all()
    return [{"id": p.id, "title": p.title, "count": p.count} for p in proposals]


@app.delete("/admin/proposals/{proposal_id}", status_code=204)
async def admin_delete_proposal(proposal_id: int, db: Session = Depends(get_db), _=Depends(check_admin)):
    db.query(Proposal).filter(Proposal.id == proposal_id).delete()
    db.commit()
    await manager.broadcast({"event": "proposals_update"})


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

# The mobile API (api.ultimate-guitar.com) now rejects requests, so we read the
# JSON that the website embeds in its pages (<div class="js-store" data-content=...>).
UG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
UG_TAB_URL = re.compile(r"^https://tabs\.ultimate-guitar\.com/tab/[\w\-/]+$")


def _ug_page_data(url: str) -> dict:
    req = urllib.request.Request(url, headers=UG_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        page = r.read().decode("utf-8")
    m = re.search(r'class="js-store" data-content="([^"]*)"', page)
    if not m:
        raise RuntimeError("UG page format changed (no js-store)")
    return json.loads(html.unescape(m.group(1)))["store"]["page"]["data"]


def ug_search(artist: str, title: str) -> list:
    """Search Ultimate Guitar for chord tabs, most voted first.

    Returns:
        list: results found.
    Raises:
        RuntimeError: if UG is unreachable or its page format changed.
    """
    query = urllib.parse.urlencode({"search_type": "title", "value": f"{artist} {title}"})
    try:
        data = _ug_page_data(f"https://www.ultimate-guitar.com/search.php?{query}")
    except Exception as exc:
        logger.error("UG search error for '%s %s': %s", artist, title, exc)
        raise RuntimeError(f"Ultimate Guitar unreachable: {exc}") from exc
    tabs = [t for t in data.get("results", []) if t.get("type") == "Chords"]
    tabs.sort(key=lambda t: t.get("votes") or 0, reverse=True)
    results = [{
        "id": t.get("id"),
        "url": t.get("tab_url", ""),
        "song_name": t.get("song_name", ""),
        "artist_name": t.get("artist_name", ""),
        "rating": t.get("rating", 0),
        "votes": t.get("votes", 0),
        "tonality": t.get("tonality_name", ""),
        "difficulty": t.get("difficulty", ""),
    } for t in tabs[:8]]
    logger.info("UG search '%s %s' → %d chords results", artist, title, len(results))
    return results


def ug_fetch_tab(url: str) -> str:
    """Fetch a UG tab page's content and clean up its proprietary format."""
    try:
        data = _ug_page_data(url)
        content = data.get("tab_view", {}).get("wiki_tab", {}).get("content", "")
    except Exception as exc:
        logger.error("UG fetch error for %s: %s", url, exc)
        return ""
    # Clean up the UG format: [ch]Am[/ch] → Am, [tab]...[/tab] → ...
    content = content.replace("\r\n", "\n")
    content = re.sub(r"\[ch\](.*?)\[/ch\]", r"\1", content)
    content = re.sub(r"\[/?tab\]", "", content)
    return content.strip()


@app.get("/admin/chords/search")
def chords_search(artist: str, title: str, _=Depends(check_admin)):
    if not artist or not title:
        raise HTTPException(status_code=400, detail="artist and title are required")
    try:
        results = ug_search(artist, title)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if not results:
        raise HTTPException(status_code=404, detail="No results on Ultimate Guitar")
    return results


@app.get("/admin/chords/top")
def chords_top(page: int = 1, _=Depends(check_admin)):
    """UG's most viewed chord tabs of all time, 50 per page."""
    try:
        data = _ug_page_data(f"https://www.ultimate-guitar.com/explore?type[]=Chords&order=hitstotal_desc&page={page}")
    except Exception as exc:
        logger.error("UG explore error (page %d): %s", page, exc)
        raise HTTPException(status_code=502, detail=f"Ultimate Guitar unreachable: {exc}")
    return [{
        "id": t.get("id"),
        "url": t.get("tab_url", ""),
        "song_name": t.get("song_name", ""),
        "artist_name": t.get("artist_name", ""),
        "votes": t.get("votes", 0),
    } for t in data.get("data", {}).get("tabs", []) if t.get("type") == "Chords"]


@app.get("/admin/chords/fetch")
def chords_fetch(url: str, _=Depends(check_admin)):
    # Only UG tab pages: this endpoint must not fetch arbitrary URLs
    if not UG_TAB_URL.match(url):
        raise HTTPException(status_code=400, detail="Not an Ultimate Guitar tab URL")
    content = ug_fetch_tab(url)
    if not content:
        raise HTTPException(status_code=404, detail="Content not found")
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
        raise HTTPException(status_code=404, detail="Lyrics not found on ChartLyrics")
    return {"lyrics": lyrics}


# ── Static / SPA ──────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root():
    return FileResponse("static/index.html")

@app.get("/admin-panel")
def admin_panel():
    return FileResponse("static/admin.html")

@app.get("/interpreter")
def interpreter_view():
    return FileResponse("static/interpreter.html")
