-- EQUIPA Database Schema
-- Canonical DDL for creating a fresh EQUIPA database.
-- Generated from the live theforge.db schema.
-- Used by equipa_setup.py to create new installations.
--
-- Core (public) product tables live here. The owner-only personal-PM data
-- model (competitors, content_tickler, posting_schedule, product_opportunities,
-- social_media_posts, writing_style, reminders, voice_messages + the
-- v_upcoming_reminders / v_content_alerts views) lives in schema_personal.sql
-- and is applied only when the personal_pm_tables feature flag is enabled.
--
-- Tables: 22, Views: 7, Triggers: 1, Indexes: 14

-- ============================================================
-- TABLES
-- ============================================================

CREATE TABLE projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    codename TEXT,
    category TEXT,
    status TEXT DEFAULT 'active',
    summary TEXT,
    target_market TEXT,
    revenue_model TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    icon_base64 TEXT,
    local_path TEXT
);

CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'todo',
    priority TEXT DEFAULT 'medium',
    blocked_by TEXT,
    due_date DATE,
    completed_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    task_type TEXT DEFAULT 'feature',
    role TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    topic TEXT NOT NULL,
    decision TEXT NOT NULL,
    rationale TEXT,
    alternatives_considered TEXT,
    decided_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_validated DATETIME,
    decision_type TEXT NOT NULL DEFAULT 'general',
    status TEXT NOT NULL DEFAULT 'open',
    resolved_by_task_id INTEGER DEFAULT NULL,
    verified_at DATETIME DEFAULT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (resolved_by_task_id) REFERENCES tasks(id)
);

