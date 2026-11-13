"""sqlite 存储：valid 与 quarantine 两张表，按 record_id 幂等写入。"""
import json
import os
import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS valid (
    record_id TEXT PRIMARY KEY,
    retries   INTEGER NOT NULL DEFAULT 0,
    data      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine (
    record_id TEXT PRIMARY KEY,
    rule_id   TEXT NOT NULL,
    reason    TEXT NOT NULL,
    raw       TEXT NOT NULL,
    retries   INTEGER NOT NULL DEFAULT 0
);
"""


class Store:
    def __init__(self, path):
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(_SCHEMA)

    def has(self, record_id):
        cur = self.conn.execute(
            "SELECT 1 FROM valid WHERE record_id = ? "
            "UNION ALL SELECT 1 FROM quarantine WHERE record_id = ? LIMIT 1",
            (record_id, record_id),
        )
        return cur.fetchone() is not None

    def add_valid(self, record_id, record, retries):
        self.conn.execute(
            "INSERT OR IGNORE INTO valid (record_id, retries, data) VALUES (?, ?, ?)",
            (record_id, retries, json.dumps(record, ensure_ascii=False, sort_keys=True)),
        )
        self.conn.commit()

    def add_quarantine(self, record_id, rule_id, reason, raw_record, retries):
        self.conn.execute(
            "INSERT OR IGNORE INTO quarantine (record_id, rule_id, reason, raw, retries)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                record_id,
                rule_id,
                reason,
                json.dumps(raw_record, ensure_ascii=False, sort_keys=True),
                retries,
            ),
        )
        self.conn.commit()

    def counts(self):
        valid = self.conn.execute("SELECT COUNT(*) FROM valid").fetchone()[0]
        quarantine = self.conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0]
        return valid, quarantine

    def close(self):
        self.conn.close()
