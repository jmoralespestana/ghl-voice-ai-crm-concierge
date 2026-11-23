from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.config import Settings, get_settings
from app.db import get_connection, new_id, row_to_contact_dict, utc_now_iso
from app.domain import HitlState, PipelineStage


@dataclass(frozen=True)
class CRMSyncResult:
    adapter_name: str
    mode: str
    contact_external_id: str
    opportunity_external_id: str
    http_status: int
    latency_ms: int
    detail: str


class CRMAdapter(Protocol):
    name: str

    async def sync_contact_and_deal(
        self,
        contact: dict[str, Any],
        note_body: str | None = None,
    ) -> CRMSyncResult: ...


def _resolve_ghl_credentials(
    settings: Settings, db_path: str | None = None
) -> tuple[str, str, str]:
    """Return `(access_token, location_id, auth_source)` preferring live OAuth row, then `.env` PIT."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT access_token, location_id FROM oauth_tokens WHERE provider = 'ghl'"
        ).fetchone()
    if row and row["access_token"] and row["location_id"]:
        return str(row["access_token"]), str(row["location_id"]), "OAUTH_APP"
    if settings.GHL_ACCESS_TOKEN.strip() and settings.GHL_LOCATION_ID.strip():
        return (
            settings.GHL_ACCESS_TOKEN.strip(),
            settings.GHL_LOCATION_ID.strip(),
            "PRIVATE_INTEGRATION_TOKEN",
        )
    return "", "", "SANDBOX_PREVIEW"


def _compute_tags_for_contact(contact: dict[str, Any]) -> list[str]:
    tags: set[str] = {"horizon-concierge-ai"}
    doc = (contact.get("doc_type") or "ITIN").upper()
    tags.add(f"doc-{doc.lower()}")
    lot_code = (contact.get("interested_lot_code") or "").lower()
    if lot_code:
        tags.add(f"lot-{lot_code}")
    hitl_state: HitlState = contact.get("hitl_state", "AI_AUTONOMOUS")
    if hitl_state == "HUMAN_TAKEN_OVER":
        tags.add("human-takeover")
        tags.add("ai-paused")
    elif hitl_state == "PENDING_HITL":
        tags.add("pending-hitl")
    else:
        tags.add("ai-autonomous")
    return sorted(tags)


class LocalSQLiteCRMAdapter:
    name = "local_sqlite"

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path

    async def sync_contact_and_deal(
        self,
        contact: dict[str, Any],
        note_body: str | None = None,
    ) -> CRMSyncResult:
        started = time.perf_counter()
        cid = str(contact["id"])
        tags = _compute_tags_for_contact(contact)
        with get_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE contacts SET tags_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(tags), utc_now_iso(), cid),
            )
            latency_ms = max(1, int((time.perf_counter() - started) * 1000))
            conn.execute(
                """
                INSERT INTO crm_sync_events (
                    id, contact_id, adapter_name, operation, mode,
                    http_status, latency_ms, external_id, payload_preview_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id(),
                    cid,
                    self.name,
                    "UPSERT_LOCAL_CANONICAL",
                    "LOCAL_WAL",
                    200,
                    latency_ms,
                    cid,
                    json.dumps({"stage": contact.get("pipeline_stage"), "tags": tags}),
                    utc_now_iso(),
                ),
            )
            conn.commit()
        return CRMSyncResult(
            adapter_name=self.name,
            mode="LOCAL_WAL",
            contact_external_id=cid,
            opportunity_external_id=f"local-opp-{cid[:8]}",
            http_status=200,
            latency_ms=latency_ms,
            detail="Canonical SQLite WAL ledger updated.",
        )


