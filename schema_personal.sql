-- EQUIPA Personal-PM Schema (owner-only, opt-in)
-- Additive companion to schema.sql. Holds the owner's personal
-- product-management data model, which the public orchestrator product does
-- NOT ship by default.
--
-- Applied by equipa/db.py:ensure_schema() ONLY when this file is present AND
-- the feature flag `features.personal_pm_tables` is enabled (default TRUE, so
-- the owner's prod install keeps these tables). Public/fresh installs that set
-- the flag to false never create them.
--
-- SAFETY: additive-only. Every statement is CREATE ... IF NOT EXISTS, so
-- applying this file against a database that already has these tables (the
-- live prod DB) is a no-op — no DROP, ALTER, RENAME, DELETE, or data rewrite.
--
-- Tables: 8, Views: 2

-- ============================================================
-- TABLES
-- ============================================================

CREATE TABLE IF NOT EXISTS competitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    product_name TEXT,
    price_range TEXT,
    strengths TEXT,
    weaknesses TEXT,
    url TEXT,
    notes TEXT,
    analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS content_tickler (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    total_posts INTEGER DEFAULT 0,
    posts_used INTEGER DEFAULT 0,
    posts_remaining INTEGER DEFAULT 0,
    alert_threshold INTEGER DEFAULT 4,
    last_checked DATE,
    needs_content INTEGER DEFAULT 0,
    notes TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS posting_schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    week_number INTEGER NOT NULL,
    day_of_week TEXT NOT NULL,
    platform TEXT NOT NULL,
    product TEXT NOT NULL,
    post_id TEXT NOT NULL,
    scheduled_date DATE,
    status TEXT DEFAULT 'pending',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS product_opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    industry TEXT NOT NULL,
    opportunity_name TEXT,
    target_market TEXT NOT NULL,
    pain_points TEXT NOT NULL,
    existing_solutions TEXT,
    pricing_landscape TEXT,
    opportunity_score INTEGER DEFAULT 0,
    notes TEXT,
    status TEXT DEFAULT 'researched',
    researched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY,
    project_id INTEGER,
    title TEXT NOT NULL,
    description TEXT,
    reminder_date DATE NOT NULL,
    command TEXT,
    status TEXT DEFAULT 'pending',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    completed_at DATETIME,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS social_media_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    post_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    product TEXT NOT NULL,
    content TEXT NOT NULL,
    hashtags TEXT,
    image_notes TEXT,
    status TEXT DEFAULT 'pending',
    posted_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS writing_style (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    style_element TEXT NOT NULL,
    description TEXT NOT NULL,
    examples TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS voice_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reply_to INTEGER,
    metadata TEXT,
    created_at DATETIME DEFAULT (datetime('now')),
    processed_at DATETIME
);

-- ============================================================
-- VIEWS
-- ============================================================
-- These views read exclusively from the personal-PM tables above, so they
-- ship here rather than in core schema.sql — a fresh install without the
-- personal flag would otherwise define views over tables that do not exist.

CREATE VIEW IF NOT EXISTS v_upcoming_reminders AS
SELECT r.*, p.codename as project_name,
       julianday(r.reminder_date) - julianday('now') as days_until
FROM reminders r
LEFT JOIN projects p ON r.project_id = p.id
WHERE r.status = 'pending'
  AND julianday(r.reminder_date) - julianday('now') <= 7
ORDER BY r.reminder_date;

CREATE VIEW IF NOT EXISTS v_content_alerts AS
SELECT * FROM content_tickler
WHERE needs_content = 1
   OR posts_remaining <= alert_threshold;
