from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Literal, Protocol

import httpx

from app.adapters.crm import DualWriteCRMOrchestrator
from app.config import Settings, get_settings
from app.db import get_connection, new_id, utc_now_iso
from app.domain import (
    CA_TWO_PARTY_NOTICE_ES,
    SEED_LOTS,
    calculate_seller_financing,
    get_lot_or_none,
)
from app.events import broadcaster
from app.services.concierge import (
    get_or_create_contact,
    handle_buyer_chat_turn,
    record_message,
)

VoiceProviderName = Literal["gemini_live", "retell"]

VOICE_TOOLS_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "list_available_lots",
        "description": "Lista los terrenos disponibles en California (Apple Valley, Victorville, Hesperia) con precios exactos y enganche mínimo.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "max_down_payment_usd": {
                    "type": "INTEGER",
                    "description": "Presupuesto máximo de enganche en USD del comprador (opcional).",
                }
            },
        },
    },
    {
        "name": "calculate_financing_plan",
        "description": "Calcula la mensualidad exacta en financiamiento directo con el dueño (ITIN/Pasaporte/SSN).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "lot_code": {"type": "STRING", "description": "Código de lote, ej. LOT-AV-01"},
                "down_payment_usd": {"type": "INTEGER", "description": "Enganche en USD"},
                "term_months": {"type": "INTEGER", "description": "Plazo en meses (36, 48, 60 o 84)"},
            },
            "required": ["lot_code", "down_payment_usd"],
        },
    },
    {
        "name": "request_hitl_sales_exception",
        "description": "Escala al Director Comercial (HITL) cuando el comprador ofrece menos del enganche mínimo oficial.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "lot_code": {"type": "STRING"},
                "offered_down_payment_usd": {"type": "INTEGER"},
                "buyer_name": {"type": "STRING"},
                "buyer_phone": {"type": "STRING"},
            },
            "required": ["lot_code", "offered_down_payment_usd"],
        },
    },
    {
        "name": "book_land_consultation",
        "description": "Agenda visita presencial en Apple Valley o llamada de cierre con asesor humano.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "lot_code": {"type": "STRING"},
                "buyer_name": {"type": "STRING"},
                "buyer_phone": {"type": "STRING"},
                "preferred_day": {"type": "STRING"},
            },
            "required": ["lot_code", "buyer_name"],
        },
    },
]


class VoiceProviderAdapter(Protocol):
    name: VoiceProviderName
    exposes_phone_number: bool

    async def create_demo_session(
        self,
        *,
        buyer_name: str,
        buyer_phone: str,
    ) -> dict[str, Any]: ...


