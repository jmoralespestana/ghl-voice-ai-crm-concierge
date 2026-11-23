from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
import urllib.parse

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel, Field

from app.adapters.crm import DualWriteCRMOrchestrator, _resolve_ghl_credentials
from app.adapters.voice import (
    RetellVoiceAdapter,
    VOICE_TOOLS_DECLARATIONS,
    execute_voice_tool,
    get_voice_adapter,
    ingest_completed_voice_call,
    run_vertex_gemini_voice_agent_turn,
    synthesize_spanish_speech_mp3_b64,
)
from app.config import get_settings
from app.db import get_connection, init_db, new_id, row_to_contact_dict, utc_now_iso
from app.domain import (
    CA_TWO_PARTY_NOTICE_ES,
    DocType,
    SEED_DEAD_LEADS,
    SEED_LOTS,
    calculate_seller_financing,
)
from app.events import broadcaster
from app.services.concierge import (
    get_or_create_contact,
    handle_buyer_chat_turn,
    record_message,
    resolve_hitl_action,
    run_dead_leads_reactivation_batch,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Horizon Estates — GoHighLevel API v2, Dual Voice AI & HITL Concierge",
    version="0.1.0",
    lifespan=lifespan,
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# --- Request Schemas ---


class QuoteRequest(BaseModel):
    lot_code: str = "LOT-AV-01"
    down_payment_usd: int = Field(default=3500, ge=0, le=200_000)
    term_months: int = Field(default=60)
    doc_type: DocType = "ITIN"


class BuyerChatRequest(BaseModel):
    full_name: str = "Prospecto Demo"
    phone: str = "+17605550199"
    message: str
    channel: str = "WEB_CHAT"
    selected_lot_code: str = "LOT-AV-01"
    term_months: int = 60
    doc_type: DocType = "ITIN"
    include_audio: bool = False


class TtsRequest(BaseModel):
    text: str


class PinVerifyRequest(BaseModel):
    pin: str


class HitlActionRequest(BaseModel):
    contact_id: str
    action: Literal["APPROVE_EXCEPTION", "TAKE_OVER", "SEND_HUMAN_REPLY", "RELEASE_TO_AI"]
    advisor_name: str = "Director Comercial (Ventas)"
    custom_message: str = ""


class VoiceSessionRequest(BaseModel):
    provider: Literal["gemini_live", "retell"] | None = None
    buyer_name: str = "Visitante Demo Apple Valley"
    buyer_phone: str = "+17605550177"


class GeminiLiveTurnRequest(BaseModel):
    session_id: str
    buyer_name: str = "Visitante Demo Apple Valley"
    buyer_phone: str = "+17605550177"
    spoken_text: str
    selected_lot_code: str = "LOT-AV-01"


class VoiceCallCompleteRequest(BaseModel):
    session_id: str
    provider: Literal["gemini_live", "retell"] = "gemini_live"
    buyer_name: str = "Visitante Demo Apple Valley"
    buyer_phone: str = "+17605550177"
    lot_code: str = "LOT-AV-01"
    duration_seconds: int = 42
    summary_es: str = ""
    transcript_text: str = ""


# --- Core Frontend & Domain Endpoints ---


@app.get("/", include_in_schema=False)
def serve_dual_view_ui() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/v1/auth/pin-status")
def pin_status_endpoint() -> dict[str, Any]:
    cfg = get_settings()
    pin_required = bool(cfg.DEMO_ACCESS_PIN.strip())
    return {
        "pin_required": pin_required,
        "hint_es": "Ingresa el PIN de invitación ejecutiva para desbloquear el entorno demo.",
    }


@app.post("/api/v1/auth/verify-pin")
def verify_pin_endpoint(payload: PinVerifyRequest) -> dict[str, Any]:
    cfg = get_settings()
    expected = cfg.DEMO_ACCESS_PIN.strip()
    if not expected or payload.pin.strip() == expected:
        return {"unlocked": True}
    raise HTTPException(status_code=401, detail="PIN incorrecto.")


@app.get("/api/v1/lots")
def list_lots_endpoint() -> dict[str, Any]:
    return {
        "company": "Horizon Estates (Realty Concierge Demo)",
        "headquarters": "High Desert · San Bernardino County, CA",
        "lots": [lot.to_public_dict() for lot in SEED_LOTS],
    }


@app.post("/api/v1/financing/quote")
def quote_financing_endpoint(payload: QuoteRequest) -> dict[str, Any]:
    try:
        quote = calculate_seller_financing(
            lot_code=payload.lot_code,
            down_payment_cents=payload.down_payment_usd * 100,
            term_months=payload.term_months,
            doc_type=payload.doc_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return quote.to_dict()


@app.post("/api/v1/chat")
async def buyer_chat_endpoint(payload: BuyerChatRequest) -> dict[str, Any]:
    return await handle_buyer_chat_turn(
        full_name=payload.full_name,
        phone=payload.phone,
        message=payload.message,
        channel=payload.channel,
        selected_lot_code=payload.selected_lot_code,
        term_months=payload.term_months,
        doc_type=payload.doc_type,
        include_audio=payload.include_audio,
    )


@app.post("/api/v1/voice/tts")
async def synthesize_tts_endpoint(payload: TtsRequest) -> dict[str, str]:
    b64 = await synthesize_spanish_speech_mp3_b64(payload.text)
    return {"audio_mp3_base64": b64}


@app.get("/api/v1/cockpit/snapshot")
def cockpit_snapshot_endpoint() -> dict[str, Any]:
    cfg = get_settings()
    token, location_id, auth_source = _resolve_ghl_credentials(cfg)
    with get_connection() as conn:
        contacts = [
            row_to_contact_dict(r)
            for r in conn.execute(
                "SELECT * FROM contacts ORDER BY updated_at DESC"
            ).fetchall()
        ]
        messages = [
            {
                **dict(r),
                "meta": json.loads(r["meta_json"] or "{}"),
            }
            for r in conn.execute(
                "SELECT id, contact_id, sender_role, channel, body, meta_json, created_at "
                "FROM messages ORDER BY created_at ASC LIMIT 200"
            ).fetchall()
        ]
        hitl_requests = [
            {
                **dict(r),
                "offered_down_usd": r["offered_down_cents"] // 100,
                "required_min_down_usd": r["required_min_down_cents"] // 100,
            }
            for r in conn.execute(
                "SELECT * FROM hitl_requests ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
        ]
        voice_calls = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM voice_calls ORDER BY created_at DESC LIMIT 30"
            ).fetchall()
        ]
        sync_events = [
            {
                **dict(r),
                "payload_preview": json.loads(r["payload_preview_json"] or "{}"),
            }
            for r in conn.execute(
                "SELECT id, contact_id, adapter_name, operation, mode, http_status, "
                "latency_ms, external_id, payload_preview_json, created_at "
                "FROM crm_sync_events ORDER BY created_at DESC LIMIT 40"
            ).fetchall()
        ]
        dead_leads = [
            dict(r)
            for r in conn.execute("SELECT * FROM dead_leads ORDER BY id ASC").fetchall()
        ]

    return {
        "ghl": {
            "live_connected": bool(token and location_id),
            "auth_source": auth_source,
            "location_id_masked": (
                f"{location_id[:4]}•••{location_id[-4:]}"
                if len(location_id) >= 8
                else (location_id or None)
            ),
            "pipeline_configured": bool(cfg.GHL_PIPELINE_ID.strip()),
        },
        "voice": {
            "active_default_provider": cfg.VOICE_PROVIDER,
            "available_providers": [
                {
                    "name": "gemini_live",
                    "label": "Gemini Live WebSocket (0 Teléfono Expuesto · Demo Default)",
                    "exposes_phone_number": False,
                    "ready": True,
                },
                {
                    "name": "retell",
                    "label": "Retell AI (Web Call / PSTN Opcional)",
                    "exposes_phone_number": bool(cfg.RETELL_PUBLIC_PHONE.strip()),
                    "public_phone": cfg.RETELL_PUBLIC_PHONE.strip() or None,
                    "ready": cfg.retell_configured,
                },
            ],
            "ca_632_two_party_consent": cfg.CALIFORNIA_TWO_PARTY_CONSENT,
            "ca_632_notice_es": CA_TWO_PARTY_NOTICE_ES,
        },
        "contacts": contacts,
        "messages": messages,
        "hitl_requests": hitl_requests,
        "voice_calls": voice_calls,
        "crm_sync_events": sync_events,
        "dead_leads": dead_leads,
    }


@app.post("/api/v1/hitl/action")
async def hitl_action_endpoint(payload: HitlActionRequest) -> dict[str, Any]:
    try:
        return await resolve_hitl_action(
            contact_id=payload.contact_id,
            action=payload.action,
            advisor_name=payload.advisor_name,
            custom_message=payload.custom_message,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/v1/reactivation/run")
async def run_reactivation_endpoint() -> dict[str, Any]:
    return await run_dead_leads_reactivation_batch()


@app.post("/api/v1/cockpit/reset")
async def reset_demo_state_endpoint(seed_ghl: bool = True) -> dict[str, Any]:
    cfg = get_settings()
    token, location_id, _ = _resolve_ghl_credentials(cfg)

    # 1. Purge previous Contacts & Opportunities in GoHighLevel Sandbox so GHL starts 100% clean
    ghl_purged_contacts = 0
    if token and location_id:
        headers = {
            "Authorization": f"Bearer {token}",
            "Version": cfg.GHL_API_VERSION,
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                base_url=cfg.GHL_BASE_URL.rstrip("/"), headers=headers, timeout=15.0
            ) as ghl_client:
                rc = await ghl_client.get(
                    "/contacts/", params={"locationId": location_id, "limit": 100}
                )
                if rc.is_success:
                    for c in rc.json().get("contacts", []):
                        if c.get("id"):
                            await ghl_client.delete(f"/contacts/{c['id']}")
                            ghl_purged_contacts += 1
                ro = await ghl_client.get(
                    "/opportunities/search", params={"location_id": location_id, "limit": 100}
                )
                if ro.is_success:
                    for o in ro.json().get("opportunities", []):
                        if o.get("id"):
                            await ghl_client.delete(f"/opportunities/{o['id']}")
        except Exception:
            pass

    # 2. Reset local SQLite tables + reload sanitized historical dead leads
    with get_connection() as conn:
        conn.executescript(
            """
            DELETE FROM messages;
            DELETE FROM hitl_requests;
            DELETE FROM voice_calls;
            DELETE FROM crm_sync_events;
            DELETE FROM contacts;
            DELETE FROM dead_leads;
            """
        )
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

    # 3. Seed 3 sanitized baseline leads across Pipeline & sync LIVE into GoHighLevel
    seeded_ids: list[str] = []
    if seed_ghl:
        initial_showcase = [
            {
                "full_name": "Prospecto Inicial 01 (Nuevo Lead)",
                "phone": "+17605550181",
                "doc_type": "PASSPORT",
                "lot_code": "LOT-AV-01",
                "down_cents": 350_000,
                "stage": "NEW_LEAD",
                "hitl_state": "AI_AUTONOMOUS",
                "origin": "WEB_PORTAL",
                "buyer_msg": "Hola, vi la publicación de terrenos en Apple Valley. ¿Aceptan pasaporte vigente?",
                "sofia_msg": "¡Sí! Calificas directo con Pasaporte o ITIN sin bancos desde $3,500 USD de enganche.",
                "note": "[LEAD INICIAL DEMO] Consulta entrante por Apple Valley (LOT-AV-01) con Pasaporte.",
            },
            {
                "full_name": "Prospecto Inicial 02 (Calificado ITIN)",
                "phone": "+17605550182",
                "doc_type": "ITIN",
                "lot_code": "LOT-VV-02",
                "down_cents": 500_000,
                "stage": "QUALIFIED_ITIN",
                "hitl_state": "AI_AUTONOMOUS",
                "origin": "WHATSAPP_IA",
                "buyer_msg": "Tengo ITIN y $5,000 USD listos para el lote de 1 acre cerca de la autopista 15 en Victorville.",
                "sofia_msg": "Excelente, con $5,000 USD de enganche en LOT-VV-02 calificas inmediato a financiamiento directo.",
                "note": "[CALIFICADO DEMO ITIN] $5,000 USD de enganche verificados para Victorville (LOT-VV-02).",
            },
            {
                "full_name": "Prospecto Inicial 03 (Excepción HITL)",
                "phone": "+17605550183",
                "doc_type": "ITIN",
                "lot_code": "LOT-HE-03",
                "down_cents": 550_000,
                "stage": "PENDING_HITL_APPROVAL",
                "hitl_state": "PENDING_HITL",
                "origin": "VOICE_GEMINI_LIVE",
                "buyer_msg": "Me interesa el rancho de 2.5 acres en Hesperia; hoy cuento con $5,500 USD en mano para apartarlo.",
                "sofia_msg": "El enganche mínimo oficial en Hesperia es $7,500 USD; envié tu oferta de $5,500 USD al Director Comercial (HITL) para revisión.",
                "note": "⚠️ [ALERTA HITL DEMO] Oferta de $5,500 USD sobre LOT-HE-03 (mínimo $7,500 USD). Pendiente decisión en Cockpit.",
            },
        ]
        orchestrator = DualWriteCRMOrchestrator(settings=cfg)
        now = utc_now_iso()
        for item in initial_showcase:
            c = get_or_create_contact(
                full_name=item["full_name"],
                phone=item["phone"],
                doc_type=item["doc_type"],
                interested_lot_code=item["lot_code"],
                down_payment_cents=item["down_cents"],
                channel_origin=item["origin"],
            )
            cid = c["id"]
            with get_connection() as conn:
                conn.execute(
                    "UPDATE contacts SET pipeline_stage = ?, hitl_state = ?, updated_at = ? WHERE id = ?",
                    (item["stage"], item["hitl_state"], now, cid),
                )
                if item["hitl_state"] == "PENDING_HITL":
                    conn.execute(
                        """
                        INSERT INTO hitl_requests (
                            id, contact_id, lot_code, offered_down_cents,
                            required_min_down_cents, reason_es, status, created_at
                        ) VALUES (?, ?, ?, ?, 750000, ?, 'PENDING', ?)
                        """,
                        (
                            new_id(),
                            cid,
                            item["lot_code"],
                            item["down_cents"],
                            "Prospecto ofrece $5,500 USD sobre LOT-HE-03 (mínimo oficial $7,500 USD).",
                            now,
                        ),
                    )
                conn.commit()
            record_message(
                contact_id=cid, sender_role="BUYER", channel=item["origin"], body=item["buyer_msg"]
            )
            record_message(
                contact_id=cid, sender_role="AI_SOFIA", channel=item["origin"], body=item["sofia_msg"]
            )
            await orchestrator.sync(cid, note_body=item["note"])
            seeded_ids.append(cid)

    broadcaster.emit("demo_reset", {"seeded_contacts": len(seeded_ids)})
    return {
        "status": "reset_complete",
        "ghl_purged_contacts": ghl_purged_contacts,
        "ghl_seeded_contacts": len(seeded_ids),
    }


# --- GoHighLevel OAuth 2.0, Sandbox Verification & Inbound Human-Takeover Webhook ---


@app.get("/api/v1/ghl/oauth/authorize")
def ghl_oauth_authorize_url() -> dict[str, str]:
    cfg = get_settings()
    if not cfg.GHL_CLIENT_ID.strip():
        raise HTTPException(
            status_code=400,
            detail="Set GHL_CLIENT_ID in .env to use OAuth flow (or paste GHL_ACCESS_TOKEN PIT directly).",
        )
    scopes = (
        "contacts.readonly contacts.write "
        "conversations.readonly conversations.write "
        "conversations/message.readonly conversations/message.write "
        "locations.readonly locations/tags.readonly locations/tags.write "
        "opportunities.readonly opportunities.write "
        "pipelines.readonly pipelines.write pipelines.create"
    )
    qs = urllib.parse.urlencode(
        {
            "response_type": "code",
            "redirect_uri": cfg.GHL_REDIRECT_URI,
            "client_id": cfg.GHL_CLIENT_ID.strip(),
            "scope": scopes,
        }
    )
    url = f"https://marketplace.gohighlevel.com/oauth/chooselocation?{qs}"
    return {"authorize_url": url}


@app.get("/api/v1/crm/oauth/callback")
@app.get("/api/v1/ghl/oauth/callback")
async def ghl_oauth_callback(code: str = Query(...)) -> dict[str, Any]:
    cfg = get_settings()
    if not (cfg.GHL_CLIENT_ID.strip() and cfg.GHL_CLIENT_SECRET.strip()):
        raise HTTPException(status_code=400, detail="Missing GHL_CLIENT_ID / GHL_CLIENT_SECRET.")

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{cfg.GHL_BASE_URL.rstrip('/')}/oauth/token",
            data={
                "client_id": cfg.GHL_CLIENT_ID.strip(),
                "client_secret": cfg.GHL_CLIENT_SECRET.strip(),
                "grant_type": "authorization_code",
                "code": code,
                "user_type": "Location",
                "redirect_uri": cfg.GHL_REDIRECT_URI,
            },
            headers={"Accept": "application/json"},
        )
    if not resp.is_success:
        raise HTTPException(status_code=resp.status_code, detail=resp.text[:300])

    tok = resp.json()
    location_id = str(tok.get("locationId") or cfg.GHL_LOCATION_ID).strip()
    access_token = str(tok.get("access_token") or "").strip()
    refresh_token = str(tok.get("refresh_token") or "").strip()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO oauth_tokens (provider, location_id, access_token, refresh_token, updated_at)
            VALUES ('ghl', ?, ?, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET
                location_id = excluded.location_id,
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                updated_at = excluded.updated_at
            """,
            (location_id, access_token, refresh_token, utc_now_iso()),
        )
        conn.commit()
    return {"status": "oauth_connected", "location_id": location_id}


@app.post("/api/v1/ghl/verify")
async def ghl_verify_live_connection() -> dict[str, Any]:
    cfg = get_settings()
    token, location_id, auth_source = _resolve_ghl_credentials(cfg)
    if not token or not location_id:
        return {
            "connected": False,
            "mode": "SANDBOX_PREVIEW",
            "detail": "GHL_ACCESS_TOKEN or GHL_LOCATION_ID not configured yet.",
        }

    headers = {
        "Authorization": f"Bearer {token}",
        "Version": cfg.GHL_API_VERSION,
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(
            f"{cfg.GHL_BASE_URL.rstrip('/')}/contacts/",
            params={"locationId": location_id, "limit": 1},
            headers=headers,
        )
    return {
        "connected": resp.is_success,
        "auth_source": auth_source,
        "http_status": resp.status_code,
        "location_id": location_id,
        "detail": "GHL Sandbox API v2 reachable!" if resp.is_success else resp.text[:240],
    }


@app.post("/api/v1/webhooks/ghl/inbound")
async def ghl_inbound_webhook_endpoint(request: Request) -> dict[str, Any]:
    """
    Catches manual outbound replies written by human sales reps directly inside GoHighLevel
    (`type == "OutboundMessage"` with `userId` present) and automatically locks `HUMAN_TAKEN_OVER`.
    """
    payload = await request.json()
    event_type = str(payload.get("type") or "")
    user_id = payload.get("userId")
    ghl_cid = str(payload.get("contactId") or "")
    phone = str(payload.get("phone") or "")
    body_text = str(payload.get("body") or payload.get("message") or "").strip()

    if event_type == "OutboundMessage" and user_id:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT id FROM contacts WHERE ghl_contact_id = ? OR phone = ? LIMIT 1",
                (ghl_cid, phone),
            ).fetchone()
            if row:
                cid = str(row["id"])
                conn.execute(
                    "UPDATE contacts SET hitl_state = 'HUMAN_TAKEN_OVER', updated_at = ? WHERE id = ?",
                    (utc_now_iso(), cid),
                )
                conn.commit()
                if body_text:
                    record_message(
                        contact_id=cid,
                        sender_role="HUMAN_ADVISOR",
                        channel="GHL_NATIVE_INBOX",
                        body=body_text,
                        meta={"ghl_user_id": str(user_id)},
                    )
                broadcaster.emit(
                    "ghl_human_takeover_detected",
                    {"contact_id": cid, "ghl_user_id": str(user_id)},
                )
                return {"status": "human_takeover_locked", "contact_id": cid}
    return {"status": "ignored"}


# --- Dual Voice Provider Strategy Endpoints (Gemini Live + Retell AI) ---


@app.post("/api/v1/voice/session")
async def create_voice_session_endpoint(payload: VoiceSessionRequest) -> dict[str, Any]:
    adapter = get_voice_adapter(provider_override=payload.provider)
    return await adapter.create_demo_session(
        buyer_name=payload.buyer_name,
        buyer_phone=payload.buyer_phone,
    )


@app.post("/api/v1/voice/tools/{tool_name}")
@app.post("/api/v1/voice/retell/tools/{tool_name}")
async def voice_tool_webhook_endpoint(
    tool_name: str, request: Request
) -> dict[str, Any]:
    raw_body = await request.body()
    if "/retell/" in request.url.path:
        retell_adapter = RetellVoiceAdapter()
        if not retell_adapter.verify_signature(
            raw_body, request.headers.get("X-Retell-Signature")
        ):
            raise HTTPException(status_code=401, detail="Invalid X-Retell-Signature")

    parsed = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    args = parsed.get("args") if isinstance(parsed.get("args"), dict) else parsed
    try:
        result = await execute_voice_tool(tool_name, args or {})
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"result": result}


@app.post("/api/v1/voice/gemini-live/turn")
async def gemini_live_spoken_turn_endpoint(
    payload: GeminiLiveTurnRequest,
) -> dict[str, Any]:
    """
    Processes a spoken browser turn in `gemini_live` mode (zero phone number exposed!),
    executing deterministic land/HITL guardrails and returning concise spoken Spanish for TTS.
    """
    turn_result = await handle_buyer_chat_turn(
        full_name=payload.buyer_name,
        phone=payload.buyer_phone,
        message=payload.spoken_text,
        channel="VOICE_GEMINI_LIVE",
        selected_lot_code=payload.selected_lot_code,
    )
    raw_reply = turn_result.get("reply") or turn_result.get("system_banner_es") or ""
    spoken_clean = (
        raw_reply.replace("**", "")
        .replace("`", "")
        .replace("•", "")
        .replace("✅", "")
        .replace("📋", "")
    )
    audio_b64 = await synthesize_spanish_speech_mp3_b64(spoken_clean) if spoken_clean else ""
    return {
        "provider": "gemini_live",
        "exposes_phone_number": False,
        "session_id": payload.session_id,
        "spoken_reply_es": spoken_clean,
        "audio_mp3_base64": audio_b64,
        "turn_result": turn_result,
    }


@app.post("/api/v1/voice/gemini-live/complete-webhook")
async def gemini_live_complete_webhook_endpoint(
    payload: VoiceCallCompleteRequest,
) -> dict[str, Any]:
    summary = payload.summary_es.strip() or (
        f"Sesión de voz web Gemini Live ({payload.duration_seconds}s) sin exponer teléfono personal. "
        f"Interés en {payload.lot_code}; aviso legal California Penal Code § 632 reproducido en segundo 0."
    )
    transcript = payload.transcript_text.strip() or f"Sofía: {CA_TWO_PARTY_NOTICE_ES}"
    return await ingest_completed_voice_call(
        provider="gemini_live",
        external_call_id=payload.session_id,
        buyer_name=payload.buyer_name,
        buyer_phone=payload.buyer_phone,
        lot_code=payload.lot_code,
        duration_seconds=payload.duration_seconds,
        summary_es=summary,
        transcript_text=transcript,
        ca_632_disclosure_played=True,
    )


@app.websocket("/api/v1/voice/gemini-live/ws")
async def gemini_live_websocket_endpoint(websocket: WebSocket) -> None:
    """
    Bidirectional WebSocket for Gemini Live demo calls (zero phone exposure).
    Immediately emits the mandatory CA Penal Code § 632 opening notice at t=0,
    handles spoken JSON/audio turns with deterministic tool routing, and triggers
    post-call GHL webhook sync upon call completion.
    """
    await websocket.accept()
    session_id = f"ws-gem-{utc_now_iso()[-12:].replace(':', '')}"
    buyer_name = websocket.query_params.get("buyer_name", "Visitante Voz Web")
    buyer_phone = websocket.query_params.get("buyer_phone", "+17605550177")
    lot_code = websocket.query_params.get("lot_code", "LOT-AV-01")
    transcript_lines: list[str] = [f"Sofía (t=0s CA §632): {CA_TWO_PARTY_NOTICE_ES}"]

    greeting_text = (
        f"{CA_TWO_PARTY_NOTICE_ES} ¿En qué puedo ayudarte hoy? "
        "Puedes preguntarme precios de terrenos, financiamiento con ITIN o estados donde tenemos cobertura."
    )
    greeting_mp3 = await synthesize_spanish_speech_mp3_b64(greeting_text)

    await websocket.send_json(
        {
            "type": "ca_632_greeting",
            "session_id": session_id,
            "provider": "gemini_live",
            "exposes_phone_number": False,
            "spoken_text_es": greeting_text,
            "audio_mp3_base64": greeting_mp3,
            "tools": [t["name"] for t in VOICE_TOOLS_DECLARATIONS],
        }
    )

    try:
        while True:
            msg = await websocket.receive_json()
            msg_type = msg.get("type")
            use_vertex = bool(msg.get("use_vertex"))
            if msg_type == "user_audio":
                import base64

                b64_data = str(msg.get("audio_base64") or "")
                mime_type = str(msg.get("mime_type") or "audio/webm")
                raw_audio = base64.b64decode(b64_data) if b64_data else b""
                vres = await run_vertex_gemini_voice_agent_turn(
                    buyer_name=buyer_name,
                    buyer_phone=buyer_phone,
                    lot_code=str(msg.get("lot_code") or lot_code),
                    spoken_text=str(msg.get("hint_text") or ""),
                    audio_bytes=raw_audio,
                    audio_mime_type=mime_type,
                )
                transcript_lines.append(f"Comprador (Audio): {vres['user_transcript']}")
                if vres["spoken_text_es"]:
                    transcript_lines.append(f"Sofía (Vertex Gemini): {vres['spoken_text_es']}")
                await websocket.send_json(
                    {
                        "type": "assistant_utterance",
                        "engine": "vertex_gemini",
                        "user_transcript": vres["user_transcript"],
                        "spoken_text_es": vres["spoken_text_es"],
                        "audio_mp3_base64": vres.get("audio_mp3_base64", ""),
                        "tool_calls": vres["tool_calls"],
                        "bot_silenced": vres["bot_silenced"],
                        "hitl_state": vres["hitl_state"],
                    }
                )
            elif msg_type == "user_utterance":
                utterance = str(msg.get("text") or "").strip()
                if not utterance:
                    continue
                transcript_lines.append(f"Comprador: {utterance}")
                if use_vertex:
                    vres = await run_vertex_gemini_voice_agent_turn(
                        buyer_name=buyer_name,
                        buyer_phone=buyer_phone,
                        lot_code=str(msg.get("lot_code") or lot_code),
                        spoken_text=utterance,
                    )
                    reply = vres["spoken_text_es"]
                    if reply:
                        transcript_lines.append(f"Sofía (Vertex Gemini): {reply}")
                    await websocket.send_json(
                        {
                            "type": "assistant_utterance",
                            "engine": "vertex_gemini",
                            "user_transcript": utterance,
                            "spoken_text_es": reply,
                            "audio_mp3_base64": vres.get("audio_mp3_base64", ""),
                            "tool_calls": vres["tool_calls"],
                            "bot_silenced": vres["bot_silenced"],
                            "hitl_state": vres["hitl_state"],
                        }
                    )
                else:
                    res = await handle_buyer_chat_turn(
                        full_name=buyer_name,
                        phone=buyer_phone,
                        message=utterance,
                        channel="VOICE_GEMINI_LIVE",
                        selected_lot_code=str(msg.get("lot_code") or lot_code),
                        include_audio=True,
                    )
                    reply = (res.get("reply") or res.get("system_banner_es") or "").replace("**", "").replace("`", "")
                    if reply:
                        transcript_lines.append(f"Sofía: {reply}")
                    await websocket.send_json(
                        {
                            "type": "assistant_utterance",
                            "engine": "deterministic_guardrail",
                            "user_transcript": utterance,
                            "spoken_text_es": reply,
                            "audio_mp3_base64": res.get("audio_mp3_base64", ""),
                            "bot_silenced": res.get("bot_silenced", False),
                            "hitl_state": res.get("hitl_state"),
                            "guardrail_triggered": res.get("guardrail_triggered"),
                        }
                    )
            elif msg_type == "end_call":
                duration = int(msg.get("duration_seconds") or 35)
                synced = await ingest_completed_voice_call(
                    provider="gemini_live",
                    external_call_id=session_id,
                    buyer_name=buyer_name,
                    buyer_phone=buyer_phone,
                    lot_code=lot_code,
                    duration_seconds=duration,
                    summary_es=f"Llamada Gemini Live completada ({len(transcript_lines)} turnos). Sincronizada a GoHighLevel.",
                    transcript_text="\n".join(transcript_lines),
                    ca_632_disclosure_played=True,
                )
                await websocket.send_json({"type": "call_synced", "result": synced})
                break
    except WebSocketDisconnect:
        if len(transcript_lines) > 1:
            await ingest_completed_voice_call(
                provider="gemini_live",
                external_call_id=session_id,
                buyer_name=buyer_name,
                buyer_phone=buyer_phone,
                lot_code=lot_code,
                duration_seconds=30,
                summary_es="Llamada Gemini Live cerrada por el navegador y sincronizada en GoHighLevel.",
                transcript_text="\n".join(transcript_lines),
                ca_632_disclosure_played=True,
            )


@app.post("/api/v1/voice/retell/webhook")
async def retell_post_call_webhook_endpoint(request: Request) -> dict[str, Any]:
    raw_body = await request.body()
    adapter = RetellVoiceAdapter()
    if not adapter.verify_signature(raw_body, request.headers.get("X-Retell-Signature")):
        raise HTTPException(status_code=401, detail="Invalid X-Retell-Signature")

    payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    event = str(payload.get("event") or "")
    call_obj = payload.get("call") if isinstance(payload.get("call"), dict) else payload

    if event not in ("call_ended", "call_analyzed", ""):
        return {"status": "ignored_event", "event": event}

    dyn = call_obj.get("retell_llm_dynamic_variables") or {}
    analysis = call_obj.get("call_analysis") or {}
    duration_ms = int(call_obj.get("duration_ms") or 45_000)
    synced = await ingest_completed_voice_call(
        provider="retell",
        external_call_id=str(call_obj.get("call_id") or "retell-call-demo"),
        buyer_name=str(dyn.get("buyer_name") or "Llamada Retell California"),
        buyer_phone=str(call_obj.get("from_number") or dyn.get("buyer_phone") or "+17605550166"),
        lot_code=str(dyn.get("lot_code") or "LOT-AV-01"),
        duration_seconds=max(1, duration_ms // 1000),
        summary_es=str(
            analysis.get("call_summary")
            or "Llamada telefónica Retell AI procesada y sincronizada con GoHighLevel."
        ),
        transcript_text=str(call_obj.get("transcript") or CA_TWO_PARTY_NOTICE_ES),
        recording_url=str(call_obj.get("recording_url") or ""),
        ca_632_disclosure_played=True,
    )
    return {"status": "processed", "result": synced}


# --- Real-Time SSE Stream ---


@app.get("/api/v1/events/stream")
async def sse_events_endpoint(request: Request) -> StreamingResponse:
    queue = broadcaster.subscribe()

    async def event_generator():
        try:
            yield "event: connected\ndata: {\"ok\": true}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            broadcaster.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
