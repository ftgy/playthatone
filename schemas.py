from pydantic import BaseModel, Field
from typing import Optional

# ---- Songs ----

class SongCreate(BaseModel):
    title: str
    artist: str
    lyrics: Optional[str] = None
    chords: Optional[str] = None
    position: int = 0
    genre: Optional[str] = None
    year: Optional[int] = None
    language: Optional[str] = None

class SongUpdate(BaseModel):
    title: Optional[str] = None
    artist: Optional[str] = None
    position: Optional[int] = None
    genre: Optional[str] = Field(default=None, max_length=50)
    year: Optional[int] = Field(default=None, ge=1000, le=2100)
    language: Optional[str] = Field(default=None, max_length=30)

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
    genre: Optional[str] = None
    year: Optional[int] = None
    language: Optional[str] = None
    votes: int = 0

    model_config = {"from_attributes": True}

# ---- Proposals ----

class ProposalCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    artist: str = Field(min_length=1, max_length=200)
