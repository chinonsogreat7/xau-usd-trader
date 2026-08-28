import inspect
import unittest
from datetime import datetime, timedelta, timezone

from xau_trader.backtest import Backtester
from xau_trader.oanda import (
    MAX_CANDLES_PER_REQUEST,
    OANDA_PRACTICE_REST_URL,
    OandaPracticeConfig,
    OandaPracticeError,
    OandaPracticeMarketDataAdapter,
    OandaPracticeReadRequest,
    OandaReadOperation,
)
from xau_trader.strategies import FlatStrategy


ACCOUNT_ID = "101-001-12345678-001"


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get_json(self, request):
        self.calls.append(request)
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class SequenceClock:
    def __init__(self, *values):
        self.values = list(values)

    def __call__(self):
        if not self.values:
            raise AssertionError("unexpected clock read")
        return self.values.pop(0)


def account_response(account_id=ACCOUNT_ID):
    return {"account": {"id": account_id}}


def entitlement_response(*items):
    return {"instruments": list(items or ({"name": "XAU_USD", "type": "METAL"},))}


def candle(time="2026-01-01T10:00:00.000000000Z", complete=True, include_ask=True):
    result = {
        "time": time,
        "complete": complete,
        "volume": 123,
        "bid": {"o": "2000.0", "h": "2002.0", "l": "1999.0", "c": "2001.0"},
    }
    if include_ask:
        result["ask"] = {
            "o": "2000.3",
            "h": "2002.3",
            "l": "1999.3",
            "c": "2001.3",
        }
    return result


def candle_response(*candles, instrument="XAU_USD", granularity="H1"):
    return {
        "instrument": instrument,
        "granularity": granularity,
        "candles": list(candles),
    }


class OandaPracticeRequestTests(unittest.TestCase):
    def test_request_plan_is_get_only_and_practice_only(self):
        request = OandaPracticeReadRequest(
            OandaReadOperation.CANDLES,
            ACCOUNT_ID,
            count=2,
        )
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.base_url, OANDA_PRACTICE_REST_URL)
        self.assertEqual(
            request.path,
            "/v3/accounts/{}/instruments/XAU_USD/candles".format(ACCOUNT_ID),
        )
        self.assertEqual(
            request.query,
            {
                "count": "2",
                "granularity": "H1",
                "price": "BA",
                "smooth": "false",
                "units": "1",
            },
        )
        self.assertEqual(len(request.fingerprint), 64)
        self.assertEqual(
            set(OandaReadOperation),
            {
                OandaReadOperation.ACCOUNT_SUMMARY,
                OandaReadOperation.INSTRUMENTS,
                OandaReadOperation.CANDLES,
            },
        )

    def test_request_plan_rejects_count_outside_candle_allowlist(self):
        with self.assertRaisesRegex(OandaPracticeError, "only for the candle"):
            OandaPracticeReadRequest(
                OandaReadOperation.ACCOUNT_SUMMARY,
                ACCOUNT_ID,
                count=1,
            )
        with self.assertRaisesRegex(OandaPracticeError, "between 1"):
            OandaPracticeReadRequest(
                OandaReadOperation.CANDLES,
                ACCOUNT_ID,
                count=MAX_CANDLES_PER_REQUEST + 1,
            )

    def test_no_credential_or_network_factory_is_shipped(self):
        parameters = inspect.signature(OandaPracticeConfig).parameters
        self.assertEqual(tuple(parameters), ("account_id",))
        adapter_parameters = inspect.signature(OandaPracticeMarketDataAdapter).parameters
        self.assertIn("transport", adapter_parameters)
        self.assertIs(adapter_parameters["transport"].default, inspect.Parameter.empty)


class OandaPracticeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.config = OandaPracticeConfig(account_id=ACCOUNT_ID)
        self.clock = MutableClock(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))

    def _preflight(self, *later_responses):
        transport = FakeTransport(
            account_response(),
            entitlement_response(),
            *later_responses,
        )
        adapter = OandaPracticeMarketDataAdapter(
            self.config,
            transport=transport,
            clock=self.clock,
        )
        attestation = adapter.preflight()
        return adapter, attestation, transport

    def test_account_id_is_redacted_from_config_repr(self):
        self.assertNotIn(ACCOUNT_ID, repr(self.config))

    def test_attestation_ttl_is_capped(self):
        with self.assertRaisesRegex(OandaPracticeError, "at most five minutes"):
            OandaPracticeMarketDataAdapter(
                self.config,
                transport=FakeTransport(),
                clock=self.clock,
                attestation_ttl=timedelta(minutes=6),
            )
        with self.assertRaisesRegex(OandaPracticeError, "at most two hours"):
            OandaPracticeMarketDataAdapter(
                self.config,
                transport=FakeTransport(),
                clock=self.clock,
                max_observed_bar_age=timedelta(hours=3),
            )

    def test_preflight_binds_account_instrument_type_and_expiry(self):
        adapter, attestation, transport = self._preflight()

        self.assertEqual(attestation.identity.account_id, ACCOUNT_ID)
        self.assertEqual(attestation.identity.environment, "practice")
        self.assertEqual(attestation.instrument, "XAU_USD")
        self.assertEqual(attestation.timeframe, "PT1H")
        self.assertEqual(attestation.provider_instrument_type, "METAL")
        self.assertEqual(attestation.expires_at, self.clock.value + timedelta(minutes=5))
        self.assertEqual(transport.calls[0].operation, OandaReadOperation.ACCOUNT_SUMMARY)
        self.assertEqual(transport.calls[1].operation, OandaReadOperation.INSTRUMENTS)
        self.assertFalse(hasattr(adapter, "submit"))

    def test_fetch_requires_a_current_preflight(self):
        transport = FakeTransport(candle_response(candle()))
        adapter = OandaPracticeMarketDataAdapter(
            self.config,
            transport=transport,
            clock=self.clock,
        )
        with self.assertRaisesRegex(OandaPracticeError, "current preflight"):
            adapter.fetch_historical_bars(None, count=1)
        self.assertEqual(transport.calls, [])

    def test_preflight_fails_on_identity_or_entitlement_schema(self):
        non_object = OandaPracticeMarketDataAdapter(
            self.config,
            transport=FakeTransport([]),
            clock=self.clock,
        )
        with self.assertRaisesRegex(OandaPracticeError, "response must be an object"):
            non_object.preflight()

        wrong_identity = OandaPracticeMarketDataAdapter(
            self.config,
            transport=FakeTransport(account_response("different-account")),
            clock=self.clock,
        )
        with self.assertRaisesRegex(OandaPracticeError, "identity did not match"):
            wrong_identity.preflight()

        missing_type = OandaPracticeMarketDataAdapter(
            self.config,
            transport=FakeTransport(
                account_response(),
                entitlement_response({"name": "XAU_USD"}),
            ),
            clock=self.clock,
        )
        with self.assertRaisesRegex(OandaPracticeError, "type was missing"):
            missing_type.preflight()

    def test_historical_candle_preserves_start_end_and_close_assumption(self):
        adapter, attestation, transport = self._preflight(candle_response(candle()))

        batch = adapter.fetch_historical_bars(attestation, count=1)
        bars = batch.bars

        self.assertEqual(len(bars), 1)
        self.assertEqual(
            bars[0].start_time,
            datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            bars[0].timestamp,
            datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(bars[0].available_at, bars[0].timestamp)
        self.assertEqual(bars[0].bid_close, 2001.0)
        self.assertEqual(bars[0].ask_close, 2001.3)
        self.assertEqual(bars[0].volume, 123.0)
        self.assertEqual(transport.calls[2].operation, OandaReadOperation.CANDLES)
        self.assertEqual(transport.calls[2].query["count"], "1")
        self.assertEqual(batch.attestation, attestation)
        self.assertEqual(batch.retrieved_at, self.clock.value)
        self.assertEqual(batch.request_sha256, transport.calls[2].fingerprint)

    def test_latest_bar_uses_receipt_time_not_historical_backdating(self):
        adapter, attestation, _ = self._preflight(
            candle_response(candle(), candle(time="2026-01-01T11:00:00Z", complete=False))
        )

        observation = adapter.latest_completed_bar(attestation)
        latest = observation.bar

        self.assertEqual(latest.timestamp, datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc))
        self.assertEqual(latest.available_at, self.clock.value)
        self.assertGreater(latest.available_at, latest.timestamp)
        self.assertEqual(observation.attestation, attestation)
        self.assertEqual(observation.retrieved_at, self.clock.value)
        with self.assertRaisesRegex(ValueError, "observed-receipt"):
            Backtester().run((latest,), FlatStrategy())

    def test_observed_bar_cannot_masquerade_as_close_assumed_history(self):
        self.clock.value = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)
        adapter, attestation, _ = self._preflight(candle_response(candle()))
        with self.assertRaisesRegex(OandaPracticeError, "received after"):
            adapter.latest_completed_bar(attestation)

    def test_latest_observed_bar_must_be_fresh(self):
        self.clock.value = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
        adapter, attestation, _ = self._preflight(candle_response(candle()))
        with self.assertRaisesRegex(OandaPracticeError, "stale"):
            adapter.latest_completed_bar(attestation)

    def test_expired_or_foreign_attestation_is_rejected_before_fetch(self):
        adapter, attestation, transport = self._preflight(candle_response(candle()))
        self.clock.value += timedelta(minutes=5)
        with self.assertRaisesRegex(OandaPracticeError, "expired"):
            adapter.fetch_historical_bars(attestation, count=1)
        self.assertEqual(len(transport.calls), 2)

        other = OandaPracticeMarketDataAdapter(
            self.config,
            transport=FakeTransport(),
            clock=self.clock,
        )
        with self.assertRaisesRegex(OandaPracticeError, "current preflight"):
            other.fetch_historical_bars(attestation, count=1)

    def test_clock_rollback_during_fetch_fails_closed(self):
        noon = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        clock = SequenceClock(
            noon,
            noon,
            noon + timedelta(minutes=4),
            noon + timedelta(minutes=3),
        )
        transport = FakeTransport(
            account_response(),
            entitlement_response(),
            candle_response(candle()),
        )
        adapter = OandaPracticeMarketDataAdapter(
            self.config,
            transport=transport,
            clock=clock,
        )
        attestation = adapter.preflight()
        with self.assertRaisesRegex(OandaPracticeError, "clock moved backwards"):
            adapter.fetch_historical_bars(attestation, count=1)

    def test_request_market_allowlist_fails_before_transport(self):
        transport = FakeTransport()
        adapter = OandaPracticeMarketDataAdapter(
            self.config,
            transport=transport,
            clock=self.clock,
        )
        with self.assertRaisesRegex(OandaPracticeError, "canonical instrument"):
            adapter.preflight(instrument="EUR_USD")
        with self.assertRaisesRegex(OandaPracticeError, "canonical timeframe"):
            adapter.preflight(timeframe="PT5M")
        self.assertEqual(transport.calls, [])

    def test_missing_bid_or_ask_is_rejected(self):
        adapter, attestation, _ = self._preflight(
            candle_response(candle(include_ask=False))
        )
        with self.assertRaisesRegex(OandaPracticeError, "ask must be an object"):
            adapter.fetch_historical_bars(attestation, count=1)

    def test_response_identity_and_all_source_times_are_revalidated(self):
        wrong_instrument, attestation, _ = self._preflight(
            candle_response(candle(), instrument="EUR_USD")
        )
        with self.assertRaisesRegex(OandaPracticeError, "instrument did not match"):
            wrong_instrument.fetch_historical_bars(attestation, count=1)

        duplicate, attestation, _ = self._preflight(
            candle_response(candle(complete=False), candle(complete=False))
        )
        with self.assertRaisesRegex(OandaPracticeError, "not strictly increasing"):
            duplicate.fetch_historical_bars(attestation, count=2)

        bad_suffix, attestation, _ = self._preflight(
            candle_response(
                candle(complete=False),
                candle(time="2026-01-01T11:00:00Z", complete=True),
            )
        )
        with self.assertRaisesRegex(OandaPracticeError, "terminal suffix"):
            bad_suffix.fetch_historical_bars(attestation, count=2)

    def test_future_complete_and_misaligned_h1_candles_fail_closed(self):
        future, attestation, _ = self._preflight(
            candle_response(candle(time="2099-01-01T10:00:00Z"))
        )
        with self.assertRaisesRegex(OandaPracticeError, "future H1"):
            future.fetch_historical_bars(attestation, count=1)

        misaligned, attestation, _ = self._preflight(
            candle_response(candle(time="2026-01-01T10:30:00Z"))
        )
        with self.assertRaisesRegex(OandaPracticeError, "not hour-aligned"):
            misaligned.fetch_historical_bars(attestation, count=1)

        submicrosecond, attestation, _ = self._preflight(
            candle_response(candle(time="2026-01-01T10:00:00.000000001Z"))
        )
        with self.assertRaisesRegex(OandaPracticeError, "sub-microsecond"):
            submicrosecond.fetch_historical_bars(attestation, count=1)

    def test_oversized_numeric_values_are_sanitized(self):
        oversized = candle()
        oversized["volume"] = 10**10_000
        adapter, attestation, _ = self._preflight(candle_response(oversized))
        with self.assertRaisesRegex(OandaPracticeError, "failed quote-bar validation"):
            adapter.fetch_historical_bars(attestation, count=1)


if __name__ == "__main__":
    unittest.main()
