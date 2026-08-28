import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

from xau_trader.domain import AvailabilityBasis, QuoteBar
from xau_trader.multitimeframe import MultiTimeframeDataError
from xau_trader.supply_demand import (
    ImpulseDirection,
    OriginSelectionPolicy,
    ZoneEvent,
    ZoneKind,
)
from xau_trader.zone_lifecycle import (
    CompletedBarObservation,
    EpisodeEndReason,
    EqualTimeOrder,
    ObservationTimeframe,
    OverlapSelection,
    RetestEpisodeEnded,
    RetestEpisodeStarted,
    RetestPolicy,
    TouchEpisodePolicy,
    ZoneInvalidated,
    ZoneLifecyclePolicy,
    ZonePriceBasis,
    advance_zone_book_group,
    evolve_zone_book,
    new_zone_book,
    order_observations,
    seal_availability_group,
    select_retest_starts,
)


UTC = timezone.utc
BASE = datetime(2026, 1, 5, tzinfo=UTC)
FORMED = BASE + timedelta(hours=5)


def policy(
    *,
    retest=RetestPolicy.FIRST_TOUCH_ONLY,
    order=EqualTimeOrder.H1_INVALIDATION_THEN_M15_TOUCH,
    selection=OverlapSelection.NEWEST_ORIGIN_THEN_ZONE_KEY,
):
    return ZoneLifecyclePolicy(
        retest_policy=retest,
        touch_episode=TouchEpisodePolicy.CONTIGUOUS_INCLUSIVE_OVERLAP,
        equal_time_order=order,
        overlap_selection=selection,
        price_basis=ZonePriceBasis.MID,
    )


def zone(
    *,
    kind=ZoneKind.DEMAND,
    origin_hour=0,
    lower=100.0,
    upper=110.0,
    available_at=FORMED,
    policy_fingerprint="1" * 64,
):
    origin_start = BASE + timedelta(hours=origin_hour)
    impulse_window_start = FORMED - timedelta(hours=4)
    origin_policy = (
        OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW
        if origin_start + timedelta(hours=1) <= impulse_window_start
        else OriginSelectionPolicy.LAST_OPPOSITE_AT_OR_BEFORE_WINDOW_END
    )
    direction = (
        ImpulseDirection.BULLISH
        if kind == ZoneKind.DEMAND
        else ImpulseDirection.BEARISH
    )
    return ZoneEvent(
        kind=kind,
        origin_policy=origin_policy,
        origin_lookback_bars=20,
        origin_bar_start=origin_start,
        origin_bar_end=origin_start + timedelta(hours=1),
        impulse_window_start=impulse_window_start,
        impulse_window_end=FORMED,
        formed_at=FORMED,
        available_at=available_at,
        availability_basis=AvailabilityBasis.SYNTHETIC,
        lower_price=lower,
        upper_price=upper,
        impulse_key=(
            policy_fingerprint,
            direction,
            impulse_window_start,
            FORMED,
        ),
    )


def quote_bar(start, duration, *, low, high, close=None, available_at=None):
    middle = (low + high) / 2.0 if close is None else close
    end = start + duration
    return QuoteBar.from_mid(
        start_time=start,
        timestamp=end,
        available_at=end if available_at is None else available_at,
        availability_basis=AvailabilityBasis.SYNTHETIC,
        mid_open=middle,
        mid_high=high,
        mid_low=low,
        mid_close=middle,
        spread=0.2,
        volume=1.0,
    )


def m15(index, *, low, high, close=None, available_at=None):
    return quote_bar(
        FORMED + timedelta(minutes=15 * index),
        timedelta(minutes=15),
        low=low,
        high=high,
        close=close,
        available_at=available_at,
    )


def h1(index, *, low, high, close, available_at=None):
    return quote_bar(
        FORMED + timedelta(hours=index),
        timedelta(hours=1),
        low=low,
        high=high,
        close=close,
        available_at=available_at,
    )


def observation(timeframe, bar):
    return CompletedBarObservation(timeframe=timeframe, bar=bar)


def make_book(
    zones,
    selected_policy,
    *,
    h1_start=FORMED,
    m15_start=FORMED,
):
    return new_zone_book(
        zones,
        selected_policy,
        h1_start=h1_start,
        m15_start=m15_start,
    )


