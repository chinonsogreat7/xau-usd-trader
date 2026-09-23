"""Explicit synthetic closure fixtures, not any broker's trading schedule."""

import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta

from xau_trader.multitimeframe import H1_DURATION, M15_DURATION, MultiTimeframeDataError
from xau_trader.domain import AvailabilityBasis
from xau_trader.candidate_signals import CandidateAction
from xau_trader.diagnostic_replay import run_diagnostic_replay
from xau_trader.research_baseline import provisional_session_diagnostic_baseline_v1
from xau_trader.session_calendar import ScheduledClosure, SessionCalendarArtifact
from xau_trader.session_timing import SESSION_STRATEGY_SEMANTICS
from xau_trader.zone_lifecycle import (
    EpisodeEndReason,
    ObservationTimeframe,
    RetestEpisodeEnded,
    RetestEpisodeStarted,
    ZoneBookUpdate,
    ZoneInvalidated,
    advance_zone_book_group,
    evolve_zone_book,
    new_zone_book,
    seal_availability_group,
    select_retest_starts,
)
from test_zone_lifecycle import BASE, FORMED, observation, policy, quote_bar, zone
from session_feature_fixture import session_fixture


def calendar(*, closure_start=6, closure_end=8, revision="fixture-v1"):
    return SessionCalendarArtifact(
        calendar_id="synthetic-lifecycle",
        revision=revision,
        provider_name="Synthetic lifecycle fixture",
        provider_legal_entity="Synthetic fixture only",
        instrument="XAU_USD",
        product_form="synthetic",
        coverage_start=BASE,
        coverage_end=BASE + timedelta(hours=48),
        source_reference="local-fixture://session-lifecycle",
        retrieved_at=BASE,
        closures=(ScheduledClosure(
            start=BASE + timedelta(hours=closure_start),
            end=BASE + timedelta(hours=closure_end),
            reason="Synthetic closure for regression testing",
        ),),
    )


def bar_at(hour, *, timeframe=ObservationTimeframe.M15, low=100, high=108, close=None):
    duration = H1_DURATION if timeframe == ObservationTimeframe.H1 else M15_DURATION
    return observation(timeframe, quote_bar(
        BASE + timedelta(hours=hour), duration, low=low, high=high, close=close
    ))


def fixture_observations():
    return tuple(bar_at(hour) for hour in (5, 5.25, 5.5, 5.75, 8, 8.25)) + (
        bar_at(5, timeframe=ObservationTimeframe.H1),
    )


class SessionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.calendar = calendar()
        self.policy = replace(policy(), calendar=self.calendar)
        self.zone = replace(zone(), calendar=self.calendar)

    def evolve(self, observations, *, zones=None):
        return evolve_zone_book(
            (self.zone,) if zones is None else zones, observations, self.policy,
            h1_start=FORMED, m15_start=FORMED,
        )

    def test_calendar_identity_is_explicit_and_strict_identity_is_unchanged(self):
        strict = policy()
        self.assertNotIn("calendar", strict.canonical_identity)
        self.assertIn(SESSION_STRATEGY_SEMANTICS, self.policy.canonical_identity)
        self.assertIn(self.calendar.fingerprint, self.policy.canonical_identity)
        self.assertNotEqual(strict.fingerprint, self.policy.fingerprint)
        self.assertNotEqual(
            self.policy.fingerprint,
            replace(self.policy, calendar=calendar(revision="fixture-v2")).fingerprint,
        )

    def test_retest_episode_and_valid_zone_survive_closure_without_fake_events(self):
        update = self.evolve(fixture_observations())
        starts = tuple(event for event in update.events if isinstance(event, RetestEpisodeStarted))
        self.assertEqual(len(starts), 1)
        self.assertEqual(update.events, starts)
        state = update.book.states[0]
        self.assertTrue(state.valid)
        self.assertEqual(state.retest_episode_count, 1)
        self.assertEqual(state.active_episode.first_touch_bar_start, FORMED)
        self.assertEqual(state.active_episode.last_touch_bar_end, BASE + timedelta(hours=8.5))
        self.assertEqual(update.book.next_h1_start, BASE + timedelta(hours=8))
        self.assertEqual(update.book.next_m15_start, BASE + timedelta(hours=8.5))
        self.assertEqual(len(update.transitions), 7)
        self.assertTrue(all(item.calendar == self.calendar for item in update.transitions))
        self.assertFalse(any(
            BASE + timedelta(hours=6) <= item.observation.bar.start_time < BASE + timedelta(hours=8)
            for item in update.transitions
        ))

    def test_first_reopening_h1_close_can_invalidate_preserved_episode(self):
        observations = fixture_observations() + (
            bar_at(8.5), bar_at(8.75),
            bar_at(8, timeframe=ObservationTimeframe.H1, low=95, high=109, close=98),
        )
        update = self.evolve(observations)
        invalidations = [event for event in update.events if isinstance(event, ZoneInvalidated)]
        endings = [event for event in update.events if isinstance(event, RetestEpisodeEnded)]
        self.assertEqual(len(invalidations), 1)
        self.assertEqual(invalidations[0].h1_bar_start, BASE + timedelta(hours=8))
        self.assertEqual(endings[0].reason, EpisodeEndReason.ZONE_INVALIDATED)
        self.assertEqual(endings[0].ordinal, 1)
        self.assertFalse(update.book.states[0].valid)
        self.assertIsNone(update.book.states[0].active_episode)

    def test_reopening_non_overlap_ends_existing_episode_at_actual_bar(self):
        update = self.evolve(tuple(bar_at(hour) for hour in (5, 5.25, 5.5, 5.75)) + (
            bar_at(8, low=111, high=114), bar_at(8.25),
        ))
        endings = [event for event in update.events if isinstance(event, RetestEpisodeEnded)]
        self.assertEqual(len(endings), 1)
        self.assertEqual(endings[0].ended_by_bar_start, BASE + timedelta(hours=8))
        self.assertEqual(endings[0].last_touch_bar_end, BASE + timedelta(hours=6))
        self.assertEqual(endings[0].reason, EpisodeEndReason.PRICE_LEFT_ZONE)
        self.assertEqual(update.book.states[0].retest_episode_count, 2)

    def test_cold_start_and_delayed_formation_inside_closure_wait_for_open(self):
        book = new_zone_book(
            (self.zone,), self.policy,
            h1_start=BASE + timedelta(hours=6), m15_start=BASE + timedelta(hours=6.25),
        )
        self.assertEqual(book.next_h1_start, BASE + timedelta(hours=8))
        self.assertEqual(book.next_m15_start, BASE + timedelta(hours=8))
        self.assertEqual(book.states[0].next_h1_start, BASE + timedelta(hours=8))
        late = replace(self.zone, available_at=BASE + timedelta(hours=6, minutes=5))
        update = self.evolve(fixture_observations(), zones=(late,))
        starts = [event for event in update.events if isinstance(event, RetestEpisodeStarted)]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0].m15_bar_start, BASE + timedelta(hours=8))

    def test_impulse_window_requires_exactly_four_scheduled_open_hours(self):
        selected = calendar(closure_start=2, closure_end=4)
        end = BASE + timedelta(hours=7)
        original = zone()
        spanning = replace(
            original, calendar=selected, impulse_window_end=end, formed_at=end,
            available_at=end,
            impulse_key=original.impulse_key[:3] + (end,),
        )
        bound_policy = replace(self.policy, calendar=selected)
        book = new_zone_book((spanning,), bound_policy, h1_start=end, m15_start=end)
        self.assertEqual(book.states[0].zone, spanning)
        for wrong_end in (BASE + timedelta(hours=5), BASE + timedelta(hours=6), BASE + timedelta(hours=8)):
            malformed = replace(
                spanning, impulse_window_end=wrong_end, formed_at=wrong_end,
                available_at=wrong_end,
                impulse_key=spanning.impulse_key[:3] + (wrong_end,),
            )
            with self.assertRaisesRegex(ValueError, "four H1"):
                new_zone_book((malformed,), bound_policy, h1_start=end, m15_start=end)

    def test_finite_origin_lookback_counts_only_open_hours_and_rejects_closed_origin(self):
        selected = calendar(closure_start=2, closure_end=4)
        start, end = BASE + timedelta(hours=4), BASE + timedelta(hours=8)
        original = zone()
        lookback = replace(
            original, calendar=selected, origin_lookback_bars=1,
            origin_bar_start=BASE + timedelta(hours=1), origin_bar_end=BASE + timedelta(hours=2),
            impulse_window_start=start, impulse_window_end=end, formed_at=end, available_at=end,
            impulse_key=original.impulse_key[:2] + (start, end),
        )
        bound_policy = replace(self.policy, calendar=selected)
        new_zone_book((lookback,), bound_policy, h1_start=end, m15_start=end)
        too_old = replace(lookback, origin_bar_start=BASE, origin_bar_end=BASE + H1_DURATION)
        with self.assertRaisesRegex(ValueError, "finite lookback"):
            new_zone_book((too_old,), bound_policy, h1_start=end, m15_start=end)
        closed = replace(lookback, origin_bar_start=BASE + timedelta(hours=3), origin_bar_end=start)
        with self.assertRaisesRegex(ValueError, "closure"):
            new_zone_book((closed,), bound_policy, h1_start=end, m15_start=end)

    def test_four_open_hour_count_cannot_hide_closed_impulse_endpoint(self):
        for closure_start, closure_end, start_hour, end_hour in (
            (6, 8, 2, 8),  # Four open hours then a closure is not a confirming bar.
            (2, 4, 3, 8),  # A closed first hour cannot be an impulse source.
        ):
            selected = calendar(closure_start=closure_start, closure_end=closure_end)
            start = BASE + timedelta(hours=start_hour)
            end = BASE + timedelta(hours=end_hour)
            original = zone()
            forged = replace(
                original, calendar=selected, impulse_window_start=start,
                impulse_window_end=end, formed_at=end, available_at=end,
                impulse_key=original.impulse_key[:2] + (start, end),
            )
            with self.assertRaisesRegex(ValueError, "closure"):
                new_zone_book((forged,), replace(self.policy, calendar=selected), h1_start=end, m15_start=end)

    def test_missing_open_bar_closed_bar_and_unknown_gap_fail_even_without_zones(self):
        for zones in ((), (self.zone,)):
            for observations in (
                (bar_at(5), bar_at(5.5)),
                tuple(bar_at(hour) for hour in (5, 5.25, 5.5, 5.75, 8.25)),
                (bar_at(6),),
            ):
                with self.assertRaises(ValueError):
                    self.evolve(observations, zones=zones)
        strict_zone = replace(self.zone, calendar=replace(self.calendar, closures=()))
        strict_policy = replace(self.policy, calendar=strict_zone.calendar)
        with self.assertRaises(MultiTimeframeDataError):
            evolve_zone_book((strict_zone,), fixture_observations(), strict_policy, h1_start=FORMED, m15_start=FORMED)

    def test_mixed_calendar_groups_zones_and_empty_transitions_fail(self):
        alternative = calendar(revision="fixture-v2")
        with self.assertRaisesRegex(ValueError, "calendar"):
            new_zone_book((zone(),), self.policy, h1_start=FORMED, m15_start=FORMED)
        with self.assertRaisesRegex(ValueError, "calendar"):
            seal_availability_group((), (self.zone,), sealed_through=FORMED)
        empty = new_zone_book((), self.policy, h1_start=FORMED, m15_start=FORMED)
        observation = bar_at(5)
        wrong = seal_availability_group((observation,), (), sealed_through=observation.bar.available_at, calendar=alternative)
        with self.assertRaisesRegex(ValueError, "calendar"):
            advance_zone_book_group(empty, wrong)
        update = self.evolve((observation,), zones=())
        forged = replace(update.transitions[0], calendar=alternative)
        with self.assertRaisesRegex(ValueError, "calendar"):
            ZoneBookUpdate(book=update.book, events=(), transitions=(forged,))
        with self.assertRaisesRegex(ValueError, "calendar"):
            replace(self.evolve((observation,)).transitions[0], calendar=alternative)

    def test_batch_and_chunked_fold_match_and_snapshots_are_immutable(self):
        observations = fixture_observations()
        batch = self.evolve(observations)
        book = new_zone_book((), self.policy, h1_start=FORMED, m15_start=FORMED)
        groups = {FORMED: [[], [self.zone]]}
        for item in observations:
            groups.setdefault(item.bar.available_at, [[], []])[0].append(item)
        events, transitions, snapshots = [], [], []
        for available_at, (items, zones) in sorted(groups.items()):
            snapshots.append(book)
            update = advance_zone_book_group(book, seal_availability_group(
                items, zones, sealed_through=available_at, calendar=self.calendar,
            ))
            book = update.book
            events.extend(update.events)
            transitions.extend(update.transitions)
        self.assertEqual(ZoneBookUpdate(book, tuple(events), tuple(transitions)), batch)
        self.assertEqual(snapshots[0].states, ())
        self.assertEqual(snapshots[1].states[0].retest_episode_count, 0)
        with self.assertRaises(FrozenInstanceError):
            book.next_m15_start = FORMED

    def test_forged_closed_anchor_touch_and_coverage_overrun_fail(self):
        update = self.evolve((bar_at(5),))
        with self.assertRaisesRegex(ValueError, "closure"):
            replace(update.book.states[0], next_m15_start=BASE + timedelta(hours=6))
        forged_book = replace(update.book, next_m15_start=BASE + timedelta(hours=6))
        with self.assertRaisesRegex(ValueError, "closure"):
            advance_zone_book_group(forged_book, seal_availability_group(
                (bar_at(5.25),), (), sealed_through=BASE + timedelta(hours=5.5), calendar=self.calendar,
            ))
        forged_event = replace(update.events[0], m15_bar_start=BASE + timedelta(hours=6), m15_bar_end=BASE + timedelta(hours=6.25), available_at=BASE + timedelta(hours=6.25))
        with self.assertRaisesRegex(ValueError, "closure"):
            select_retest_starts((forged_event,), kind=self.zone.kind, policy=self.policy)
        with self.assertRaisesRegex(ValueError, "coverage"):
            new_zone_book((), self.policy, h1_start=BASE + timedelta(hours=49), m15_start=BASE + timedelta(hours=49))

    def test_empty_state_ledger_still_rejects_omitted_open_bars_and_forged_final_anchor(self):
        update = self.evolve(tuple(bar_at(hour) for hour in (5, 5.25, 5.5)), zones=())
        with self.assertRaisesRegex(ValueError, "omitted an open bar"):
            replace(update, transitions=(update.transitions[0], update.transitions[2]))
        with self.assertRaisesRegex(ValueError, "cadence conflicts"):
            replace(update, book=replace(update.book, next_m15_start=BASE + timedelta(hours=8)))

    def test_formation_receipt_after_coverage_is_untouched_at_exhausted_sentinel(self):
        late = replace(self.zone, available_at=self.calendar.coverage_end + timedelta(seconds=5))
        update = self.evolve(fixture_observations(), zones=(late,))
        self.assertEqual(update.events, ())
        self.assertEqual(update.book.states[0].next_h1_start, self.calendar.coverage_end)
        self.assertEqual(update.book.states[0].next_m15_start, self.calendar.coverage_end)
        self.assertEqual(update.book.states[0].retest_episode_count, 0)
        self.assertEqual(update.book.last_group_available_at, late.available_at)
        self.assertLess(update.book.next_m15_start, late.available_at)
        with self.assertRaisesRegex(ValueError, "future zone"):
            new_zone_book((late,), self.policy, h1_start=FORMED, m15_start=FORMED)
        with self.assertRaisesRegex(ValueError, "coverage"):
            seal_availability_group(
                (bar_at(48),), (), sealed_through=BASE + timedelta(hours=48.25),
                calendar=self.calendar,
            )

    def test_delayed_full_replay_handles_arbitrary_partial_calendar_tail(self):
        source, original_calendar = session_fixture()
        for tail_minutes in (7, 15, 30):
            with self.subTest(tail_minutes=tail_minutes):
                selected = replace(
                    original_calendar,
                    coverage_end=original_calendar.coverage_end + timedelta(minutes=tail_minutes),
                )
                receipt = selected.coverage_end + timedelta(hours=2)
                bars = tuple(replace(
                    item, available_at=receipt,
                    availability_basis=AvailabilityBasis.OBSERVED_RECEIPT,
                ) for item in source)
                replay = run_diagnostic_replay(
                    bars, dataset_fingerprint="1" * 64,
                    policy_bundle=provisional_session_diagnostic_baseline_v1(selected),
                )
                self.assertEqual(len(replay.decisions), 184)
                self.assertGreater(len(replay.zones), 0)
                self.assertEqual(replay.lifecycle.events, ())
                self.assertTrue(all(item.action == CandidateAction.NO_TRADE for item in replay.decisions))
                exhausted_h1 = selected.coverage_end.replace(minute=0, second=0, microsecond=0)
                exhausted_m15 = selected.coverage_end.replace(
                    minute=(selected.coverage_end.minute // 15) * 15, second=0, microsecond=0,
                )
                for state in replay.lifecycle.book.states:
                    self.assertEqual(state.zone.available_at, receipt)
                    self.assertEqual(state.next_h1_start, exhausted_h1)
                    self.assertEqual(state.next_m15_start, exhausted_m15)
                    self.assertEqual(state.retest_episode_count, 0)
                    self.assertIsNone(state.active_episode)
                    self.assertLess(state.next_h1_start, state.zone.available_at)
                    self.assertLess(state.next_m15_start, state.zone.available_at)
                for start, duration in ((exhausted_h1, H1_DURATION), (exhausted_m15, M15_DURATION)):
                    with self.assertRaisesRegex(ValueError, "coverage"):
                        selected.validate_bar(start, start + duration)
                with self.assertRaisesRegex(ValueError, "coverage"):
                    new_zone_book(
                        (), replay.policy_bundle.lifecycle_policy,
                        h1_start=exhausted_h1 + timedelta(hours=3),
                        m15_start=exhausted_h1 + timedelta(hours=3),
                    )


if __name__ == "__main__":
    unittest.main()
