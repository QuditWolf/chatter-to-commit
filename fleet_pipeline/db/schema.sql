-- Fleet Log Pipeline — SQLite Schema
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Canonical truck registry
CREATE TABLE IF NOT EXISTS trucks (
    truck_id    TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    aliases     TEXT NOT NULL,           -- JSON array of known aliases
    is_active   BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Canonical site registry
CREATE TABLE IF NOT EXISTS sites (
    site_id      TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    aliases      TEXT NOT NULL,          -- JSON array of known aliases
    site_type    TEXT,                   -- "loading" | "unloading" | "depot"
    is_active    BOOLEAN DEFAULT TRUE,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Raw messages as parsed by Level 1
CREATE TABLE IF NOT EXISTS raw_messages (
    msg_id      TEXT PRIMARY KEY,
    source_file TEXT,
    timestamp_iso TEXT NOT NULL,
    sender_name TEXT,
    sender_id   TEXT,
    raw_text    TEXT,
    is_edited   BOOLEAN DEFAULT FALSE,
    is_deleted  BOOLEAN DEFAULT FALSE,
    media_type  TEXT,
    quoted_wa_message_id TEXT,          -- WA msg ID this message is replying to
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Simulation run tracking
CREATE TABLE IF NOT EXISTS simulation_runs (
    run_id       TEXT PRIMARY KEY,
    source_file  TEXT,
    started_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at  TIMESTAMP,
    total_msgs   INTEGER DEFAULT 0,
    committed    INTEGER DEFAULT 0,
    flagged      INTEGER DEFAULT 0,
    held         INTEGER DEFAULT 0,
    errors       INTEGER DEFAULT 0,
    hitl_created INTEGER DEFAULT 0,
    notes        TEXT
);

-- Parsed and committed truck events
CREATE TABLE IF NOT EXISTS events (
    event_id           TEXT PRIMARY KEY,
    msg_id             TEXT REFERENCES raw_messages(msg_id),
    truck_id           TEXT REFERENCES trucks(truck_id),
    truck_alias        TEXT,
    status             TEXT NOT NULL,    -- ENTER | LS | LO | LEFT | US | UO
    site_id            TEXT REFERENCES sites(site_id),
    site_alias         TEXT,
    material           TEXT,
    timestamp_effective TEXT NOT NULL,
    inferred           BOOLEAN DEFAULT FALSE,
    confidence         REAL,
    reasoning          TEXT,
    commit_status      TEXT DEFAULT 'PENDING',  -- COMMITTED | FLAGGED | HELD | DELETED
    corrects_event_id  TEXT REFERENCES events(event_id),
    processing_id      TEXT,
    simulation_run_id  TEXT REFERENCES simulation_runs(run_id),
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tally snapshots
CREATE TABLE IF NOT EXISTS tallies (
    tally_id     TEXT PRIMARY KEY,
    msg_id       TEXT REFERENCES raw_messages(msg_id),
    timestamp_iso TEXT,
    tally_data   TEXT NOT NULL,          -- JSON blob
    commit_status TEXT DEFAULT 'COMMITTED',
    simulation_run_id TEXT REFERENCES simulation_runs(run_id),
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Human-in-the-loop question queue
CREATE TABLE IF NOT EXISTS hitl_queue (
    question_id           TEXT PRIMARY KEY,
    msg_id                TEXT REFERENCES raw_messages(msg_id),
    event_id              TEXT REFERENCES events(event_id),
    question_type         TEXT NOT NULL,         -- UNKNOWN_TRUCK | UNKNOWN_SITE | LOW_CONFIDENCE | CORRECTION_AMBIGUOUS | DELETED_MESSAGE
    question_text         TEXT NOT NULL,
    context               TEXT,                  -- JSON: relevant context
    status                TEXT DEFAULT 'OPEN',   -- OPEN | ANSWERED | DISMISSED
    answer                TEXT,
    answered_by           TEXT,
    answered_at           TIMESTAMP,
    simulation_run_id     TEXT REFERENCES simulation_runs(run_id),
    original_wa_message_id TEXT,                 -- WA msg ID of the message that triggered this question
    bot_wa_message_id     TEXT,                  -- WA msg ID of the bot's clarification reply (for reply routing)
    group_jid             TEXT,                  -- WA group to reply in
    created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Audit log of all DB mutations
CREATE TABLE IF NOT EXISTS audit_log (
    log_id       TEXT PRIMARY KEY,
    action       TEXT NOT NULL,          -- INSERT | UPDATE | DELETE | CORRECTION
    table_name   TEXT NOT NULL,
    record_id    TEXT NOT NULL,
    old_value    TEXT,                   -- JSON
    new_value    TEXT,                   -- JSON
    triggered_by TEXT,                   -- "pipeline" | "hitl_answer" | "manual"
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Raw WhatsApp messages from Baileys real-time listener
CREATE TABLE IF NOT EXISTS wa_messages (
    wa_message_id   TEXT PRIMARY KEY,
    sender_phone    TEXT,
    group_jid       TEXT,
    raw_text        TEXT,
    received_at     TIMESTAMP,
    message_type    TEXT DEFAULT 'fleet_event',  -- fleet_event | shift_signal | unknown
    processed       BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Shift records
CREATE TABLE IF NOT EXISTS shifts (
    shift_id         TEXT PRIMARY KEY,
    shift_number     INTEGER NOT NULL,
    started_at       TIMESTAMP NOT NULL,
    ended_at         TIMESTAMP,
    detection_method TEXT DEFAULT 'time_based',  -- time_based | wa_signal | manual
    notes            TEXT,
    simulation_run_id TEXT REFERENCES simulation_runs(run_id),
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Shift configuration (admin-editable)
CREATE TABLE IF NOT EXISTS shift_config (
    shift_number   INTEGER PRIMARY KEY,
    start_time     TEXT NOT NULL,   -- "HH:MM"
    expected_end   TEXT,            -- "HH:MM"
    wa_keyword     TEXT,            -- e.g. "s1" or "shift start"
    last_detected  TIMESTAMP,
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Operator corrections (append-only, never overwrite events)
CREATE TABLE IF NOT EXISTS corrections (
    correction_id      TEXT PRIMARY KEY,
    original_event_id  TEXT REFERENCES events(event_id),
    corrected_by       TEXT NOT NULL,
    corrected_at       TIMESTAMP NOT NULL,
    field_changed      TEXT NOT NULL,  -- truck_id | status | site_id | shift_id
    original_value     TEXT,
    corrected_value    TEXT,
    note               TEXT,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- LLM raw output archive — forensic recovery for every LLM call.
-- Stores the unparsed LLM text alongside the parsed result so that
-- if the parser or sanitizer makes a mistake, the original output
-- can be re-examined and re-parsed.
CREATE TABLE IF NOT EXISTS llm_outputs (
    output_id          TEXT PRIMARY KEY,
    msg_id             TEXT REFERENCES raw_messages(msg_id),
    raw_llm_text       TEXT,                  -- unparsed LLM output (may be malformed)
    parsed_json        TEXT,                  -- JSON after parsing + sanitization
    sanitizer_issues   TEXT,                  -- JSON array of sanitization findings
    model_name         TEXT,
    prompt_hash        TEXT,                  -- SHA-256 of prompt (dedup detection)
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_events_truck_id ON events(truck_id);
CREATE INDEX IF NOT EXISTS idx_events_msg_id ON events(msg_id);
CREATE INDEX IF NOT EXISTS idx_events_commit_status ON events(commit_status);
CREATE INDEX IF NOT EXISTS idx_events_simulation_run ON events(simulation_run_id);
CREATE INDEX IF NOT EXISTS idx_hitl_status ON hitl_queue(status);
CREATE INDEX IF NOT EXISTS idx_raw_messages_timestamp ON raw_messages(timestamp_iso);
CREATE INDEX IF NOT EXISTS idx_raw_messages_quoted ON raw_messages(quoted_wa_message_id);
CREATE INDEX IF NOT EXISTS idx_wa_messages_received ON wa_messages(received_at);
CREATE INDEX IF NOT EXISTS idx_shifts_started ON shifts(started_at);
CREATE INDEX IF NOT EXISTS idx_corrections_event ON corrections(original_event_id);
CREATE INDEX IF NOT EXISTS idx_llm_outputs_msg ON llm_outputs(msg_id);
