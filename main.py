import asyncio
import os
import string
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

from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, Header, Request, Response, Cookie, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from dotenv import load_dotenv

from sqlalchemy import inspect as sa_inspect, text
from database import engine, get_db, Base, SessionLocal
import enrich
from enrich import UG_TAB_URL, ug_search, ug_fetch_tab
from models import Song, Vote, Proposal
from schemas import SongCreate, SongUpdate, LyricsUpdate, ChordsUpdate, SongOut, ProposalCreate

load_dotenv()

ADMIN_TOKEN  = os.getenv("ADMIN_TOKEN", "rockNroll2024")
# Votes each audience member can have on songs not yet played (a vote comes back
# when its song is played)
VOTES_PER_SESSION = int(os.getenv("VOTES_PER_SESSION", "3"))
# Suggestions: minimum similarity (0-1) of both title and artist to treat a
# misspelled suggestion as a catalog song, an earlier suggestion or a known song
SUGGESTION_SIMILARITY = float(os.getenv("SUGGESTION_SIMILARITY", "0.8"))
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
        for col, sql_type in (("genre", "VARCHAR(50)"), ("year", "INTEGER"), ("language", "VARCHAR(30)")):
            if col not in existing:
                conn.execute(text(f"ALTER TABLE songs ADD COLUMN {col} {sql_type}"))
                conn.commit()
        proposal_cols = [c["name"] for c in sa_inspect(engine).get_columns("proposals")]
        for col, sql_type in (("artist", "VARCHAR(200)"), ("typed", "VARCHAR(400)"), ("similarity", "INTEGER")):
            if col not in proposal_cols:
                conn.execute(text(f"ALTER TABLE proposals ADD COLUMN {col} {sql_type}"))
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
            "genre": s.genre,
            "year": s.year,
            "language": s.language,
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
    title, artist = body.title.strip(), body.artist.strip()
    if not title or not artist:
        raise HTTPException(status_code=400, detail="Título y artista requeridos")

    def in_catalog(t, a):
        song, _ = enrich.best_match(t, a, ((s.title, s.artist, s) for s in db.query(Song)), SUGGESTION_SIMILARITY)
        return song

    # Already in the catalog, even misspelled ("wonderwal / oasys"): vote for it instead
    song = in_catalog(title, artist)
    if song:
        return {"ok": True, "in_catalog": True, "song_id": song.id, "title": song.title, "artist": song.artist}

    # 1 suggestion / 30 s per IP
    ip = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip() or request.client.host
    now = time.time()
    if now - _proposal_last_ts[ip] < 30:
        raise HTTPException(status_code=429, detail="Espera un poco antes de sugerir otra")
    _proposal_last_ts[ip] = now

    # Fix the spelling with a music catalog (iTunes, Deezer) when it is close enough
    typed, similarity = None, None
    corrected = await asyncio.to_thread(enrich.correct_spelling, title, artist, SUGGESTION_SIMILARITY)
    if corrected and (corrected[0], corrected[1]) != (title, artist):
        typed, similarity = f"{title} — {artist}", round(corrected[2] * 100)
        title, artist = corrected[0], corrected[1]
        song = in_catalog(title, artist)
        if song:
            return {"ok": True, "in_catalog": True, "song_id": song.id, "title": song.title, "artist": song.artist}

    # Same song already suggested (exactly or nearly) → increment its count
    proposal, _ = enrich.best_match(title, artist, ((p.title, p.artist, p) for p in db.query(Proposal)),
                                    SUGGESTION_SIMILARITY)
    if proposal:
        proposal.count += 1
    else:
        slug = _slug_title(title) + "--" + _slug_title(artist)
        proposal = Proposal(title=title, artist=artist, slug=slug, count=1, typed=typed, similarity=similarity)
        db.add(proposal)
    db.commit()
    # Content-free signal: the interpreter view reloads the list with its token
    await manager.broadcast({"event": "proposals_update"})
    return {"ok": True, "title": proposal.title, "artist": proposal.artist, "corrected": typed is not None}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, db: Session = Depends(get_db)):
    await manager.connect(ws)
    await ws.send_text(json.dumps({"event": "votes_update", "songs": songs_with_votes(db)}))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)

# ── Online enrichment (lyrics, chords, style/year/language, photo) ────────────

ENRICH_STEPS = ("lyrics", "photo", "chords", "info")
# song id → {"running": bool, "finished_at": float|None, <step>: state}
# step states: "pending", "searching", "found", "partial" (info: some fields),
#              "had" (already filled), "not_found", "error"
enrich_status: dict = {}
_enrich_slots = asyncio.Semaphore(2)   # songs searched at the same time (the sources rate-limit)


