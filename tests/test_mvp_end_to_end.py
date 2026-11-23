from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from fastapi.testclient import TestClient
import httpx
import pytest

from app.adapters.crm import DualWriteCRMOrchestrator
from app.adapters.voice import normalize_spanish_tts_text
from app.config import Settings, get_settings
from app.db import init_db
from app.domain import calculate_seller_financing
from app.main import app
from app.services.concierge import handle_buyer_chat_turn, resolve_hitl_action


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_file = tmp_path / "test_horizon.db"
    monkeypatch.setenv("SQLITE_DB_PATH", str(db_file))
    monkeypatch.setenv("VOICE_PROVIDER", "gemini_live")
    monkeypatch.setenv("CRM_PROVIDER", "dual_ghl")
    monkeypatch.setenv("GHL_ACCESS_TOKEN", "")
    monkeypatch.setenv("GHL_LOCATION_ID", "")
    monkeypatch.setenv("VERTEX_USE_ADC", "false")
    init_db(str(db_file))
    yield db_file


def test_spanish_tts_number_and_currency_normalization() -> None:
    sample = (
        "Enganche desde $7,500 USD, contado $39,900 USD y cuota de $771.60 USD/mes "
        "por 2.50 Acres bajo CA § 632."
    )
    out = normalize_spanish_tts_text(sample)
    assert "siete mil quinientos dólares" in out
    assert "treinta y nueve mil novecientos dólares" in out
    assert "setecientos setenta y un dólares con sesenta centavos al mes" in out
    assert "dos acres y medio" in out
    assert "sección 632" in out
    assert "$7,500" not in out