def evolve(
    zones,
    observations,
    selected_policy,
    *,
    h1_start=FORMED,
    m15_start=FORMED,
):
    return evolve_zone_book(
        zones,
        observations,
        selected_policy,
        h1_start=h1_start,
        m15_start=m15_start,
    )


def advance_group(book, *observations, formations=()):
    availability_values = tuple(
        item.bar.available_at for item in observations
    ) + tuple(item.available_at for item in formations)
    sealed = seal_availability_group(
        observations,
        formations,
        sealed_through=availability_values[0],
    )
    return advance_zone_book_group(
        book,
        sealed,
    )


class RequiredPolicyTests(unittest.TestCase):
    def test_policy_has_no_silent_defaults_and_rejects_strings(self):
        with self.assertRaises(TypeError):
            ZoneLifecyclePolicy()  # type: ignore
        with self.assertRaisesRegex(TypeError, "retest_policy"):
            ZoneLifecyclePolicy(
                retest_policy="first_touch_only",  # type: ignore
                touch_episode=TouchEpisodePolicy.CONTIGUOUS_INCLUSIVE_OVERLAP,
                equal_time_order=EqualTimeOrder.H1_INVALIDATION_THEN_M15_TOUCH,
                overlap_selection=OverlapSelection.ALL_ELIGIBLE,
                price_basis=ZonePriceBasis.MID,
            )

    def test_complete_lifecycle_policy_identity_is_bound_to_the_book(self):
        selected = policy()
        alternative = policy(
            order=EqualTimeOrder.M15_TOUCH_THEN_H1_INVALIDATION
        )

        self.assertEqual(len(selected.fingerprint), 64)
        self.assertNotEqual(selected.fingerprint, alternative.fingerprint)
        self.assertEqual(
            make_book((zone(),), selected).policy_fingerprint,
            selected.fingerprint,
        )

    def test_zone_provenance_and_geometry_are_revalidated_at_admission(self):
        valid = zone()
        malformed = (
            (replace(valid, origin_policy="before"), "origin_policy"),
            (replace(valid, origin_lookback_bars=True), "origin_lookback_bars"),
            (replace(valid, origin_lookback_bars=0), "origin_lookback_bars"),
            (replace(valid, availability_basis="synthetic"), "availability_basis"),
            (
                replace(
                    valid,
                    origin_bar_start=valid.origin_bar_start + timedelta(minutes=30),
                ),
                "origin",
            ),
            (
                replace(
                    valid,
                    origin_bar_start=valid.origin_bar_start + timedelta(hours=1),
                    origin_bar_end=valid.origin_bar_end + timedelta(hours=1),
                ),
                "before-window origin",
            ),
            (
                replace(
                    valid,
                    impulse_window_start=valid.impulse_window_start
                    + timedelta(hours=1),
                ),
                "impulse window",
            ),
            (
                replace(
                    valid,
                    origin_lookback_bars=1,
                    origin_bar_start=valid.origin_bar_start
                    - timedelta(hours=1),
                    origin_bar_end=valid.origin_bar_end - timedelta(hours=1),
                ),
                "finite lookback",
            ),
        )

        for candidate, message in malformed:
            with self.subTest(message=message):
                with self.assertRaisesRegex((TypeError, ValueError), message):
                    make_book((candidate,), policy())