class GoHighLevelCRMAdapter:
    """
    GoHighLevel API v2 Strategy Adapter (`https://services.leadconnectorhq.com`).
    Executes real HTTP calls when `GHL_ACCESS_TOKEN` + `GHL_LOCATION_ID` (or OAuth install)
    are present; otherwise records an inspectable `SANDBOX_PREVIEW` envelope with exact v2 payloads.
    """

    name = "gohighlevel_v2"

    def __init__(
        self,
        settings: Settings | None = None,
        db_path: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.db_path = db_path
        self._client = http_client

    def _stage_id_for(self, stage: PipelineStage | str) -> str:
        mapping = {
            "NEW_LEAD": self.settings.GHL_STAGE_NEW_ID,
            "QUALIFIED_ITIN": self.settings.GHL_STAGE_QUALIFIED_ID,
            "PENDING_HITL_APPROVAL": self.settings.GHL_STAGE_HITL_ID,
            "SHOWING_BOOKED": self.settings.GHL_STAGE_BOOKED_ID,
        }
        return (mapping.get(stage) or self.settings.GHL_STAGE_NEW_ID or "").strip()

    async def sync_contact_and_deal(
        self,
        contact: dict[str, Any],
        note_body: str | None = None,
    ) -> CRMSyncResult:
        started = time.perf_counter()
        token, location_id, auth_source = _resolve_ghl_credentials(
            self.settings, self.db_path
        )
        tags = _compute_tags_for_contact(contact)

        upsert_payload: dict[str, Any] = {
            "locationId": location_id or "PENDING_GHL_LOCATION_ID",
            "name": contact["full_name"],
            "phone": contact["phone"],
            "source": "Horizon Estates AI Concierge (GHL API v2)",
            "tags": tags,
        }
        if contact.get("email"):
            upsert_payload["email"] = contact["email"]

        if not token or not location_id:
            digest = hashlib.sha256(
                f"{contact['id']}:{contact['phone']}".encode()
            ).hexdigest()[:12]
            preview_cid = contact.get("ghl_contact_id") or f"ghl_sbx_{digest}"
            preview_oid = contact.get("ghl_opportunity_id") or f"ghl_opp_{digest[:10]}"
            latency_ms = max(1, int((time.perf_counter() - started) * 1000))
            self._persist_sync_result(
                contact_id=contact["id"],
                mode="SANDBOX_PREVIEW",
                http_status=202,
                latency_ms=latency_ms,
                ghl_contact_id=preview_cid,
                ghl_opp_id=preview_oid,
                payload_preview={
                    "endpoint": f"{self.settings.GHL_BASE_URL}/contacts/upsert",
                    "body": upsert_payload,
                    "note": (note_body or "")[:240],
                },
            )
            return CRMSyncResult(
                adapter_name=self.name,
                mode="SANDBOX_PREVIEW",
                contact_external_id=preview_cid,
                opportunity_external_id=preview_oid,
                http_status=202,
                latency_ms=latency_ms,
                detail="GHL v2 payload staged (paste GHL_ACCESS_TOKEN & GHL_LOCATION_ID to switch to LIVE).",
            )

        headers = {
            "Authorization": f"Bearer {token}",
            "Version": self.settings.GHL_API_VERSION,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        base = self.settings.GHL_BASE_URL.rstrip("/")
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=8.0)

        ghl_cid = str(contact.get("ghl_contact_id") or "")
        ghl_oid = str(contact.get("ghl_opportunity_id") or "")
        status_code = 200
        detail_msg = f"Synced via {auth_source}"

        try:
            resp = await client.post(
                f"{base}/contacts/upsert", headers=headers, json=upsert_payload
            )
            status_code = resp.status_code
            if resp.is_success:
                body = resp.json()
                c_obj = body.get("contact") or body
                ghl_cid = str(c_obj.get("id") or ghl_cid)
            else:
                detail_msg = f"GHL contacts/upsert HTTP {resp.status_code}: {resp.text[:160]}"

            if ghl_cid and note_body:
                await client.post(
                    f"{base}/contacts/{ghl_cid}/notes",
                    headers=headers,
                    json={"body": note_body[:4000]},
                )

            stage_id = self._stage_id_for(contact.get("pipeline_stage", "NEW_LEAD"))
            if ghl_cid and self.settings.GHL_PIPELINE_ID.strip() and stage_id:
                opp_payload = {
                    "pipelineId": self.settings.GHL_PIPELINE_ID.strip(),
                    "locationId": location_id,
                    "contactId": ghl_cid,
                    "name": f"{contact['full_name']} — {contact.get('interested_lot_code', 'LOT-AV-01')}",
                    "pipelineStageId": stage_id,
                    "status": "open",
                    "monetaryValue": round(
                        int(contact.get("down_payment_cents", 350000)) / 100, 2
                    ),
                }
                opp_resp = await client.post(
                    f"{base}/opportunities/upsert", headers=headers, json=opp_payload
                )
                if opp_resp.is_success:
                    opp_data = opp_resp.json()
                    opp_obj = opp_data.get("opportunity") or opp_data
                    ghl_oid = str(opp_obj.get("id") or ghl_oid)
        except httpx.HTTPError as exc:
            status_code = 503
            detail_msg = f"GHL network error: {exc}"
        finally:
            if owns_client:
                await client.aclose()

        latency_ms = max(1, int((time.perf_counter() - started) * 1000))
        self._persist_sync_result(
            contact_id=contact["id"],
            mode=f"LIVE_{auth_source}" if 200 <= status_code < 300 else "LIVE_ERROR",
            http_status=status_code,
            latency_ms=latency_ms,
            ghl_contact_id=ghl_cid,
            ghl_opp_id=ghl_oid,
            payload_preview={"upsert": upsert_payload, "detail": detail_msg},
        )
        return CRMSyncResult(
            adapter_name=self.name,
            mode=f"LIVE_{auth_source}" if 200 <= status_code < 300 else "LIVE_ERROR",
            contact_external_id=ghl_cid,
            opportunity_external_id=ghl_oid,
            http_status=status_code,
            latency_ms=latency_ms,
            detail=detail_msg,
        )

    def _persist_sync_result(
        self,
        *,
        contact_id: str,
        mode: str,
        http_status: int,
        latency_ms: int,
        ghl_contact_id: str,
        ghl_opp_id: str,
        payload_preview: dict[str, Any],
    ) -> None:
        with get_connection(self.db_path) as conn:
            conn.execute(
                """
                UPDATE contacts
                SET ghl_contact_id = ?, ghl_opportunity_id = ?, ghl_sync_mode = ?,
                    ghl_last_latency_ms = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    ghl_contact_id,
                    ghl_opp_id,
                    mode,
                    latency_ms,
                    utc_now_iso(),
                    contact_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO crm_sync_events (
                    id, contact_id, adapter_name, operation, mode,
                    http_status, latency_ms, external_id, payload_preview_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id(),
                    contact_id,
                    self.name,
                    "CONTACTS_AND_OPPORTUNITIES_UPSERT",
                    mode,
                    http_status,
                    latency_ms,
                    ghl_contact_id,
                    json.dumps(payload_preview, ensure_ascii=False),
                    utc_now_iso(),
                ),
            )
            conn.commit()


class DualWriteCRMOrchestrator:
    """Executes LocalSQLiteCRMAdapter first (zero data loss), then secondary adapter (GHL v2)."""

    def __init__(
        self,
        settings: Settings | None = None,
        db_path: str | None = None,
        ghl_http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.db_path = db_path
        self.local = LocalSQLiteCRMAdapter(db_path=db_path)
        self.ghl = GoHighLevelCRMAdapter(
            settings=self.settings, db_path=db_path, http_client=ghl_http_client
        )

    async def sync(
        self, contact_id: str, note_body: str | None = None
    ) -> list[CRMSyncResult]:
        with get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM contacts WHERE id = ?", (contact_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"Contact {contact_id} not found")
        contact = row_to_contact_dict(row)
        results = [await self.local.sync_contact_and_deal(contact, note_body=note_body)]
        if self.settings.CRM_PROVIDER == "dual_ghl":
            with get_connection(self.db_path) as conn:
                refreshed = row_to_contact_dict(
                    conn.execute(
                        "SELECT * FROM contacts WHERE id = ?", (contact_id,)
                    ).fetchone()
                )
            results.append(
                await self.ghl.sync_contact_and_deal(refreshed, note_body=note_body)
            )
        return results