class _SongGone(Exception):
    pass


def _queue_enrich(song_id: int, background_tasks: BackgroundTasks):
    if enrich_status.get(song_id, {}).get("running"):
        return
    # Forget results older than an hour
    for sid, st in list(enrich_status.items()):
        if not st["running"] and time.time() - st["finished_at"] > 3600:
            del enrich_status[sid]
    enrich_status[song_id] = {"running": True, "finished_at": None, **{s: "pending" for s in ENRICH_STEPS}}
    background_tasks.add_task(_enrich_song, song_id)


async def _save_song(song_id: int, fill: dict = None, rename: dict = None, event: str = "song_enriched"):
    """Write what was found, without overwriting anything set in the meantime.

    fill:   fields written only if still empty
    rename: {"title": (typed, canonical)}: written only if the value is still the typed one
    """
    db = SessionLocal()
    try:
        song = db.query(Song).filter(Song.id == song_id).first()
        if not song:
            raise _SongGone()
        for field, value in (fill or {}).items():
            if value and not getattr(song, field):
                setattr(song, field, value)
        for field, (typed, canonical) in (rename or {}).items():
            if getattr(song, field) == typed:
                setattr(song, field, canonical)
        db.commit()
        await manager.broadcast({"event": event, "songs": songs_with_votes(db)})
    finally:
        db.close()


async def _enrich_song(song_id: int):
    status = enrich_status[song_id]
    db = SessionLocal()
    song = db.query(Song).filter(Song.id == song_id).first()
    db.close()
    if not song:
        status.update(running=False, finished_at=time.time())
        return
    found = {"lyrics": song.lyrics, "chords": song.chords, "genre": song.genre,
             "year": song.year, "language": song.language}
    names = {"title": song.title, "artist": song.artist}

    async def lyrics_step():
        if found["lyrics"]:
            return "had"
        lyrics, _, track, artist = await asyncio.to_thread(enrich.find_lyrics, names["artist"], names["title"])
        # Names typed all in lowercase: the source's spelling, else simple capitals
        rename = {}
        for field, canonical in (("title", track), ("artist", artist)):
            typed = names[field]
            better = enrich.better_spelling(typed, canonical)
            if not better and typed == typed.lower():
                better = typed[:1].upper() + typed[1:] if field == "title" else string.capwords(typed)
            if better and better != typed:
                rename[field] = (typed, better)
                names[field] = better
        if not lyrics:
            if rename:
                await _save_song(song_id, rename=rename)
            return "not_found"
        found["lyrics"] = lyrics
        await _save_song(song_id, fill={"lyrics": lyrics}, rename=rename)
        return "found"

    async def photo_step():
        path = enrich.photo_path(names["artist"])
        if path and os.path.exists(path):
            return "had"
        if not await asyncio.to_thread(enrich.save_artist_photo, names["artist"]):
            return "not_found"
        await _save_song(song_id, event="photo_added")   # audience pages reload the photos
        return "found"

    async def chords_step():
        if found["chords"]:
            return "had"
        chords, _ = await asyncio.to_thread(enrich.find_chords, names["artist"], names["title"])
        if not chords:
            return "not_found"
        await _save_song(song_id, fill={"chords": chords})
        return "found"

    async def info_step():
        if found["genre"] and found["year"] and found["language"]:
            return "had"
        fill = {}
        if not found["language"]:
            fill["language"] = enrich.detect_language(found["lyrics"])
        if not (found["year"] and found["genre"]):
            fill["year"], fill["genre"] = await asyncio.to_thread(enrich.find_year_and_style, names["artist"], names["title"])
        if not any(fill.values()):
            return "not_found"
        await _save_song(song_id, fill=fill)
        complete = all(found[f] or fill.get(f) for f in ("genre", "year", "language"))
        return "found" if complete else "partial"

    steps = {"lyrics": lyrics_step, "photo": photo_step, "chords": chords_step, "info": info_step}
    async with _enrich_slots:
        for name in ENRICH_STEPS:
            status[name] = "searching"
            try:
                status[name] = await steps[name]()
            except _SongGone:
                break
            except Exception as exc:
                logger.error("Enrich %s failed for song %d: %s", name, song_id, exc)
                status[name] = "error"
    status.update(running=False, finished_at=time.time())
    logger.info("Enriched song %d (%s — %s): %s", song_id, names["title"], names["artist"],
                {s: status[s] for s in ENRICH_STEPS})


