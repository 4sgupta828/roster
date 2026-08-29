"""Live product settings — DB-backed toggles the admin UI can flip WITHOUT a redeploy.

Resolution order for a controlled flag: DB override ("on"/"off") wins; empty/absent → the env-var
default (so the env flag remains the boot default and the panel is a live override, mirroring the
factra `rs_worker_setting` pattern). Values are read through a short in-process TTL cache so per-request
checks don't hit Postgres.

Same store discipline as sessions/accounts: vertical-scoped, lazy DDL, best-effort (a settings-store
failure falls back to the env default — never breaks a request).
"""
from __future__ import annotations

import time
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS roster_setting (
    vertical   TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (vertical, key)
);
"""

_TTL_S = 15.0   # how long a read is served from memory — a toggle applies within this window


class SettingStore:
    def __init__(self, dsn: str, *, vertical: str):
        self._dsn = dsn
        self._vertical = vertical
        self._pool = None
        self._ready = False
        self._cache: dict[str, str] = {}
        self._cache_at = 0.0

    async def _get_pool(self):
        if self._pool is None:
            import asyncpg
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=2)
        return self._pool

    async def _ensure(self) -> None:
        if self._ready:
            return
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(_DDL)
        self._ready = True

    async def all(self, *, fresh: bool = False) -> dict[str, str]:
        """All overrides for this vertical, via the TTL cache (fresh=True bypasses it)."""
        now = time.monotonic()
        if not fresh and (now - self._cache_at) < _TTL_S:
            return dict(self._cache)
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM roster_setting WHERE vertical=$1", self._vertical)
        self._cache = {r["key"]: r["value"] for r in rows}
        self._cache_at = now
        return dict(self._cache)

    async def get(self, key: str) -> str:
        return (await self.all()).get(key, "")

    async def set(self, key: str, value: str) -> None:
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if value == "":
                await conn.execute(
                    "DELETE FROM roster_setting WHERE vertical=$1 AND key=$2", self._vertical, key)
            else:
                await conn.execute(
                    """INSERT INTO roster_setting (vertical, key, value) VALUES ($1,$2,$3)
                       ON CONFLICT (vertical, key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()""",
                    self._vertical, key, value)
        self._cache_at = 0.0   # bust the cache so the change is visible immediately in this process

    @staticmethod
    def resolve_flag(override: str, env_default: bool) -> bool:
        """'on'/'off' override wins; anything else follows the env default."""
        v = (override or "").strip().lower()
        if v in ("on", "1", "true"):
            return True
        if v in ("off", "0", "false"):
            return False
        return env_default
