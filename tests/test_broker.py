import unittest
from datetime import datetime, timedelta, timezone

from xau_trader.broker import PracticeAccountIdentity, PracticeDataAttestation


class PaperBrokerContractTests(unittest.TestCase):
    def test_identity_requires_https_and_timezone(self):
        with self.assertRaisesRegex(ValueError, "absolute HTTPS"):
            PracticeAccountIdentity(
                broker_name="Example",
                account_id="paper-1",
                rest_base_url="http://example.test/practice",
                stream_base_url="https://stream.example.test/practice",
                verified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        with self.assertRaisesRegex(ValueError, "timezone"):
            PracticeAccountIdentity(
                broker_name="OANDA",
                account_id="paper-1",
                rest_base_url="https://api-fxpractice.oanda.com",
                stream_base_url="https://stream-fxpractice.oanda.com",
                verified_at=datetime(2026, 1, 1),
            )

    def test_identity_rejects_embedded_credentials(self):
        with self.assertRaisesRegex(ValueError, "must not contain credentials"):
            PracticeAccountIdentity(
                broker_name="Example",
                account_id="paper-1",
                rest_base_url="https://token@example.test/practice",
                stream_base_url="https://stream.example.test/practice",
                verified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )

    def test_identity_repr_redacts_account_id(self):
        identity = PracticeAccountIdentity(
            broker_name="OANDA",
            account_id="paper-sensitive-identifier",
            rest_base_url="https://api-fxpractice.oanda.com",
            stream_base_url="https://stream-fxpractice.oanda.com",
            verified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.assertNotIn("paper-sensitive-identifier", repr(identity))

    def test_identity_rejects_known_live_origin(self):
        with self.assertRaisesRegex(ValueError, "registered practice"):
            PracticeAccountIdentity(
                broker_name="OANDA",
                account_id="paper-1",
                rest_base_url="https://api-fxtrade.oanda.com",
                stream_base_url="https://stream-fxpractice.oanda.com",
                verified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )

    def test_attestation_binds_identity_and_expiry(self):
        verified_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        identity = PracticeAccountIdentity(
            broker_name="OANDA",
            account_id="paper-1",
            rest_base_url="https://api-fxpractice.oanda.com",
            stream_base_url="https://stream-fxpractice.oanda.com",
            verified_at=verified_at,
        )
        attestation = PracticeDataAttestation(
            identity=identity,
            instrument="XAU_USD",
            timeframe="PT1H",
            provider_instrument_type="METAL",
            verified_at=verified_at,
            expires_at=verified_at + timedelta(minutes=5),
        )
        self.assertEqual(attestation.identity, identity)
        with self.assertRaisesRegex(ValueError, "later"):
            PracticeDataAttestation(
                identity=identity,
                instrument="XAU_USD",
                timeframe="PT1H",
                provider_instrument_type="METAL",
                verified_at=verified_at,
                expires_at=verified_at,
            )


if __name__ == "__main__":
    unittest.main()
