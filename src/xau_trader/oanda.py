"""Offline-tested OANDA v20 practice request and candle-normalization boundary.

No credential loader or network transport is shipped while provider eligibility and data
rights remain unresolved. Tests inject a fake transport. A future credentialed transport
must first satisfy the provider gate recorded in ADR 0001.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
from typing import Any, Callable, List, Mapping, Optional, Protocol, Sequence, Tuple
from urllib.parse import quote

from .broker import (
    AttestedQuoteBar,
    AttestedQuoteBatch,
    PracticeAccountIdentity,
    PracticeDataAttestation,
)
from .domain import AvailabilityBasis, QuoteBar


OANDA_PRACTICE_REST_URL = "https://api-fxpractice.oanda.com"
OANDA_PRACTICE_STREAM_URL = "https://stream-fxpractice.oanda.com"
OANDA_INSTRUMENT = "XAU_USD"
INTERNAL_TIMEFRAME = "PT1H"
OANDA_GRANULARITY = "H1"
MAX_CANDLES_PER_REQUEST = 5_000
DEFAULT_ATTESTATION_TTL = timedelta(minutes=5)
MAX_ATTESTATION_TTL = timedelta(minutes=5)
MAX_OBSERVED_BAR_AGE = timedelta(hours=2)

_RFC3339 = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?P<fraction>\.\d+)?(?P<offset>Z|[+-]\d{2}:\d{2})$"
)


class OandaPracticeError(ValueError):
    """A sanitized request, preflight, or response validation failure."""


class OandaReadOperation(str, Enum):
    ACCOUNT_SUMMARY = "account_summary"
    INSTRUMENTS = "instruments"
    CANDLES = "candles"


@dataclass(frozen=True)
class OandaPracticeReadRequest:
    """An immutable request plan that cannot represent a mutation or live origin."""

    operation: OandaReadOperation
    account_id: str = field(repr=False)
    count: Optional[int] = None
    method: str = field(default="GET", init=False)
    base_url: str = field(default=OANDA_PRACTICE_REST_URL, init=False)

    def __post_init__(self) -> None:
        account_id = self.account_id.strip()
        if not account_id or any(character.isspace() for character in account_id):
            raise OandaPracticeError("the OANDA practice account ID is invalid")
        object.__setattr__(self, "account_id", account_id)
        try:
            operation = OandaReadOperation(self.operation)
        except ValueError as exc:
            raise OandaPracticeError("OANDA read operation is not allowlisted") from exc
        object.__setattr__(self, "operation", operation)
        if operation == OandaReadOperation.CANDLES:
            _validate_count(self.count)
        elif self.count is not None:
            raise OandaPracticeError("count is supported only for the candle request")

    @property
    def path(self) -> str:
        account = quote(self.account_id, safe="")
        if self.operation == OandaReadOperation.ACCOUNT_SUMMARY:
            return "/v3/accounts/{}/summary".format(account)
        if self.operation == OandaReadOperation.INSTRUMENTS:
            return "/v3/accounts/{}/instruments".format(account)
        return "/v3/accounts/{}/instruments/{}/candles".format(
            account,
            OANDA_INSTRUMENT,
        )

    @property
    def query(self) -> Mapping[str, str]:
        if self.operation == OandaReadOperation.ACCOUNT_SUMMARY:
            return {}
        if self.operation == OandaReadOperation.INSTRUMENTS:
            return {"instruments": OANDA_INSTRUMENT}
        assert self.count is not None
        return {
            "count": str(self.count),
            "granularity": OANDA_GRANULARITY,
            "price": "BA",
            "smooth": "false",
            "units": "1",
        }

    @property
    def fingerprint(self) -> str:
        payload = {
            "method": self.method,
            "base_url": self.base_url,
            "path": self.path,
            "query": dict(self.query),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class _JsonGetTransport(Protocol):
    """Test seam; the repository intentionally provides no credentialed implementation."""

    def get_json(self, request: OandaPracticeReadRequest) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class OandaPracticeConfig:
    """Non-secret account binding for the offline-tested adapter boundary."""

    account_id: str = field(repr=False)

    def __post_init__(self) -> None:
        account_id = self.account_id.strip()
        if not account_id or any(character.isspace() for character in account_id):
            raise OandaPracticeError("the OANDA practice account ID is invalid")
        object.__setattr__(self, "account_id", account_id)


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise OandaPracticeError("OANDA response field {} must be an object".format(field_name))
    return value


def _sequence(value: Any, field_name: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise OandaPracticeError("OANDA response field {} must be an array".format(field_name))
    return value


def _parse_rfc3339(value: Any) -> datetime:
    if not isinstance(value, str):
        raise OandaPracticeError("OANDA candle time must be an RFC3339 string")
    match = _RFC3339.fullmatch(value.strip())
    if match is None:
        raise OandaPracticeError("OANDA candle time is not valid RFC3339")
    fraction = match.group("fraction") or ""
    if fraction:
        digits = fraction[1:]
        if len(digits) > 6 and any(digit != "0" for digit in digits[6:]):
            raise OandaPracticeError(
                "OANDA candle time has unsupported sub-microsecond precision"
            )
        fraction = fraction[:7]
    offset = "+00:00" if match.group("offset") == "Z" else match.group("offset")
    try:
        parsed = datetime.fromisoformat(match.group("base") + fraction + offset)
    except ValueError as exc:
        raise OandaPracticeError("OANDA candle time is not a real timestamp") from exc
    return parsed.astimezone(timezone.utc)


def _price(component: Mapping[str, Any], key: str, field_name: str) -> float:
    raw = component.get(key)
    if not isinstance(raw, str):
        raise OandaPracticeError(
            "OANDA response field {} must be a price string".format(field_name)
        )
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise OandaPracticeError(
            "OANDA response field {} is not numeric".format(field_name)
        ) from exc
    if not value.is_finite() or value <= 0:
        raise OandaPracticeError("OANDA response field {} must be positive".format(field_name))
    return float(value)


def _validate_count(count: Optional[int]) -> None:
    if isinstance(count, bool) or not isinstance(count, int):
        raise OandaPracticeError("count must be an integer")
    if count < 1 or count > MAX_CANDLES_PER_REQUEST:
        raise OandaPracticeError(
            "count must be between 1 and {}".format(MAX_CANDLES_PER_REQUEST)
        )


def _validate_market(instrument: str, timeframe: str) -> None:
    if instrument != OANDA_INSTRUMENT:
        raise OandaPracticeError("only the canonical instrument XAU_USD is enabled")
    if timeframe != INTERNAL_TIMEFRAME:
        raise OandaPracticeError("only the canonical timeframe PT1H is enabled")


class OandaPracticeMarketDataAdapter:
    """Attested OANDA schema adapter, currently usable only with an injected fake transport."""

    def __init__(
        self,
        config: OandaPracticeConfig,
        transport: _JsonGetTransport,
        clock: Optional[Callable[[], datetime]] = None,
        attestation_ttl: timedelta = DEFAULT_ATTESTATION_TTL,
        max_observed_bar_age: timedelta = MAX_OBSERVED_BAR_AGE,
    ) -> None:
        if (
            not isinstance(attestation_ttl, timedelta)
            or attestation_ttl <= timedelta(0)
            or attestation_ttl > MAX_ATTESTATION_TTL
        ):
            raise OandaPracticeError("attestation_ttl must be positive and at most five minutes")
        if (
            not isinstance(max_observed_bar_age, timedelta)
            or max_observed_bar_age <= timedelta(0)
            or max_observed_bar_age > MAX_OBSERVED_BAR_AGE
        ):
            raise OandaPracticeError(
                "max_observed_bar_age must be positive and at most two hours"
            )
        self._config = config
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._attestation_ttl = attestation_ttl
        self._max_observed_bar_age = max_observed_bar_age
        self._active_attestation: Optional[PracticeDataAttestation] = None
        self._last_observed_at: Optional[datetime] = None

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise OandaPracticeError("adapter clock must return a timezone-aware datetime")
        normalized = value.astimezone(timezone.utc)
        if self._last_observed_at is not None and normalized < self._last_observed_at:
            self._active_attestation = None
            raise OandaPracticeError("the adapter clock moved backwards")
        self._last_observed_at = normalized
        return normalized

    def preflight(
        self,
        instrument: str = OANDA_INSTRUMENT,
        timeframe: str = INTERNAL_TIMEFRAME,
    ) -> PracticeDataAttestation:
        self._active_attestation = None
        _validate_market(instrument, timeframe)
        self._now()
        account_payload = _mapping(
            self._transport.get_json(
                OandaPracticeReadRequest(
                    OandaReadOperation.ACCOUNT_SUMMARY,
                    self._config.account_id,
                )
            ),
            "response",
        )
        account = _mapping(account_payload.get("account"), "account")
        if account.get("id") != self._config.account_id:
            raise OandaPracticeError("OANDA practice account identity did not match configuration")

        instrument_payload = _mapping(
            self._transport.get_json(
                OandaPracticeReadRequest(
                    OandaReadOperation.INSTRUMENTS,
                    self._config.account_id,
                )
            ),
            "response",
        )
        instruments = _sequence(instrument_payload.get("instruments"), "instruments")
        matches: List[Mapping[str, Any]] = []
        for index, item in enumerate(instruments):
            details = _mapping(item, "instruments[{}]".format(index))
            if details.get("name") == instrument:
                matches.append(details)
        if len(matches) != 1:
            raise OandaPracticeError(
                "XAU_USD entitlement was missing or duplicated for this practice account"
            )
        provider_type = matches[0].get("type")
        if not isinstance(provider_type, str) or not provider_type.strip():
            raise OandaPracticeError("XAU_USD provider instrument type was missing")

        verified_at = self._now()
        identity = PracticeAccountIdentity(
            broker_name="OANDA",
            account_id=self._config.account_id,
            rest_base_url=OANDA_PRACTICE_REST_URL,
            stream_base_url=OANDA_PRACTICE_STREAM_URL,
            verified_at=verified_at,
        )
        attestation = PracticeDataAttestation(
            identity=identity,
            instrument=instrument,
            timeframe=timeframe,
            provider_instrument_type=provider_type,
            verified_at=verified_at,
            expires_at=verified_at + self._attestation_ttl,
        )
        self._active_attestation = attestation
        return attestation

    def _require_attestation(
        self,
        attestation: PracticeDataAttestation,
        observed_at: datetime,
    ) -> None:
        if self._active_attestation is None or attestation is not self._active_attestation:
            raise OandaPracticeError("a current preflight attestation is required")
        if observed_at >= attestation.expires_at:
            self._active_attestation = None
            raise OandaPracticeError("the practice-data attestation has expired")
        if observed_at < attestation.verified_at:
            self._active_attestation = None
            raise OandaPracticeError("the adapter clock moved backwards after preflight")
        if (
            attestation.identity.account_id != self._config.account_id
            or attestation.identity.broker_name != "OANDA"
            or attestation.identity.rest_base_url != OANDA_PRACTICE_REST_URL
            or attestation.identity.environment != "practice"
            or attestation.instrument != OANDA_INSTRUMENT
            or attestation.timeframe != INTERNAL_TIMEFRAME
        ):
            self._active_attestation = None
            raise OandaPracticeError(
                "the practice-data attestation no longer matches configuration"
            )

    def _candle_response(
        self,
        attestation: PracticeDataAttestation,
        count: int,
    ) -> Tuple[Mapping[str, Any], datetime, OandaPracticeReadRequest]:
        _validate_count(count)
        self._require_attestation(attestation, self._now())
        request = OandaPracticeReadRequest(
            OandaReadOperation.CANDLES,
            self._config.account_id,
            count=count,
        )
        payload = _mapping(
            self._transport.get_json(request),
            "response",
        )
        received_at = self._now()
        self._require_attestation(attestation, received_at)
        return payload, received_at, request

    def fetch_historical_bars(
        self,
        attestation: PracticeDataAttestation,
        count: int = 500,
    ) -> AttestedQuoteBatch:
        """Return history using an explicit close-time availability assumption."""

        payload, received_at, request = self._candle_response(attestation, count)
        bars = self._normalize_candles(payload, received_at, observed_feed=False)
        return AttestedQuoteBatch(
            attestation=attestation,
            bars=bars,
            retrieved_at=received_at,
            availability_basis=AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION,
            request_sha256=request.fingerprint,
            requested_count=count,
        )

    def latest_completed_bar(self, attestation: PracticeDataAttestation) -> AttestedQuoteBar:
        """Return the latest bar stamped with its actual adapter receipt time."""

        payload, received_at, request = self._candle_response(attestation, count=2)
        bar = self._normalize_candles(payload, received_at, observed_feed=True)[-1]
        if received_at - bar.timestamp > self._max_observed_bar_age:
            raise OandaPracticeError("the latest completed OANDA candle is stale")
        return AttestedQuoteBar(
            attestation=attestation,
            bar=bar,
            retrieved_at=received_at,
            availability_basis=AvailabilityBasis.OBSERVED_RECEIPT,
            request_sha256=request.fingerprint,
        )

    def _normalize_candles(
        self,
        payload: Mapping[str, Any],
        received_at: datetime,
        observed_feed: bool,
    ) -> Tuple[QuoteBar, ...]:
        if payload.get("instrument") != OANDA_INSTRUMENT:
            raise OandaPracticeError("OANDA candle instrument did not match XAU_USD")
        if payload.get("granularity") != OANDA_GRANULARITY:
            raise OandaPracticeError("OANDA candle granularity did not match H1")
        candles = _sequence(payload.get("candles"), "candles")
        bars: List[QuoteBar] = []
        previous_source_time: Optional[datetime] = None
        incomplete_seen = False
        for index, item in enumerate(candles):
            candle = _mapping(item, "candles[{}]".format(index))
            start_time = _parse_rfc3339(candle.get("time"))
            if start_time.minute or start_time.second or start_time.microsecond:
                raise OandaPracticeError("OANDA H1 candle start time is not hour-aligned")
            if previous_source_time is not None and start_time <= previous_source_time:
                raise OandaPracticeError("OANDA source candles are not strictly increasing")
            previous_source_time = start_time

            complete = candle.get("complete")
            if not isinstance(complete, bool):
                raise OandaPracticeError(
                    "OANDA response field candles[{}].complete must be boolean".format(index)
                )
            if not complete:
                incomplete_seen = True
                continue
            if incomplete_seen:
                raise OandaPracticeError("an incomplete OANDA candle must be a terminal suffix")

            end_time = start_time + timedelta(hours=1)
            if end_time > received_at:
                raise OandaPracticeError("OANDA marked a future H1 candle as complete")
            if observed_feed and received_at <= end_time:
                raise OandaPracticeError(
                    "an observed completed candle must be received after its end time"
                )
            bid = _mapping(candle.get("bid"), "candles[{}].bid".format(index))
            ask = _mapping(candle.get("ask"), "candles[{}].ask".format(index))
            volume = candle.get("volume")
            if isinstance(volume, bool) or not isinstance(volume, int) or volume < 0:
                raise OandaPracticeError(
                    "OANDA response field candles[{}].volume must be a non-negative integer".format(
                        index
                    )
                )
            available_at = received_at if observed_feed else end_time
            try:
                bar = QuoteBar(
                    start_time=start_time,
                    timestamp=end_time,
                    availability_basis=(
                        AvailabilityBasis.OBSERVED_RECEIPT
                        if observed_feed
                        else AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION
                    ),
                    available_at=available_at,
                    bid_open=_price(bid, "o", "candles[{}].bid.o".format(index)),
                    bid_high=_price(bid, "h", "candles[{}].bid.h".format(index)),
                    bid_low=_price(bid, "l", "candles[{}].bid.l".format(index)),
                    bid_close=_price(bid, "c", "candles[{}].bid.c".format(index)),
                    ask_open=_price(ask, "o", "candles[{}].ask.o".format(index)),
                    ask_high=_price(ask, "h", "candles[{}].ask.h".format(index)),
                    ask_low=_price(ask, "l", "candles[{}].ask.l".format(index)),
                    ask_close=_price(ask, "c", "candles[{}].ask.c".format(index)),
                    volume=float(volume),
                )
            except (ValueError, OverflowError) as exc:
                raise OandaPracticeError(
                    "OANDA candle {} failed quote-bar validation: {}".format(index, exc)
                ) from exc
            bars.append(bar)
        if not bars:
            raise OandaPracticeError("OANDA response contained no completed H1 candles")
        return tuple(bars)