class RetestEpisodeTests(unittest.TestCase):
    def test_first_observation_must_match_explicit_stream_anchor(self):
        book = make_book((zone(),), policy())

        with self.assertRaisesRegex(MultiTimeframeDataError, "cadence anchor"):
            advance_group(
                book,
                observation(
                    ObservationTimeframe.M15,
                    m15(1, low=100.0, high=105.0),
                ),
            )

        self.assertEqual(book.next_m15_start, FORMED)
        self.assertEqual(book.states[0].retest_episode_count, 0)

    def test_contiguous_overlaps_are_one_episode_and_boundary_contact_counts(self):
        observations = (
            observation(ObservationTimeframe.M15, m15(0, low=95.0, high=100.0)),
            observation(ObservationTimeframe.M15, m15(1, low=105.0, high=115.0)),
            observation(ObservationTimeframe.M15, m15(2, low=106.0, high=112.0)),
        )

        update = evolve((zone(),), observations, policy())

        starts = tuple(
            event for event in update.events if isinstance(event, RetestEpisodeStarted)
        )
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0].ordinal, 1)
        self.assertTrue(starts[0].eligible)
        state = update.book.states[0]
        self.assertEqual(state.retest_episode_count, 1)
        self.assertEqual(
            state.active_episode.last_touch_bar_end,
            m15(2, low=106.0, high=112.0).timestamp,
        )

    def test_non_overlap_ends_episode_and_later_overlap_starts_second(self):
        observations = tuple(
            observation(ObservationTimeframe.M15, bar)
            for bar in (
                m15(0, low=105.0, high=115.0),
                m15(1, low=106.0, high=114.0),
                m15(2, low=90.0, high=99.0),
                m15(3, low=100.0, high=102.0),
            )
        )

        update = evolve((zone(),), observations, policy())

        starts = tuple(
            event for event in update.events if isinstance(event, RetestEpisodeStarted)
        )
        ends = tuple(
            event for event in update.events if isinstance(event, RetestEpisodeEnded)
        )
        self.assertEqual(tuple(event.ordinal for event in starts), (1, 2))
        self.assertEqual(tuple(event.eligible for event in starts), (True, False))
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0].reason, EpisodeEndReason.PRICE_LEFT_ZONE)
        self.assertEqual(update.book.states[0].active_episode.ordinal, 2)

    def test_allow_one_prior_makes_only_first_two_episodes_eligible(self):
        observations = tuple(
            observation(ObservationTimeframe.M15, bar)
            for bar in (
                m15(0, low=100.0, high=105.0),
                m15(1, low=90.0, high=99.0),
                m15(2, low=100.0, high=105.0),
                m15(3, low=90.0, high=99.0),
                m15(4, low=100.0, high=105.0),
            )
        )

        update = evolve(
            (zone(),), observations, policy(retest=RetestPolicy.ALLOW_ONE_PRIOR)
        )
        starts = tuple(
            event for event in update.events if isinstance(event, RetestEpisodeStarted)
        )
        self.assertEqual(tuple(event.eligible for event in starts), (True, True, False))
        self.assertEqual(update.book.states[0].retest_episode_count, 3)

    def test_gap_and_duplicate_fail_closed_without_guessing_an_episode(self):
        book = make_book((zone(),), policy())
        first = advance_group(
            book,
            observation(ObservationTimeframe.M15, m15(0, low=100.0, high=105.0)),
        ).book
        with self.assertRaisesRegex(MultiTimeframeDataError, "cadence anchor"):
            advance_group(
                first,
                observation(ObservationTimeframe.M15, m15(2, low=100.0, high=105.0)),
            )
        with self.assertRaisesRegex(MultiTimeframeDataError, "watermark"):
            advance_group(
                first,
                observation(ObservationTimeframe.M15, m15(0, low=100.0, high=105.0)),
            )


