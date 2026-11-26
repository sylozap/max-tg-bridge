CREATE TABLE IF NOT EXISTS pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_chat INTEGER NOT NULL UNIQUE,
    max_chat INTEGER NOT NULL UNIQUE,
    paused INTEGER NOT NULL DEFAULT 0,
    generation INTEGER NOT NULL DEFAULT 0,
    delivered INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    last_delivery TEXT,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS cursors (
    platform TEXT NOT NULL,
    bot INTEGER NOT NULL,
    cursor INTEGER NOT NULL,
    saved_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER)),
    PRIMARY KEY (platform, bot)
);
CREATE TABLE IF NOT EXISTS receipts (
    platform TEXT NOT NULL,
    chat INTEGER NOT NULL,
    mid TEXT NOT NULL,
    event TEXT NOT NULL,
    revision TEXT NOT NULL,
    PRIMARY KEY (platform, chat, mid, event, revision)
);
CREATE TABLE IF NOT EXISTS outgoing (
    platform TEXT NOT NULL,
    chat INTEGER NOT NULL,
    mid TEXT NOT NULL,
    PRIMARY KEY (platform, chat, mid)
);
CREATE TABLE IF NOT EXISTS copies (
    pair INTEGER NOT NULL REFERENCES pairs(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    root TEXT NOT NULL,
    part INTEGER NOT NULL,
    target_mid TEXT NOT NULL,
    mode TEXT NOT NULL,
    PRIMARY KEY (pair, source, root, part)
);
CREATE INDEX IF NOT EXISTS copies_target_idx ON copies(pair, source, target_mid);
CREATE TABLE IF NOT EXISTS originals (
    pair INTEGER NOT NULL REFERENCES pairs(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    mid TEXT NOT NULL,
    root TEXT NOT NULL,
    position INTEGER NOT NULL,
    media_key TEXT,
    has_caption INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (pair, source, mid)
);
CREATE INDEX IF NOT EXISTS originals_root_idx ON originals(pair, source, root, position);
PRAGMA user_version = 2;
