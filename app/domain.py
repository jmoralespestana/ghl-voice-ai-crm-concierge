from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal

LotStatus = Literal["AVAILABLE", "LAST_UNIT", "RESERVED", "SOLD"]
DocType = Literal["ITIN", "PASSPORT", "SSN", "CASH"]
HitlState = Literal["AI_AUTONOMOUS", "PENDING_HITL", "HUMAN_TAKEN_OVER"]
PipelineStage = Literal[
    "NEW_LEAD",
    "QUALIFIED_ITIN",
    "PENDING_HITL_APPROVAL",
    "SHOWING_BOOKED",
]

CA_TWO_PARTY_NOTICE_ES = (
    "Gracias por comunicarte con Horizon Estates. "
    "Para tu seguridad y en cumplimiento con la ley de California, esta conversación "
    "es grabada y atendida inicialmente por Sofía, nuestra asistente virtual."
)


@dataclass(frozen=True)
class LandLot:
    code: str
    title: str
    city: str
    county: str
    acres: str
    zoning: str
    cash_price_cents: int
    min_down_payment_cents: int
    annual_rate_bps: int
    utilities_summary: str
    gps_coordinates: str
    status: LotStatus
    badge_es: str

    def to_public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["cash_price_usd"] = self.cash_price_cents // 100
        data["min_down_payment_usd"] = self.min_down_payment_cents // 100
        data["annual_rate_pct"] = round(self.annual_rate_bps / 100, 2)
        return data


SEED_LOTS: tuple[LandLot, ...] = (
    LandLot(
        code="LOT-AV-01",
        title="Apple Valley — Vista del Sol",
        city="Apple Valley, CA",
        county="San Bernardino",
        acres="0.50 Acres (21,780 sq ft)",
        zoning="Residencial R-1 (Casa Unifamiliar / ADU)",
        cash_price_cents=3_990_000,
        min_down_payment_cents=350_000,
        annual_rate_bps=990,
        utilities_summary="Agua municipal y electricidad a pie de calle pavimentada",
        gps_coordinates="34.5008,-117.1859",
        status="AVAILABLE",
        badge_es="Disponible · Agua y Luz a Pie de Calle",
    ),
    LandLot(
        code="LOT-VV-02",
        title="Victorville — Corredor I-15 Norte",
        city="Victorville, CA",
        county="San Bernardino",
        acres="1.00 Acre (43,560 sq ft)",
        zoning="Residencial / Inversión Plusvalía",
        cash_price_cents=5_450_000,
        min_down_payment_cents=490_000,
        annual_rate_bps=990,
        utilities_summary="Electricidad frente al lote, a 6 min de Autopista I-15 y centros comerciales",
        gps_coordinates="34.5362,-117.2928",
        status="LAST_UNIT",
        badge_es="Último Lote en Esta Cuadra",
    ),
    LandLot(
        code="LOT-HE-03",
        title="Hesperia — Rancho Los Álamos",
        city="Hesperia, CA",
        county="San Bernardino",
        acres="2.50 Acres (108,900 sq ft)",
        zoning="Agrícola Residencial A-1 (Rancho, Caballos, Casa Manufacturada)",
        cash_price_cents=7_900_000,
        min_down_payment_cents=750_000,
        annual_rate_bps=1050,
        utilities_summary="Permite caballos, taller y casa móvil/manufacturada; electricidad en esquina",
        gps_coordinates="34.4264,-117.3009",
        status="AVAILABLE",
        badge_es="Disponible · Ideal Rancho Familiar",
    ),
    LandLot(
        code="LOT-PH-04",
        title="Phelan — Mirador San Gabriel",
        city="Phelan, CA",
        county="San Bernardino",
        acres="5.00 Acres (217,800 sq ft)",
        zoning="Rural Lote Grande RL-5",
        cash_price_cents=9_200_000,
        min_down_payment_cents=900_000,
        annual_rate_bps=1050,
        utilities_summary="5 acres planos con vista panorámica a las montañas de Wrightwood",
        gps_coordinates="34.4267,-117.5723",
        status="RESERVED",
        badge_es="Apartado · Bloqueado por Inventario",
    ),
)

