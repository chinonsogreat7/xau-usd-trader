"""Provider-neutral boundaries for authorized transcript-to-strategy extraction."""

from dataclasses import dataclass
from datetime import timezone
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import math
from typing import Any, Mapping, Protocol, Sequence, Tuple

from .domain import ContentSource, ExtractedStrategyCandidate, RightsBasis, StrategyStatus
from .strategy_schema import ValidatedStrategySpecV1, validate_strategy_spec_v1


_TRANSCRIPT_DECIMAL_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)


def _transcript_milliseconds(seconds: float) -> Decimal:
    with localcontext(_TRANSCRIPT_DECIMAL_CONTEXT):
        return Decimal(str(seconds)) * Decimal(1000)


@dataclass(frozen=True)
class TranscriptSegment:
    start_seconds: float
    end_seconds: float
    text: str

    def __post_init__(self) -> None:
        if not math.isfinite(self.start_seconds) or self.start_seconds < 0:
            raise ValueError("start_seconds must be finite and non-negative")
        if not math.isfinite(self.end_seconds) or self.end_seconds < self.start_seconds:
            raise ValueError("end_seconds must be at or after start_seconds")
        if not self.text.strip():
            raise ValueError("transcript segment text must not be empty")
        object.__setattr__(self, "text", self.text.strip())


@dataclass(frozen=True)
class TranscriptDocument:
    source_id: str
    language: str
    segments: Tuple[TranscriptSegment, ...]

    def __post_init__(self) -> None:
        if not self.source_id.strip() or not self.language.strip():
            raise ValueError("source_id and language must not be empty")
        segments = tuple(self.segments)
        if not segments:
            raise ValueError("a transcript must contain at least one segment")
        if any(not isinstance(segment, TranscriptSegment) for segment in segments):
            raise ValueError("segments must contain TranscriptSegment values")
        for previous, current in zip(segments, segments[1:]):
            if current.start_seconds < previous.start_seconds:
                raise ValueError("transcript segments must be time ordered")
        object.__setattr__(self, "segments", segments)


