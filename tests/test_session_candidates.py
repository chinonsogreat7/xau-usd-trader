"""Calendar-bound confirmations and next-open research decisions stay fail-closed."""

import unittest
from dataclasses import replace
from datetime import timedelta

import test_candidate_signals as candidate_fixture
import test_m15_confirmation as confirmation_fixture
from xau_trader.candidate_signals import (
    CandidateAction,
    CandidateDataError,
    CandidateReason,
    paper_candidate_decisions,
)
from xau_trader.m15_confirmation import (
    confirmation_time_eligible_for_touch,
    m15_candle_events,
    m15_confirmation_events,
    m15_ema_events,
)
from xau_trader.market_regime import MarketRegime, h1_regime_events
from xau_trader.multitimeframe import MultiTimeframeDataError, PivotKind
from xau_trader.session_calendar import (
    ScheduledClosure,
    SessionCalendarArtifact,
    SessionCalendarError,
)
from xau_trader.session_timing import SESSION_STRATEGY_SEMANTICS
from xau_trader.zone_lifecycle import (
    CompletedBarObservation,
    ObservationTimeframe,
    evolve_zone_book,
)


BASE = candidate_fixture.BASE
FORMED = candidate_fixture.FORMED
H1 = timedelta(hours=1)
M15 = timedelta(minutes=15)


def calendar_fixture(*, closure_hour=6, coverage_hour=48):
    return SessionCalendarArtifact(
        calendar_id="synthetic-candidate-sessions",
        revision="fixture-v1",
        provider_name="Local deterministic generator",
        provider_legal_entity="Research project owner",
        instrument="XAU_USD",
        product_form="synthetic bid/ask series",
        coverage_start=BASE - timedelta(days=4),
        coverage_end=BASE + coverage_hour * H1,
        source_reference="local-generator://candidate-session-fixture-v1",
        retrieved_at=BASE,
        closures=(ScheduledClosure(
            BASE + closure_hour * H1,
            BASE + (closure_hour + 1) * H1,
            "Synthetic maintenance closure",
        ),),
    )


def policies(calendar):
    return (
        replace(candidate_fixture.candidate_policy(), calendar=calendar),
        replace(candidate_fixture.lifecycle_policy(), calendar=calendar),
        replace(candidate_fixture.regime_policy(), calendar=calendar),
        replace(candidate_fixture.confirmation_policy(), calendar=calendar),
    )


def session_update(calendar, bars, *, zones=True, h1_bars=()):
    selected = policies(calendar)[1]
    observations = tuple(
        CompletedBarObservation(ObservationTimeframe.M15, bar) for bar in bars
    ) + tuple(
        CompletedBarObservation(ObservationTimeframe.H1, bar) for bar in h1_bars
    )
    return evolve_zone_book(
        (replace(candidate_fixture.zone(), calendar=calendar),) if zones else (),
        observations,
        selected,
        h1_start=FORMED,
        m15_start=FORMED,
    )


def decide(calendar, update, confirmations=(), *, regimes=None, as_of=None):
    candidate, _, regime, confirmation = policies(calendar)
    if regimes is None:
        event = candidate_fixture.regime_event(MarketRegime.BULLISH, FORMED)
        regimes = (replace(event, policy_fingerprint=regime.fingerprint),)
    return paper_candidate_decisions(
        update, regimes, confirmations,
        candidate_policy=candidate,
        regime_policy=regime,
        confirmation_policy=confirmation,
        as_of=as_of,
    )