LOTS_BY_CODE: dict[str, LandLot] = {lot.code: lot for lot in SEED_LOTS}
ALLOWED_TERMS_MONTHS: tuple[int, ...] = (36, 48, 60, 84)


@dataclass(frozen=True)
class FinancingQuote:
    lot_code: str
    lot_title: str
    lot_status: LotStatus
    available_for_sale: bool
    cash_price_cents: int
    down_payment_cents: int
    min_down_payment_cents: int
    financed_principal_cents: int
    term_months: int
    annual_rate_bps: int
    monthly_payment_cents: int
    requires_hitl_exception: bool
    doc_type: DocType
    disclaimer_es: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cash_price_usd"] = round(self.cash_price_cents / 100, 2)
        d["down_payment_usd"] = round(self.down_payment_cents / 100, 2)
        d["min_down_payment_usd"] = round(self.min_down_payment_cents / 100, 2)
        d["financed_principal_usd"] = round(self.financed_principal_cents / 100, 2)
        d["monthly_payment_usd"] = round(self.monthly_payment_cents / 100, 2)
        return d


def get_lot_or_none(lot_code: str) -> LandLot | None:
    cleaned = (lot_code or "").strip().upper()
    if cleaned in LOTS_BY_CODE:
        return LOTS_BY_CODE[cleaned]
    lowered = (lot_code or "").strip().lower()
    for lot in SEED_LOTS:
        if lot.city.split(",")[0].lower() in lowered or lot.code.lower() == lowered:
            return lot
    return None