class InvalidationTests(unittest.TestCase):
    def test_demand_and_supply_invalidation_are_strict_and_mirrored(self):
        demand_book = make_book((zone(),), policy())
        equality = advance_group(
            demand_book,
            observation(
                ObservationTimeframe.H1,
                h1(0, low=95.0, high=105.0, close=100.0),
            ),
        )
        self.assertTrue(equality.book.states[0].valid)
        breach = advance_group(
            equality.book,
            observation(
                ObservationTimeframe.H1,
                h1(1, low=90.0, high=102.0, close=99.9),
            ),
        )
        self.assertFalse(breach.book.states[0].valid)
        self.assertIsInstance(breach.events[0], ZoneInvalidated)

        supply_book = make_book((zone(kind=ZoneKind.SUPPLY),), policy())
        supply_equal = advance_group(
            supply_book,
            observation(
                ObservationTimeframe.H1,
                h1(0, low=105.0, high=115.0, close=110.0),
            ),
        )
        self.assertTrue(supply_equal.book.states[0].valid)
        supply_breach = advance_group(
            supply_equal.book,
            observation(
                ObservationTimeframe.H1,
                h1(1, low=108.0, high=116.0, close=110.1),
            ),
        )
        self.assertFalse(supply_breach.book.states[0].valid)

    def test_invalidation_is_absorbing_and_ends_an_active_episode(self):
        book = make_book((zone(),), policy())
        touched = advance_group(
            book,
            observation(ObservationTimeframe.M15, m15(0, low=100.0, high=105.0)),
        ).book
        invalidated = advance_group(
            touched,
            observation(
                ObservationTimeframe.H1,
                h1(0, low=90.0, high=105.0, close=99.0),
            ),
        )

        self.assertEqual(
            tuple(type(event) for event in invalidated.events),
            (ZoneInvalidated, RetestEpisodeEnded),
        )
        self.assertEqual(
            invalidated.events[1].reason, EpisodeEndReason.ZONE_INVALIDATED
        )
        self.assertIsNone(invalidated.book.states[0].active_episode)
        later = advance_group(
            invalidated.book,
            observation(
                ObservationTimeframe.M15,
                m15(
                    1,
                    low=100.0,
                    high=105.0,
                    available_at=FORMED + timedelta(hours=1, minutes=1),
                ),
            ),
        )
        self.assertEqual(later.events, ())
        self.assertFalse(later.book.states[0].valid)