class SessionConfirmationTests(unittest.TestCase):
    def reversal(self):
        # The reversal uses the previous traded candle across the one-hour break.
        return (
            confirmation_fixture.bar(
                3, open_price=12, close_price=10,
            ),
            confirmation_fixture.bar(
                8, open_price=9, close_price=13,
            ),
            confirmation_fixture.bar(
                9, open_price=13, close_price=14,
            ),
        )

    def test_detectors_preserve_history_and_real_times_across_declared_closure(self):
        calendar = calendar_fixture(closure_hour=1)
        selected = confirmation_fixture.policy(calendar=calendar)
        bars = self.reversal()
        contiguous = tuple(replace(
            bar,
            start_time=bars[0].start_time + index * M15,
            timestamp=bars[0].timestamp + index * M15,
            available_at=bars[0].timestamp + index * M15,
        ) for index, bar in enumerate(bars))
        strict = replace(selected, calendar=None)
        expected = m15_ema_events(contiguous, policy=strict)
        actual = m15_ema_events(bars, policy=selected)
        self.assertEqual([event.value for event in actual], [event.value for event in expected])
        self.assertEqual(actual[1].bar_start, BASE + 2 * H1)
        candles = m15_candle_events(bars, policy=selected)
        confirmations = m15_confirmation_events(bars, policy=selected)
        self.assertTrue(candles)
        self.assertTrue(confirmations)
        self.assertEqual(confirmations[0].bar_start, bars[1].start_time)
        self.assertEqual(confirmations[0].available_at, bars[1].timestamp)
        self.assertEqual(confirmations[0].policy_fingerprint, selected.fingerprint)
        self.assertIn(SESSION_STRATEGY_SEMANTICS, selected.canonical_identity)
        self.assertNotEqual(selected.fingerprint, strict.fingerprint)

    def test_elapsed_confirmation_expiry_does_not_pause_at_closure(self):
        selected = confirmation_fixture.policy(calendar=calendar_fixture(closure_hour=1))
        bars = self.reversal()
        event = m15_confirmation_events(bars, policy=selected)[0]
        self.assertFalse(confirmation_time_eligible_for_touch(
            event, touch_bar_start=bars[0].start_time,
            touch_bar_end=bars[0].timestamp, policy=selected,
        ))
        extended = replace(selected, confirmation_expiry_bars=5)
        extended_event = m15_confirmation_events(bars, policy=extended)[0]
        self.assertTrue(confirmation_time_eligible_for_touch(
            extended_event, touch_bar_start=bars[0].start_time,
            touch_bar_end=bars[0].timestamp, policy=extended,
        ))

    def test_policy_calendar_inference_override_checks_and_strict_regressions(self):
        calendar = calendar_fixture(closure_hour=1)
        selected = confirmation_fixture.policy(calendar=calendar)
        bars = self.reversal()
        self.assertEqual(m15_ema_events(bars, policy=selected),
                         m15_ema_events(bars, policy=selected, calendar=calendar))
        with self.assertRaisesRegex(ValueError, "conflicts"):
            m15_ema_events(bars, policy=selected, calendar=replace(calendar, revision="other-v1"))
        # Existing feature-only EMA override remains accepted without binding strategy policy.
        unbound = replace(selected, calendar=None)
        self.assertTrue(m15_ema_events(bars, policy=unbound, calendar=calendar))
        for detector in (m15_ema_events, m15_candle_events, m15_confirmation_events):
            with self.subTest(detector=detector.__name__):
                with self.assertRaises(MultiTimeframeDataError):
                    detector(bars, policy=unbound)
                with self.assertRaises(SessionCalendarError):
                    detector((bars[0], bars[2]), policy=selected)
        with self.assertRaises(TypeError):
            replace(selected, calendar="calendar")

    def test_future_suffix_and_as_of_preserve_available_confirmations(self):
        selected = confirmation_fixture.policy(calendar=calendar_fixture(closure_hour=1))
        bars = self.reversal()
        prefix = m15_confirmation_events(bars[:2], policy=selected)
        full_as_of = m15_confirmation_events(bars, policy=selected, as_of=bars[1].timestamp)
        self.assertEqual(prefix, full_as_of)
        self.assertEqual(m15_confirmation_events(
            bars, policy=selected, as_of=bars[1].timestamp - timedelta(microseconds=1),
        ), ())