async def execute_voice_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    settings: Settings | None = None,
    db_path: str | None = None,
    ghl_http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Deterministic tool dispatcher shared identically by Gemini Live and Retell AI."""
    cfg = settings or get_settings()

    if tool_name == "list_available_lots":
        max_down = arguments.get("max_down_payment_usd")
        available = []
        blocked = []
        for lot in SEED_LOTS:
            d = lot.to_public_dict()
            if lot.status in ("AVAILABLE", "LAST_UNIT"):
                if max_down is None or int(max_down) >= d["min_down_payment_usd"]:
                    available.append(d)
            else:
                blocked.append({"code": lot.code, "title": lot.title, "status": lot.status})
        return {
            "available_lots": available if available else [SEED_LOTS[0].to_public_dict()],
            "non_purchasable_lots": blocked,
            "financing_policy": "Aceptamos ITIN, Pasaporte y SSN sin bancos.",
        }

    if tool_name == "calculate_financing_plan":
        lot_code = str(arguments.get("lot_code") or "LOT-AV-01")
        down_usd = int(arguments.get("down_payment_usd") or 3500)
        term = int(arguments.get("term_months") or 60)
        quote = calculate_seller_financing(lot_code, down_usd * 100, term_months=term)
        return quote.to_dict()

    if tool_name == "request_hitl_sales_exception":
        lot_code = str(arguments.get("lot_code") or "LOT-AV-01")
        offered_usd = int(arguments.get("offered_down_payment_usd") or 2500)
        buyer_name = str(arguments.get("buyer_name") or "Llamada Web Sofía")
        buyer_phone = str(arguments.get("buyer_phone") or "+17605550150")
        res = await handle_buyer_chat_turn(
            full_name=buyer_name,
            phone=buyer_phone,
            message=f"Ofrezco ${offered_usd} USD de enganche hoy mismo para el lote {lot_code}, ¿pueden aprobar excepción?",
            channel="VOICE_TOOL_CALL",
            selected_lot_code=lot_code,
            settings=cfg,
            db_path=db_path,
            ghl_http_client=ghl_http_client,
        )
        return {
            "status": "ESCALATED_TO_HITL_DIRECTOR",
            "hitl_state": res["hitl_state"],
            "contact_id": res["contact_id"],
            "spoken_instruction_es": (
                f"Informa al cliente que el mínimo oficial es mayor, pero acabas de enviar su oferta de "
                f"${offered_usd} dólares en vivo al panel del Director Comercial para autorización."
            ),
        }

    if tool_name == "book_land_consultation":
        lot_code = str(arguments.get("lot_code") or "LOT-AV-01")
        lot = get_lot_or_none(lot_code) or SEED_LOTS[0]
        if lot.status in ("RESERVED", "SOLD"):
            return {
                "booked": False,
                "reason": f"El lote {lot.code} está Apartado. Ofrece LOT-AV-01 (Apple Valley) o LOT-HE-03 (Hesperia).",
            }
        buyer_name = str(arguments.get("buyer_name") or "Comprador Voz California")
        buyer_phone = str(arguments.get("buyer_phone") or "+17605550155")
        preferred_day = str(arguments.get("preferred_day") or "este sábado a las 11:00 AM")
        contact = get_or_create_contact(
            full_name=buyer_name,
            phone=buyer_phone,
            interested_lot_code=lot.code,
            down_payment_cents=lot.min_down_payment_cents,
            channel_origin="VOICE_RECEPTIONIST",
            db_path=db_path,
        )
        with get_connection(db_path) as conn:
            conn.execute(
                "UPDATE contacts SET pipeline_stage = 'SHOWING_BOOKED', updated_at = ? WHERE id = ?",
                (utc_now_iso(), contact["id"]),
            )
            conn.commit()
        orchestrator = DualWriteCRMOrchestrator(
            settings=cfg, db_path=db_path, ghl_http_client=ghl_http_client
        )
        await orchestrator.sync(
            contact["id"],
            note_body=f"📅 [CITA AGENDADA POR VOZ IA] {buyer_name} visitará {lot.title} ({lot.code}) — {preferred_day}.",
        )
        broadcaster.emit(
            "voice_booking_created",
            {"contact_id": contact["id"], "lot_code": lot.code, "preferred_day": preferred_day},
        )
        return {
            "booked": True,
            "contact_id": contact["id"],
            "lot_code": lot.code,
            "office_address": "100 High Desert Blvd, Suite 200, Apple Valley, CA",
            "preferred_day": preferred_day,
        }

    raise ValueError(f"Unsupported voice tool: {tool_name}")


async def ingest_completed_voice_call(
    *,
    provider: VoiceProviderName,
    external_call_id: str,
    buyer_name: str,
    buyer_phone: str,
    lot_code: str = "LOT-AV-01",
    duration_seconds: int = 48,
    summary_es: str,
    transcript_text: str,
    recording_url: str = "",
    ca_632_disclosure_played: bool = True,
    settings: Settings | None = None,
    db_path: str | None = None,
    ghl_http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Unified post-call webhook processor syncing completed voice calls to SQLite + GoHighLevel."""
    cfg = settings or get_settings()
    contact = get_or_create_contact(
        full_name=buyer_name,
        phone=buyer_phone,
        interested_lot_code=lot_code,
        channel_origin=f"VOICE_{provider.upper()}",
        db_path=db_path,
    )
    cid = contact["id"]
    vc_id = new_id()
    now = utc_now_iso()

    with get_connection(db_path) as conn:
        if contact["pipeline_stage"] == "NEW_LEAD":
            conn.execute(
                "UPDATE contacts SET pipeline_stage = 'QUALIFIED_ITIN', updated_at = ? WHERE id = ?",
                (now, cid),
            )
        conn.execute(
            """
            INSERT INTO voice_calls (
                id, contact_id, provider, external_call_id,
                ca_632_disclosure_played, duration_seconds, summary_es,
                transcript_text, recording_url, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                vc_id,
                cid,
                provider,
                external_call_id,
                1 if ca_632_disclosure_played else 0,
                max(1, duration_seconds),
                summary_es,
                transcript_text,
                recording_url,
                now,
            ),
        )
        conn.commit()

    record_message(
        contact_id=cid,
        sender_role="AI_SOFIA",
        channel=f"VOICE_{provider.upper()}",
        body=f"📞 Llamada de Voz Finalizada ({duration_seconds}s · Aviso CA § 632 ✅): {summary_es}",
        meta={"voice_call_id": vc_id, "provider": provider, "transcript": transcript_text},
        db_path=db_path,
    )

    orchestrator = DualWriteCRMOrchestrator(
        settings=cfg, db_path=db_path, ghl_http_client=ghl_http_client
    )
    ghl_note = (
        f"🎙️ [LLAMADA RECEPCIONISTA IA SOFÍA — {provider.upper()}]\n"
        f"• Cumplimiento California Penal Code § 632 (Segundo 0): {'SÍ' if ca_632_disclosure_played else 'NO'}\n"
        f"• Duración: {duration_seconds}s | Lote de interés: {lot_code}\n"
        f"• Resumen Ejecutivo: {summary_es}\n"
        f"• Transcripción:\n{transcript_text[:1800]}"
    )
    sync_results = await orchestrator.sync(cid, note_body=ghl_note)
    event_data = {
        "voice_call_id": vc_id,
        "contact_id": cid,
        "provider": provider,
        "duration_seconds": duration_seconds,
        "summary_es": summary_es,
        "ca_632_disclosure_played": ca_632_disclosure_played,
        "crm_sync": [r.__dict__ for r in sync_results],
    }
    broadcaster.emit("voice_call_completed", event_data)
    return event_data


class GeminiLiveVoiceAdapter:
    """
    Primary Demo Voice Strategy: Browser-to-FastAPI WebSocket & Turn Webhook Bridge.
    Exposes ZERO personal or PSTN phone numbers while exercising full voice tool-calling
    and post-call GoHighLevel webhook synchronization.
    """

    name: VoiceProviderName = "gemini_live"
    exposes_phone_number: bool = False

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def create_demo_session(
        self,
        *,
        buyer_name: str,
        buyer_phone: str,
    ) -> dict[str, Any]:
        session_id = f"gemlive-{new_id()[:12]}"
        return {
            "provider": self.name,
            "session_id": session_id,
            "exposes_phone_number": False,
            "public_phone_number": None,
            "ca_632_opening_es": CA_TWO_PARTY_NOTICE_ES,
            "gemini_native_audio_ready": self.settings.gemini_configured,
            "ws_endpoint": "/api/v1/voice/gemini-live/ws",
            "turn_webhook_endpoint": "/api/v1/voice/gemini-live/turn",
            "complete_webhook_endpoint": "/api/v1/voice/gemini-live/complete-webhook",
            "tools_registered": [t["name"] for t in VOICE_TOOLS_DECLARATIONS],
            "buyer_name": buyer_name,
            "buyer_phone": buyer_phone,
        }


class RetellVoiceAdapter:
    """
    Secondary Production/Demo Voice Strategy: Retell AI Web Call + Optional PSTN Number.
    Never exposes a phone number unless `RETELL_PUBLIC_PHONE` is explicitly configured.
    """

    name: VoiceProviderName = "retell"

    def __init__(
        self,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = http_client

    @property
    def exposes_phone_number(self) -> bool:
        return bool(self.settings.RETELL_PUBLIC_PHONE.strip())

    def verify_signature(self, raw_body: bytes, signature_header: str | None) -> bool:
        secret = self.settings.RETELL_WEBHOOK_SECRET.strip()
        if not secret:
            return True
        if not signature_header:
            return False
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature_header.strip())

    async def create_demo_session(
        self,
        *,
        buyer_name: str,
        buyer_phone: str,
    ) -> dict[str, Any]:
        if not self.settings.retell_configured:
            return {
                "provider": self.name,
                "mode": "SANDBOX_PREVIEW",
                "session_id": f"retell-sbx-{new_id()[:10]}",
                "exposes_phone_number": self.exposes_phone_number,
                "public_phone_number": self.settings.RETELL_PUBLIC_PHONE.strip() or None,
                "ca_632_opening_es": CA_TWO_PARTY_NOTICE_ES,
                "retell_access_token": None,
                "detail": "Set RETELL_API_KEY & RETELL_AGENT_ID in .env to issue live WebRTC Retell tokens.",
            }

        owns = self._client is None
        client = self._client or httpx.AsyncClient(timeout=8.0)
        try:
            resp = await client.post(
                "https://api.retellai.com/v2/create-web-call",
                headers={
                    "Authorization": f"Bearer {self.settings.RETELL_API_KEY.strip()}",
                    "Content-Type": "application/json",
                },
                json={
                    "agent_id": self.settings.RETELL_AGENT_ID.strip(),
                    "retell_llm_dynamic_variables": {
                        "buyer_name": buyer_name,
                        "buyer_phone": buyer_phone,
                        "ca_632_notice": CA_TWO_PARTY_NOTICE_ES,
                    },
                },
            )
            resp.raise_for_status()
            payload = resp.json()
            return {
                "provider": self.name,
                "mode": "LIVE_RETELL_WEBCALL",
                "session_id": payload.get("call_id", f"retell-{new_id()[:8]}"),
                "exposes_phone_number": self.exposes_phone_number,
                "public_phone_number": self.settings.RETELL_PUBLIC_PHONE.strip() or None,
                "ca_632_opening_es": CA_TWO_PARTY_NOTICE_ES,
                "retell_access_token": payload.get("access_token"),
            }
        finally:
            if owns:
                await client.aclose()


def get_voice_adapter(
    provider_override: str | None = None,
    settings: Settings | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> GeminiLiveVoiceAdapter | RetellVoiceAdapter:
    cfg = settings or get_settings()
    chosen = (provider_override or cfg.VOICE_PROVIDER).strip().lower()
    if chosen == "retell":
        return RetellVoiceAdapter(settings=cfg, http_client=http_client)
    return GeminiLiveVoiceAdapter(settings=cfg)


_TTS_CACHE: dict[str, str] = {}
_GCP_CREDS: Any = None


def _get_gcp_bearer_token() -> str:
    global _GCP_CREDS
    import google.auth
    import google.auth.transport.requests

    if _GCP_CREDS is None:
        _GCP_CREDS, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    if not _GCP_CREDS.valid or _GCP_CREDS.expired or not _GCP_CREDS.token:
        _GCP_CREDS.refresh(google.auth.transport.requests.Request())
    return str(_GCP_CREDS.token)


_UNITS_ES = (
    "",
    "uno",
    "dos",
    "tres",
    "cuatro",
    "cinco",
    "seis",
    "siete",
    "ocho",
    "nueve",
    "diez",
    "once",
    "doce",
    "trece",
    "catorce",
    "quince",
    "dieciséis",
    "diecisiete",
    "dieciocho",
    "diecinueve",
    "veinte",
    "veintiuno",
    "veintidós",
    "veintitrés",
    "veinticuatro",
    "veinticinco",
    "veintiséis",
    "veintisiete",
    "veintiocho",
    "veintinueve",
)
_TENS_ES = (
    "",
    "diez",
    "veinte",
    "treinta",
    "cuarenta",
    "cincuenta",
    "sesenta",
    "setenta",
    "ochenta",
    "noventa",
)
_HUNDREDS_ES = (
    "",
    "ciento",
    "doscientos",
    "trescientos",
    "cuatrocientos",
    "quinientos",
    "seiscientos",
    "setecientos",
    "ochocientos",
    "novecientos",
)


def _under_1000_es(n: int, apocopate: bool = False) -> str:
    if n == 0:
        return ""
    if n == 100:
        return "cien"
    h, rem = divmod(n, 100)
    parts: list[str] = []
    if h:
        parts.append(_HUNDREDS_ES[h])
    if rem > 0:
        if rem < 30:
            w = _UNITS_ES[rem]
            if apocopate and rem == 1:
                w = "un"
            elif apocopate and rem == 21:
                w = "veintiún"
            parts.append(w)
        else:
            t, u = divmod(rem, 10)
            if u == 0:
                parts.append(_TENS_ES[t])
            elif u == 1 and apocopate:
                parts.append(f"{_TENS_ES[t]} y un")
            else:
                parts.append(f"{_TENS_ES[t]} y {_UNITS_ES[u]}")
    return " ".join(parts)


def _int_to_spanish_words(n: int, apocopate_final_one: bool = True) -> str:
    if n == 0:
        return "cero"
    if n < 0:
        return f"menos {_int_to_spanish_words(-n, apocopate_final_one)}"
    millions, rem = divmod(n, 1_000_000)
    thousands, units = divmod(rem, 1000)
    chunks: list[str] = []
    if millions == 1:
        chunks.append("un millón")
    elif millions > 1:
        chunks.append(f"{_under_1000_es(millions, apocopate=True)} millones")
    if thousands == 1:
        chunks.append("mil")
    elif thousands > 1:
        chunks.append(f"{_under_1000_es(thousands, apocopate=True)} mil")
    if units > 0:
        chunks.append(_under_1000_es(units, apocopate=apocopate_final_one))
    return " ".join(chunks)


_CURRENCY_DOLLAR_RE = re.compile(
    r"\$\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?"
    r"(?:\s*(?:USD|usd|dólares|dolares))?"
    r"(\s*/\s*mes|\s+al\s+mes|\s+mensuales)?",
)
_POSTFIX_USD_RE = re.compile(
    r"\b(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?\s*(?:USD|usd|dólares|dolares)\b"
    r"(\s*/\s*mes|\s+al\s+mes|\s+mensuales)?",
)
_COMMA_NUMBER_RE = re.compile(r"\b(\d{1,3}(?:,\d{3})+)\b")


def _format_currency_match_es(dollars_raw: str, cents_raw: str | None, per_month_raw: str | None) -> str:
    dollars = int(dollars_raw.replace(",", ""))
    cents = int((cents_raw or "").ljust(2, "0")) if cents_raw else 0
    unit_word = "dólar" if dollars == 1 else "dólares"
    spoken = f"{_int_to_spanish_words(dollars, apocopate_final_one=True)} {unit_word}"
    if cents > 0:
        cent_word = "centavo" if cents == 1 else "centavos"
        spoken += f" con {_int_to_spanish_words(cents, apocopate_final_one=True)} {cent_word}"
    if per_month_raw:
        spoken += " al mes"
    return spoken


def normalize_spanish_tts_text(text: str) -> str:
    """
    Normalizes US-formatted currency, commas, acres, and legal symbols into natural spoken Spanish
    so Google Cloud TTS (`es-US-Journey-F`) never misreads `$7,500` as 'siete dólares quinientos'.
    """
    s = re.sub(r"[*`•📋✅⚠️🛑🎙️📅⚖️]", "", text or "")
    s = re.sub(r"§\s*(\d+)", r"sección \1", s)
    s = re.sub(r"\b0\.50?\s*[Aa]cres?\b", "medio acre", s)
    s = re.sub(r"\b1\.00?\s*[Aa]cres?\b", "un acre", s)
    s = re.sub(r"\b2\.50?\s*[Aa]cres?\b", "dos acres y medio", s)
    s = re.sub(r"\b5\.00?\s*[Aa]cres?\b", "cinco acres", s)

    s = _CURRENCY_DOLLAR_RE.sub(
        lambda m: _format_currency_match_es(m.group(1), m.group(2), m.group(3)),
        s,
    )
    s = _POSTFIX_USD_RE.sub(
        lambda m: _format_currency_match_es(m.group(1), m.group(2), m.group(3)),
        s,
    )
    s = _COMMA_NUMBER_RE.sub(
        lambda m: _int_to_spanish_words(int(m.group(1).replace(",", "")), apocopate_final_one=False),
        s,
    )
    s = re.sub(r"\bUSD\b", "dólares", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


async def synthesize_spanish_speech_mp3_b64(
    text: str, settings: Settings | None = None
) -> str:
    """
    Synthesizes natural US-Hispanic voice (`es-US-Journey-F`) into base64 MP3 via Google Cloud TTS
    using local Google Cloud ADC (`VERTEX_PROJECT_ID`). Works on Linux/macOS/iOS/Android without browser voices.
    """
    cleaned = normalize_spanish_tts_text(text)
    if not cleaned:
        return ""
    cache_key = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:24]
    if cache_key in _TTS_CACHE:
        return _TTS_CACHE[cache_key]

    cfg = settings or get_settings()
    if not cfg.VERTEX_USE_ADC or not cfg.VERTEX_PROJECT_ID.strip():
        return ""
    try:
        import asyncio

        token = await asyncio.to_thread(_get_gcp_bearer_token)
        async with httpx.AsyncClient(timeout=8.0) as client:
            for voice_name in ("es-US-Journey-F", "es-US-Neural2-A"):
                resp = await client.post(
                    "https://texttospeech.googleapis.com/v1/text:synthesize",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "x-goog-user-project": cfg.VERTEX_PROJECT_ID,
                        "Content-Type": "application/json",
                    },
                    json={
                        "input": {"text": cleaned[:1500]},
                        "voice": {"languageCode": "es-US", "name": voice_name},
                        "audioConfig": {"audioEncoding": "MP3"},
                    },
                )
                if resp.is_success:
                    b64 = str(resp.json().get("audioContent") or "")
                    if b64:
                        if len(_TTS_CACHE) < 128:
                            _TTS_CACHE[cache_key] = b64
                        return b64
    except Exception:
        return ""
    return ""


COMPANY_KNOWLEDGE_GROUNDING_ES = """
CONOCIMIENTO OFICIAL INMUTABLE DE LA EMPRESA (HORIZON ESTATES DEMO):
- Nombre comercial de demostración: Horizon Estates (operadora inmobiliaria de lotes en el High Desert).
- Oficina comercial demo: 100 High Desert Blvd, Suite 200, Apple Valley, CA (Condado de San Bernardino).
- COBERTURA GEOGRÁFICA ACTUAL: Operamos EXCLUSIVAMENTE en el estado de CALIFORNIA (Apple Valley, Victorville, Hesperia y Phelan).
- OTROS ESTADOS (Colorado, Texas, Arizona, Nevada, Florida, etc.): AÚN NO tenemos terrenos ni servicios en Colorado ni fuera de California. Tenemos planes de expandirnos a otros estados en el futuro, pero hoy solo vendemos terrenos en California. Si preguntan por Colorado u otro estado, diles claramente que hoy NO tenemos inventario allá, solo en California.
- QUÉ VENDEMOS Y QUÉ NO: Vendemos terrenos con financiamiento directo del dueño (Seller Financing) enfocados en familias e inversionistas hispanos. NO construimos casas ni damos préstamos hipotecarios para propiedades de terceros.
- REQUISITOS DE CALIFICACIÓN: Aceptamos ITIN, Pasaporte vigente o Seguro Social (SSN), sin bancos ni revisión de crédito bancario tradicional.
"""


async def run_vertex_gemini_voice_agent_turn(
    *,
    buyer_name: str,
    buyer_phone: str,
    lot_code: str = "LOT-AV-01",
    spoken_text: str = "",
    audio_bytes: bytes | None = None,
    audio_mime_type: str = "audio/wav",
    include_tts_audio: bool = True,
    settings: Settings | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """
    Executes a live multi-turn conversational voice/chat turn on Vertex AI (`gemini-2.5-flash`):
    1. Respects `HUMAN_TAKEN_OVER` lock immediately.
    2. Loads recent conversation history for true multi-turn continuity.
    3. Accepts raw microphone audio (`audio/webm`, `audio/wav`) or text, invokes Gemini Function Calling,
       and synthesizes real `es-US-Journey-F` MP3 audio so Linux/iOS/Android browsers play crystal-clear voice.
    """
    from google.genai import types

    cfg = settings or get_settings()
    contact = get_or_create_contact(
        full_name=buyer_name,
        phone=buyer_phone,
        interested_lot_code=lot_code,
        channel_origin="VOICE_GEMINI_LIVE",
        db_path=db_path,
    )
    if contact["hitl_state"] == "HUMAN_TAKEN_OVER":
        record_message(
            contact_id=contact["id"],
            sender_role="BUYER",
            channel="VOICE_GEMINI_LIVE",
            body=spoken_text or "[Audio entrante mientras asesor humano tiene control]",
            db_path=db_path,
        )
        return {
            "engine": "vertex_gemini",
            "bot_silenced": True,
            "hitl_state": "HUMAN_TAKEN_OVER",
            "user_transcript": spoken_text,
            "tool_calls": [],
            "spoken_text_es": "",
            "audio_mp3_base64": "",
        }

    with get_connection(db_path) as conn:
        prior_rows = conn.execute(
            "SELECT sender_role, body FROM messages WHERE contact_id = ? ORDER BY rowid DESC LIMIT 10",
            (contact["id"],),
        ).fetchall()

    merged_turns: list[tuple[str, str]] = []
    for row in reversed(prior_rows):
        role = "user" if row["sender_role"] == "BUYER" else "model"
        txt = str(row["body"] or "").strip()
        if not txt:
            continue
        if merged_turns and merged_turns[-1][0] == role:
            merged_turns[-1] = (role, f"{merged_turns[-1][1]}\n{txt[:500]}")
        else:
            merged_turns.append((role, txt[:600]))

    while merged_turns and merged_turns[0][0] != "user":
        merged_turns.pop(0)
    while merged_turns and merged_turns[-1][0] == "user":
        merged_turns.pop()

    history_contents: list[Any] = [
        types.Content(role=r, parts=[types.Part.from_text(text=t)])
        for r, t in merged_turns
    ]

    client = cfg.build_genai_client()
    fn_decls = [
        types.FunctionDeclaration(
            name=t["name"],
            description=t["description"],
            parameters=t["parameters"],
        )
        for t in VOICE_TOOLS_DECLARATIONS
    ]
    lot_catalog_context = json.dumps(
        [lot.to_public_dict() for lot in SEED_LOTS], ensure_ascii=False
    )
    sys_prompt = (
        "Eres Sofía, recepcionista telefónica de Horizon Estates en el High Desert (Apple Valley). "
        "Mantienes una conversación fluida, natural, amable y ejecutiva en español (2 a 3 oraciones breves para hablar por teléfono, "
        "CERO asteriscos, CERO viñetas markdown).\n"
        f"{COMPANY_KNOWLEDGE_GROUNDING_ES}\n"
        "REGLAS ESTRICTAS DE HERRAMIENTAS Y SEGURIDAD:\n"
        "1. Si preguntan por estados donde NO operamos (ej. Colorado, Texas, Florida, Arizona) o servicios que NO damos, "
        "responde honestamente que NO tenemos cobertura ahí hoy (solo en California: Apple Valley, Victorville y Hesperia) "
        "y pregunta amablemente si le interesa evaluar California.\n"
        "2. Para consultar inventario, calcular mensualidades, escalar rebajas menores al enganche mínimo o agendar visitas, "
        "invoca SIEMPRE tus herramientas (`list_available_lots`, `calculate_financing_plan`, `request_hitl_sales_exception`, `book_land_consultation`).\n"
        "3. JAMÁS inventes precios ni aceptes enganches menores al mínimo oficial: si ofrecen menos, llama `request_hitl_sales_exception`.\n"
        "4. JAMÁS vendas ni agendes sobre `LOT-PH-04` (Phelan) porque está APARTADO.\n"
        f"Catálogo verificado en vivo: {lot_catalog_context}\n"
        f"Datos del interlocutor: nombre={buyer_name}, teléfono={buyer_phone}, lote preseleccionado={lot_code}."
    )

    transcribed_from_audio = ""
    if audio_bytes and not spoken_text:
        try:
            tr_resp = await client.aio.models.generate_content(
                model=cfg.GEMINI_MODEL,
                contents=[
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_bytes(
                                data=audio_bytes, mime_type=audio_mime_type or "audio/webm"
                            ),
                            types.Part.from_text(
                                text="Transcribe exactamente en español lo que dice la persona en este audio. Si solo hay silencio o ruido sin palabras, responde exactamente: [SILENCIO]"
                            ),
                        ],
                    )
                ],
                config=types.GenerateContentConfig(temperature=0.0),
            )
            transcribed_from_audio = (tr_resp.text or "").strip()
        except Exception:
            transcribed_from_audio = ""

    if audio_bytes and not spoken_text and (
        not transcribed_from_audio or "[silencio]" in transcribed_from_audio.lower()
    ):
        msg_retry = "Disculpa, no alcancé a escucharte con claridad. ¿Me puedes repetir tu pregunta?"
        b64_retry = (
            await synthesize_spanish_speech_mp3_b64(msg_retry, cfg)
            if include_tts_audio
            else ""
        )
        return {
            "engine": "vertex_gemini",
            "contact_id": contact["id"],
            "bot_silenced": False,
            "hitl_state": contact["hitl_state"],
            "pipeline_stage": contact["pipeline_stage"],
            "user_transcript": "[Silencio o audio muy bajo]",
            "tool_calls": [],
            "spoken_text_es": msg_retry,
            "audio_mp3_base64": b64_retry,
        }

    user_parts: list[Any] = []
    if transcribed_from_audio:
        user_parts.append(types.Part.from_text(text=transcribed_from_audio))
    elif audio_bytes:
        user_parts.append(
            types.Part.from_bytes(data=audio_bytes, mime_type=audio_mime_type or "audio/wav")
        )
        user_parts.append(
            types.Part.from_text(
                text=spoken_text
                or "Escucha el audio del cliente en español y respóndele de forma natural."
            )
        )
    else:
        user_parts.append(types.Part.from_text(text=spoken_text))

    contents: list[Any] = [*history_contents, types.Content(role="user", parts=user_parts)]
    gen_cfg = types.GenerateContentConfig(
        system_instruction=sys_prompt,
        tools=[types.Tool(function_declarations=fn_decls)],
        temperature=0.15,
    )

    resp = await client.aio.models.generate_content(
        model=cfg.GEMINI_MODEL,
        contents=contents,
        config=gen_cfg,
    )

    executed_tools: list[dict[str, Any]] = []
    if resp.function_calls:
        contents.append(resp.candidates[0].content)
        fn_response_parts: list[Any] = []
        for fc in resp.function_calls:
            args = dict(fc.args or {})
            if fc.name in ("request_hitl_sales_exception", "book_land_consultation"):
                args.setdefault("buyer_name", buyer_name)
                args.setdefault("buyer_phone", buyer_phone)
            tool_out = await execute_voice_tool(
                fc.name, args, settings=cfg, db_path=db_path
            )
            executed_tools.append({"name": fc.name, "args": args, "output": tool_out})
            fn_response_parts.append(
                types.Part.from_function_response(name=fc.name, response=tool_out)
            )
        contents.append(types.Content(role="user", parts=fn_response_parts))
        resp = await client.aio.models.generate_content(
            model=cfg.GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=sys_prompt,
                temperature=0.15,
            ),
        )

    spoken_reply = (resp.text or "").replace("**", "").replace("*", "").replace("`", "").strip()
    user_label = spoken_text or transcribed_from_audio or "[Audio de voz del cliente]"
    record_message(
        contact_id=contact["id"],
        sender_role="BUYER",
        channel="VOICE_VERTEX_GEMINI",
        body=user_label,
        meta={"tools_triggered": [t["name"] for t in executed_tools]},
        db_path=db_path,
    )
    if spoken_reply:
        record_message(
            contact_id=contact["id"],
            sender_role="AI_SOFIA",
            channel="VOICE_VERTEX_GEMINI",
            body=spoken_reply,
            meta={"tools_triggered": [t["name"] for t in executed_tools]},
            db_path=db_path,
        )

    audio_mp3_b64 = (
        await synthesize_spanish_speech_mp3_b64(spoken_reply, cfg)
        if (include_tts_audio and spoken_reply)
        else ""
    )

    if not executed_tools:
        orchestrator = DualWriteCRMOrchestrator(settings=cfg, db_path=db_path)
        await orchestrator.sync(
            contact["id"],
            note_body=f"🎙️ [CONVERSACIÓN VOZ GEMINI]\nCliente: {user_label}\nSofía: {spoken_reply}",
        )

    with get_connection(db_path) as conn:
        refreshed = conn.execute(
            "SELECT hitl_state, pipeline_stage FROM contacts WHERE id = ?",
            (contact["id"],),
        ).fetchone()

    return {
        "engine": "vertex_gemini",
        "contact_id": contact["id"],
        "bot_silenced": False,
        "hitl_state": refreshed["hitl_state"] if refreshed else "AI_AUTONOMOUS",
        "pipeline_stage": refreshed["pipeline_stage"] if refreshed else "NEW_LEAD",
        "user_transcript": user_label,
        "tool_calls": executed_tools,
        "spoken_text_es": spoken_reply,
        "audio_mp3_base64": audio_mp3_b64,
    }
