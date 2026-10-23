from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.sql import func
from database import Base

class Song(Base):
    __tablename__ = "songs"

    id         = Column(Integer, primary_key=True, index=True)
    title      = Column(String(200), nullable=False)
    artist     = Column(String(200), nullable=False)
    lyrics     = Column(Text, nullable=True)
    chords     = Column(Text, nullable=True)
    position   = Column(Integer, default=0)
    state      = Column(String(20), nullable=True)   # None | "now_playing" | "played"
    genre      = Column(String(50), nullable=True)   # style, e.g. "Rock", "Folk"
    year       = Column(Integer, nullable=True)      # original release year
    language   = Column(String(30), nullable=True)   # e.g. "English", "Spanish"
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Vote(Base):
    __tablename__ = "votes"

    id         = Column(Integer, primary_key=True, index=True)
    song_id    = Column(Integer, nullable=False)
    session_id = Column(String(64), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Proposal(Base):
    __tablename__ = "proposals"

    id         = Column(Integer, primary_key=True, index=True)
    title      = Column(String(200), nullable=False)
    slug       = Column(String(200), nullable=False, index=True)
    count      = Column(Integer, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