class PointInTimeOrderingTests(unittest.TestCase):
    def test_future_zone_is_admitted_dynamically_and_skips_partial_bar(self):
        future_zone = zone(
            origin_hour=1, available_at=FORMED + timedelta(minutes=30)
        )
        with self.assertRaisesRegex(ValueError, "future zone"):
            make_book((future_zone,), policy())

        book = make_book((), policy())
        before = advance_group(
            book,
            observation(
                ObservationTimeframe.M15,
                m15(0, low=100.0, high=105.0),
            ),
        ).book
        admitted = advance_group(
            before,
            observation(
                ObservationTimeframe.M15,
                m15(1, low=100.0, high=105.0),
            ),
            formations=(future_zone,),
        )
        self.assertEqual(admitted.events, ())
        self.assertEqual(admitted.book.states[0].retest_episode_count, 0)

        first_full_bar = advance_group(
            admitted.book,
            observation(
                ObservationTimeframe.M15,
                m15(2, low=100.0, high=105.0),
            )
        )
        self.assertIsInstance(first_full_bar.events[0], RetestEpisodeStarted)

    def _same_close_observations(self, *, h1_available=None):
        end = FORMED + timedelta(hours=1)
        return (
            observation(
                ObservationTimeframe.H1,
                h1(
                    0,
                    low=90.0,
                    high=105.0,
                    close=99.0,
                    available_at=end if h1_available is None else h1_available,
                ),
            ),
            observation(
                ObservationTimeframe.M15,
                m15(3, low=100.0, high=105.0, available_at=end),
            ),
        )

    def test_h1_first_tie_invalidates_before_m15_can_retest(self):
        h1_observation, m15_observation = self._same_close_observations()
        selected_policy = policy(
            order=EqualTimeOrder.H1_INVALIDATION_THEN_M15_TOUCH
        )
        ordered = order_observations(
            (m15_observation, h1_observation), selected_policy
        )
        self.assertEqual(
            tuple(item.timeframe for item in ordered),
            (ObservationTimeframe.H1, ObservationTimeframe.M15),
        )

        update = evolve(
            (zone(),),
            ordered,
            selected_policy,
            m15_start=FORMED + timedelta(minutes=45),
        )
        self.assertEqual(
            tuple(type(event) for event in update.events), (ZoneInvalidated,)
        )
        self.assertEqual(
            tuple(
                transition.observation.timeframe
                for transition in update.transitions
            ),
            (ObservationTimeframe.H1, ObservationTimeframe.M15),
        )
        h1_transition, m15_transition = update.transitions
        self.assertTrue(h1_transition.states_before[0].valid)
        self.assertFalse(h1_transition.states_after[0].valid)
        self.assertEqual(
            tuple(type(event) for event in h1_transition.events),
            (ZoneInvalidated,),
        )
        self.assertEqual(m15_transition.states_before, h1_transition.states_after)
        self.assertFalse(m15_transition.states_before[0].valid)
        self.assertFalse(m15_transition.states_after[0].valid)
        self.assertEqual(m15_transition.events, ())

    def test_atomic_tie_result_is_independent_of_caller_order(self):
        h1_observation, m15_observation = self._same_close_observations()
        selected_policy = policy(
            order=EqualTimeOrder.H1_INVALIDATION_THEN_M15_TOUCH
        )
        first_book = make_book(
            (zone(),),
            selected_policy,
            m15_start=FORMED + timedelta(minutes=45),
        )
        second_book = make_book(
            (zone(),),
            selected_policy,
            m15_start=FORMED + timedelta(minutes=45),
        )

        reversed_input = advance_zone_book_group(
            first_book,
            seal_availability_group(
                (m15_observation, h1_observation),
                (),
                sealed_through=m15_observation.bar.available_at,
            ),
        )
        forward_input = advance_zone_book_group(
            second_book,
            seal_availability_group(
                (h1_observation, m15_observation),
                (),
                sealed_through=m15_observation.bar.available_at,
            ),
        )

        self.assertEqual(reversed_input, forward_input)
        self.assertEqual(
            tuple(type(event) for event in reversed_input.events),
            (ZoneInvalidated,),
        )

    def test_transition_requires_an_explicit_complete_group_witness(self):
        h1_observation = observation(
            ObservationTimeframe.H1,
            h1(
                0,
                low=90.0,
                high=105.0,
                close=99.0,
                available_at=FORMED + timedelta(hours=1, minutes=15),
            ),
        )
        m15_observation = observation(
            ObservationTimeframe.M15,
            m15(
                4,
                low=100.0,
                high=105.0,
                available_at=FORMED + timedelta(hours=1, minutes=15),
            ),
        )
        book = make_book(
            (zone(),),
            policy(),
            m15_start=FORMED + timedelta(hours=1),
        )

        with self.assertRaisesRegex(TypeError, "SealedAvailabilityGroup"):
            advance_zone_book_group(
                book,
                (m15_observation,),  # type: ignore
            )
        with self.assertRaisesRegex(ValueError, "sealed_through"):
            seal_availability_group(
                (m15_observation,),
                (),
                sealed_through=m15_observation.bar.available_at
                - timedelta(microseconds=1),
            )

        atomic = advance_zone_book_group(
            book,
            seal_availability_group(
                (m15_observation, h1_observation),
                (),
                sealed_through=m15_observation.bar.available_at,
            ),
        )

        self.assertTrue(book.states[0].valid)
        self.assertEqual(book.states[0].retest_episode_count, 0)
        self.assertEqual(
            tuple(type(event) for event in atomic.events),
            (ZoneInvalidated,),
        )

    def test_m15_first_tie_records_touch_then_invalidation(self):
        h1_observation, m15_observation = self._same_close_observations()
        selected_policy = policy(
            order=EqualTimeOrder.M15_TOUCH_THEN_H1_INVALIDATION
        )
        ordered = order_observations(
            (h1_observation, m15_observation), selected_policy
        )
        update = evolve(
            (zone(),),
            ordered,
            selected_policy,
            m15_start=FORMED + timedelta(minutes=45),
        )

        self.assertEqual(
            tuple(type(event) for event in update.events),
            (RetestEpisodeStarted, ZoneInvalidated, RetestEpisodeEnded),
        )
        self.assertEqual(
            tuple(
                transition.observation.timeframe
                for transition in update.transitions
            ),
            (ObservationTimeframe.M15, ObservationTimeframe.H1),
        )
        m15_transition, h1_transition = update.transitions
        self.assertTrue(m15_transition.states_before[0].valid)
        self.assertIsNone(m15_transition.states_before[0].active_episode)
        self.assertTrue(m15_transition.states_after[0].valid)
        self.assertIsNotNone(m15_transition.states_after[0].active_episode)
        self.assertEqual(
            tuple(type(event) for event in m15_transition.events),
            (RetestEpisodeStarted,),
        )
        self.assertEqual(h1_transition.states_before, m15_transition.states_after)
        self.assertFalse(h1_transition.states_after[0].valid)
        self.assertIsNone(h1_transition.states_after[0].active_episode)
        self.assertEqual(
            tuple(type(event) for event in h1_transition.events),
            (ZoneInvalidated, RetestEpisodeEnded),
        )

    def test_availability_dominates_same_bar_end_without_retroactive_erasure(self):
        end = FORMED + timedelta(hours=1)
        h1_observation, m15_observation = self._same_close_observations(
            h1_available=end + timedelta(minutes=7)
        )
        selected_policy = policy(
            order=EqualTimeOrder.H1_INVALIDATION_THEN_M15_TOUCH
        )
        ordered = order_observations(
            (h1_observation, m15_observation), selected_policy
        )
        self.assertEqual(ordered[0].timeframe, ObservationTimeframe.M15)
        update = evolve(
            (zone(),),
            ordered,
            selected_policy,
            m15_start=FORMED + timedelta(minutes=45),
        )
        self.assertEqual(
            tuple(type(event) for event in update.events),
            (RetestEpisodeStarted, ZoneInvalidated, RetestEpisodeEnded),
        )
        self.assertLess(update.events[0].available_at, update.events[1].available_at)

    def test_h1_bar_that_began_before_zone_visibility_cannot_invalidate(self):
        delayed_zone = zone(available_at=FORMED + timedelta(minutes=7))
        observations = (
            observation(
                ObservationTimeframe.H1,
                h1(0, low=90.0, high=105.0, close=99.0),
            ),
            observation(
                ObservationTimeframe.H1,
                h1(1, low=90.0, high=105.0, close=99.0),
            ),
        )

        update = evolve((delayed_zone,), observations, policy())

        invalidations = tuple(
            event for event in update.events if isinstance(event, ZoneInvalidated)
        )
        self.assertEqual(len(invalidations), 1)
        self.assertEqual(invalidations[0].h1_bar_start, FORMED + timedelta(hours=1))

    def test_future_zone_suffix_does_not_change_prefix_events(self):
        original_zone = zone(policy_fingerprint="a" * 64)
        future_zone = zone(
            origin_hour=1,
            available_at=FORMED + timedelta(hours=1),
            policy_fingerprint="b" * 64,
        )
        prefix_observations = tuple(
            observation(
                ObservationTimeframe.M15,
                m15(index, low=100.0, high=105.0),
            )
            for index in range(3)
        )
        full_observations = tuple(
            observation(
                ObservationTimeframe.M15,
                m15(index, low=100.0, high=105.0),
            )
            for index in range(5)
        )

        prefix = evolve((original_zone,), prefix_observations, policy())
        full = evolve(
            (original_zone, future_zone), full_observations, policy()
        )

        cutoff = prefix_observations[-1].bar.available_at
        self.assertEqual(
            tuple(event for event in full.events if event.available_at <= cutoff),
            prefix.events,
        )