def test_executive_pin_gate(isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_ACCESS_PIN", "2026")
    with TestClient(app) as client:
        st = client.get("/api/v1/auth/pin-status").json()
        assert st["pin_required"] is True
        bad = client.post("/api/v1/auth/verify-pin", json={"pin": "0000"})
        assert bad.status_code == 401
        ok = client.post("/api/v1/auth/verify-pin", json={"pin": "2026"})
        assert ok.status_code == 200
        assert ok.json()["unlocked"] is True


def test_exact_bigint_cents_financing_and_reserved_lot() -> None:
    quote_ok = calculate_seller_financing("LOT-AV-01", 350_000, term_months=60, doc_type="ITIN")
    assert quote_ok.available_for_sale is True
    assert quote_ok.requires_hitl_exception is False
    assert quote_ok.financed_principal_cents == 3_640_000
    assert quote_ok.monthly_payment_cents == 77_160  # Exact integer cents ($771.60/mo)

    quote_low = calculate_seller_financing("LOT-AV-01", 250_000, term_months=60, doc_type="ITIN")
    assert quote_low.requires_hitl_exception is True

    quote_reserved = calculate_seller_financing("LOT-PH-04", 1_000_000, term_months=60)
    assert quote_reserved.lot_status == "RESERVED"
    assert quote_reserved.available_for_sale is False


def test_guardrails_reserved_lot_and_hitl_bargaining_flow(isolated_env: Path) -> None:
    with TestClient(app) as client:
        # 1. Reserved lot Phelan is deterministically blocked
        r_res = client.post(
            "/api/v1/chat",
            json={
                "full_name": "Prospecto Test A",
                "phone": "+17605550121",
                "message": "Quiero apartar el lote de 5 acres en Phelan hoy mismo.",
                "selected_lot_code": "LOT-PH-04",
            },
        )
        assert r_res.status_code == 200
        body_res = r_res.json()
        assert body_res["guardrail_triggered"] == "INVENTORY_RESERVED_BLOCK"
        assert "Apartado" in body_res["reply"]

        # 1B. Out-of-state inquiry (e.g., Colorado) is answered honestly (California only)
        r_oos = client.post(
            "/api/v1/chat",
            json={
                "full_name": "Prospecto Test A",
                "phone": "+17605550121",
                "message": "¿Ustedes tienen terrenos o servicios en Colorado?",
            },
        )
        assert r_oos.status_code == 200
        body_oos = r_oos.json()
        assert body_oos["guardrail_triggered"] == "OUT_OF_STATE_COVERAGE"
        assert "no tenemos terrenos ni servicios en Colorado" in body_oos["reply"]
        assert "California" in body_oos["reply"]

        # 2. Bargaining below $3,500 minimum on Apple Valley triggers PENDING_HITL
        r_hitl = client.post(
            "/api/v1/chat",
            json={
                "full_name": "Prospecto Test B",
                "phone": "+17605550122",
                "message": "Solo tengo $2,500 dólares para el terreno de Apple Valley, ¿me hacen excepción?",
                "selected_lot_code": "LOT-AV-01",
            },
        )
        assert r_hitl.status_code == 200
        body_hitl = r_hitl.json()
        assert body_hitl["hitl_state"] == "PENDING_HITL"
        assert body_hitl["guardrail_triggered"] == "HITL_EXCEPTION_ESCALATION"
        cid = body_hitl["contact_id"]

        # 3. Director approves exception -> advances to SHOWING_BOOKED
        r_approve = client.post(
            "/api/v1/hitl/action",
            json={"contact_id": cid, "action": "APPROVE_EXCEPTION"},
        )
        assert r_approve.status_code == 200
        assert r_approve.json()["contact"]["pipeline_stage"] == "SHOWING_BOOKED"

        # 4. Human salesperson takes over -> AI MUST stay 100% silent
        r_take = client.post(
            "/api/v1/hitl/action",
            json={"contact_id": cid, "action": "TAKE_OVER"},
        )
        assert r_take.status_code == 200
        assert r_take.json()["contact"]["hitl_state"] == "HUMAN_TAKEN_OVER"

        r_silenced = client.post(
            "/api/v1/chat",
            json={
                "full_name": "Prospecto Test B",
                "phone": "+17605550122",
                "message": "Ya voy saliendo por la autopista 15 hacia su oficina.",
            },
        )
        assert r_silenced.status_code == 200
        assert r_silenced.json()["bot_silenced"] is True
        assert r_silenced.json()["reply"] is None

        # 5. Release back to AI -> AI answers again
        client.post("/api/v1/hitl/action", json={"contact_id": cid, "action": "RELEASE_TO_AI"})
        r_resumed = client.post(
            "/api/v1/chat",
            json={
                "full_name": "Prospecto Test B",
                "phone": "+17605550122",
                "message": "¿Tienen agua y luz en Apple Valley?",
            },
        )
        assert r_resumed.json()["bot_silenced"] is False
        assert r_resumed.json()["reply"] is not None


async def test_live_gohighlevel_v2_adapter_with_mock_transport(isolated_env: Path) -> None:
    captured_requests: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer mock-ghl-v2-bearer-token"
        assert request.headers["Version"] == "2021-07-28"
        payload = json.loads(request.content.decode("utf-8")) if request.content else {}
        captured_requests.append((request.method, request.url.path, payload))
        if request.url.path == "/contacts/upsert":
            return httpx.Response(200, json={"contact": {"id": "ghl_real_cnt_98765"}})
        if request.url.path.endswith("/notes"):
            return httpx.Response(201, json={"note": {"id": "note_001"}})
        if request.url.path == "/opportunities/upsert":
            return httpx.Response(200, json={"opportunity": {"id": "ghl_real_opp_54321"}})
        return httpx.Response(200, json={"ok": True})

    cfg = Settings(
        SQLITE_DB_PATH=str(isolated_env),
        CRM_PROVIDER="dual_ghl",
        GHL_ACCESS_TOKEN="mock-ghl-v2-bearer-token",
        GHL_LOCATION_ID="loc_apple_valley_ca",
        GHL_PIPELINE_ID="pipe_land_sales",
        GHL_STAGE_QUALIFIED_ID="stage_itin_qualified",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_http:
        res = await handle_buyer_chat_turn(
            full_name="Prospecto Test C",
            phone="+17605550128",
            message="Hola, tengo ITIN y $4,000 de enganche para Apple Valley.",
            settings=cfg,
            db_path=str(isolated_env),
            ghl_http_client=mock_http,
        )
        ghl_sync = res["crm_sync"][1]
        assert ghl_sync["mode"] == "LIVE_PRIVATE_INTEGRATION_TOKEN"
        assert ghl_sync["contact_external_id"] == "ghl_real_cnt_98765"
        assert ghl_sync["opportunity_external_id"] == "ghl_real_opp_54321"

        # Verify human takeover adds `human-takeover` tag in GHL payload
        await resolve_hitl_action(
            contact_id=res["contact_id"],
            action="TAKE_OVER",
            settings=cfg,
            db_path=str(isolated_env),
            ghl_http_client=mock_http,
        )

    paths = [p for _, p, _ in captured_requests]
    assert "/contacts/upsert" in paths
    assert "/contacts/ghl_real_cnt_98765/notes" in paths
    assert "/opportunities/upsert" in paths
    last_upsert = [b for _, p, b in captured_requests if p == "/contacts/upsert"][-1]
    assert "human-takeover" in last_upsert["tags"]


def test_dual_voice_strategy_gemini_live_ws_and_retell_hmac(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RETELL_WEBHOOK_SECRET", "retell-super-secret")

    with TestClient(app) as client:
        # Strategy 1: Gemini Live WebSocket (ZERO phone number exposed + CA § 632 at t=0)
        sess = client.post(
            "/api/v1/voice/session",
            json={"provider": "gemini_live", "buyer_name": "Prospecto Test Voz", "buyer_phone": "+17605550133"},
        ).json()
        assert sess["provider"] == "gemini_live"
        assert sess["exposes_phone_number"] is False
        assert sess["public_phone_number"] is None

        with client.websocket_connect(
            "/api/v1/voice/gemini-live/ws?buyer_name=Prospecto+Test+Voz&buyer_phone=%2B17605550133&lot_code=LOT-AV-01"
        ) as ws:
            greeting = ws.receive_json()
            assert greeting["type"] == "ca_632_greeting"
            assert greeting["exposes_phone_number"] is False
            assert "California" in greeting["spoken_text_es"]

            ws.send_json({"type": "user_utterance", "text": "¿Qué precio tiene el terreno de Apple Valley?"})
            turn = ws.receive_json()
            assert turn["type"] == "assistant_utterance"
            assert "39,900" in turn["spoken_text_es"]

            ws.send_json({"type": "end_call", "duration_seconds": 41})
            done = ws.receive_json()
            assert done["type"] == "call_synced"
            assert done["result"]["ca_632_disclosure_played"] is True

        # Strategy 2: Retell AI Webhook HMAC-SHA256 verification
        retell_payload = json.dumps(
            {
                "event": "call_analyzed",
                "call": {
                    "call_id": "retell_call_777",
                    "from_number": "+17605550134",
                    "duration_ms": 52000,
                    "transcript": "Sofía: Gracias por llamar... Cliente: Tengo ITIN y $5,000.",
                    "call_analysis": {"call_summary": "Cliente calificado con ITIN y $5,000 para Victorville."},
                    "retell_llm_dynamic_variables": {"buyer_name": "Prospecto Retell Test", "lot_code": "LOT-VV-02"},
                },
            }
        ).encode("utf-8")

        bad_resp = client.post(
            "/api/v1/voice/retell/webhook",
            content=retell_payload,
            headers={"Content-Type": "application/json", "X-Retell-Signature": "forged"},
        )
        assert bad_resp.status_code == 401

        good_sig = hmac.new(b"retell-super-secret", retell_payload, hashlib.sha256).hexdigest()
        ok_resp = client.post(
            "/api/v1/voice/retell/webhook",
            content=retell_payload,
            headers={"Content-Type": "application/json", "X-Retell-Signature": good_sig},
        )
        assert ok_resp.status_code == 200
        assert ok_resp.json()["result"]["provider"] == "retell"


def test_ghl_native_human_outbound_webhook_locks_bot_and_dead_leads_reactivate(
    isolated_env: Path,
) -> None:
    with TestClient(app) as client:
        # Reactivate 2022-2025 dead leads (4 actionable out of 5)
        r_react = client.post("/api/v1/reactivation/run")
        assert r_react.status_code == 200
        data = r_react.json()
        assert data["total_scanned"] == 5
        assert data["reactivated_opportunities"] == 4

        # Simulate a human sales rep replying directly inside GoHighLevel UI
        snap = client.get("/api/v1/cockpit/snapshot").json()
        first_contact = snap["contacts"][0]
        r_wh = client.post(
            "/api/v1/webhooks/ghl/inbound",
            json={
                "type": "OutboundMessage",
                "userId": "ghl_user_closer_01",
                "phone": first_contact["phone"],
                "body": "Hola, te saluda Dirección Comercial desde la oficina de Apple Valley, te marco ahora.",
            },
        )
        assert r_wh.status_code == 200
        assert r_wh.json()["status"] == "human_takeover_locked"
