from copy import deepcopy


def valid_strategy_spec(frozen=True):
    document = {
        "spec_version": "1.0",
        "strategy_id": "xau-sma-cross-atr",
        "revision": 1,
        "name": "Synthetic SMA crossover fixture",
        "description": "Test-only closed rule document; it is not a trading recommendation.",
        "created_at": "2026-01-02T10:00:00Z",
        "frozen_at": "2026-01-02T11:00:00Z" if frozen else None,
        "provenance": {
            "extraction_run_id": "extract-test-001",
            "parent": None,
            "sources": [
                {
                    "source_id": "source-1",
                    "type": "video",
                    "uri": "https://example.invalid/authorized-video",
                    "title": "Authorized synthetic lesson",
                    "creator": "Test operator",
                    "published_at": "2026-01-01T09:00:00Z",
                    "ingested_at": "2026-01-02T09:00:00Z",
                    "content_sha256": "a" * 64,
                    "rights_basis": "user_supplied",
                    "evidence": [
                        {
                            "evidence_id": "evidence-rules",
                            "start_ms": 0,
                            "end_ms": 5000,
                            "excerpt_sha256": "b" * 64,
                            "supports": [
                                "/entry",
                                "/exit",
                                "/features",
                                "/market/bar_duration",
                                "/parameters",
                                "/sizing",
                                "/schedule",
                                "/execution/decision_point",
                            ],
                            "extractor_confidence": "0.90",
                        }
                    ],
                }
            ],
        },
        "market": {
            "instrument": "XAU_USD",
            "asset_type": "spot_metal_cfd",
            "data_feed_id": "synthetic-test-feed",
            "execution_venue_id": "paper-test-venue",
            "timezone": "UTC",
            "bar_duration": "PT1H",
            "feature_price": "mid",
            "required_quote_fields": ["bid", "ask"],
            "required_bar_fields": ["open", "high", "low", "close"],
        },
        "parameters": {
            "fast_period": {
                "type": "integer",
                "value": 2,
                "research_bounds": {"min": 2, "max": 4, "step": 1},
                "choices": None,
                "description": "Fast SMA period.",
            },
            "slow_period": {
                "type": "integer",
                "value": 3,
                "research_bounds": {"min": 3, "max": 5, "step": 1},
                "choices": None,
                "description": "Slow SMA period.",
            },
            "atr_period": {
                "type": "integer",
                "value": 2,
                "research_bounds": None,
                "choices": None,
                "description": "ATR period.",
            },
            "stop_multiple": {
                "type": "decimal",
                "value": "1.50",
                "research_bounds": {"min": "1.00", "max": "2.00", "step": "0.25"},
                "choices": None,
                "description": "Initial ATR stop multiple.",
            },
            "reward_multiple": {
                "type": "decimal",
                "value": "2.00",
                "research_bounds": None,
                "choices": None,
                "description": "Initial risk reward multiple.",
            },
        },
        "features": [
            {
                "id": "fast",
                "kind": "sma",
                "inputs": [{"ref": "bar.close"}],
                "params": {"period": {"param": "fast_period"}},
                "output_type": "decimal",
                "warmup_bars": 2,
                "availability": "bar_close",
            },
            {
                "id": "slow",
                "kind": "sma",
                "inputs": [{"ref": "bar.close"}],
                "params": {"period": {"param": "slow_period"}},
                "output_type": "decimal",
                "warmup_bars": 3,
                "availability": "bar_close",
            },
            {
                "id": "atr",
                "kind": "atr",
                "inputs": [{"ref": "bar.high"}, {"ref": "bar.low"}, {"ref": "bar.close"}],
                "params": {"period": {"param": "atr_period"}},
                "output_type": "decimal",
                "warmup_bars": 3,
                "availability": "bar_close",
            },
        ],
        "schedule": {
            "days": ["MON", "TUE", "WED", "THU", "FRI"],
            "windows": [{"start_utc": "00:00", "end_utc": "23:59"}],
            "exclude_dates": [],
            "close_before_weekend": {"enabled": False, "cutoff_utc": None},
        },
        "entry": {
            "long": {
                "op": "crosses_above",
                "args": [{"ref": "features.fast"}, {"ref": "features.slow"}],
            },
            "short": {
                "op": "crosses_below",
                "args": [{"ref": "features.fast"}, {"ref": "features.slow"}],
            },
            "confirmation_bars": 1,
            "cooldown_bars": 1,
            "max_entries_per_bar": 1,
        },
        "exit": {
            "signal": {
                "long": {
                    "op": "crosses_below",
                    "args": [{"ref": "features.fast"}, {"ref": "features.slow"}],
                },
                "short": {
                    "op": "crosses_above",
                    "args": [{"ref": "features.fast"}, {"ref": "features.slow"}],
                },
            },
            "initial_stop": {
                "type": "feature_multiple",
                "feature_ref": "features.atr",
                "multiple": {"param": "stop_multiple"},
                "snapshot": "at_signal",
            },
            "take_profit": {
                "type": "initial_risk_multiple",
                "multiple": {"param": "reward_multiple"},
            },
            "trailing_stop": None,
            "max_holding_bars": 24,
        },
        "sizing": {
            "type": "fixed_fractional",
            "risk_fraction_of_equity": "0.0025",
            "equity_source": "paper_account_nav",
            "stop_distance_source": "initial_stop",
            "quantity_rounding": "down",
            "min_units": "0.01",
            "max_units": "1.00",
            "unit_step": "0.01",
        },
        "execution": {
            "environment": "PAPER",
            "decision_point": "bar_close",
            "earliest_entry": "next_observable_quote",
            "entry_order_type": "market",
            "time_in_force": "FOK",
            "allow_partial_fill": False,
            "price_side": "natural_bid_ask",
            "cost_model_id": "test-cost-v1",
            "intrabar_ambiguity": "stop_first",
            "signal_ttl_bars": 1,
        },
        "risk": {
            "max_open_positions": 1,
            "max_trades_per_utc_day": 3,
            "max_daily_loss_fraction": "0.01",
            "max_spread_price": "1.00",
            "max_quote_age_ms": 3000,
            "min_stop_distance_price": "0.50",
            "max_stop_distance_price": "50.00",
            "close_on_data_stale": False,
            "entry_kill_switch_required": True,
        },
        "assumptions": [
            {
                "id": "stop-first-policy",
                "text": "When both are touched in a bar, the stop is evaluated first.",
                "affects": ["/execution/intrabar_ambiguity"],
                "source": "simulation_policy",
            }
        ],
        "unresolved_items": [],
        "tags": ["synthetic", "test-only"],
    }
    return deepcopy(document)
