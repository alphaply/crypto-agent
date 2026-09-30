"""Numerical reference cases and timing boundaries for added market context."""

import numpy as np
import pandas as pd
import pytest

from backend.utils.indicators import calc_choppiness, calc_cmf, calc_squeeze_state, build_market_regime_context


def test_chop_distinguishes_direct_path_and_repeated_range():
    trend = pd.DataFrame({'low': np.arange(30), 'high': np.arange(30) + 1, 'close': np.arange(30) + 1})
    chop = calc_choppiness(trend)
    assert chop.iloc[:13].isna().all()
    assert chop.iloc[-1] == pytest.approx(0)
    sideways = pd.DataFrame({'low': [99] * 30, 'high': [101] * 30, 'close': [100] * 30})
    assert calc_choppiness(sideways).iloc[-1] == pytest.approx(100)
    assert calc_choppiness(sideways.assign(low=100, high=100)).isna().all()


def test_cmf_is_volume_weighted_and_preserves_unknown_zero_volume():
    bars = pd.DataFrame({'low': [90] * 20, 'high': [110] * 20,
                         'close': [110] * 15 + [90] * 5, 'volume': [1] * 15 + [3] * 5})
    assert calc_cmf(bars).iloc[:19].isna().all()
    assert calc_cmf(bars).iloc[-1] == pytest.approx(0)
    assert calc_cmf(bars.assign(close=110)).iloc[-1] == pytest.approx(1)
    assert calc_cmf(bars.assign(close=90)).iloc[-1] == pytest.approx(-1)
    assert calc_cmf(bars.assign(low=100, high=100, close=100)).iloc[-1] == 0
    assert calc_cmf(bars.assign(volume=0)).isna().all()


def test_squeeze_release_is_only_reported_after_observed_compression():
    closes = [100 + (-1) ** i * .2 for i in range(40)] + [130, 130]
    bars = pd.DataFrame({'close': closes, 'high': np.array(closes) + 1,
                         'low': np.array(closes) - 1, 'volume': 10})
    states = calc_squeeze_state(bars)
    assert states.iloc[:19].eq('unavailable').all()
    assert states.iloc[19:40].eq('on').all()
    assert states.iloc[40:].eq('off').all()
    assert build_market_regime_context(bars.iloc[:40])['squeeze'] == {
        'state': 'on', 'on_bars': 21, 'bars_since_release': None}
    assert build_market_regime_context(bars.iloc[:41])['squeeze']['bars_since_release'] == 0
    assert build_market_regime_context(bars)['squeeze']['bars_since_release'] == 1
    # A trend observed without a preceding squeeze must not invent a release event.
    trend = pd.DataFrame({'close': np.arange(60) + 1, 'high': np.arange(60) + 2,
                         'low': np.arange(60), 'volume': 10})
    assert build_market_regime_context(trend)['squeeze']['bars_since_release'] is None


def test_context_marks_warmup_and_flat_market_without_fabricating_regime():
    bars = pd.DataFrame({'close': [100] * 30, 'high': [100] * 30,
                         'low': [100] * 30, 'volume': [0] * 30})
    for subset in (bars.iloc[:10], bars):
        context = build_market_regime_context(subset)
        assert context['chop']['value'] is None
        assert context['cmf']['value'] is None
        assert context['squeeze']['state'] == 'unavailable'
        assert context['squeeze']['bars_since_release'] is None


def test_regime_changes_and_durations_use_unrounded_history():
    bars = pd.DataFrame({'close': [100.0] * 23, 'high': [101] * 23,
                         'low': [99] * 23, 'volume': [10] * 23})
    bars.loc[22, 'close'] = 100.6
    context = build_market_regime_context(bars)
    assert context['chop']['state'] == 'choppy'
    assert context['chop']['state_bars'] == 10
    assert context['cmf']['value'] == pytest.approx(.03)
    assert context['cmf']['change_3_bars'] == pytest.approx(.03)
    assert context['cmf']['sign_bars'] == 1
