from pydantic import BaseModel, Field
from typing import Optional

# ---- Songs ----

class SongCreate(BaseModel):
    title: str
    artist: str
    lyrics: Optional[str] = None
    chords: Optional[str] = None
    position: int = 0

class SongUpdate(BaseModel):
    title: Optional[str] = None
    artist: Optional[str] = None
    position: Optional[int] = None

class LyricsUpdate(BaseModel):
    lyrics: str

class ChordsUpdate(BaseModel):
    chords: str

class SongOut(BaseModel):
    id: int
    title: str
    artist: str
    lyrics: Optional[str] = None
    chords: Optional[str] = None
    position: int
    state: Optional[str] = None
    votes: int = 0

    model_config = {"from_attributes": True}

# ---- Proposals ----

class ProposalCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
