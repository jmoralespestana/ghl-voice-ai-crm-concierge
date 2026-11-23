from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.adapters.crm import DualWriteCRMOrchestrator
from app.config import Settings, get_settings
from app.db import get_connection, new_id, row_to_contact_dict, utc_now_iso
from app.domain import (
    DocType,
    SEED_LOTS,
    calculate_seller_financing,
    get_lot_or_none,
)
from app.events import broadcaster

_DOLLAR_AMOUNT_RE = re.compile(
    r"(?:\$\s*|usd\s*)(\d{1,3}(?:[.,]\d{3})+|\d{3,6})|"
    r"\b(\d{1,3}(?:[.,]\d{3})+|\d{4,6})\s*(?:dólares|dolares|usd|bucks)\b",
    re.IGNORECASE,
)
_BARGAIN_KEYWORDS_RE = re.compile(
    r"\b(?:descuento|rebaja|menos\s+enganche|0%\s*inter[eé]s|sin\s+inter[eé]s|"
    r"excepci[oó]n|oferta|te\s+doy|solo\s+tengo|nada\s+m[aá]s\s+tengo)\b",
    re.IGNORECASE,
)
_BOOKING_INTENT_RE = re.compile(
    r"\b(?:agendar|cita|visitar|ver\s+el\s+terreno|ir\s+a\s+verlo|s[aá]bado|domingo|"
    r"viernes|mañana|ll[aá]mame|comprar\s+hoy)\b",
    re.IGNORECASE,
)
_OUT_OF_STATE_RE = re.compile(
    r"\b(colorado|denver|texas|houston|dallas|austin|san\s+antonio|arizona|phoenix|tucson|"
    r"nevada|las\s+vegas|reno|florida|miami|orlando|tampa|nuevo\s+m[eé]xico|new\s+mexico|"
    r"utah|oregon|washington|chicago|illinois|nueva\s+york|new\s+york|georgia|atlanta|"
    r"carolina|m[eé]xico|tijuana|otro\s+estado|otros\s+estados|fuera\s+de\s+california)\b",
    re.IGNORECASE,
)


