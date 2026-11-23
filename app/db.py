from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.domain import SEED_DEAD_LEADS


def new_id() -> str:
    """Generate a time-ordered RFC 9562 UUIDv7 string across Python 3.12–3.14+."""
    uuid7_fn = getattr(uuid, "uuid7", None)
    if callable(uuid7_fn):
        return str(uuid7_fn())
    ts_ms = time.time_ns() // 1_000_000
    rand_a = int.from_bytes(os.urandom(2), "big") & 0x0FFF
    rand_b = int.from_bytes(os.urandom(8), "big") & 0x3FFFFFFFFFFFFFFF
    value = (
        ((ts_ms & 0xFFFFFFFFFFFF) << 80)
        | (0x7 << 76)
        | (rand_a << 64)
        | (0x2 << 62)
        | rand_b
    )
    return str(uuid.UUID(int=value))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    resolved = Path(db_path or get_settings().SQLITE_DB_PATH)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(db_path: str | None = None) -> None:
    with get_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS contacts (
                id TEXT PRIMARY KEY,
                full_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT NOT NULL DEFAULT '',
                doc_type TEXT NOT NULL DEFAULT 'ITIN',
                interested_lot_code TEXT NOT NULL DEFAULT 'LOT-AV-01',
                down_payment_cents INTEGER NOT NULL DEFAULT 350000,
                pipeline_stage TEXT NOT NULL DEFAULT 'NEW_LEAD',
                hitl_state TEXT NOT NULL DEFAULT 'AI_AUTONOMOUS',
                channel_origin TEXT NOT NULL DEFAULT 'WEB_CHAT',
                ghl_contact_id TEXT NOT NULL DEFAULT '',
                ghl_opportunity_id TEXT NOT NULL DEFAULT '',
                ghl_sync_mode TEXT NOT NULL DEFAULT 'PENDING',
                ghl_last_latency_ms INTEGER NOT NULL DEFAULT 0,
                tags_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
                sender_role TEXT NOT NULL,
                channel TEXT NOT NULL,
                body TEXT NOT NULL,
                meta_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS hitl_requests (
                id TEXT PRIMARY KEY,
                contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
                lot_code TEXT NOT NULL,
                offered_down_cents INTEGER NOT NULL,
                required_min_down_cents INTEGER NOT NULL,
                reason_es TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                resolved_by TEXT NOT NULL DEFAULT '',
                resolution_note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                resolved_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS voice_calls (
                id TEXT PRIMARY KEY,
                contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                external_call_id TEXT NOT NULL,
                ca_632_disclosure_played INTEGER NOT NULL DEFAULT 1,
                duration_seconds INTEGER NOT NULL DEFAULT 0,
                summary_es TEXT NOT NULL,
                transcript_text TEXT NOT NULL,
                recording_url TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS crm_sync_events (
                id TEXT PRIMARY KEY,
                contact_id TEXT NOT NULL,
                adapter_name TEXT NOT NULL,
                operation TEXT NOT NULL,
                mode TEXT NOT NULL,
                http_status INTEGER NOT NULL,
                latency_ms INTEGER NOT NULL,
                external_id TEXT NOT NULL DEFAULT '',
                payload_preview_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS dead_leads (
                id TEXT PRIMARY KEY,
                full_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                origin_channel TEXT NOT NULL,
                last_note TEXT NOT NULL,
                estimated_down_usd INTEGER NOT NULL,
                reactivation_outcome TEXT NOT NULL,
                ai_opening_es TEXT NOT NULL,
                simulated_reply_es TEXT NOT NULL,
                processed INTEGER NOT NULL DEFAULT 0,
                promoted_contact_id TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS oauth_tokens (
                provider TEXT PRIMARY KEY,
                location_id TEXT NOT NULL,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            """
        )
        existing_dl = conn.execute("SELECT COUNT(*) FROM dead_leads").fetchone()[0]
        if existing_dl == 0:
            for item in SEED_DEAD_LEADS:
                conn.execute(
                    """
                    INSERT INTO dead_leads (
                        id, full_name, phone, origin_channel, last_note,
                        estimated_down_usd, reactivation_outcome, ai_opening_es, simulated_reply_es
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["id"],
                        item["full_name"],
                        item["phone"],
                        item["origin_channel"],
                        item["last_note"],
                        item["estimated_down_usd"],
                        item["reactivation_outcome"],
                        item["ai_opening_es"],
                        item["simulated_reply_es"],
                    ),
                )
        conn.commit()


def row_to_contact_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["tags"] = json.loads(d.pop("tags_json", "[]") or "[]")
    d["down_payment_usd"] = round(d["down_payment_cents"] / 100, 2)
    return d