def transcript_excerpt_sha256(
    transcript: TranscriptDocument,
    start_ms: int,
    end_ms: int,
) -> str:
    """Hash the exact transcript segments supporting one evidence interval.

    The canonical payload includes source/language identity, the requested interval, and every
    overlapping segment with normalized decimal millisecond timestamps. Extractors can call this
    helper when constructing StrategySpec v1 evidence.
    """

    if not isinstance(transcript, TranscriptDocument):
        raise ValueError("transcript must be a TranscriptDocument")
    if (
        isinstance(start_ms, bool)
        or not isinstance(start_ms, int)
        or isinstance(end_ms, bool)
        or not isinstance(end_ms, int)
        or start_ms < 0
        or end_ms <= start_ms
    ):
        raise ValueError("excerpt interval must use non-negative increasing integer milliseconds")

    def millisecond_text(seconds: float) -> str:
        value = _transcript_milliseconds(seconds)
        if value == 0:
            return "0"
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text

    overlapping = [
        segment
        for segment in transcript.segments
        if Decimal(start_ms) < _transcript_milliseconds(segment.end_seconds)
        and Decimal(end_ms) > _transcript_milliseconds(segment.start_seconds)
    ]
    if not overlapping:
        raise ValueError("excerpt interval does not overlap a transcript segment")
    payload = {
        "source_id": transcript.source_id,
        "language": transcript.language,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "segments": [
            {
                "start_ms": millisecond_text(segment.start_seconds),
                "end_ms": millisecond_text(segment.end_seconds),
                "text": segment.text,
            }
            for segment in overlapping
        ],
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TranscriptProvider(Protocol):
    def transcribe(self, source: ContentSource) -> TranscriptDocument:
        """Transcribe media the project is authorized to process."""


class StrategyExtractor(Protocol):
    def extract(
        self,
        source: ContentSource,
        transcript: TranscriptDocument,
    ) -> Sequence[ExtractedStrategyCandidate]:
        """Propose cited candidates; it must never deploy or execute them."""


class LearningPipeline:
    """Coordinates providers while preserving source identity and human review gates."""

    def __init__(self, transcript_provider: TranscriptProvider, extractor: StrategyExtractor) -> None:
        self.transcript_provider = transcript_provider
        self.extractor = extractor

    def propose(self, source: ContentSource) -> Tuple[ExtractedStrategyCandidate, ...]:
        transcript = self.transcript_provider.transcribe(source)
        if transcript.source_id != source.source_id:
            raise ValueError("transcript source_id does not match the requested source")
        candidates = tuple(self.extractor.extract(source, transcript))
        transcript_end = max(segment.end_seconds for segment in transcript.segments)
        for candidate in candidates:
            if candidate.source_id != source.source_id:
                raise ValueError("candidate source_id does not match the requested source")
            if candidate.status != StrategyStatus.DRAFT:
                raise ValueError("an extractor may produce draft candidates only")
            for citation in candidate.citations:
                citation_end = (
                    citation.start_seconds
                    if citation.end_seconds is None
                    else citation.end_seconds
                )
                if citation_end > transcript_end:
                    raise ValueError("candidate citation extends beyond the transcript")
                overlaps = any(
                    citation.start_seconds <= segment.end_seconds
                    and citation_end >= segment.start_seconds
                    for segment in transcript.segments
                )
                if not overlaps:
                    raise ValueError("candidate citation does not overlap a transcript segment")
        return candidates


class ClosedStrategyExtractor(Protocol):
    def extract_v1(
        self,
        source: ContentSource,
        transcript: TranscriptDocument,
    ) -> Sequence[Mapping[str, Any]]:
        """Return JSON-shaped v1 drafts; strings are never treated as executable code."""


class ClosedLearningPipeline:
    """Validate structured extractor output and bind video evidence to transcript time ranges.

    This pipeline returns validated drafts only. It never freezes, compiles, promotes, backtests,
    or submits a candidate, and it never converts legacy free-text rules automatically.
    """

    def __init__(
        self,
        transcript_provider: TranscriptProvider,
        extractor: ClosedStrategyExtractor,
    ) -> None:
        self.transcript_provider = transcript_provider
        self.extractor = extractor

    def propose(self, source: ContentSource) -> Tuple[ValidatedStrategySpecV1, ...]:
        transcript = self.transcript_provider.transcribe(source)
        if transcript.source_id != source.source_id:
            raise ValueError("transcript source_id does not match the requested source")
        documents = tuple(self.extractor.extract_v1(source, transcript))
        validated_documents = []
        transcript_end_ms = max(
            _transcript_milliseconds(segment.end_seconds)
            for segment in transcript.segments
        )
        rights_mapping = {
            RightsBasis.OWNED: "operator_authored",
            RightsBasis.LICENSED: "licensed",
            RightsBasis.CREATOR_PROVIDED: "user_supplied",
            RightsBasis.USER_SUPPLIED: "user_supplied",
            RightsBasis.USER_NOTES: "operator_authored",
            RightsBasis.PUBLIC_API: "public_api",
        }

        def source_timestamp(value):
            if value.microsecond:
                return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
                    "+00:00", "Z"
                )
            return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            )

        for document in documents:
            validated = validate_strategy_spec_v1(document)
            canonical = validated.as_dict()
            if canonical["frozen_at"] is not None:
                raise ValueError("a structured extractor may produce unfrozen drafts only")
            if len(canonical["provenance"]["sources"]) != 1:
                raise ValueError(
                    "single-source extraction cannot add unadmitted provenance sources"
                )
            matching_sources = [
                item
                for item in canonical["provenance"]["sources"]
                if item["source_id"] == source.source_id
            ]
            if len(matching_sources) != 1:
                raise ValueError("candidate provenance must bind exactly one requested source")
            candidate_source = matching_sources[0]
            if candidate_source["type"] != "video":
                raise ValueError("closed transcript extraction requires video provenance")
            if source.content_sha256 is None:
                raise ValueError("closed extraction requires the source content_sha256")
            if candidate_source["content_sha256"] != source.content_sha256:
                raise ValueError("candidate content_sha256 does not match the requested source")
            expected_identity = {
                "uri": source.source_uri,
                "title": source.title,
                "creator": source.creator,
                "published_at": source_timestamp(source.published_at),
                "ingested_at": source_timestamp(source.ingested_at),
                "rights_basis": rights_mapping[source.rights_basis],
            }
            for field_name, expected_value in expected_identity.items():
                if candidate_source[field_name] != expected_value:
                    raise ValueError(
                        "candidate source {} does not match the requested source".format(
                            field_name
                        )
                    )
            for evidence in candidate_source["evidence"]:
                start_ms = evidence["start_ms"]
                end_ms = evidence["end_ms"]
                if start_ms is None or end_ms is None:
                    raise ValueError("video evidence requires a timestamp interval")
                if end_ms > transcript_end_ms:
                    raise ValueError("candidate evidence extends beyond the transcript")
                overlaps = any(
                    Decimal(start_ms) < _transcript_milliseconds(segment.end_seconds)
                    and Decimal(end_ms) > _transcript_milliseconds(segment.start_seconds)
                    for segment in transcript.segments
                )
                if not overlaps:
                    raise ValueError("candidate evidence does not overlap a transcript segment")
                if evidence["excerpt_sha256"] != transcript_excerpt_sha256(
                    transcript, start_ms, end_ms
                ):
                    raise ValueError(
                        "candidate excerpt_sha256 does not match the canonical transcript excerpt"
                    )
            validated_documents.append(validated)
        return tuple(validated_documents)
