"""Synthetic mechanics fixture with a genuine unmodified-baseline BUY.

The one deliberately deepened candle wick creates a zone retest in generated
data. Nothing here replaces candidate evidence, changes strategy parameters,
or asserts trading performance. The production replay derives all decisions.
"""

from dataclasses import replace

from xau_trader.data import generate_demo_m15_bars


def structural_exit_m15_bars():
    """Return 224 M15 bars with a causal BUY at 2024-01-03 07:30 UTC.

    Generating 480 bars before truncation is intentional: the demo generator's
    drift depends on its requested total length. The replay receives only this
    224-bar prefix, never the unused later candles.
    """

    bars = list(generate_demo_m15_bars(480)[:224])
    original = bars[221]
    half_spread = (original.ask_low - original.bid_low) / 2.0
    bars[221] = replace(
        original,
        bid_low=2002.8 - half_spread,
        ask_low=2002.8 + half_spread,
    )
    return tuple(bars)
