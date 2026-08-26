"""
Protocol Adapter Base Interface — Agent Commerce Gateway
========================================================

All protocol adapters (ACP, x402, AP2, UAP) must implement this interface.

The two-method contract:

    parse_request(payload)  →  CommerceRequest
        Converts a raw, protocol-specific payload dict into the canonical
        CommerceRequest.  The resulting object must pass Pydantic validation
        before being returned.  Partial or unvalidated objects must never
        be returned.

    build_receipt(receipt)  →  dict
        Converts a canonical CommerceReceipt into a protocol-specific
        response dict that can be sent back to the originating client.

This interface is intentionally minimal.  Protocol-specific concepts
(headers, HTTP status codes, field names, etc.) belong entirely inside
the adapter, never in the canonical models.
"""

from abc import ABC, abstractmethod

from app.core.schemas import CommerceReceipt, CommerceRequest


class ProtocolAdapter(ABC):
    """
    Abstract base class for all protocol adapters.

    Each supported protocol (ACP, x402, AP2, UAP) will have a concrete
    subclass that implements both methods.

    Adapters are the ONLY place where protocol-specific concepts exist.
    Once parse_request() returns, the rest of the gateway works entirely
    on canonical models.
    """

    @abstractmethod
    def parse_request(self, payload: dict) -> CommerceRequest:
        """
        Parse a raw protocol-specific payload into a CommerceRequest.

        Contract:
          - Must raise ValueError for semantically malformed payloads
            before Pydantic validation (e.g., missing required protocol fields).
          - Pydantic ValidationError propagates naturally if the resulting
            CommerceRequest is structurally invalid.
          - Must never return a partially constructed or unvalidated object.
          - Must not perform authorization checks — that is the mandate layer.

        Args:
            payload: Raw protocol payload as a dict.  Structure is
                     protocol-specific.

        Returns:
            A fully validated CommerceRequest.

        Raises:
            ValueError: If the payload is malformed at the protocol level.
            pydantic.ValidationError: If the mapped CommerceRequest is invalid.
            NotImplementedError: Until the protocol is implemented.
        """
        ...

    @abstractmethod
    def build_receipt(self, receipt: CommerceReceipt) -> dict:
        """
        Translate a canonical CommerceReceipt into a protocol-specific response.

        Contract:
          - Must not raise on a valid CommerceReceipt.
          - The returned dict structure is entirely protocol-specific.
          - Must not modify the receipt object.

        Args:
            receipt: A valid, fully populated CommerceReceipt.

        Returns:
            Protocol-specific response payload as a dict.

        Raises:
            NotImplementedError: Until the protocol is implemented.
        """
        ...
