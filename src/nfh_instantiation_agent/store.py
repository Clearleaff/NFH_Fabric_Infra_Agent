from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import AuditEvent


class SQLiteStateStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute("create table if not exists state (key text primary key, value text not null)")
            conn.execute(
                "create table if not exists audit (id integer primary key autoincrement, event text not null)"
            )
            conn.execute(
                "create table if not exists manifests (release_id text primary key, manifest text not null)"
            )
            conn.execute("create table if not exists plans (plan_id text primary key, plan text not null)")
            conn.execute("create table if not exists approvals (id integer primary key autoincrement, approval text not null)")

    def get_json(self, key: str, default: Any = None) -> Any:
        with self._conn() as conn:
            row = conn.execute("select value from state where key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put_json(self, key: str, value: Any) -> None:
        with self._conn() as conn:
            conn.execute(
                "insert into state(key, value) values(?, ?) on conflict(key) do update set value = excluded.value",
                (key, json.dumps(value, sort_keys=True)),
            )

    def list_json(self, prefix: str) -> list[Any]:
        with self._conn() as conn:
            rows = conn.execute("select value from state where key like ? order by key", (f"{prefix}%",)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def audit(self, event: AuditEvent) -> None:
        with self._conn() as conn:
            conn.execute("insert into audit(event) values(?)", (json.dumps(event.__dict__, sort_keys=True),))

    def save_manifest(self, release_id: str, manifest: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                "insert into manifests(release_id, manifest) values(?, ?) on conflict(release_id) do update set manifest = excluded.manifest",
                (release_id, json.dumps(manifest, sort_keys=True)),
            )

    def load_manifest(self, release_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("select manifest from manifests where release_id = ?", (release_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def manifest_count(self) -> int:
        with self._conn() as conn:
            row = conn.execute("select count(*) from manifests").fetchone()
        return int(row[0])

    def save_plan(self, plan_id: str, plan: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute("insert into plans(plan_id, plan) values(?, ?) on conflict(plan_id) do update set plan = excluded.plan",
                         (plan_id, json.dumps(plan, sort_keys=True)))

    def load_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("select plan from plans where plan_id = ?", (plan_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def record_approval(self, approval: dict[str, Any]) -> None:
        # Append-only evidence; approval never overwrites a different plan.
        with self._conn() as conn:
            conn.execute("insert into approvals(approval) values(?)", (json.dumps(approval, sort_keys=True),))

    def audit_events(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("select event from audit order by id").fetchall()
        return [json.loads(row[0]) for row in rows]

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)
