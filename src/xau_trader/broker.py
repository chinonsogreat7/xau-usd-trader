"""Read-only broker boundary for the current paper-research milestone.

Order submission is intentionally absent. It will be added only with risk-decision binding,
practice-endpoint attestation, persistence, reconciliation, idempotency, and expiry controls.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Protocol, Tuple
from urllib.parse import urlparse

from .domain import AvailabilityBasis, QuoteBar


_PRACTICE_ENDPOINTS = {
    "OANDA": (
        "https://api-fxpractice.oanda.com",
        "https://stream-fxpractice.oanda.com",
    ),
}


def _practice_url(value: str, field_name: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.netloc or not parsed.hostname:
        raise ValueError("{} must be an absolute HTTPS URL".format(field_name))
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "{} must not contain credentials, a query, or a fragment".format(field_name)
        )
    return value.strip().rstrip("/")


@dataclass(frozen=True)
class PracticeAccountIdentity:
    """Identity returned after verification; its account ID is redacted from ``repr``."""

    broker_name: str
    account_id: str = field(repr=False)
    rest_base_url: str
    stream_base_url: str
    verified_at: datetime
    environment: str = field(default="practice", init=False)

    def __post_init__(self) -> None:
        broker_name = self.broker_name.strip()
        if not broker_name or not self.account_id.strip():
            raise ValueError("broker_name and account_id must not be empty")
        object.__setattr__(self, "broker_name", broker_name)
        object.__setattr__(
            self,
            "rest_base_url",
            _practice_url(self.rest_base_url, "rest_base_url"),
        )
        object.__setattr__(
            self,
            "stream_base_url",
            _practice_url(self.stream_base_url, "stream_base_url"),
        )
        registered = _PRACTICE_ENDPOINTS.get(self.broker_name)
        if registered is None:
            raise ValueError("broker has no registered practice endpoints")
        if (self.rest_base_url, self.stream_base_url) != registered:
            raise ValueError("URLs do not match the broker's registered practice endpoints")
        if self.verified_at.tzinfo is None or self.verified_at.utcoffset() is None:
            raise ValueError("verified_at must include a timezone")
        object.__setattr__(self, "verified_at", self.verified_at.astimezone(timezone.utc))


@dataclass(frozen=True)
class PracticeDataAttestation:
    """Short-lived binding of a verified practice identity to one data entitlement.

    ``provider_instrument_type`` is API metadata, not a legal product classification.
    """

    identity: PracticeAccountIdentity
    instrument: str
    timeframe: str
    provider_instrument_type: str
    verified_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        for name in ("instrument", "timeframe", "provider_instrument_type"):
            value = getattr(self, name).strip()
            if not value:
                raise ValueError("{} must not be empty".format(name))
            object.__setattr__(self, name, value)
        for name in ("verified_at", "expires_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("{} must include a timezone".format(name))
            object.__setattr__(self, name, value.astimezone(timezone.utc))
        if self.verified_at != self.identity.verified_at:
            raise ValueError("attestation and identity verification times must match")
        if self.expires_at <= self.verified_at:
            raise ValueError("expires_at must be later than verified_at")

    @property
    def fingerprint(self) -> str:
        payload = {
            "broker_name": self.identity.broker_name,
            "account_id": self.identity.account_id,
            "rest_base_url": self.identity.rest_base_url,
            "stream_base_url": self.identity.stream_base_url,
            "environment": self.identity.environment,
            "instrument": self.instrument,
            "timeframe": self.timeframe,
            "provider_instrument_type": self.provider_instrument_type,
            "verified_at": self.verified_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _digest(value: str, field_name: str) -> str:
    clean = value.strip().lower()
    if len(clean) != 64 or any(character not in "0123456789abcdef" for character in clean):
        raise ValueError("{} must be a SHA-256 digest".format(field_name))
    return clean


def _retrieved_at(value: datetime, attestation: PracticeDataAttestation) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("retrieved_at must include a timezone")
    normalized = value.astimezone(timezone.utc)
    if normalized < attestation.verified_at or normalized >= attestation.expires_at:
        raise ValueError("retrieved_at must fall within the attestation lifetime")
    return normalized


@dataclass(frozen=True)
class AttestedQuoteBar:
    """One observed bar with provider/account entitlement and acquisition provenance."""

    attestation: PracticeDataAttestation
    bar: QuoteBar
    retrieved_at: datetime
    availability_basis: AvailabilityBasis
    request_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "retrieved_at", _retrieved_at(self.retrieved_at, self.attestation))
        try:
            basis = AvailabilityBasis(self.availability_basis)
        except ValueError as exc:
            raise ValueError("availability_basis is not supported") from exc
        object.__setattr__(self, "availability_basis", basis)
        if self.bar.availability_basis != basis:
            raise ValueError("bar and envelope availability bases must match")
        if basis != AvailabilityBasis.OBSERVED_RECEIPT:
            raise ValueError("AttestedQuoteBar requires observed receipt provenance")
        if self.bar.available_at != self.retrieved_at:
            raise ValueError("observed bar availability must equal retrieved_at")
        object.__setattr__(
            self,
            "request_sha256",
            _digest(self.request_sha256, "request_sha256"),
        )


@dataclass(frozen=True)
class AttestedQuoteBatch:
    """Historical bars bound to one entitlement and one immutable request plan."""

    attestation: PracticeDataAttestation
    bars: Tuple[QuoteBar, ...]
    retrieved_at: datetime
    availability_basis: AvailabilityBasis
    request_sha256: str
    requested_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "retrieved_at", _retrieved_at(self.retrieved_at, self.attestation))
        bars = tuple(self.bars)
        if not bars:
            raise ValueError("bars must not be empty")
        object.__setattr__(self, "bars", bars)
        try:
            basis = AvailabilityBasis(self.availability_basis)
        except ValueError as exc:
            raise ValueError("availability_basis is not supported") from exc
        object.__setattr__(self, "availability_basis", basis)
        if basis != AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION:
            raise ValueError("AttestedQuoteBatch requires historical close-time provenance")
        if any(bar.availability_basis != basis for bar in bars):
            raise ValueError("batch contains mixed availability bases")
        if any(bar.available_at != bar.timestamp for bar in bars):
            raise ValueError("historical batch must use explicit close-time availability")
        if isinstance(self.requested_count, bool) or not isinstance(self.requested_count, int):
            raise ValueError("requested_count must be an integer")
        if self.requested_count < len(bars):
            raise ValueError("requested_count cannot be smaller than the returned bar count")
        object.__setattr__(
            self,
            "request_sha256",
            _digest(self.request_sha256, "request_sha256"),
        )


class PaperMarketDataAdapter(Protocol):
    """Read-only practice-market-data contract; it cannot express an order."""

    def preflight(self, instrument: str, timeframe: str) -> PracticeDataAttestation:
        ...

    def latest_completed_bar(self, attestation: PracticeDataAttestation) -> AttestedQuoteBar:
        ...
