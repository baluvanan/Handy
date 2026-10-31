//! Keeps `analysis-worker/tests/fixtures/handy_schema.sql` identical to the
//! schema Handy's **real** migrations produce (upstream History migrations
//! plus the fork's dataset migrations), so the worker's tests build their
//! databases from the same schema Handy writes, never a hand-written one.
//!
//! When a migration changes, this test fails; regenerate with
//! `UPDATE_SCHEMA_FIXTURE=1 cargo test schema_fixture` and update the
//! worker's known schema versions (`handy_db.py`) in the same commit.

#[cfg(test)]
mod tests {
    use rusqlite::Connection;
    use std::path::PathBuf;

    use crate::managers::history::HistoryManager;
    use crate::personal::dataset;

    fn fixture_path() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("analysis-worker")
            .join("tests")
            .join("fixtures")
            .join("handy_schema.sql")
    }

    /// The SQL script that recreates the migrated schema.
    pub(crate) fn dump_schema() -> String {
        let mut conn = Connection::open_in_memory().unwrap();
        HistoryManager::apply_migrations(&mut conn).unwrap();
        dataset::migrate(&mut conn, dataset::MIGRATIONS).unwrap();

        let user_version: i64 = conn
            .pragma_query_value(None, "user_version", |r| r.get(0))
            .unwrap();
        let mut out = String::from(
            "-- GENERATED from Handy's real migrations by the Rust test\n\
             -- personal::schema_fixture (src-tauri/src/personal/schema_fixture.rs).\n\
             -- Do not edit: run `UPDATE_SCHEMA_FIXTURE=1 cargo test schema_fixture`.\n",
        );
        out.push_str(&format!("PRAGMA user_version = {user_version};\n"));
        let mut stmt = conn
            .prepare(
                "SELECT sql FROM sqlite_master
                 WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
                 ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, rowid",
            )
            .unwrap();
        let sqls: Vec<String> = stmt
            .query_map([], |r| r.get(0))
            .unwrap()
            .collect::<Result<_, _>>()
            .unwrap();
        for sql in sqls {
            out.push_str(sql.trim());
            out.push_str(";\n");
        }
        let mut stmt = conn
            .prepare("SELECT name, version FROM handy_personal_schema ORDER BY name")
            .unwrap();
        let rows: Vec<(String, i64)> = stmt
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))
            .unwrap()
            .collect::<Result<_, _>>()
            .unwrap();
        for (name, version) in rows {
            out.push_str(&format!(
                "INSERT INTO handy_personal_schema (name, version) VALUES ('{name}', {version});\n"
            ));
        }
        out
    }

    #[test]
    fn worker_schema_fixture_matches_real_migrations() {
        let expected = dump_schema();
        let path = fixture_path();
        if std::env::var_os("UPDATE_SCHEMA_FIXTURE").is_some() {
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(&path, &expected).unwrap();
        }
        let actual = std::fs::read_to_string(&path).unwrap_or_default();
        assert_eq!(
            actual,
            expected,
            "{} is out of date; run UPDATE_SCHEMA_FIXTURE=1 cargo test schema_fixture",
            path.display()
        );
    }

    #[test]
    fn the_dump_recreates_an_equivalent_database() {
        let script = dump_schema();
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(&script).unwrap();
        let version: i64 = conn
            .pragma_query_value(None, "user_version", |r| r.get(0))
            .unwrap();
        assert!(version > 0);
        let n: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM pragma_table_info('dictation_log')",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(n > 10);
    }
}
