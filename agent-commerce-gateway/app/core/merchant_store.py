"""
Merchant State Management — Agent Commerce Gateway
==================================================

Provides an abstract interface and an in-memory implementation for storing
merchant configurations, API keys, policies, and Razorpay credentials.

Crucially, this separates public configuration (MerchantConfig) from private
secrets (MerchantSecrets) to ensure secrets are never serialized, logged,
or returned to the frontend.
"""

import hashlib
from abc import ABC, abstractmethod
from typing import List, Optional

from pydantic import BaseModel


class MerchantConfig(BaseModel):
    """
    Public merchant configuration. Safe to serialize and return to the dashboard.
    Does NOT contain API keys or Razorpay key_secret.
    """
    merchant_id: str
    merchant_name: str
    razorpay_key_id: Optional[str] = None
    max_transaction_amount: Optional[int] = None
    allowed_currency: Optional[str] = None
    blocked_categories: Optional[List[str]] = None


class MerchantSecrets(BaseModel):
    """
    Private merchant secrets. STRICTLY BACKEND ONLY.
    Never return this object in an API response or log it.
    """
    merchant_id: str
    razorpay_key_secret: Optional[str] = None
    api_key_hash: Optional[str] = None


class MerchantStore(ABC):
    """Abstract interface for merchant state storage."""
    
    @abstractmethod
    def get_merchant(self, merchant_id: str) -> Optional[MerchantConfig]:
        """Retrieve public merchant configuration."""
        pass

    @abstractmethod
    def get_merchant_secrets(self, merchant_id: str) -> Optional[MerchantSecrets]:
        """Retrieve private merchant secrets."""
        pass

    @abstractmethod
    def save_merchant(self, config: MerchantConfig, secrets: MerchantSecrets) -> None:
        """Save both public configuration and private secrets."""
        pass

    @abstractmethod
    def get_merchant_id_by_api_key_hash(self, api_key_hash: str) -> Optional[str]:
        """Look up a merchant ID by their API key hash for authentication."""
        pass


class InMemoryMerchantStore(MerchantStore):
    """
    In-memory implementation for the demo phase.
    Provides strict isolation per merchant ID.
    """
    def __init__(self):
        self._configs: dict[str, MerchantConfig] = {}
        self._secrets: dict[str, MerchantSecrets] = {}

    def get_merchant(self, merchant_id: str) -> Optional[MerchantConfig]:
        return self._configs.get(merchant_id)

    def get_merchant_secrets(self, merchant_id: str) -> Optional[MerchantSecrets]:
        return self._secrets.get(merchant_id)

    def save_merchant(self, config: MerchantConfig, secrets: MerchantSecrets) -> None:
        if config.merchant_id != secrets.merchant_id:
            raise ValueError("Config and Secrets must have the same merchant_id")
        
        self._configs[config.merchant_id] = config
        self._secrets[secrets.merchant_id] = secrets

    def get_merchant_id_by_api_key_hash(self, api_key_hash: str) -> Optional[str]:
        for merchant_id, secrets in self._secrets.items():
            if secrets.api_key_hash == api_key_hash:
                return merchant_id
        return None

# Global instance for the runtime
merchant_store = InMemoryMerchantStore()

# Seed a default merchant for the demo
default_merchant_id = "merchant-demo-001"
default_api_key = "sk_test_f22ff116facae2ec5d6a6266cb366dae0e93d85674311019"
default_api_key_hash = hashlib.sha256(default_api_key.encode("utf-8")).hexdigest()

try:
    from app.config import settings
    default_rzp_key_id = settings.razorpay_key_id or "rzp_test_TSuG9gfvyjCsK2"
    default_rzp_key_secret = settings.razorpay_key_secret or "mock_secret_for_demo_purposes"
except Exception:
    default_rzp_key_id = "rzp_test_TSuG9gfvyjCsK2"
    default_rzp_key_secret = "mock_secret_for_demo_purposes"

merchant_store.save_merchant(
    MerchantConfig(
        merchant_id=default_merchant_id,
        merchant_name="Demo Merchant",
        razorpay_key_id=default_rzp_key_id,
        max_transaction_amount=1000000, # 10,000.00 max limit (₹10,000)
        allowed_currency="INR",
        blocked_categories=[]
    ),
    MerchantSecrets(
        merchant_id=default_merchant_id,
        razorpay_key_secret=default_rzp_key_secret,
        api_key_hash=default_api_key_hash
    )
)
