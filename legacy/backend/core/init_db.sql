-- init_db.sql
CREATE TABLE IF NOT EXISTS songs (
    id TEXT PRIMARY KEY,
    name TEXT,
    artist TEXT,
    year INTEGER,
    language TEXT,
    genre TEXT,
    popularity INTEGER,
    duration INTEGER,
    mood TEXT,
    key TEXT,
    tempo TEXT
);
