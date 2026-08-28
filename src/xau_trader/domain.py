"""Core immutable types shared by research, evaluation, and execution layers."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum, IntEnum
import math
from typing import Any, Dict, Optional, Tuple


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("{} must include a timezone".format(field_name))
    return value.astimezone(timezone.utc)


def _required(value: str, field_name: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError("{} must not be empty".format(field_name))
    return clean


class RightsBasis(str, Enum):
    """Why the project is permitted to process a content source."""

    OWNED = "owned"
    LICENSED = "licensed"
    CREATOR_PROVIDED = "creator_provided"
    USER_SUPPLIED = "user_supplied"
    USER_NOTES = "user_notes"
    PUBLIC_API = "public_api"


class StrategyStatus(str, Enum):
    DRAFT = "draft"
    REVIEWED = "reviewed"
    FROZEN = "frozen"
    REJECTED = "rejected"


class TargetPosition(IntEnum):
    SHORT = -1
    FLAT = 0
    LONG = 1


class AvailabilityBasis(str, Enum):
    """How a completed bar's availability time was established."""

    SYNTHETIC = "synthetic"
    HISTORICAL_CLOSE_ASSUMPTION = "historical_close_assumption"
    OBSERVED_RECEIPT = "observed_receipt"


@dataclass(frozen=True)
class SourceCitation:
    start_seconds: float
    claim: str
    end_seconds: Optional[float] = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.start_seconds) or self.start_seconds < 0:
            raise ValueError("start_seconds must be a finite non-negative number")
        if self.end_seconds is not None:
            if not math.isfinite(self.end_seconds) or self.end_seconds < self.start_seconds:
                raise ValueError("end_seconds must be at or after start_seconds")
        object.__setattr__(self, "claim", _required(self.claim, "claim"))


@dataclass(frozen=True)
class ContentSource:
    """Provenance and rights metadata for one educational source."""

    source_id: str
    source_uri: str
    creator: str
    title: str
    published_at: datetime
    ingested_at: datetime
    rights_basis: RightsBasis
    content_sha256: Optional[str] = None

    def __post_init__(self) -> None:
        for name in ("source_id", "source_uri", "creator", "title"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        object.__setattr__(self, "published_at", _utc(self.published_at, "published_at"))
        object.__setattr__(self, "ingested_at", _utc(self.ingested_at, "ingested_at"))
        try:
            object.__setattr__(self, "rights_basis", RightsBasis(self.rights_basis))
        except ValueError as exc:
            raise ValueError("rights_basis is not supported") from exc
        if self.ingested_at < self.published_at:
            raise ValueError("ingested_at cannot be earlier than published_at")
        if self.content_sha256 is not None:
            digest = self.content_sha256.lower().strip()
            if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                raise ValueError("content_sha256 must be a 64-character hexadecimal digest")
            object.__setattr__(self, "content_sha256", digest)


@dataclass(frozen=True)
class ExtractedStrategyCandidate:
    """Legacy free-text research draft; it is never accepted by the closed compiler."""

    strategy_id: str
    version: int
    source_id: str
    instrument: str
    timeframe: str
    hypothesis: str
    entry_rules: Tuple[str, ...]
    exit_rules: Tuple[str, ...]
    risk_rules: Tuple[str, ...]
    citations: Tuple[SourceCitation, ...]
    filters: Tuple[str, ...] = field(default_factory=tuple)
    ambiguities: Tuple[str, ...] = field(default_factory=tuple)
    status: StrategyStatus = StrategyStatus.DRAFT
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        for name in ("strategy_id", "source_id", "instrument", "timeframe", "hypothesis"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if self.version < 1:
            raise ValueError("version must be at least 1")
        for name in ("entry_rules", "exit_rules", "risk_rules"):
            values = tuple(_required(value, name) for value in getattr(self, name))
            if not values:
                raise ValueError("{} must contain at least one rule".format(name))
            object.__setattr__(self, name, values)
        object.__setattr__(self, "filters", tuple(_required(v, "filters") for v in self.filters))
        object.__setattr__(
            self, "ambiguities", tuple(_required(v, "ambiguities") for v in self.ambiguities)
        )
        citations = tuple(self.citations)
        if not citations:
            raise ValueError("citations must contain at least one source timestamp")
        if any(not isinstance(citation, SourceCitation) for citation in citations):
            raise ValueError("citations must contain SourceCitation values")
        object.__setattr__(self, "citations", citations)
        try:
            object.__setattr__(self, "status", StrategyStatus(self.status))
        except ValueError as exc:
            raise ValueError("status is not supported") from exc
        if self.status == StrategyStatus.FROZEN and self.ambiguities:
            raise ValueError("a frozen strategy cannot contain unresolved ambiguities")
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))

    @property
    def key(self) -> str:
        return "{}:v{}".format(self.strategy_id, self.version)

    def as_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["created_at"] = self.created_at.isoformat()
        return payload


