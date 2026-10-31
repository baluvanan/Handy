-- GENERATED from Handy's real migrations by the Rust test
-- personal::schema_fixture (src-tauri/src/personal/schema_fixture.rs).
-- Do not edit: run `UPDATE_SCHEMA_FIXTURE=1 cargo test schema_fixture`.
PRAGMA user_version = 4;
CREATE TABLE transcription_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            saved BOOLEAN NOT NULL DEFAULT 0,
            title TEXT NOT NULL,
            transcription_text TEXT NOT NULL
        , post_processed_text TEXT, post_process_prompt TEXT, post_process_requested BOOLEAN NOT NULL DEFAULT 0, source_name TEXT, language TEXT, language_source TEXT);
CREATE TABLE handy_personal_schema (
            name TEXT PRIMARY KEY,
            version INTEGER NOT NULL
        );
CREATE TABLE dictation_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        audio_file TEXT,
        audio_path TEXT,
        model_id TEXT,
        language TEXT,
        raw_asr_text TEXT NOT NULL,
        rule_cleaned_text TEXT NOT NULL,
        llm_text TEXT,
        final_text TEXT NOT NULL,
        post_process_requested INTEGER NOT NULL DEFAULT 0,
        llm_fallback_reason TEXT,
        verbatim INTEGER NOT NULL DEFAULT 0,
        app_bundle_id TEXT,
        recording_ms INTEGER,
        speech_audio_ms INTEGER,
        vad_segments TEXT NOT NULL DEFAULT '[]',
        asr_ms INTEGER,
        cleanup_ms INTEGER,
        llm_ms INTEGER,
        paste_ms INTEGER,
        total_ms INTEGER,
        outcome TEXT
    );
CREATE INDEX idx_dictation_log_created_at ON dictation_log(created_at);
INSERT INTO handy_personal_schema (name, version) VALUES ('dictation_log', 2);