class SessionCandidateTests(unittest.TestCase):
    def test_open_session_still_proposes_exact_next_open(self):
        calendar = calendar_fixture()
        bar = candidate_fixture.m15(0, low=100, high=105)
        selected = policies(calendar)[3]
        update = session_update(calendar, (bar,))
        decision = decide(calendar, update, (candidate_fixture.confirmation(bar, policy=selected),))[0]
        self.assertEqual(decision.action, CandidateAction.BUY)
        self.assertEqual(decision.next_m15_open, bar.timestamp)
        self.assertEqual(decision.proposed_entry_at, bar.timestamp)
        self.assertEqual(decision.calendar, calendar)
        self.assertIn(SESSION_STRATEGY_SEMANTICS, policies(calendar)[0].canonical_identity)

    def test_closing_bar_cancels_instead_of_queuing_reopening_entry(self):
        calendar = calendar_fixture()
        bars = tuple(candidate_fixture.m15(index, low=100, high=105) for index in range(4))
        update = session_update(calendar, bars, h1_bars=(candidate_fixture.h1(0, low=100, high=105, close=103),))
        event = candidate_fixture.confirmation(bars[-1], policy=policies(calendar)[3])
        last = decide(calendar, update, (event,))[-1]
        self.assertEqual(last.action, CandidateAction.NO_TRADE)
        self.assertEqual(last.reasons, (CandidateReason.SCHEDULED_CLOSURE_NEXT_OPEN,))
        self.assertEqual(last.next_m15_open, BASE + 7 * H1)
        self.assertIsNone(last.proposed_entry_at)
        with self.assertRaises(ValueError):
            replace(last, next_m15_open=last.m15_bar_end)
        with self.assertRaises(ValueError):
            replace(last, reasons=(CandidateReason.NO_TIMELY_CONFIRMATION,))
        with self.assertRaises(ValueError):
            replace(last, action=CandidateAction.BUY, proposed_entry_at=last.next_m15_open)

    def test_calendar_coverage_end_is_explicit_no_trade_not_assumed_open(self):
        calendar = calendar_fixture(closure_hour=2, coverage_hour=6)
        bars = tuple(candidate_fixture.m15(index, low=100, high=105) for index in range(4))
        update = session_update(calendar, bars, zones=False,
                                h1_bars=(candidate_fixture.h1(0, low=100, high=105, close=103),))
        last = decide(calendar, update, regimes=())[-1]
        self.assertEqual(last.reasons, (CandidateReason.CALENDAR_COVERAGE_EXHAUSTED,))
        self.assertEqual(last.next_m15_open, calendar.coverage_end)
        self.assertIsNone(last.proposed_entry_at)

        partial_next_bar = replace(calendar, coverage_end=calendar.coverage_end + timedelta(minutes=5))
        partial_update = session_update(partial_next_bar, bars, zones=False,
                                        h1_bars=(candidate_fixture.h1(0, low=100, high=105, close=103),))
        partial_last = decide(partial_next_bar, partial_update, regimes=())[-1]
        self.assertEqual(partial_last.reasons, (CandidateReason.CALENDAR_COVERAGE_EXHAUSTED,))
        self.assertEqual(partial_last.next_m15_open, bars[-1].timestamp)
        self.assertIsNone(partial_last.proposed_entry_at)

    def test_candidate_touch_window_expires_during_scheduled_closure(self):
        calendar = calendar_fixture()
        bars = tuple(candidate_fixture.m15(index, low=100, high=105) for index in (0, 1, 2, 3, 8))
        update = session_update(calendar, bars,
                                h1_bars=(candidate_fixture.h1(0, low=100, high=105, close=103),))
        event = candidate_fixture.confirmation(bars[-1], policy=policies(calendar)[3])
        last = decide(calendar, update, (event,))[-1]
        self.assertEqual(last.action, CandidateAction.NO_TRADE)
        self.assertIn(CandidateReason.CONFIRMATION_WINDOW_EXPIRED, last.reasons)
        self.assertIsNone(last.proposed_entry_at)

    def test_mixed_calendar_policies_and_zone_provenance_are_rejected(self):
        calendar = calendar_fixture()
        bar = candidate_fixture.m15(0, low=100, high=105)
        update = session_update(calendar, (bar,))
        candidate, _, regime, confirmation = policies(calendar)
        other = replace(calendar, revision="another-fixture-v1")
        for mismatched in (None, other):
            with self.subTest(mismatched=mismatched):
                with self.assertRaisesRegex(CandidateDataError, "calendar"):
                    paper_candidate_decisions(
                        update, (), (), candidate_policy=replace(candidate, calendar=mismatched),
                        regime_policy=regime, confirmation_policy=confirmation,
                    )
                with self.assertRaisesRegex(CandidateDataError, "calendar"):
                    paper_candidate_decisions(
                        update, (), (), candidate_policy=candidate,
                        regime_policy=replace(regime, calendar=mismatched),
                        confirmation_policy=confirmation,
                    )

    def test_future_suffix_and_as_of_cannot_rewrite_open_bar_decision(self):
        calendar = calendar_fixture()
        bars = tuple(candidate_fixture.m15(index, low=100, high=105) for index in range(2))
        event = candidate_fixture.confirmation(bars[0], policy=policies(calendar)[3])
        prefix = decide(calendar, session_update(calendar, bars[:1]), (event,))
        full = decide(calendar, session_update(calendar, bars), (event,))
        visible = decide(calendar, session_update(calendar, bars), (event,), as_of=bars[0].timestamp)
        self.assertEqual(prefix, full[:1])
        self.assertEqual(prefix, visible)

    def test_regime_pivot_validation_counts_exact_open_hours_across_closure(self):
        calendar = calendar_fixture(closure_hour=4)
        selected = policies(calendar)[2]
        pivot = candidate_fixture.pivot(PivotKind.HIGH, BASE + 3 * H1, 110, BASE + 8 * H1)
        pivot = replace(pivot, confirmed_at=BASE + 8 * H1)
        regimes = h1_regime_events((pivot,), policy=selected)
        # No decision bars are necessary to validate the sealed regime input.
        update = session_update(calendar, (), zones=False)
        self.assertEqual(decide(calendar, update, regimes=regimes), ())
        wrong_pivot = replace(pivot, confirmed_at=BASE + 7 * H1)
        bad = replace(regimes[0], confirmed_at=wrong_pivot.confirmed_at, high_pivots=(wrong_pivot,))
        with self.assertRaisesRegex(CandidateDataError, "confirmation time"):
            decide(calendar, update, regimes=(bad,))
        # Merely reaching the right count at a closed hour end is not sufficient.
        preclose = candidate_fixture.pivot(PivotKind.HIGH, BASE, 110, BASE + 5 * H1)
        closed_confirmation = replace(preclose, confirmed_at=BASE + 5 * H1)
        bad = replace(regimes[0], structure_bar_start=BASE, structure_bar_end=BASE + H1,
                      confirmed_at=BASE + 5 * H1, high_pivots=(closed_confirmation,))
        with self.assertRaises(SessionCalendarError):
            decide(calendar, update, regimes=(bad,))


if __name__ == "__main__":
    unittest.main()
