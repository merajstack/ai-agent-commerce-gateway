from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, ValidationError
from typing import Optional
import sys

class Settings(BaseSettings):
    razorpay_key_id: str = Field(..., description="Razorpay test-mode key ID")
    razorpay_key_secret: str = Field(..., description="Razorpay test-mode key secret")
    # Optional secret that protects dashboard mutation endpoints.
    # If unset, mutations are BLOCKED (fail-closed). Set via DASHBOARD_SECRET env var.
    dashboard_secret: Optional[str] = Field(
        default=None,
        description="Secret token required for dashboard mutation endpoints (POST/PUT/DELETE). "
                    "Pass as X-Dashboard-Token header. Must be set to enable mutations."
    )

    # Allows loading from .env file
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

try:
    settings = Settings()
except ValidationError as e:
    # Fail fast and produce a readable error without printing secrets
    print("Configuration Error: Missing or invalid required environment variables.")
    for err in e.errors():
        print(f" - {err['loc'][0]}: {err['msg']}")
    sys.exit(1)