CREATE TABLE open_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    question TEXT NOT NULL,
    context TEXT,
    priority TEXT DEFAULT 'medium',
    resolved INTEGER DEFAULT 0,
    resolution TEXT,
    asked_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    resolved_at DATETIME,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE session_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    session_date DATE DEFAULT CURRENT_DATE,
    summary TEXT NOT NULL,
    key_points TEXT,
    next_steps TEXT,
    chat_url TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE code_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    language TEXT,
    code TEXT NOT NULL,
    purpose TEXT,
    file_path TEXT,
    version TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE components (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    type TEXT,
    specs TEXT,
    price REAL,
    currency TEXT DEFAULT 'USD',
    vendor TEXT,
    url TEXT,
    status TEXT DEFAULT 'candidate',
    notes TEXT,
    added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

-- NOTE: competitors and content_tickler moved to schema_personal.sql
-- (owner-only personal-PM data model; applied via the personal_pm_tables flag).

CREATE TABLE cross_references (
    id INTEGER PRIMARY KEY,
    source_table TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    target_table TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    relationship TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    type TEXT,
    file_path TEXT,
    description TEXT,
    version TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

-- NOTE: posting_schedule and product_opportunities moved to
-- schema_personal.sql (owner-only personal-PM data model).

CREATE TABLE project_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    asset_type TEXT NOT NULL,
    asset_name TEXT NOT NULL,
    description TEXT,
    is_primary BOOLEAN DEFAULT 0,
    file_path TEXT,
    base64_data TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE build_info (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    build_type TEXT NOT NULL,
    build_command TEXT NOT NULL,
    output_path TEXT,
    output_filename TEXT,
    prerequisites TEXT,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

-- NOTE: reminders moved to schema_personal.sql (owner-only personal-PM data model).

CREATE TABLE research (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    topic TEXT NOT NULL,
    findings TEXT NOT NULL,
    source_url TEXT,
    source_name TEXT,
    relevance TEXT,
    researched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

-- NOTE: social_media_posts and writing_style moved to schema_personal.sql
-- (owner-only personal-PM data model).

CREATE TABLE agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    project_id INTEGER,
    role TEXT NOT NULL,
    model TEXT NOT NULL,
    complexity TEXT,
    num_turns INTEGER DEFAULT 0,
    max_turns_allowed INTEGER DEFAULT 25,
    duration_seconds REAL DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    outcome TEXT,
    success INTEGER DEFAULT 0,
    cycle_number INTEGER DEFAULT 1,
    continuation_count INTEGER DEFAULT 0,
    files_changed_count INTEGER DEFAULT 0,
    error_type TEXT,
    error_summary TEXT,
    turns_allocated INTEGER,
    prompt_version TEXT DEFAULT 'baseline',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

-- NOTE: voice_messages moved to schema_personal.sql (owner-only personal-PM data model).

CREATE TABLE api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    label TEXT NOT NULL,
    api_key TEXT NOT NULL,
    notes TEXT,
    active INTEGER DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ForgeSmith self-improvement tables

CREATE TABLE lessons_learned (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    role TEXT,
    error_type TEXT,
    error_signature TEXT,
    lesson TEXT NOT NULL,
    source TEXT DEFAULT 'forgesmith',
    times_seen INTEGER DEFAULT 1,
    times_injected INTEGER DEFAULT 0,
    effectiveness_score REAL,
    active INTEGER DEFAULT 1,
    embedding TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Partial unique index supporting INSERT ON CONFLICT upserts in
-- _create_review_lessons. Scoped to active=1 so deactivated lessons
-- can coexist with newer active ones for the same (signature, source).
CREATE UNIQUE INDEX idx_lessons_sig_source_active
    ON lessons_learned(error_signature, source) WHERE active = 1;

CREATE TABLE agent_episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    role TEXT,
    task_type TEXT,
    project_id INTEGER,
    approach_summary TEXT,
    turns_used INTEGER,
    outcome TEXT,
    error_patterns TEXT,
    reflection TEXT,
    q_value REAL DEFAULT 0.5,
    embedding TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    times_injected INTEGER DEFAULT 0
);

CREATE TABLE forgesmith_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    agent_runs_analyzed INTEGER DEFAULT 0,
    changes_made INTEGER DEFAULT 0,
    summary TEXT,
    mode TEXT DEFAULT 'auto'
);

CREATE TABLE forgesmith_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    change_type TEXT NOT NULL,
    target_file TEXT,
    old_value TEXT,
    new_value TEXT,
    rationale TEXT NOT NULL,
    evidence TEXT,
    effectiveness_score REAL,
    reverted_at TEXT,
    impact_assessment TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE rubric_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_run_id INTEGER NOT NULL,
    task_id INTEGER,
    project_id INTEGER,
    role TEXT NOT NULL,
    rubric_version INTEGER DEFAULT 1,
    criteria_scores TEXT NOT NULL,
    total_score REAL NOT NULL,
    max_possible REAL NOT NULL,
    normalized_score REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE rubric_evolution_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rubric_version INTEGER NOT NULL,
    role TEXT NOT NULL,
    criterion TEXT NOT NULL,
    old_weight REAL NOT NULL,
    new_weight REAL NOT NULL,
    correlation REAL NOT NULL,
    sample_size_success INTEGER,
    sample_size_failure INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Model registry for tracking ML training runs and results
CREATE TABLE model_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    model_type TEXT NOT NULL,        -- lstm_v3, xgboost, ensemble
    version TEXT NOT NULL,            -- v3_20260305
    symbol_count INTEGER,
    avg_directional_accuracy REAL,
    median_da REAL,
    above_50_pct REAL,
    top_symbol TEXT,
    top_da REAL,
    trained_on TEXT,                  -- pc_wsl, forge-inference, claudinator
    model_path TEXT,                  -- where the models live
    synced_to TEXT,                   -- comma-separated: claudinator, forge-inference
    trained_at DATETIME,
    logged_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    notes TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

-- ============================================================
-- INDEXES
-- ============================================================

CREATE INDEX idx_tasks_project_status ON tasks(project_id, status);
CREATE INDEX idx_decisions_project ON decisions(project_id);
CREATE INDEX idx_decisions_type ON decisions(decision_type);
CREATE INDEX idx_decisions_status ON decisions(status);
CREATE INDEX idx_decisions_resolved_by ON decisions(resolved_by_task_id);
CREATE INDEX idx_open_questions_resolved ON open_questions(resolved);
CREATE INDEX idx_projects_status ON projects(status);
CREATE INDEX idx_components_project ON components(project_id);
CREATE INDEX idx_xref_source ON cross_references(source_table, source_id);
CREATE INDEX idx_xref_target ON cross_references(target_table, target_id);
CREATE INDEX idx_agent_runs_project ON agent_runs(project_id);
CREATE INDEX idx_agent_runs_role ON agent_runs(role);
CREATE INDEX idx_model_registry_project ON model_registry(project_id, model_type);
CREATE INDEX idx_model_registry_version ON model_registry(version);

-- ============================================================
-- TRIGGERS
-- ============================================================

CREATE TRIGGER update_project_timestamp
AFTER UPDATE ON projects
BEGIN
    UPDATE projects SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

-- ============================================================
-- VIEWS
-- ============================================================

CREATE VIEW v_project_dashboard AS
SELECT
    p.id,
    p.codename,
    p.status,
    (SELECT COUNT(*) FROM tasks WHERE project_id = p.id AND status = 'todo') as todo_count,
    (SELECT COUNT(*) FROM tasks WHERE project_id = p.id AND status = 'in_progress') as in_progress_count,
    (SELECT COUNT(*) FROM tasks WHERE project_id = p.id AND status = 'blocked') as blocked_count,
    (SELECT COUNT(*) FROM open_questions WHERE project_id = p.id AND resolved = 0) as open_questions,
    (SELECT MAX(session_date) FROM session_notes WHERE project_id = p.id) as last_session
FROM projects p
WHERE p.status = 'active';

CREATE VIEW v_stale_tasks AS
SELECT t.*, p.codename as project_name,
       julianday('now') - julianday(t.created_at) as days_stale
FROM tasks t
JOIN projects p ON t.project_id = p.id
WHERE t.status = 'in_progress'
  AND julianday('now') - julianday(t.created_at) > 3;

CREATE VIEW v_stale_questions AS
SELECT q.*, p.codename as project_name,
       julianday('now') - julianday(q.asked_at) as days_open
FROM open_questions q
JOIN projects p ON q.project_id = p.id
WHERE q.resolved = 0
  AND julianday('now') - julianday(q.asked_at) > 7;

CREATE VIEW v_stale_decisions AS
SELECT d.*, p.codename as project_name,
       julianday('now') - julianday(COALESCE(d.last_validated, d.decided_at)) as days_since_validation
FROM decisions d
JOIN projects p ON d.project_id = p.id
WHERE d.status NOT IN ('resolved', 'wont_fix', 'failed_resolution')
  AND julianday('now') - julianday(COALESCE(d.last_validated, d.decided_at)) > 60;

CREATE VIEW v_open_security_findings AS
SELECT d.id, d.project_id, p.codename as project_name,
       d.topic, d.decision, d.rationale,
       d.decided_at, d.last_validated,
       d.resolved_by_task_id
FROM decisions d
JOIN projects p ON d.project_id = p.id
WHERE d.decision_type = 'security_finding'
  AND d.status = 'open';

-- NOTE: v_upcoming_reminders (reads `reminders`) and v_content_alerts (reads
-- `content_tickler`) moved to schema_personal.sql alongside their tables.

CREATE VIEW v_cost_by_project AS
SELECT
    p.codename,
    COUNT(ar.id) as total_runs,
    SUM(ar.turns_used) as total_turns,
    ROUND(SUM(ar.duration_s), 1) as total_duration_s,
    ROUND(SUM(ar.cost_usd), 4) as total_cost_usd,
    SUM(CASE WHEN ar.success = 1 THEN 1 ELSE 0 END) as successful_runs,
    SUM(CASE WHEN ar.success = 0 THEN 1 ELSE 0 END) as failed_runs
FROM agent_runs ar
JOIN projects p ON ar.project_id = p.id
GROUP BY p.codename
ORDER BY total_cost_usd DESC;

CREATE VIEW v_cost_by_role AS
SELECT
    ar.role,
    COUNT(ar.id) as total_runs,
    SUM(ar.turns_used) as total_turns,
    ROUND(SUM(ar.duration_s), 1) as total_duration_s,
    ROUND(SUM(ar.cost_usd), 4) as total_cost_usd,
    ROUND(AVG(ar.cost_usd), 4) as avg_cost_per_run,
    SUM(CASE WHEN ar.success = 1 THEN 1 ELSE 0 END) as successful_runs
FROM agent_runs ar
GROUP BY ar.role
ORDER BY total_cost_usd DESC;

-- ============================================================
-- AGENT COMMUNICATION & OBSERVABILITY TABLES
-- ============================================================

-- Inter-agent message channel for structured message passing between agents
-- across dev-test cycles. Messages are posted after each agent completes and
-- injected into the next agent's system prompt.
CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    cycle_number INTEGER NOT NULL,
    from_role TEXT NOT NULL,
    to_role TEXT NOT NULL,
    message_type TEXT NOT NULL,  -- test_results, blocker_update, code_notes, security_flag
    content TEXT NOT NULL,       -- JSON structured content
    read_by_cycle INTEGER,       -- Which cycle consumed this? NULL = unread
    created_at TEXT DEFAULT (datetime('now'))
);

-- Per-tool action logging for agent observability and ForgeSmith analysis
CREATE TABLE IF NOT EXISTS agent_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    run_id INTEGER,            -- FK to agent_runs.id
    cycle_number INTEGER NOT NULL,
    role TEXT NOT NULL,
    turn_number INTEGER NOT NULL,
    tool_name TEXT NOT NULL,    -- Read, Write, Edit, Bash, Glob, Grep, etc.
    tool_input_preview TEXT,    -- First 200 chars of input (for debugging)
    input_hash TEXT,            -- SHA256 of full tool input (for dedup detection)
    output_length INTEGER,
    success INTEGER NOT NULL DEFAULT 1,
    error_type TEXT,            -- timeout, file_not_found, permission, syntax_error, etc.
    error_summary TEXT,         -- First 200 chars of error
    duration_ms INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_agent_actions_task ON agent_actions(task_id, cycle_number);
CREATE INDEX IF NOT EXISTS idx_agent_actions_tool ON agent_actions(tool_name, success);

-- ============================================================
-- LESSON GRAPH EDGES TABLE
-- ============================================================
-- Stores relationships between lessons for graph-based retrieval
CREATE TABLE IF NOT EXISTS lesson_graph_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_id INTEGER NOT NULL,
    dst_id INTEGER NOT NULL,
    edge_type TEXT NOT NULL,
    weight REAL DEFAULT 1.0,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(src_id, dst_id, edge_type)
);
CREATE INDEX IF NOT EXISTS idx_lesson_graph_src ON lesson_graph_edges(src_id);
CREATE INDEX IF NOT EXISTS idx_lesson_graph_dst ON lesson_graph_edges(dst_id);

-- ============================================================
-- TASK FLOW TABLES (OpenClaw-inspired multi-step orchestration)
-- ============================================================
-- A `flow` represents a multi-step orchestration spanning one or more child
-- tasks (e.g. developer -> [security-reviewer, code-reviewer, tester] fanout).
-- Each flow has an integer revision counter that is bumped on every state
-- transition; this lets the orchestrator survive a Claudinator restart and
-- detect interleaved updates from concurrent dispatchers (optimistic
-- concurrency).
--
-- States:
--   queued     - created, no child task started yet
--   running    - at least one child is in flight
--   paused     - user/admin pause; children may still finish but no new work
--   cancelled  - sticky cancel; propagated to all children, no new work
--   done       - terminal success (all children completed)
--   failed     - terminal failure (one or more children failed and policy says abort)
CREATE TABLE IF NOT EXISTS flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    parent_task_id INTEGER,            -- the task that spawned the fanout (nullable)
    title TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued','running','paused','cancelled','done','failed')),
    revision INTEGER NOT NULL DEFAULT 0,
    metadata TEXT,                     -- JSON blob: roles, fanout config, etc.
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    cancelled_at TEXT,
    cancelled_reason TEXT,
    completed_at TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (parent_task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_flows_project_state ON flows(project_id, state);
CREATE INDEX IF NOT EXISTS idx_flows_state ON flows(state);

-- Append-only audit log of every flow state/revision transition. Survives
-- restarts so recovery code can replay the last-known-good state.
CREATE TABLE IF NOT EXISTS flow_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    state TEXT NOT NULL,
    event TEXT NOT NULL,               -- create, transition, child_added, child_done, cancel, resume, ...
    payload TEXT,                      -- JSON blob: child task IDs, error, etc.
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (flow_id) REFERENCES flows(id),
    UNIQUE(flow_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_flow_revisions_flow ON flow_revisions(flow_id, revision);

-- Many-to-many between flows and the tasks they manage. A child task may be
-- "managed" (created by the flow) or "mirrored" (pre-existing task pulled in).
CREATE TABLE IF NOT EXISTS flow_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id INTEGER NOT NULL,
    task_id INTEGER NOT NULL,
    role TEXT,                         -- developer, security-reviewer, etc.
    relationship TEXT NOT NULL DEFAULT 'managed'
        CHECK (relationship IN ('managed','mirrored')),
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','running','done','failed','cancelled')),
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    FOREIGN KEY (flow_id) REFERENCES flows(id),
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    UNIQUE(flow_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_flow_tasks_flow ON flow_tasks(flow_id);
CREATE INDEX IF NOT EXISTS idx_flow_tasks_task ON flow_tasks(task_id);

-- ============================================================
-- PAPERCLIP CONFIG VERSIONS (v10)
-- ============================================================
-- Snapshot history of orchestrator configuration files. One config_versions
-- row per snapshot; many config_version_files rows hang off it. content_sha
-- is computed over sorted (file_path, file_sha) pairs and is used to dedup
-- identical snapshots within a project.
CREATE TABLE IF NOT EXISTS config_versions (
    id INTEGER PRIMARY KEY,
    project_id INTEGER,
    created_at TEXT,
    source TEXT CHECK (source IN ('manual','auto-dispatch','auto-cli','auto-rollback')),
    commit_message TEXT,
    content_sha TEXT NOT NULL,
    parent_version_id INTEGER,
    FOREIGN KEY(project_id) REFERENCES projects(id)
);
CREATE INDEX IF NOT EXISTS idx_config_versions_project_created
    ON config_versions(project_id, created_at);

-- Blobs for each file in a config snapshot. content_blob is plain text
-- (configs are tiny). ON DELETE CASCADE removes file rows when their parent
-- version is deleted.
CREATE TABLE IF NOT EXISTS config_version_files (
    id INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    content_blob TEXT NOT NULL,
    file_sha TEXT NOT NULL,
    byte_size INTEGER,
    FOREIGN KEY(version_id) REFERENCES config_versions(id) ON DELETE CASCADE,
    UNIQUE(version_id, file_path)
);
CREATE INDEX IF NOT EXISTS idx_cvf_version ON config_version_files(version_id);

-- ============================================================
-- PAPERCLIP AGENT SESSIONS (v11)
-- ============================================================
-- Capture of an in-flight agent's rolling state so the orchestrator can
-- resume / postmortem after a crash, kill, or context compaction. Per
-- PLAN-1067 B1.
--
-- state_json is JSON text matching the contract documented on
-- db_migrate.migrate_v10_to_v11 (open_files / files_changed / files_read /
-- recent_tool_calls / partial_reasoning / turn_count / compaction_count /
-- soft_checkpoint_path). cycle_id is opaque to the persistence layer
-- (heartbeat-tick UUID or flow-revision marker).
--
-- expires_at defaults to created_at + 14 days when capture writes a row
-- (set by the caller, not enforced in schema). FK to tasks is intentionally
-- NOT ON DELETE CASCADE: sessions are kept after task purge so they are
-- available for postmortem analysis.
CREATE TABLE IF NOT EXISTS agent_sessions (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    project_id INTEGER NOT NULL,
    cycle_id TEXT NOT NULL,
    state_json TEXT NOT NULL,
    byte_size INTEGER,
    created_at TEXT,
    last_seen_at TEXT,
    expires_at TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(id),
    FOREIGN KEY(project_id) REFERENCES projects(id)
);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_task_role_seen
    ON agent_sessions(task_id, role, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_expires
    ON agent_sessions(expires_at);

-- ============================================================
-- VERSION STAMP
-- ============================================================
-- Marks fresh installs as v11. Migrations handle upgrades from older versions.
PRAGMA user_version = 11;