def calculate_seller_financing(
    lot_code: str,
    down_payment_cents: int,
    term_months: int = 60,
    doc_type: DocType = "ITIN",
) -> FinancingQuote:
    lot = get_lot_or_none(lot_code)
    if lot is None:
        raise ValueError(f"Unknown lot_code: {lot_code}")

    safe_term = term_months if term_months in ALLOWED_TERMS_MONTHS else 60
    clamped_down = max(0, min(down_payment_cents, lot.cash_price_cents))
    available = lot.status in ("AVAILABLE", "LAST_UNIT")
    requires_hitl = clamped_down < lot.min_down_payment_cents
    principal_cents = max(0, lot.cash_price_cents - clamped_down)

    if principal_cents == 0 or doc_type == "CASH":
        monthly_cents = 0
    else:
        monthly_rate = (Decimal(lot.annual_rate_bps) / Decimal(10_000)) / Decimal(12)
        n = Decimal(safe_term)
        factor = (Decimal(1) + monthly_rate) ** n
        raw_payment = (Decimal(principal_cents) * monthly_rate * factor) / (factor - Decimal(1))
        monthly_cents = int(raw_payment.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    return FinancingQuote(
        lot_code=lot.code,
        lot_title=lot.title,
        lot_status=lot.status,
        available_for_sale=available,
        cash_price_cents=lot.cash_price_cents,
        down_payment_cents=clamped_down,
        min_down_payment_cents=lot.min_down_payment_cents,
        financed_principal_cents=principal_cents,
        term_months=safe_term,
        annual_rate_bps=lot.annual_rate_bps,
        monthly_payment_cents=monthly_cents,
        requires_hitl_exception=requires_hitl,
        doc_type=doc_type,
        disclaimer_es=(
            "Simulación informativa de financiamiento directo con el propietario (ITIN/Pasaporte/SSN). "
            "Cifras exactas sujetas a disponibilidad de inventario y contrato oficial en California."
        ),
    )


SEED_DEAD_LEADS: tuple[dict[str, Any], ...] = (
    {
        "id": "dl-2023-01",
        "full_name": "Prospecto Demo 01 (Apple Valley)",
        "phone": "+17605550141",
        "origin_channel": "Facebook Marketplace / Orgánico (May 2024)",
        "last_note": "Preguntó por medio acre en Apple Valley con ITIN; sin seguimiento previo.",
        "estimated_down_usd": 4500,
        "reactivation_outcome": "HOT_BOOKED",
        "ai_opening_es": "Hola, soy Sofía de Horizon Estates. Vi que el año pasado buscabas medio acre con ITIN en Apple Valley; acaba de liberarse Vista del Sol con agua y luz desde $3,500 de enganche. ¿Sigues buscando terreno o ya compraste?",
        "simulated_reply_es": "¡Hola! Justo tengo $4,500 ahorrados y no habíamos encontrado quién acepte ITIN sin banco. ¿Podemos verlo este sábado?",
    },
    {
        "id": "dl-2023-02",
        "full_name": "Prospecto Demo 02 (Hesperia)",
        "phone": "+17605550142",
        "origin_channel": "Instagram DM Orgánico (Nov 2023)",
        "last_note": "Pidió mensualidades para rancho en Hesperia; quedó en visto.",
        "estimated_down_usd": 8000,
        "reactivation_outcome": "HOT_BOOKED",
        "ai_opening_es": "Hola, te escribe Sofía de Horizon Estates. Nos consultaste por redes sobre terrenos grandes para rancho en Hesperia; tenemos 2.5 acres zonificación caballos (A-1) con financiamiento directo. ¿Te envío el cálculo de mensualidad?",
        "simulated_reply_es": "Sí por favor, tenemos $8,000 de enganche listos para este mes. Agéndame llamada con el asesor.",
    },
    {
        "id": "dl-2024-03",
        "full_name": "Prospecto Demo 03 (Victorville)",
        "phone": "+17605550143",
        "origin_channel": "Llamada Perdida Domingo (Abr 2024)",
        "last_note": "Llamó domingo fuera de horario; buzón sin respuesta.",
        "estimated_down_usd": 2800,
        "reactivation_outcome": "PENDING_HITL",
        "ai_opening_es": "Hola, soy Sofía de Horizon Estates. Vimos tu consulta anterior por lotes cerca de la I-15 en el High Desert. ¿Aún te interesa invertir en tierra con financiamiento directo?",
        "simulated_reply_es": "Me interesa el de Apple Valley pero hoy tengo $2,800 en efectivo de enganche, ¿me pueden hacer excepción si firmo el viernes?",
    },
    {
        "id": "dl-2024-04",
        "full_name": "Prospecto Demo 04 (Archivo)",
        "phone": "+17605550144",
        "origin_channel": "Facebook Messenger (Sep 2024)",
        "last_note": "Preguntó ubicación y servicios.",
        "estimated_down_usd": 0,
        "reactivation_outcome": "NURTURE",
        "ai_opening_es": "Hola, te saluda Sofía de Horizon Estates. ¿Sigues evaluando terrenos financiados para este trimestre?",
        "simulated_reply_es": "Gracias Sofía, ya resolvimos por ahora, quizá el próximo año para inversión.",
    },
    {
        "id": "dl-2025-05",
        "full_name": "Prospecto Demo 05 (Inversión)",
        "phone": "+17605550145",
        "origin_channel": "Comentario en Video Social (Feb 2025)",
        "last_note": "Comentó 'Info precio' en publicación de lotes.",
        "estimated_down_usd": 5000,
        "reactivation_outcome": "QUALIFIED_ITIN",
        "ai_opening_es": "Hola, nos pediste información en redes sobre terrenos en California. Con $5,000 de enganche calificas hoy mismo sin banco a Victorville (1 acre) o Apple Valley (0.5 acres). ¿Cuál zona prefieres?",
        "simulated_reply_es": "El de 1 acre en Victorville cerca de la 15 suena excelente, mándame cómo queda a 60 meses.",
    },
)