def _clean_text_for_tts(text: str) -> str:
    cleaned = re.sub(r"[*`•📋✅⚠️🛑🎙️📅]", "", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


async def _maybe_tts_b64(text: str, include_audio: bool, cfg: Settings) -> str:
    if not include_audio or not text or not cfg.VERTEX_USE_ADC:
        return ""
    from app.adapters.voice import synthesize_spanish_speech_mp3_b64

    return await synthesize_spanish_speech_mp3_b64(_clean_text_for_tts(text), settings=cfg)


def _extract_offered_dollars(text: str) -> int | None:
    for m in _DOLLAR_AMOUNT_RE.finditer(text or ""):
        raw = m.group(1) or m.group(2)
        if not raw:
            continue
        digits = re.sub(r"[^\d]", "", raw)
        if digits:
            val = int(digits)
            if 500 <= val <= 200_000:
                return val
    return None


def _detect_lot_in_text(text: str, default_code: str = "LOT-AV-01") -> str:
    lowered = (text or "").lower()
    if "phelan" in lowered or "ph-04" in lowered or "5 acres" in lowered or "cinco acres" in lowered:
        return "LOT-PH-04"
    if "hesperia" in lowered or "he-03" in lowered or "rancho" in lowered or "caballo" in lowered or "2.5" in lowered:
        return "LOT-HE-03"
    if "victorville" in lowered or "vv-02" in lowered or "1 acre" in lowered or "un acre" in lowered or "i-15" in lowered:
        return "LOT-VV-02"
    if "apple valley" in lowered or "av-01" in lowered or "vista del sol" in lowered or "medio acre" in lowered or "0.5" in lowered:
        return "LOT-AV-01"
    return default_code


def _detect_doc_type(text: str, current: str = "ITIN") -> DocType:
    lowered = (text or "").lower()
    if "contado" in lowered or "cash" in lowered:
        return "CASH"
    if "pasaporte" in lowered or "matricula" in lowered:
        return "PASSPORT"
    if "social" in lowered or "ssn" in lowered:
        return "SSN"
    if "itin" in lowered or "tax id" in lowered:
        return "ITIN"
    return current if current in ("ITIN", "PASSPORT", "SSN", "CASH") else "ITIN"


def get_or_create_contact(
    *,
    full_name: str,
    phone: str,
    email: str = "",
    doc_type: DocType = "ITIN",
    interested_lot_code: str = "LOT-AV-01",
    down_payment_cents: int = 350_000,
    channel_origin: str = "WEB_CHAT",
    db_path: str | None = None,
) -> dict[str, Any]:
    clean_phone = re.sub(r"[^\d+]", "", phone or "") or "+17605550199"
    now = utc_now_iso()
    with get_connection(db_path) as conn:
        existing = conn.execute(
            "SELECT * FROM contacts WHERE phone = ?", (clean_phone,)
        ).fetchone()
        if existing is not None:
            conn.execute(
                """
                UPDATE contacts
                SET full_name = COALESCE(NULLIF(?, ''), full_name),
                    interested_lot_code = COALESCE(NULLIF(?, ''), interested_lot_code),
                    updated_at = ?
                WHERE id = ?
                """,
                (full_name.strip(), interested_lot_code.strip(), now, existing["id"]),
            )
            conn.commit()
            refreshed = conn.execute(
                "SELECT * FROM contacts WHERE id = ?", (existing["id"],)
            ).fetchone()
            return row_to_contact_dict(refreshed)

        cid = new_id()
        conn.execute(
            """
            INSERT INTO contacts (
                id, full_name, phone, email, doc_type, interested_lot_code,
                down_payment_cents, pipeline_stage, hitl_state, channel_origin,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'NEW_LEAD', 'AI_AUTONOMOUS', ?, ?, ?)
            """,
            (
                cid,
                full_name.strip() or "Prospecto Web California",
                clean_phone,
                email.strip(),
                doc_type,
                interested_lot_code,
                down_payment_cents,
                channel_origin,
                now,
                now,
            ),
        )
        conn.commit()
        created = conn.execute(
            "SELECT * FROM contacts WHERE id = ?", (cid,)
        ).fetchone()
        return row_to_contact_dict(created)


def record_message(
    *,
    contact_id: str,
    sender_role: str,
    channel: str,
    body: str,
    meta: dict[str, Any] | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    mid = new_id()
    now = utc_now_iso()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO messages (id, contact_id, sender_role, channel, body, meta_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mid,
                contact_id,
                sender_role,
                channel,
                body,
                json.dumps(meta or {}, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
    msg = {
        "id": mid,
        "contact_id": contact_id,
        "sender_role": sender_role,
        "channel": channel,
        "body": body,
        "meta": meta or {},
        "created_at": now,
    }
    broadcaster.emit("message_created", msg)
    return msg


async def _generate_grounded_chat_reply_vertex(
    *,
    contact_id: str,
    buyer_message: str,
    lot: Any,
    quote: Any,
    inferred_doc: str,
    wants_booking: bool,
    fallback_reply: str,
    cfg: Settings,
    db_path: str | None = None,
) -> str:
    if not cfg.VERTEX_USE_ADC:
        return fallback_reply
    try:
        from google.genai import types
        from app.adapters.voice import COMPANY_KNOWLEDGE_GROUNDING_ES

        with get_connection(db_path) as conn:
            # Exclude the current BUYER row we just inserted (LIMIT 9 -> skip newest 1)
            rows = conn.execute(
                "SELECT sender_role, body FROM messages WHERE contact_id = ? ORDER BY rowid DESC LIMIT 9",
                (contact_id,),
            ).fetchall()
        prior_rows = rows[1:] if len(rows) > 1 else []
        merged: list[tuple[str, str]] = []
        for r in reversed(prior_rows):
            role = "user" if r["sender_role"] == "BUYER" else "model"
            txt = str(r["body"] or "").strip()
            if not txt:
                continue
            if merged and merged[-1][0] == role:
                merged[-1] = (role, f"{merged[-1][1]}\n{txt[:450]}")
            else:
                merged.append((role, txt[:500]))
        while merged and merged[0][0] != "user":
            merged.pop(0)
        while merged and merged[-1][0] == "user":
            merged.pop()

        contents: list[Any] = [
            types.Content(role=rl, parts=[types.Part.from_text(text=tx)])
            for rl, tx in merged
        ]
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=buyer_message)]))

        catalog_json = json.dumps([l.to_public_dict() for l in SEED_LOTS], ensure_ascii=False)
        sys_instruction = (
            "Eres Sofía, asesora digital de Horizon Estates (100 High Desert Blvd #200, Apple Valley, CA).\n"
            f"{COMPANY_KNOWLEDGE_GROUNDING_ES}\n"
            f"Catálogo verificado en tiempo real: {catalog_json}\n"
            f"Cotización matemática exacta precalculada para el lote activo ({lot.code} - {lot.title}): "
            f"Contado=${lot.cash_price_cents // 100:,} USD, Enganche considerado=${quote.down_payment_cents // 100:,} USD "
            f"(mínimo oficial=${lot.min_down_payment_cents // 100:,} USD), Plazo={quote.term_months} meses, "
            f"Mensualidad exacta=${quote.monthly_payment_cents / 100:,.2f} USD/mes, Documento={inferred_doc}, "
            f"Intención de agendar visita detectada={wants_booking}.\n"
            "INSTRUCCIONES DE RESPUESTA:\n"
            "- Responde directamente a lo que preguntó la persona manteniendo el hilo de la conversación en español natural, cálido y ejecutivo (2 a 4 oraciones claras).\n"
            "- Usa SIEMPRE las cifras exactas precalculadas arriba cuando hables de precios o mensualidades (jamás inventes otros montos).\n"
            "- Si preguntan por otros estados fuera de California o servicios que no ofrecemos (construcción de casas, préstamos bancarios), aclara con honestidad que solo vendemos terrenos con financiamiento directo en California."
        )
        client = cfg.build_genai_client()
        resp = await client.aio.models.generate_content(
            model=cfg.GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=sys_instruction,
                temperature=0.2,
            ),
        )
        txt = (resp.text or "").strip()
        return txt if len(txt) >= 15 else fallback_reply
    except Exception:
        return fallback_reply


