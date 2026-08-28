import unittest
from datetime import datetime, timezone

from xau_trader.domain import (
    ContentSource,
    RightsBasis,
    SourceCitation,
    StrategySpec,
    StrategyStatus,
)
from xau_trader.learning import (
    LearningPipeline,
    TranscriptDocument,
    TranscriptSegment,
)


def source():
    return ContentSource(
        source_id="source-1",
        source_uri="local://authorized-notes/source-1",
        creator="Research operator",
        title="Authorized notes",
        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ingested_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        rights_basis=RightsBasis.USER_NOTES,
    )


class Provider:
    def __init__(self, source_id="source-1"):
        self.source_id = source_id

    def transcribe(self, content_source):
        return TranscriptDocument(
            source_id=self.source_id,
            language="en",
            segments=(TranscriptSegment(0.0, 5.0, "One testable claim"),),
        )


class Extractor:
    def __init__(self, status=StrategyStatus.DRAFT, citation_end=5.0):
        self.status = status
        self.citation_end = citation_end

    def extract(self, content_source, transcript):
        return (
            StrategySpec(
                strategy_id="candidate-1",
                version=1,
                source_id=content_source.source_id,
                instrument="XAU_USD",
                timeframe="1h",
                hypothesis="A falsifiable hypothesis.",
                entry_rules=("One exact entry rule",),
                exit_rules=("One exact exit rule",),
                risk_rules=("One exact risk rule",),
                citations=(
                    SourceCitation(0.0, "One testable claim", self.citation_end),
                ),
                status=self.status,
            ),
        )


class LearningPipelineTests(unittest.TestCase):
    def test_pipeline_preserves_source_identity(self):
        candidates = LearningPipeline(Provider(), Extractor()).propose(source())
        self.assertEqual(candidates[0].source_id, "source-1")

    def test_pipeline_rejects_mismatched_transcript(self):
        with self.assertRaisesRegex(ValueError, "transcript source_id"):
            LearningPipeline(Provider("different-source"), Extractor()).propose(source())

    def test_extractor_cannot_approve_its_own_candidate(self):
        with self.assertRaisesRegex(ValueError, "draft candidates only"):
            LearningPipeline(Provider(), Extractor(StrategyStatus.FROZEN)).propose(source())

    def test_pipeline_rejects_citation_beyond_transcript(self):
        with self.assertRaisesRegex(ValueError, "beyond the transcript"):
            LearningPipeline(Provider(), Extractor(citation_end=6.0)).propose(source())


if __name__ == "__main__":
    unittest.main()
