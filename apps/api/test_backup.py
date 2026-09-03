"""Backup restore path + retention — pure/unit, no R2 or Postgres."""
from api.backup import coerce_value, insert_sql, prune_backups


class _Store:
    def __init__(self, keys):
        self.keys = list(keys)
        self.deleted = []

    def list(self, prefix):
        return [k for k in self.keys if k.startswith(prefix)]

    def delete(self, keys):
        self.deleted += keys
        self.keys = [k for k in self.keys if k not in set(keys)]
        return len(keys)


def test_insert_sql_casts_by_column_type_and_is_idempotent():
    sql = insert_sql("rs_map", ["id", "rows", "created_at", "tags"],
                     {"id": "text", "rows": "jsonb", "created_at": "timestamptz", "tags": "_text"})
    assert sql == ('INSERT INTO rs_map ("id", "rows", "created_at", "tags") VALUES '
                   '($1::text, $2::jsonb, $3::timestamptz, $4::text[]) ON CONFLICT DO NOTHING')
    assert coerce_value({"a": 1}, "jsonb") == '{"a": 1}'          # dict → JSON text for jsonb
    assert coerce_value('{"a": 1}', "jsonb") == '{"a": 1}'        # already text (asyncpg dumps jsonb as text)
    assert coerce_value(["x", "y"], "_text") == ["x", "y"]        # arrays stay lists
    assert coerce_value("2026-09-03T07:31:04+00:00", "timestamptz") == "2026-09-03T07:31:04+00:00"
    assert coerce_value(None, "jsonb") is None


def test_prune_keeps_newest_complete_backups_and_unfinished_recent_runs():
    st = _Store(["backups/2026-08-30/MANIFEST.json", "backups/2026-08-30/rs_map.jsonl.gz",
                 "backups/2026-09-01/MANIFEST.json", "backups/2026-09-01/rs_job-part0000.jsonl.gz",
                 "backups/2026-09-02/rs_entity-part0000.jsonl.gz",          # unfinished (no manifest), recent
                 "backups/2026-09-03/MANIFEST.json", "backups/2026-09-03/rs_job-part0000.jsonl.gz"])
    dry = prune_backups(st, keep=2, dry=True)
    assert dry["kept"] == ["2026-09-01", "2026-09-03"] and dry["deleted"] == {"2026-08-30": 2} and st.deleted == []
    res = prune_backups(st, keep=2)
    assert res["deleted"] == {"2026-08-30": 2}
    assert not any(k.startswith("backups/2026-08-30/") for k in st.keys)
    assert any(k.startswith("backups/2026-09-02/") for k in st.keys)       # unfinished newer run untouched
    res = prune_backups(st, keep=1)
    assert res["kept"] == ["2026-09-03"] and set(res["deleted"]) == {"2026-09-01", "2026-09-02"}
