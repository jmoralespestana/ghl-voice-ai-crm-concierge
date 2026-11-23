from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_ENV: str = "development"
    APP_PORT: int = 8090
    SQLITE_DB_PATH: str = "./data/horizon_demo.db"
    DEMO_ACCESS_PIN: str = ""

    VOICE_PROVIDER: Literal["gemini_live", "retell"] = "gemini_live"
    CRM_PROVIDER: Literal["dual_ghl", "local_only"] = "dual_ghl"
    CALIFORNIA_TWO_PARTY_CONSENT: bool = True

    GEMINI_API_KEY: str = ""
    VERTEX_USE_ADC: bool = True
    VERTEX_PROJECT_ID: str = ""
    VERTEX_LOCATION: str = "us-central1"
    GEMINI_MODEL: str = "gemini-2.5-flash"
    GEMINI_LIVE_MODEL: str = "gemini-2.0-flash-live-preview-04-09"

    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL: str = "openai/gpt-4o-mini"

    GHL_BASE_URL: str = "https://services.leadconnectorhq.com"
    GHL_API_VERSION: str = "2021-07-28"
    GHL_ACCESS_TOKEN: str = ""
    GHL_LOCATION_ID: str = ""
    GHL_PIPELINE_ID: str = ""
    GHL_STAGE_NEW_ID: str = ""
    GHL_STAGE_QUALIFIED_ID: str = ""
    GHL_STAGE_HITL_ID: str = ""
    GHL_STAGE_BOOKED_ID: str = ""
    GHL_CLIENT_ID: str = ""
    GHL_CLIENT_SECRET: str = ""
    GHL_REDIRECT_URI: str = "http://localhost:8090/api/v1/crm/oauth/callback"

    RETELL_API_KEY: str = ""
    RETELL_AGENT_ID: str = ""
    RETELL_WEBHOOK_SECRET: str = ""
    RETELL_PUBLIC_PHONE: str = ""

    @property
    def ghl_live_configured(self) -> bool:
        return bool(self.GHL_ACCESS_TOKEN.strip() and self.GHL_LOCATION_ID.strip())

    @property
    def gemini_configured(self) -> bool:
        return bool(self.GEMINI_API_KEY.strip() or (self.VERTEX_USE_ADC and self.VERTEX_PROJECT_ID.strip()))

    def build_genai_client(self):
        from google import genai

        if self.GEMINI_API_KEY.strip():
            return genai.Client(api_key=self.GEMINI_API_KEY.strip())
        return genai.Client(
            vertexai=True,
            project=self.VERTEX_PROJECT_ID.strip(),
            location=self.VERTEX_LOCATION.strip(),
        )

    @property
    def retell_configured(self) -> bool:
        return bool(self.RETELL_API_KEY.strip() and self.RETELL_AGENT_ID.strip())


def get_settings() -> Settings:
    return Settings()
