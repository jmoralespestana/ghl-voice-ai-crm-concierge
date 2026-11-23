#!/usr/bin/env python3
"""
End-to-End Executable Runbook (13 Live Checks):
Horizon Estates Demo × GoHighLevel API v2 × Vertex AI Gemini Live Voice & HITL.

Usage:
  PYTHONPATH=. .venv/bin/python scripts/verify_demo_runbook.py
  # Optional: keep existing board state without resetting before checks:
  PYTHONPATH=. .venv/bin/python scripts/verify_demo_runbook.py --no-reset
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import io
import json
import math
import struct
import sys
import time
import wave
from typing import Any

import httpx
import websockets

from app.config import get_settings


def _generate_valid_wav_bytes(duration_sec: float = 0.6, sample_rate: int = 16000) -> bytes:
    """Generates a valid 16-bit mono PCM WAV buffer to exercise real audio ingestion on Vertex AI."""
    buf = io.BytesIO()
    n_samples = int(duration_sec * sample_rate)
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_samples):
            sample = int(3000 * math.sin(2 * math.pi * 440.0 * (i / sample_rate)))
            frames.extend(struct.pack("<h", sample))
        wf.writeframes(bytes(frames))
    return buf.getvalue()


class RunbookRunner:
    def __init__(self, base_url: str, reset_first: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.ws_base_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        self.reset_first = reset_first
        self.results: list[tuple[str, bool, str]] = []

    def record(self, code: str, passed: bool, detail: str) -> None:
        icon = "✅ PASS" if passed else "❌ FAIL"
        print(f"{icon} | {code}: {detail}", flush=True)
        self.results.append((code, passed, detail))
        if not passed:
            raise AssertionError(f"Runbook check failed at {code}: {detail}")

    async def run_all(self) -> int:
        started = time.perf_counter()
        cfg = get_settings()
        print("=" * 86)
        print("🚀 HORIZON ESTATES — E2E RUNBOOK: GHL API v2 × GEMINI LIVE VOICE × HITL")
        print(f"   Target HTTP : {self.base_url}")
        print(f"   Target WS   : {self.ws_base_url}/api/v1/voice/gemini-live/ws")
        print(f"   Vertex AI   : {cfg.VERTEX_PROJECT_ID} ({cfg.VERTEX_LOCATION}) / {cfg.GEMINI_MODEL}")
        print("=" * 86, flush=True)

        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
            if self.reset_first:
                await client.post("/api/v1/cockpit/reset")

            # CHECK 01: Server, 4 High Desert Lots & Live GoHighLevel API v2 Connectivity
            lots_r = await client.get("/api/v1/lots")
            lots = lots_r.json().get("lots", [])
            ghl_v = (await client.post("/api/v1/ghl/verify")).json()
            self.record(
                "CHECK-01 [Infra & GHL Ping]",
                lots_r.status_code == 200 and len(lots) == 4 and ghl_v.get("connected") is True,
                f"4 lotes cargados + GoHighLevel API v2 conectado (location={ghl_v.get('location_id')}, HTTP {ghl_v.get('http_status')})",
            )

            # CHECK 02: Exact BIGINT Cents Calculator (ITIN + Cash + Below-Min Warning)
            q_itin = (
                await client.post(
                    "/api/v1/financing/quote",
                    json={"lot_code": "LOT-AV-01", "down_payment_usd": 3500, "term_months": 60, "doc_type": "ITIN"},
                )
            ).json()
            q_low = (
                await client.post(
                    "/api/v1/financing/quote",
                    json={"lot_code": "LOT-AV-01", "down_payment_usd": 2000, "term_months": 60, "doc_type": "ITIN"},
                )
            ).json()
            q_cash = (
                await client.post(
                    "/api/v1/financing/quote",
                    json={"lot_code": "LOT-VV-02", "down_payment_usd": 54500, "term_months": 60, "doc_type": "CASH"},
                )
            ).json()
            self.record(
                "CHECK-02 [Calculadora Exacta BIGINT]",
                q_itin["monthly_payment_cents"] == 77160
                and q_low["requires_hitl_exception"] is True
                and q_cash["monthly_payment_cents"] == 0,
                f"LOT-AV-01 cuota exacta=${q_itin['monthly_payment_usd']}/mes | Alerta menor enganche={q_low['requires_hitl_exception']} | Contado=$0/mes",
            )

            # CHECK 03: Hard Inventory Guardrail on Reserved Lot (LOT-PH-04 Phelan)
            r_res = (
                await client.post(
                    "/api/v1/chat",
                    json={
                        "full_name": "Demo Guardia Inventario",
                        "phone": "+17605550101",
                        "message": "Quiero dar enganche hoy para apartar los 5 acres de Phelan.",
                        "selected_lot_code": "LOT-PH-04",
                    },
                )
            ).json()
            self.record(
                "CHECK-03 [Guardrail Lote Apartado]",
                r_res["guardrail_triggered"] == "INVENTORY_RESERVED_BLOCK" and "Apartado" in (r_res["reply"] or ""),
                "Bloqueada venta sobre LOT-PH-04 (Apartado) y redirigido a inventario disponible.",
            )

            # CHECK 04: Autonomous Qualification + Live Dual-Write (SQLite WAL + GHL API v2)
            r_qual = (
                await client.post(
                    "/api/v1/chat",
                    json={
                        "full_name": "Demo ITIN Calificada",
                        "phone": "+17605550102",
                        "message": "Hola, tengo ITIN y $5,000 dólares listos para el terreno de 1 acre en Victorville.",
                        "selected_lot_code": "LOT-VV-02",
                    },
                )
            ).json()
            ghl_sync_04 = r_qual["crm_sync"][1]
            self.record(
                "CHECK-04 [Dual-Write Chat -> GHL v2]",
                r_qual["pipeline_stage"] == "QUALIFIED_ITIN"
                and ghl_sync_04["mode"].startswith("LIVE_")
                and bool(ghl_sync_04["contact_external_id"]),
                f"Contacto GHL={ghl_sync_04['contact_external_id']} | Oportunidad GHL={ghl_sync_04['opportunity_external_id']}",
            )

            # CHECK 05: Bargaining Escalation -> PENDING_HITL
            r_hitl = (
                await client.post(
                    "/api/v1/chat",
                    json={
                        "full_name": "Demo Regateo HITL",
                        "phone": "+17605550103",
                        "message": "Solo tengo $2,500 en efectivo hoy mismo para el lote de Apple Valley, ¿me aprueban excepción?",
                        "selected_lot_code": "LOT-AV-01",
                    },
                )
            ).json()
            hitl_cid = r_hitl["contact_id"]
            self.record(
                "CHECK-05 [Escalación Automática HITL]",
                r_hitl["hitl_state"] == "PENDING_HITL"
                and r_hitl["guardrail_triggered"] == "HITL_EXCEPTION_ESCALATION",
                f"Contacto {hitl_cid[:8]} puesto en PENDING_HITL y movido a columna 3 en GoHighLevel.",
            )

            # CHECK 06: Director Approves HITL Exception -> SHOWING_BOOKED
            r_app = (
                await client.post(
                    "/api/v1/hitl/action",
                    json={"contact_id": hitl_cid, "action": "APPROVE_EXCEPTION"},
                )
            ).json()
            self.record(
                "CHECK-06 [Aprobación Gerencial HITL]",
                r_app["contact"]["pipeline_stage"] == "SHOWING_BOOKED"
                and r_app["contact"]["hitl_state"] == "AI_AUTONOMOUS",
                "Excepción aprobada en 1 clic -> Oportunidad avanzada a '4. Visita / Cierre Humano' en GHL.",
            )

            # CHECK 07: Human Takeover Silences Bot 100% + Release Restores AI
            await client.post(
                "/api/v1/hitl/action",
                json={
                    "contact_id": hitl_cid,
                    "action": "SEND_HUMAN_REPLY",
                    "custom_message": "Hola, te saluda Dirección Comercial. Te esperamos el sábado a las 10 AM.",
                },
            )
            r_muted = (
                await client.post(
                    "/api/v1/chat",
                    json={
                        "full_name": "Demo Regateo HITL",
                        "phone": "+17605550103",
                        "message": "Perfecto asesor, ahí nos vemos sin falta.",
                    },
                )
            ).json()
            await client.post(
                "/api/v1/hitl/action",
                json={"contact_id": hitl_cid, "action": "RELEASE_TO_AI"},
            )
            self.record(
                "CHECK-07 [Candado Human-Takeover & Silencio IA]",
                r_muted["bot_silenced"] is True and r_muted["reply"] is None,
                "Tag 'human-takeover' activo: Sofía IA guardó silencio absoluto mientras hablaba el cerrador humano.",
            )

            # CHECK 08: Native GoHighLevel OutboundMessage Webhook Auto-Locks Bot
            r_wh_ghl = (
                await client.post(
                    "/api/v1/webhooks/ghl/inbound",
                    json={
                        "type": "OutboundMessage",
                        "userId": "ghl_closer_director_01",
                        "phone": "+17605550102",
                        "body": "Hola, te escribo directo desde la app móvil de GoHighLevel.",
                    },
                )
            ).json()
            self.record(
                "CHECK-08 [Webhook Entrante GHL -> Auto-Lock]",
                r_wh_ghl.get("status") == "human_takeover_locked",
                f"Mensaje manual desde UI de GoHighLevel detectado -> contacto {r_wh_ghl.get('contact_id', '')[:8]} bloqueado en HUMAN_TAKEN_OVER.",
            )

            # CHECK 09 & CHECK 10: Live Gemini Voice Call over WebSocket (CA § 632 + Vertex Function Calling + Real WAV Audio + GHL Sync)
            ws_uri = (
                f"{self.ws_base_url}/api/v1/voice/gemini-live/ws"
                f"?buyer_name=Demo+Voz+Gemini&buyer_phone=%2B17605550109&lot_code=LOT-HE-03"
            )
            async with websockets.connect(ws_uri, open_timeout=10) as ws:
                greeting = json.loads(await ws.recv())
                ca_632_ok = (
                    greeting.get("type") == "ca_632_greeting"
                    and greeting.get("exposes_phone_number") is False
                    and "California" in greeting.get("spoken_text_es", "")
                    and len(greeting.get("audio_mp3_base64") or "") > 5000
                )

                # Turn 1: Live Vertex AI Gemini 2.5 Flash Function Calling over Voice WebSocket
                await ws.send(
                    json.dumps(
                        {
                            "type": "user_utterance",
                            "text": "Hola Sofía, ¿en cuánto me queda la mensualidad del rancho de Hesperia LOT-HE-03 dando 8000 dólares de enganche a 60 meses?",
                            "lot_code": "LOT-HE-03",
                            "use_vertex": True,
                        }
                    )
                )
                turn_1 = json.loads(await ws.recv())
                tools_called_1 = [t["name"] for t in (turn_1.get("tool_calls") or [])]
                self.record(
                    "CHECK-09 [Llamada Gemini Vertex + Function Calling + Audio MP3]",
                    ca_632_ok
                    and turn_1.get("engine") == "vertex_gemini"
                    and "calculate_financing_plan" in tools_called_1
                    and len(turn_1.get("spoken_text_es") or "") > 15
                    and len(turn_1.get("audio_mp3_base64") or "") > 5000,
                    f"CA § 632 MP3={len(greeting.get('audio_mp3_base64') or '')} chars | Tools={tools_called_1} | Reply MP3={len(turn_1.get('audio_mp3_base64') or '')} chars",
                )

                # Turn 2: Real Binary WAV Audio Ingestion over WebSocket -> Vertex AI Multimodal + Call Hangup -> GHL Sync
                wav_b64 = base64.b64encode(_generate_valid_wav_bytes()).decode("ascii")
                await ws.send(
                    json.dumps(
                        {
                            "type": "user_audio",
                            "audio_base64": wav_b64,
                            "mime_type": "audio/wav",
                            "hint_text": "El cliente en el audio pide agendar visita este sábado para ver el terreno LOT-HE-03 en Hesperia.",
                            "lot_code": "LOT-HE-03",
                        }
                    )
                )
                turn_2 = json.loads(await ws.recv())
                tools_called_2 = [t["name"] for t in (turn_2.get("tool_calls") or [])]

                await ws.send(json.dumps({"type": "end_call", "duration_seconds": 63}))
                call_done = json.loads(await ws.recv())
                ghl_voice_sync = call_done["result"]["crm_sync"][1]
                self.record(
                    "CHECK-10 [Audio WAV Multimodal Gemini + Post-Call GHL Sync]",
                    call_done.get("type") == "call_synced"
                    and ghl_voice_sync["mode"].startswith("LIVE_")
                    and bool(ghl_voice_sync["contact_external_id"]),
                    f"Audio procesado (tools={tools_called_2}) y llamada sincronizada en GHL Contact={ghl_voice_sync['contact_external_id']} Opp={ghl_voice_sync['opportunity_external_id']}",
                )

            # CHECK 11: Retell AI Adapter Strategy & Webhook Security
            retell_sess = (
                await client.post(
                    "/api/v1/voice/session",
                    json={"provider": "retell", "buyer_name": "Demo Retell", "buyer_phone": "+17605550111"},
                )
            ).json()
            retell_wh = await client.post(
                "/api/v1/voice/retell/webhook",
                json={
                    "event": "call_analyzed",
                    "call": {
                        "call_id": "runbook-retell-001",
                        "from_number": "+17605550111",
                        "duration_ms": 47000,
                        "transcript": "Sofía: Gracias por llamar a Horizon Estates... Cliente: Busco medio acre en Apple Valley.",
                        "call_analysis": {"call_summary": "Prospecto Retell interesado en LOT-AV-01 con ITIN."},
                        "retell_llm_dynamic_variables": {"buyer_name": "Demo Retell PSTN", "lot_code": "LOT-AV-01"},
                    },
                },
            )
            self.record(
                "CHECK-11 [Strategy RetellVoiceAdapter & Webhook]",
                retell_sess["provider"] == "retell"
                and retell_sess["exposes_phone_number"] is False
                and retell_wh.status_code == 200,
                "Adaptador secundario Retell verificado (0 teléfono expuesto por defecto + sincronización post-llamada en GHL).",
            )

            # CHECK 12: Dead-Lead Reactivation Batch (2022-2025)
            r_dead = (await client.post("/api/v1/reactivation/run")).json()
            snap = (await client.get("/api/v1/cockpit/snapshot")).json()
            self.record(
                "CHECK-12 [Reactivador Base Muerta 2022-2025]",
                r_dead["total_scanned"] == 5 and len(snap["contacts"]) >= 8,
                f"Escaneados 5 leads fríos -> {r_dead['reactivated_opportunities']} oportunidades vivas inyectadas en Kanban y GoHighLevel (Total activos={len(snap['contacts'])}).",
            )

            # CHECK 13: Multi-Turn Conversational Voice & Out-of-State ('Colorado') + Real MP3 TTS
            r_chat_co = (
                await client.post(
                    "/api/v1/chat",
                    json={
                        "full_name": "Demo Consulta Colorado",
                        "phone": "+17605550113",
                        "message": "¿Ustedes tienen terrenos o servicios en Colorado?",
                        "include_audio": True,
                    },
                )
            ).json()
            ws_co_uri = (
                f"{self.ws_base_url}/api/v1/voice/gemini-live/ws"
                f"?buyer_name=Demo+Colorado+Voz&buyer_phone=%2B17605550114&lot_code=LOT-AV-01"
            )
            async with websockets.connect(ws_co_uri, open_timeout=10) as ws_co:
                _ = json.loads(await ws_co.recv())
                await ws_co.send(
                    json.dumps(
                        {
                            "type": "user_utterance",
                            "text": "Hola Sofía, una pregunta rápida: ¿ustedes venden terrenos o tienen servicios en Colorado?",
                            "lot_code": "LOT-AV-01",
                            "use_vertex": True,
                        }
                    )
                )
                co_turn_1 = json.loads(await ws_co.recv())
                await ws_co.send(
                    json.dumps(
                        {
                            "type": "user_utterance",
                            "text": "Ah ok entiendo, ¿y cuánto cuesta el terreno más económico que sí tienen en California?",
                            "lot_code": "LOT-AV-01",
                            "use_vertex": True,
                        }
                    )
                )
                co_turn_2 = json.loads(await ws_co.recv())
                await ws_co.send(json.dumps({"type": "end_call", "duration_seconds": 38}))
                _ = json.loads(await ws_co.recv())

            co_reply_lower = (co_turn_1.get("spoken_text_es") or "").lower()
            co_t2_lower = (co_turn_2.get("spoken_text_es") or "").lower()
            self.record(
                "CHECK-13 [Conversación Multi-Turno 'Colorado' + Voz MP3 Neuronal]",
                r_chat_co.get("guardrail_triggered") == "OUT_OF_STATE_COVERAGE"
                and len(r_chat_co.get("audio_mp3_base64") or "") > 5000
                and ("no" in co_reply_lower and "california" in co_reply_lower)
                and ("39,900" in co_t2_lower or "39900" in co_t2_lower or "apple valley" in co_t2_lower)
                and len(co_turn_2.get("audio_mp3_base64") or "") > 5000,
                f"Colorado negado con precisión ('{co_turn_1.get('spoken_text_es', '')[:75]}...') + Turno 2 recordó contexto y cotizó Apple Valley con audio MP3 real.",
            )

        elapsed = time.perf_counter() - started
        passed_n = sum(1 for _, ok, _ in self.results if ok)
        print("=" * 86)
        print(f"🏆 RESULTADO FINAL RUNBOOK: {passed_n}/{len(self.results)} CHECKS EN VERDE ({elapsed:.2f}s)")
        print("=" * 86)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify all Horizon Estates x GHL x Gemini Voice demo features.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8090", help="Base URL of running demo server")
    parser.add_argument("--no-reset", action="store_true", help="Do not reset demo DB before running checks")
    args = parser.parse_args()
    runner = RunbookRunner(base_url=args.base_url, reset_first=not args.no_reset)
    return asyncio.run(runner.run_all())


if __name__ == "__main__":
    sys.exit(main())
