import unittest
from copy import deepcopy
from datetime import datetime, timezone
from decimal import getcontext

from strategy_spec_fixture import valid_strategy_spec
from xau_trader.domain import ContentSource, RightsBasis
from xau_trader.learning import (
    ClosedLearningPipeline,
    TranscriptDocument,
    TranscriptSegment,
    transcript_excerpt_sha256,
)


def content_source(content_hash="a" * 64):
    return ContentSource(
        source_id="source-1",
        source_uri="https://example.invalid/authorized-video",
        creator="Test operator",
        title="Authorized synthetic lesson",
        published_at=datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc),
        ingested_at=datetime(2026, 1, 2, 9, 0, tzinfo=timezone.utc),
        rights_basis=RightsBasis.USER_SUPPLIED,
        content_sha256=content_hash,
    )


class Provider:
    def transcribe(self, source):
        return TranscriptDocument(
            source_id=source.source_id,
            language="en",
            segments=(TranscriptSegment(0.0, 5.0, "Synthetic authorized rule explanation."),),
        )


class Extractor:
    def __init__(self, document=None, bind_excerpt=True):
        self.document = document or valid_strategy_spec(frozen=False)
        self.bind_excerpt = bind_excerpt

    def extract_v1(self, source, transcript):
        document = deepcopy(self.document)
        if self.bind_excerpt:
            for candidate_source in document["provenance"]["sources"]:
                if candidate_source["source_id"] != source.source_id:
                    continue
                for evidence in candidate_source["evidence"]:
                    evidence["excerpt_sha256"] = transcript_excerpt_sha256(
                        transcript,
                        evidence["start_ms"],
                        evidence["end_ms"],
                    )
        return (document,)


class ClosedLearningPipelineTests(unittest.TestCase):
    def test_structured_pipeline_returns_validated_unfrozen_draft(self):
        candidates = ClosedLearningPipeline(Provider(), Extractor()).propose(content_source())

        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0].compilation_ready)
        self.assertFalse(candidates[0].promotion_ready)
        self.assertIsNone(candidates[0].as_dict()["frozen_at"])

    def test_extractor_cannot_freeze_its_own_document(self):
        with self.assertRaisesRegex(ValueError, "unfrozen drafts only"):
            ClosedLearningPipeline(Provider(), Extractor(valid_strategy_spec())).propose(
                content_source()
            )

    def test_source_hash_and_identity_are_bound(self):
        wrong_hash = content_source("c" * 64)
        with self.assertRaisesRegex(ValueError, "content_sha256"):
            ClosedLearningPipeline(Provider(), Extractor()).propose(wrong_hash)

        wrong_source = valid_strategy_spec(frozen=False)
        wrong_source["provenance"]["sources"][0]["source_id"] = "source-2"
        with self.assertRaisesRegex(ValueError, "exactly one requested source"):
            ClosedLearningPipeline(Provider(), Extractor(wrong_source)).propose(content_source())

        extra_source = valid_strategy_spec(frozen=False)
        fabricated = deepcopy(extra_source["provenance"]["sources"][0])
        fabricated["source_id"] = "source-2"
        fabricated["content_sha256"] = "d" * 64
        fabricated["evidence"][0]["evidence_id"] = "fabricated-evidence"
        fabricated["evidence"][0]["start_ms"] = 999_000_000
        fabricated["evidence"][0]["end_ms"] = 999_001_000
        extra_source["provenance"]["sources"].append(fabricated)
        with self.assertRaisesRegex(ValueError, "unadmitted provenance"):
            ClosedLearningPipeline(Provider(), Extractor(extra_source)).propose(
                content_source()
            )

    def test_evidence_must_fit_and_overlap_transcript(self):
        beyond = valid_strategy_spec(frozen=False)
        beyond["provenance"]["sources"][0]["evidence"][0]["end_ms"] = 5001
        with self.assertRaisesRegex(ValueError, "beyond the transcript"):
            ClosedLearningPipeline(Provider(), Extractor(beyond)).propose(content_source())

        class GappedProvider:
            def transcribe(self, source):
                return TranscriptDocument(
                    source_id=source.source_id,
                    language="en",
                    segments=(
                        TranscriptSegment(0.0, 1.0, "First segment."),
                        TranscriptSegment(4.0, 6.0, "Second segment."),
                    ),
                )

        gap = valid_strategy_spec(frozen=False)
        evidence = gap["provenance"]["sources"][0]["evidence"][0]
        evidence["start_ms"] = 2000
        evidence["end_ms"] = 3000
        with self.assertRaisesRegex(ValueError, "does not overlap"):
            ClosedLearningPipeline(GappedProvider(), Extractor(gap)).propose(content_source())

    def test_excerpt_hash_is_bound_to_canonical_transcript_segments(self):
        tampered = valid_strategy_spec(frozen=False)
        tampered["provenance"]["sources"][0]["evidence"][0]["excerpt_sha256"] = "f" * 64

        with self.assertRaisesRegex(ValueError, "canonical transcript excerpt"):
            ClosedLearningPipeline(
                Provider(), Extractor(tampered, bind_excerpt=False)
            ).propose(content_source())

    def test_fractional_transcript_hash_and_pipeline_ignore_ambient_decimal_context(self):
        class FractionalProvider:
            def transcribe(self, source):
                return TranscriptDocument(
                    source_id=source.source_id,
                    language="en",
                    segments=(
                        TranscriptSegment(
                            1.234567,
                            2.345678,
                            "Precision-sensitive synthetic segment.",
                        ),
                    ),
                )

        document = valid_strategy_spec(frozen=False)
        evidence = document["provenance"]["sources"][0]["evidence"][0]
        evidence["start_ms"] = 1235
        evidence["end_ms"] = 2345
        previous_precision = getcontext().prec
        try:
            getcontext().prec = 3
            low = ClosedLearningPipeline(
                FractionalProvider(), Extractor(document)
            ).propose(content_source())
            getcontext().prec = 50
            high = ClosedLearningPipeline(
                FractionalProvider(), Extractor(document)
            ).propose(content_source())
        finally:
            getcontext().prec = previous_precision

        self.assertEqual(low[0].spec_hash, high[0].spec_hash)


if __name__ == "__main__":
    unittest.main()