class TransitionLedgerTests(unittest.TestCase):
    def test_transition_events_aggregate_in_canonical_step_and_zone_order(self):
        demand = zone(kind=ZoneKind.DEMAND, policy_fingerprint="a" * 64)
        supply = zone(
            kind=ZoneKind.SUPPLY,
            origin_hour=1,
            policy_fingerprint="b" * 64,
        )
        selected_policy = policy(selection=OverlapSelection.ALL_ELIGIBLE)
        update = evolve(
            (supply, demand),
            (
                observation(
                    ObservationTimeframe.M15,
                    m15(0, low=104.0, high=106.0),
                ),
                observation(
                    ObservationTimeframe.M15,
                    m15(1, low=90.0, high=99.0),
                ),
            ),
            selected_policy,
        )

        self.assertEqual(len(update.transitions), 2)
        first, second = update.transitions
        self.assertEqual(second.states_before, first.states_after)
        self.assertEqual(
            update.events,
            first.events + second.events,
        )
        self.assertEqual(
            tuple(type(event) for event in update.events),
            (
                RetestEpisodeStarted,
                RetestEpisodeStarted,
                RetestEpisodeEnded,
                RetestEpisodeEnded,
            ),
        )
        canonical_zone_keys = tuple(state.zone.key for state in first.states_before)
        self.assertEqual(
            tuple(event.zone_key for event in first.events),
            canonical_zone_keys,
        )
        self.assertEqual(
            tuple(event.zone_key for event in second.events),
            canonical_zone_keys,
        )

        reversed_transitions = tuple(reversed(update.transitions))
        reversed_events = tuple(
            event
            for transition in reversed_transitions
            for event in transition.events
        )
        with self.assertRaisesRegex(ValueError, "canonical observation order"):
            replace(
                update,
                transitions=reversed_transitions,
                events=reversed_events,
            )

    def test_snapshots_are_immutable_and_remain_exact_after_later_updates(self):
        update = evolve(
            (zone(),),
            (
                observation(
                    ObservationTimeframe.M15,
                    m15(0, low=100.0, high=105.0),
                ),
                observation(
                    ObservationTimeframe.M15,
                    m15(1, low=101.0, high=106.0),
                ),
            ),
            policy(),
        )
        first, second = update.transitions
        first_after = first.states_after[0]
        second_after = second.states_after[0]

        self.assertEqual(second.states_before, first.states_after)
        self.assertEqual(
            first_after.active_episode.last_touch_bar_end,
            m15(0, low=100.0, high=105.0).timestamp,
        )
        self.assertEqual(
            second_after.active_episode.last_touch_bar_end,
            m15(1, low=101.0, high=106.0).timestamp,
        )
        self.assertNotEqual(first_after, second_after)
        with self.assertRaises(FrozenInstanceError):
            first.sealed_through = first.sealed_through + timedelta(seconds=1)
        with self.assertRaises(TypeError):
            first.states_after[0] = second_after

    def test_exact_replay_and_event_aggregation_reject_forged_ledger(self):
        update = evolve(
            (zone(),),
            (
                observation(
                    ObservationTimeframe.M15,
                    m15(0, low=100.0, high=105.0),
                ),
            ),
            policy(),
        )
        transition = update.transitions[0]
        forged_transition = replace(
            transition,
            states_after=transition.states_before,
            events=(),
        )
        with self.assertRaisesRegex(ValueError, "exact policy replay"):
            replace(
                update,
                transitions=(forged_transition,),
                events=(),
            )
        with self.assertRaisesRegex(ValueError, "exactly aggregate"):
            replace(update, events=())

    def test_transition_policy_event_and_watermark_consistency_fail_closed(self):
        update = evolve(
            (zone(),),
            (
                observation(
                    ObservationTimeframe.M15,
                    m15(0, low=100.0, high=105.0),
                ),
            ),
            policy(),
        )
        transition = update.transitions[0]
        with self.assertRaisesRegex(ValueError, "state policy fingerprint"):
            replace(transition, policy_fingerprint="f" * 64)
        forged_event = replace(
            transition.events[0], policy_fingerprint="f" * 64
        )
        with self.assertRaisesRegex(ValueError, "event policy fingerprint"):
            replace(transition, events=(forged_event,))
        with self.assertRaisesRegex(ValueError, "watermark cannot precede"):
            replace(
                transition,
                sealed_through=transition.observation.bar.available_at
                - timedelta(microseconds=1),
            )

        future_watermark = replace(
            transition,
            sealed_through=update.book.sealed_through + timedelta(seconds=1),
        )
        with self.assertRaisesRegex(ValueError, "exceeds the final book watermark"):
            replace(update, transitions=(future_watermark,))