# ── Admin routes ──────────────────────────────────────────────────────────────

@app.get("/admin/songs")
def admin_get_songs(db: Session = Depends(get_db), _=Depends(check_admin)):
    return songs_with_votes(db)


def _catalog_artist(db: Session, artist: str) -> str:
    """The catalog's spelling of this artist if already there (one group, one photo)."""
    for (existing,) in db.query(Song.artist).distinct():
        if enrich.norm(existing) == enrich.norm(artist):
            return existing
    return artist


def _find_song(db: Session, title: str, artist: str):
    artist = _catalog_artist(db, artist)
    for song in db.query(Song).filter(Song.artist == artist):
        if enrich.title_key(song.title) == enrich.title_key(title):
            return song
    return None


async def _add_song(db: Session, background_tasks: BackgroundTasks, data: dict) -> Song:
    """Create a song and search online for what it is missing."""
    data["title"], data["artist"] = data["title"].strip(), data["artist"].strip()
    if not data["title"] or not data["artist"]:
        raise HTTPException(status_code=400, detail="Titulo y artista requeridos")
    existing = _find_song(db, data["title"], data["artist"])
    if existing:
        raise HTTPException(status_code=409, detail={"message": "Esa cancion ya esta en el catalogo",
                                                     "song_id": existing.id})
    data["artist"] = _catalog_artist(db, data["artist"])
    song = Song(**data)
    db.add(song)
    db.commit()
    db.refresh(song)
    await manager.broadcast({"event": "votes_update", "songs": songs_with_votes(db)})
    _queue_enrich(song.id, background_tasks)
    return song


@app.post("/admin/songs", status_code=201)
async def admin_add_song(body: SongCreate, background_tasks: BackgroundTasks,
                         db: Session = Depends(get_db), _=Depends(check_admin)):
    return await _add_song(db, background_tasks, body.model_dump())


@app.post("/admin/songs/{song_id}/enrich", status_code=202)
def admin_enrich_song(song_id: int, background_tasks: BackgroundTasks,
                      db: Session = Depends(get_db), _=Depends(check_admin)):
    """Search online for whatever the song is missing (lyrics, chords, info, photo)."""
    if not db.query(Song).filter(Song.id == song_id).first():
        raise HTTPException(status_code=404, detail="Cancion no encontrada")
    _queue_enrich(song_id, background_tasks)
    return enrich_status[song_id]


@app.get("/admin/songs/{song_id}/enrich")
def admin_enrich_status(song_id: int, _=Depends(check_admin)):
    """Progress of the online search (kept for an hour after it ends)."""
    if song_id not in enrich_status:
        raise HTTPException(status_code=404, detail="Sin busqueda reciente")
    return enrich_status[song_id]


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
    return [{"id": p.id, "title": p.title, "artist": p.artist, "count": p.count,
             "typed": p.typed, "similarity": p.similarity} for p in proposals]


@app.post("/admin/proposals/{proposal_id}/add", status_code=201)
async def admin_add_proposal(proposal_id: int, background_tasks: BackgroundTasks,
                             db: Session = Depends(get_db), _=Depends(check_admin)):
    """Turn a suggestion into a catalog song (searched online like any new song)."""
    proposal = db.query(Proposal).filter(Proposal.id == proposal_id).first()
    if not proposal:
        raise HTTPException(status_code=404, detail="Sugerencia no encontrada")
    if not proposal.artist:
        raise HTTPException(status_code=400, detail="La sugerencia no tiene artista")
    try:
        song = await _add_song(db, background_tasks, {"title": proposal.title, "artist": proposal.artist,
                                                      "position": (db.query(func.max(Song.position)).scalar() or 0) + 1})
    except HTTPException as exc:
        if exc.status_code == 409:   # already in the catalog: the suggestion is done anyway
            db.delete(proposal)
            db.commit()
            await manager.broadcast({"event": "proposals_update"})
        raise
    db.delete(proposal)
    db.commit()
    await manager.broadcast({"event": "proposals_update"})
    return {"song_id": song.id, "title": song.title, "artist": song.artist, "enrich": enrich_status[song.id]}


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
        "enrich": enrich_status,
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
        return enrich.ug_top(page)
    except Exception as exc:
        logger.error("UG explore error (page %d): %s", page, exc)
        raise HTTPException(status_code=502, detail=f"Ultimate Guitar unreachable: {exc}")


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