# Backward-compatible bootstrap name. New structured extraction must use StrategySpec v1 from
# strategy_schema and may not auto-convert this free-text candidate into executable rules.
StrategySpec = ExtractedStrategyCandidate


@dataclass(frozen=True)
class QuoteBar:
    """A half-open UTC bar ``[start_time, timestamp)`` with bid/ask prices.

    ``timestamp`` is the completed bar's end time. ``available_at`` records when the
    completed values became usable and can be later than the end time.
    """

    timestamp: datetime
    start_time: datetime
    availability_basis: AvailabilityBasis
    available_at: datetime
    bid_open: float
    bid_high: float
    bid_low: float
    bid_close: float
    ask_open: float
    ask_high: float
    ask_low: float
    ask_close: float
    volume: Optional[float] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _utc(self.timestamp, "timestamp"))
        object.__setattr__(self, "start_time", _utc(self.start_time, "start_time"))
        if self.start_time >= self.timestamp:
            raise ValueError("start_time must be earlier than the completed bar timestamp")
        try:
            object.__setattr__(
                self,
                "availability_basis",
                AvailabilityBasis(self.availability_basis),
            )
        except ValueError as exc:
            raise ValueError("availability_basis is not supported") from exc
        object.__setattr__(self, "available_at", _utc(self.available_at, "available_at"))
        if self.available_at < self.timestamp:
            raise ValueError("available_at cannot be earlier than the completed bar timestamp")
        price_fields = (
            "bid_open",
            "bid_high",
            "bid_low",
            "bid_close",
            "ask_open",
            "ask_high",
            "ask_low",
            "ask_close",
        )
        for name in price_fields:
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError("{} must be a finite positive number".format(name))
        if self.bid_high < max(self.bid_open, self.bid_close):
            raise ValueError("bid_high is below bid open/close")
        if self.bid_low > min(self.bid_open, self.bid_close):
            raise ValueError("bid_low is above bid open/close")
        if self.ask_high < max(self.ask_open, self.ask_close):
            raise ValueError("ask_high is below ask open/close")
        if self.ask_low > min(self.ask_open, self.ask_close):
            raise ValueError("ask_low is above ask open/close")
        for bid_name, ask_name in (
            ("bid_open", "ask_open"),
            ("bid_high", "ask_high"),
            ("bid_low", "ask_low"),
            ("bid_close", "ask_close"),
        ):
            if getattr(self, bid_name) > getattr(self, ask_name):
                raise ValueError("{} cannot exceed {}".format(bid_name, ask_name))
        if self.volume is not None and (not math.isfinite(self.volume) or self.volume < 0):
            raise ValueError("volume must be a finite non-negative number")

    @classmethod
    def from_mid(
        cls,
        timestamp: datetime,
        start_time: datetime,
        availability_basis: AvailabilityBasis,
        available_at: datetime,
        mid_open: float,
        mid_high: float,
        mid_low: float,
        mid_close: float,
        spread: float,
        volume: Optional[float] = None,
    ) -> "QuoteBar":
        if not math.isfinite(spread) or spread < 0:
            raise ValueError("spread must be a finite non-negative number")
        half = spread / 2.0
        return cls(
            timestamp=timestamp,
            start_time=start_time,
            availability_basis=availability_basis,
            bid_open=mid_open - half,
            bid_high=mid_high - half,
            bid_low=mid_low - half,
            bid_close=mid_close - half,
            ask_open=mid_open + half,
            ask_high=mid_high + half,
            ask_low=mid_low + half,
            ask_close=mid_close + half,
            volume=volume,
            available_at=available_at,
        )

    @property
    def mid_close(self) -> float:
        return (self.bid_close + self.ask_close) / 2.0

    @property
    def end_time(self) -> datetime:
        return self.timestamp

    @property
    def spread_bps_at_close(self) -> float:
        return (self.ask_close - self.bid_close) / self.mid_close * 10_000.0

    @property
    def spread_bps_at_open(self) -> float:
        mid_open = (self.bid_open + self.ask_open) / 2.0
        return (self.ask_open - self.bid_open) / mid_open * 10_000.0


@dataclass(frozen=True)
class Fill:
    timestamp: datetime
    previous_position: float
    new_position: float
    quantity: float
    price: float
    commission: float
    spread_cost: float
    slippage_cost: float
    reason: str


@dataclass(frozen=True)
class EquityPoint:
    timestamp: datetime
    equity: float
    position: float