class MultipleZoneTests(unittest.TestCase):
    def test_selector_rejects_policy_or_eligibility_reinterpretation(self):
        update = evolve(
            (zone(),),
            (
                observation(
                    ObservationTimeframe.M15,
                    m15(0, low=104.0, high=106.0),
                ),
            ),
            policy(),
        )
        start = next(
            event
            for event in update.events
            if isinstance(event, RetestEpisodeStarted)
        )
        self.assertEqual(start.policy_fingerprint, policy().fingerprint)

        forged_ineligible = replace(start, ordinal=3, eligible=True)
        with self.assertRaisesRegex(ValueError, "eligibility"):
            select_retest_starts(
                (forged_ineligible,), kind=ZoneKind.DEMAND, policy=policy()
            )
        forged_false = replace(start, eligible=False)
        with self.assertRaisesRegex(ValueError, "eligibility"):
            select_retest_starts(
                (forged_false,), kind=ZoneKind.DEMAND, policy=policy()
            )
        with self.assertRaisesRegex(ValueError, "policy fingerprint"):
            select_retest_starts(
                (start,),
                kind=ZoneKind.DEMAND,
                policy=policy(retest=RetestPolicy.ALLOW_ONE_PRIOR),
            )

    def test_forged_book_order_and_state_policy_fail_before_advance(self):
        first = zone(policy_fingerprint="a" * 64)
        second = zone(policy_fingerprint="b" * 64)
        book = make_book((first, second), policy())
        forged_order = replace(book, states=tuple(reversed(book.states)))

        with self.assertRaisesRegex(ValueError, "canonical"):
            advance_group(
                forged_order,
                observation(
                    ObservationTimeframe.M15,
                    m15(0, low=104.0, high=106.0),
                ),
            )

        touched = advance_group(
            make_book((first,), policy()),
            observation(
                ObservationTimeframe.M15,
                m15(0, low=104.0, high=106.0),
            ),
        ).book
        forged_episode = replace(
            touched.states[0].active_episode, eligible=False
        )
        forged_state = replace(
            touched.states[0], active_episode=forged_episode
        )
        forged_book = replace(touched, states=(forged_state,))
        with self.assertRaisesRegex(ValueError, "eligibility"):
            advance_group(
                forged_book,
                observation(
                    ObservationTimeframe.M15,
                    m15(1, low=104.0, high=106.0),
                ),
            )

    def test_full_impulse_policy_fingerprint_prevents_cross_policy_collision(self):
        first_policy_zone = zone(policy_fingerprint="a" * 64)
        second_policy_zone = zone(policy_fingerprint="b" * 64)

        book = make_book(
            (second_policy_zone, first_policy_zone), policy()
        )

        self.assertEqual(len(book.states), 2)
        self.assertEqual(
            tuple(state.zone.impulse_key[0] for state in book.states),
            ("a" * 64, "b" * 64),
        )
        update = advance_group(
            book,
            observation(
                ObservationTimeframe.M15,
                m15(0, low=104.0, high=106.0),
            ),
        )
        selected = select_retest_starts(
            tuple(
                event
                for event in update.events
                if isinstance(event, RetestEpisodeStarted)
            ),
            kind=ZoneKind.DEMAND,
            policy=policy(),
        )
        self.assertEqual(selected[0].zone.impulse_key[0], "a" * 64)

    def test_one_bar_updates_all_zones_then_selection_uses_newest_origin(self):
        older = zone(origin_hour=0, lower=100.0, upper=110.0)
        newer = zone(origin_hour=1, lower=102.0, upper=108.0)
        selected_policy = policy()
        update = evolve(
            (newer, older),
            (
                observation(
                    ObservationTimeframe.M15,
                    m15(0, low=104.0, high=106.0),
                ),
            ),
            selected_policy,
        )

        starts = tuple(
            event for event in update.events if isinstance(event, RetestEpisodeStarted)
        )
        self.assertEqual(len(starts), 2)
        self.assertEqual(
            tuple(state.retest_episode_count for state in update.book.states), (1, 1)
        )
        selected = select_retest_starts(
            starts,
            kind=ZoneKind.DEMAND,
            policy=selected_policy,
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].zone.origin_bar_end, newer.origin_bar_end)

    def test_selection_is_side_specific_and_all_mode_is_deterministic(self):
        demand = zone(kind=ZoneKind.DEMAND, origin_hour=0)
        supply = zone(kind=ZoneKind.SUPPLY, origin_hour=1)
        update = evolve(
            (supply, demand),
            (
                observation(
                    ObservationTimeframe.M15,
                    m15(0, low=104.0, high=106.0),
                ),
            ),
            policy(selection=OverlapSelection.ALL_ELIGIBLE),
        )
        starts = tuple(
            event for event in update.events if isinstance(event, RetestEpisodeStarted)
        )
        demand_only = select_retest_starts(
            starts,
            kind=ZoneKind.DEMAND,
            policy=policy(selection=OverlapSelection.ALL_ELIGIBLE),
        )
        self.assertEqual(len(demand_only), 1)
        self.assertEqual(demand_only[0].zone.kind, ZoneKind.DEMAND)


if __name__ == "__main__":
    unittest.main()