async def handle_buyer_chat_turn(
    *,
    full_name: str,
    phone: str,
    message: str,
    channel: str = "WEB_CHAT",
    selected_lot_code: str = "LOT-AV-01",
    term_months: int = 60,
    doc_type: DocType = "ITIN",
    include_audio: bool = False,
    settings: Settings | None = None,
    db_path: str | None = None,
    ghl_http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    cfg = settings or get_settings()
    lot_code = _detect_lot_in_text(message, default_code=selected_lot_code)
    inferred_doc = _detect_doc_type(message, current=doc_type)

    contact = get_or_create_contact(
        full_name=full_name,
        phone=phone,
        doc_type=inferred_doc,
        interested_lot_code=lot_code,
        channel_origin=channel,
        db_path=db_path,
    )
    cid = contact["id"]
    record_message(
        contact_id=cid,
        sender_role="BUYER",
        channel=channel,
        body=message,
        db_path=db_path,
    )

    orchestrator = DualWriteCRMOrchestrator(
        settings=cfg, db_path=db_path, ghl_http_client=ghl_http_client
    )

    # HARD GUARDRAIL 1: If a human closer took over, AI MUST stay 100% silent!
    if contact["hitl_state"] == "HUMAN_TAKEN_OVER":
        sync_results = await orchestrator.sync(
            cid,
            note_body=f"[MENSAJE ENTRANDO EN MODO CONTROL HUMANO - BOT SILENCIADO]\nCliente: {message}",
        )
        broadcaster.emit(
            "hitl_bot_silenced",
            {"contact_id": cid, "buyer_message": message},
        )
        return {
            "contact_id": cid,
            "bot_silenced": True,
            "hitl_state": "HUMAN_TAKEN_OVER",
            "guardrail_triggered": "HUMAN_TAKEOVER_LOCK",
            "reply": None,
            "audio_mp3_base64": "",
            "system_banner_es": "Un asesor comercial humano atiende personalmente esta conversación (IA en pausa).",
            "crm_sync": [r.__dict__ for r in sync_results],
        }

    # HARD GUARDRAIL 1B: Out-of-State / Out-of-Coverage Geographic Guardrail (e.g. Colorado, Texas, Arizona)
    oos_match = _OUT_OF_STATE_RE.search(message or "")
    if oos_match:
        raw_region = oos_match.group(1).strip()
        region_display = (
            "otros estados fuera de California"
            if ("otro" in raw_region.lower() or "fuera" in raw_region.lower())
            else raw_region.title()
        )
        alt_lot = SEED_LOTS[0]
        alt_quote = calculate_seller_financing(
            alt_lot.code, alt_lot.min_down_payment_cents, term_months=60, doc_type=inferred_doc
        )
        reply_text = (
            f"Por el momento **no tenemos terrenos ni servicios en {region_display}**. "
            f"En **Horizon Estates** operamos actualmente **exclusivamente en California** "
            f"(Apple Valley, Victorville y Hesperia, en el High Desert del condado de San Bernardino), "
            f"aunque tenemos planes de abrir otros estados más adelante.\n\n"
            f"Si te interesa invertir desde otro estado en tierra californiana con alta plusvalía, calificas sin bancos con **{inferred_doc} o Pasaporte** "
            f"desde **${alt_lot.min_down_payment_cents // 100:,} USD** de enganche (por ejemplo, **{alt_lot.title}** queda en **${alt_quote.monthly_payment_cents / 100:,.2f} USD/mes** a 60 meses).\n\n"
            f"¿Te gustaría ver cómo funciona la compra en California o buscas únicamente en {region_display}?"
        )
        record_message(
            contact_id=cid,
            sender_role="AI_SOFIA",
            channel=channel,
            body=reply_text,
            meta={"guardrail": "OUT_OF_STATE_COVERAGE", "requested_region": region_display},
            db_path=db_path,
        )
        sync_results = await orchestrator.sync(
            cid,
            note_body=f"[CONSULTA FUERA DE COBERTURA — {region_display}] Cliente preguntó por {region_display}; informado que solo operamos en California.",
        )
        audio_b64 = await _maybe_tts_b64(reply_text, include_audio, cfg)
        return {
            "contact_id": cid,
            "bot_silenced": False,
            "hitl_state": contact["hitl_state"],
            "pipeline_stage": contact["pipeline_stage"],
            "guardrail_triggered": "OUT_OF_STATE_COVERAGE",
            "reply": reply_text,
            "audio_mp3_base64": audio_b64,
            "crm_sync": [r.__dict__ for r in sync_results],
        }

    lot = get_lot_or_none(lot_code) or SEED_LOTS[0]

    # HARD GUARDRAIL 2: Never sell or book a RESERVED / SOLD lot!
    if lot.status in ("RESERVED", "SOLD"):
        alt_lot = SEED_LOTS[0]
        alt_quote = calculate_seller_financing(
            alt_lot.code, alt_lot.min_down_payment_cents, term_months=60, doc_type=inferred_doc
        )
        reply_text = (
            f"El terreno **{lot.title}** ({lot.acres}) se encuentra actualmente **Apartado** en nuestro inventario en vivo, "
            f"por lo que no podemos recibir depósitos sobre esa parcela.\n\n"
            f"Te recomiendo dos opciones disponibles hoy mismo con financiamiento directo ({inferred_doc} aceptado, sin bancos):\n"
            f"• **{alt_lot.title}** ({alt_lot.acres}): Contado **${alt_lot.cash_price_cents // 100:,} USD**, enganche desde "
            f"**${alt_lot.min_down_payment_cents // 100:,} USD** y cuota estimada de **${alt_quote.monthly_payment_cents / 100:,.2f} USD/mes** (60 meses).\n"
            f"• **Hesperia — Rancho Los Álamos** (2.50 Acres, uso rancho/caballos): Contado **$79,000 USD**, enganche desde **$7,500 USD**.\n\n"
            f"¿Te gustaría agendar visita para ver el lote disponible en Apple Valley o Hesperia?"
        )
        record_message(
            contact_id=cid,
            sender_role="AI_SOFIA",
            channel=channel,
            body=reply_text,
            meta={"guardrail": "INVENTORY_RESERVED_BLOCK", "blocked_lot": lot.code},
            db_path=db_path,
        )
        sync_results = await orchestrator.sync(
            cid,
            note_body=f"[GUARDRAIL INVENTARIO] Bloqueada venta sobre lote apartado {lot.code}. Redirigido a {alt_lot.code}.",
        )
        audio_b64 = await _maybe_tts_b64(reply_text, include_audio, cfg)
        return {
            "contact_id": cid,
            "bot_silenced": False,
            "hitl_state": contact["hitl_state"],
            "guardrail_triggered": "INVENTORY_RESERVED_BLOCK",
            "reply": reply_text,
            "audio_mp3_base64": audio_b64,
            "crm_sync": [r.__dict__ for r in sync_results],
        }

    # HARD GUARDRAIL 3: Below-minimum down payment OR custom discount/0% APR request -> HITL Interrupt!
    offered_usd = _extract_offered_dollars(message)
    min_down_usd = lot.min_down_payment_cents // 100
    is_below_min = offered_usd is not None and offered_usd < min_down_usd
    asks_unverified_discount = bool(_BARGAIN_KEYWORDS_RE.search(message or "")) and (
        offered_usd is None or offered_usd < min_down_usd
    )

    if is_below_min or asks_unverified_discount:
        offered_cents = (offered_usd * 100) if offered_usd is not None else int(lot.min_down_payment_cents * 0.75)
        hitl_id = new_id()
        reason_es = (
            f"Cliente ofrece ${offered_cents // 100:,} USD de enganche (o solicita condición especial) "
            f"sobre {lot.code} (mínimo oficial: ${min_down_usd:,} USD)."
        )
        with get_connection(db_path) as conn:
            conn.execute(
                """
                UPDATE contacts
                SET hitl_state = 'PENDING_HITL',
                    pipeline_stage = 'PENDING_HITL_APPROVAL',
                    interested_lot_code = ?,
                    down_payment_cents = ?,
                    doc_type = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (lot.code, offered_cents, inferred_doc, utc_now_iso(), cid),
            )
            conn.execute(
                """
                INSERT INTO hitl_requests (
                    id, contact_id, lot_code, offered_down_cents,
                    required_min_down_cents, reason_es, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?)
                """,
                (
                    hitl_id,
                    cid,
                    lot.code,
                    offered_cents,
                    lot.min_down_payment_cents,
                    reason_es,
                    utc_now_iso(),
                ),
            )
            conn.commit()

        reply_text = (
            f"Para **{lot.title}** ({lot.acres}), el enganche mínimo oficial en sistema es de **${min_down_usd:,} USD** "
            f"(precio de contado **${lot.cash_price_cents // 100:,} USD**). Como asistente virtual no tengo autorización para "
            f"modificar enganches ni tasas por mi cuenta.\n\n"
            f"✅ **Acabo de enviar tu propuesta de ${offered_cents // 100:,} USD directamente al panel de nuestro Director Comercial (HITL)** "
            f"para revisión inmediata. Si autoriza la excepción o toma el chat, recibirás confirmación aquí mismo en segundos."
        )
        record_message(
            contact_id=cid,
            sender_role="AI_SOFIA",
            channel=channel,
            body=reply_text,
            meta={"guardrail": "HITL_EXCEPTION_ESCALATION", "hitl_request_id": hitl_id},
            db_path=db_path,
        )
        sync_results = await orchestrator.sync(
            cid,
            note_body=f"⚠️ [ALERTA HITL EN VIVO] {reason_es}\nEsperando autorización de Dirección Comercial.",
        )
        broadcaster.emit(
            "hitl_requested",
            {
                "hitl_request_id": hitl_id,
                "contact_id": cid,
                "lot_code": lot.code,
                "offered_down_usd": offered_cents // 100,
                "required_min_down_usd": min_down_usd,
                "reason_es": reason_es,
            },
        )
        audio_b64 = await _maybe_tts_b64(reply_text, include_audio, cfg)
        return {
            "contact_id": cid,
            "bot_silenced": False,
            "hitl_state": "PENDING_HITL",
            "guardrail_triggered": "HITL_EXCEPTION_ESCALATION",
            "hitl_request_id": hitl_id,
            "reply": reply_text,
            "audio_mp3_base64": audio_b64,
            "crm_sync": [r.__dict__ for r in sync_results],
        }

    # Grounded Qualification, Multi-Turn FAQ or Booking Turn
    effective_down_cents = (
        offered_usd * 100
        if (offered_usd is not None and offered_usd >= min_down_usd)
        else lot.min_down_payment_cents
    )
    quote = calculate_seller_financing(
        lot.code, effective_down_cents, term_months=term_months, doc_type=inferred_doc
    )
    wants_booking = bool(_BOOKING_INTENT_RE.search(message or ""))
    new_stage = "SHOWING_BOOKED" if wants_booking else "QUALIFIED_ITIN"

    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE contacts
            SET interested_lot_code = ?, down_payment_cents = ?, doc_type = ?,
                pipeline_stage = ?, updated_at = ?
            WHERE id = ?
            """,
            (lot.code, effective_down_cents, inferred_doc, new_stage, utc_now_iso(), cid),
        )
        conn.commit()

    if wants_booking:
        fallback_reply = (
            f"¡Excelente! He pre-registrado tu visita guiada para conocer **{lot.title}** en **{lot.city}**.\n\n"
            f"📋 **Resumen Verificado para tu Asesor**:\n"
            f"• Parcela: `{lot.code}` — {lot.acres} ({lot.utilities_summary})\n"
            f"• Precio de Contado: **${lot.cash_price_cents // 100:,} USD**\n"
            f"• Enganche considerado: **${effective_down_cents // 100:,} USD** (calificación con **{inferred_doc}**, sin bancos)\n"
            f"• Mensualidad estimada ({quote.term_months} meses): **${quote.monthly_payment_cents / 100:,.2f} USD/mes**\n\n"
            f"Nuestro equipo comercial en `Apple Valley, CA` ya recibió tu ficha en GoHighLevel para confirmarte la hora exacta."
        )
    else:
        fallback_reply = (
            f"¡Con gusto! En **{lot.city}** tenemos disponible **{lot.title}** ({lot.acres}, {lot.zoning}).\n\n"
            f"• **Servicios**: {lot.utilities_summary}.\n"
            f"• **Precio de Contado**: **${lot.cash_price_cents // 100:,} USD**.\n"
            f"• **Financiamiento Directo con el Dueño ({inferred_doc} / Pasaporte aceptado)**: entrando con "
            f"**${effective_down_cents // 100:,} USD** de enganche a **{quote.term_months} meses**, tu cuota estimada queda en "
            f"**${quote.monthly_payment_cents / 100:,.2f} USD/mes**.\n\n"
            f"¿Cuánto enganche tienes planeado destinar y te gustaría agendar una visita esta semana en Apple Valley?"
        )

    if cfg.VERTEX_USE_ADC and cfg.VERTEX_PROJECT_ID.strip() and channel == "WEB_CHAT" and ghl_http_client is None:
        reply_text = await _generate_grounded_chat_reply_vertex(
            contact_id=cid,
            buyer_message=message,
            lot=lot,
            quote=quote,
            inferred_doc=inferred_doc,
            wants_booking=wants_booking,
            fallback_reply=fallback_reply,
            cfg=cfg,
            db_path=db_path,
        )
    else:
        reply_text = fallback_reply

    record_message(
        contact_id=cid,
        sender_role="AI_SOFIA",
        channel=channel,
        body=reply_text,
        meta={"lot_code": lot.code, "monthly_usd": round(quote.monthly_payment_cents / 100, 2)},
        db_path=db_path,
    )
    sync_results = await orchestrator.sync(
        cid,
        note_body=(
            f"[CALIFICACIÓN IA SOFÍA — {new_stage}]\n"
            f"Lote: {lot.code} ({lot.title}) | Documento: {inferred_doc}\n"
            f"Enganche: ${effective_down_cents // 100:,} USD | Cuota {quote.term_months}m: ${quote.monthly_payment_cents / 100:,.2f}/mes\n"
            f"Consulta del cliente: {message}"
        ),
    )
    broadcaster.emit("contact_updated", {"contact_id": cid, "pipeline_stage": new_stage})
    audio_b64 = await _maybe_tts_b64(reply_text, include_audio, cfg)
    return {
        "contact_id": cid,
        "bot_silenced": False,
        "hitl_state": contact["hitl_state"],
        "pipeline_stage": new_stage,
        "guardrail_triggered": None,
        "quote": quote.to_dict(),
        "reply": reply_text,
        "audio_mp3_base64": audio_b64,
        "crm_sync": [r.__dict__ for r in sync_results],
    }


async def resolve_hitl_action(
    *,
    contact_id: str,
    action: str,
    advisor_name: str = "Director Comercial (Ventas)",
    custom_message: str = "",
    settings: Settings | None = None,
    db_path: str | None = None,
    ghl_http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """
    Execute real-time HITL state transitions from the Director Cockpit (or inbound GHL webhook):
    - `APPROVE_EXCEPTION`: Approves custom down-payment, notifies buyer, advances to `SHOWING_BOOKED`.
    - `TAKE_OVER`: Sets `HUMAN_TAKEN_OVER` (`human-takeover` tag in GHL), silencing AI immediately.
    - `SEND_HUMAN_REPLY`: Sends a manual sales closer message while keeping AI silenced.
    - `RELEASE_TO_AI`: Removes `human-takeover` lock and restores `AI_AUTONOMOUS`.
    """
    cfg = settings or get_settings()
    now = utc_now_iso()
    orchestrator = DualWriteCRMOrchestrator(
        settings=cfg, db_path=db_path, ghl_http_client=ghl_http_client
    )

    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if row is None:
            raise ValueError(f"Contact {contact_id} not found")
        contact = row_to_contact_dict(row)

        if action == "APPROVE_EXCEPTION":
            req = conn.execute(
                "SELECT * FROM hitl_requests WHERE contact_id = ? AND status = 'PENDING' ORDER BY created_at DESC LIMIT 1",
                (contact_id,),
            ).fetchone()
            offered_usd = (req["offered_down_cents"] // 100) if req else (contact["down_payment_cents"] // 100)
            lot = get_lot_or_none(contact["interested_lot_code"]) or SEED_LOTS[0]
            quote = calculate_seller_financing(
                lot.code, offered_usd * 100, term_months=60, doc_type=contact["doc_type"]
            )
            if req:
                conn.execute(
                    "UPDATE hitl_requests SET status = 'APPROVED', resolved_by = ?, resolution_note = ?, resolved_at = ? WHERE id = ?",
                    (advisor_name, custom_message or "Excepción autorizada", now, req["id"]),
                )
            conn.execute(
                "UPDATE contacts SET hitl_state = 'AI_AUTONOMOUS', pipeline_stage = 'SHOWING_BOOKED', updated_at = ? WHERE id = ?",
                (now, contact_id),
            )
            conn.commit()
            msg_body = custom_message.strip() or (
                f"✅ **Autorización Gerencial Aprobada por {advisor_name}**: Se acepta excepcionalmente tu enganche de "
                f"**${offered_usd:,} USD** para apartar **{lot.title}** (`{lot.code}`), quedando una mensualidad estimada de "
                f"**${quote.monthly_payment_cents / 100:,.2f} USD/mes** (60 meses, sujeto a firma antes del sábado). "
                f"¿A qué hora te agendamos en nuestra oficina de Apple Valley?"
            )
            record_message(
                contact_id=contact_id,
                sender_role="HUMAN_ADVISOR",
                channel="HITL_APPROVAL",
                body=msg_body,
                meta={"approved_by": advisor_name, "approved_down_usd": offered_usd},
                db_path=db_path,
            )
            note = f"✅ [HITL APROBADO POR {advisor_name}] Excepción de enganche (${offered_usd:,} USD) autorizada en {lot.code}."

        elif action == "TAKE_OVER":
            conn.execute(
                "UPDATE contacts SET hitl_state = 'HUMAN_TAKEN_OVER', updated_at = ? WHERE id = ?",
                (now, contact_id),
            )
            conn.commit()
            if custom_message.strip():
                record_message(
                    contact_id=contact_id,
                    sender_role="HUMAN_ADVISOR",
                    channel="GHL_HUMAN_CHAT",
                    body=custom_message.strip(),
                    meta={"advisor": advisor_name},
                    db_path=db_path,
                )
            note = f"✋ [CONTROL HUMANO ACTIVADO POR {advisor_name}] Tag 'human-takeover' aplicado; IA silenciada en este contacto."

        elif action == "SEND_HUMAN_REPLY":
            conn.execute(
                "UPDATE contacts SET hitl_state = 'HUMAN_TAKEN_OVER', updated_at = ? WHERE id = ?",
                (now, contact_id),
            )
            conn.commit()
            body = custom_message.strip() or f"Hola {contact['full_name']}, te saluda {advisor_name} de Horizon Estates. Te llamo en 2 minutos para cerrar los detalles."
            record_message(
                contact_id=contact_id,
                sender_role="HUMAN_ADVISOR",
                channel="GHL_HUMAN_CHAT",
                body=body,
                meta={"advisor": advisor_name},
                db_path=db_path,
            )
            note = f"💬 [RESPUESTA HUMANA DE {advisor_name}] {body}"

        elif action == "RELEASE_TO_AI":
            conn.execute(
                "UPDATE contacts SET hitl_state = 'AI_AUTONOMOUS', updated_at = ? WHERE id = ?",
                (now, contact_id),
            )
            conn.commit()
            note = f"🤖 [CONTROL DEVUELTO A IA SOFÍA POR {advisor_name}] Candado 'human-takeover' retirado."
        else:
            raise ValueError(f"Unsupported HITL action: {action}")

    sync_results = await orchestrator.sync(contact_id, note_body=note)
    with get_connection(db_path) as conn:
        updated = row_to_contact_dict(
            conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        )
    broadcaster.emit("hitl_state_changed", {"contact": updated, "action": action})
    return {"contact": updated, "action": action, "crm_sync": [r.__dict__ for r in sync_results]}


async def run_dead_leads_reactivation_batch(
    *,
    settings: Settings | None = None,
    db_path: str | None = None,
    ghl_http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Reactivates historical 2022-2025 leads and promotes responsive buyers directly into Kanban & GHL."""
    cfg = settings or get_settings()
    orchestrator = DualWriteCRMOrchestrator(
        settings=cfg, db_path=db_path, ghl_http_client=ghl_http_client
    )
    promoted: list[dict[str, Any]] = []

    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM dead_leads ORDER BY id ASC").fetchall()

    for dl in rows:
        if dl["processed"] and dl["promoted_contact_id"]:
            continue
        outcome = dl["reactivation_outcome"]
        if outcome == "NURTURE":
            with get_connection(db_path) as conn:
                conn.execute("UPDATE dead_leads SET processed = 1 WHERE id = ?", (dl["id"],))
                conn.commit()
            continue

        lot_code = "LOT-HE-03" if dl["estimated_down_usd"] >= 7500 else (
            "LOT-VV-02" if dl["estimated_down_usd"] >= 5000 else "LOT-AV-01"
        )
        contact = get_or_create_contact(
            full_name=dl["full_name"],
            phone=dl["phone"],
            doc_type="ITIN",
            interested_lot_code=lot_code,
            down_payment_cents=max(250_000, dl["estimated_down_usd"] * 100),
            channel_origin=dl["origin_channel"],
            db_path=db_path,
        )
        cid = contact["id"]
        record_message(
            contact_id=cid,
            sender_role="AI_SOFIA",
            channel="REACTIVATION_SMS_WA",
            body=dl["ai_opening_es"],
            db_path=db_path,
        )
        record_message(
            contact_id=cid,
            sender_role="BUYER",
            channel="REACTIVATION_SMS_WA",
            body=dl["simulated_reply_es"],
            db_path=db_path,
        )

        stage = (
            "SHOWING_BOOKED"
            if outcome == "HOT_BOOKED"
            else ("PENDING_HITL_APPROVAL" if outcome == "PENDING_HITL" else "QUALIFIED_ITIN")
        )
        hitl_state = "PENDING_HITL" if outcome == "PENDING_HITL" else "AI_AUTONOMOUS"
        now = utc_now_iso()
        with get_connection(db_path) as conn:
            conn.execute(
                "UPDATE contacts SET pipeline_stage = ?, hitl_state = ?, updated_at = ? WHERE id = ?",
                (stage, hitl_state, now, cid),
            )
            if outcome == "PENDING_HITL":
                conn.execute(
                    """
                    INSERT INTO hitl_requests (
                        id, contact_id, lot_code, offered_down_cents,
                        required_min_down_cents, reason_es, status, created_at
                    ) VALUES (?, ?, 'LOT-AV-01', ?, 350000, ?, 'PENDING', ?)
                    """,
                    (
                        new_id(),
                        cid,
                        dl["estimated_down_usd"] * 100,
                        f"Reactivación 2024 ({dl['full_name']}): ofrece ${dl['estimated_down_usd']:,} USD en efectivo para firmar viernes.",
                        now,
                    ),
                )
            conn.execute(
                "UPDATE dead_leads SET processed = 1, promoted_contact_id = ? WHERE id = ?",
                (cid, dl["id"]),
            )
            conn.commit()

        await orchestrator.sync(
            cid,
            note_body=f"⚡ [BASE HISTÓRICA REACTIVADA — {dl['origin_channel']}]\nRespuesta: {dl['simulated_reply_es']}",
        )
        promoted.append({"dead_lead_id": dl["id"], "contact_id": cid, "outcome": outcome})

    broadcaster.emit("dead_leads_reactivated", {"promoted_count": len(promoted)})
    return {
        "total_scanned": len(rows),
        "reactivated_opportunities": len(promoted),
        "estimated_pipeline_cash_price_usd": len(promoted) * 54_500,
        "promoted": promoted,
    }
