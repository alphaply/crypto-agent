"""Exercise chart data transitions without a browser or exchange connection."""

from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest


FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is required for the frontend chart checks")
class KlineFrontendTests(unittest.TestCase):
    def run_node(self, source):
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", textwrap.dedent(source)],
            cwd=FRONTEND,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_dirty_exchange_snapshot_has_one_valid_bar_per_time(self):
        self.run_node("""
            import assert from 'node:assert/strict';
            import { normalizeKlineData } from './src/lib/kline.js';
            const candle = (time, close = 11) => ({ time, open: 10, high: 12, low: 9, close });
            const result = normalizeKlineData({
              candles: [candle(20), candle('10'), candle(20, 12), null,
                candle('invalid'), { ...candle(30), low: 11 },
                { ...candle(40), high: 9 }, { ...candle(50), close: null }],
              volume: [{ time: 20, value: '8' }, { time: 10, value: 3 },
                { time: 30, value: 5 }, { time: 20, value: null }],
              emas: { '20': [{ time: 10, value: '10.5' }, { time: 20, value: Infinity },
                { time: 30, value: 2 }], invalid: [{ time: 10, value: 1 }] },
            });
            assert.deepEqual(result.candles, [candle(10), candle(20, 12)]);
            assert.deepEqual(result.volume, [{ time: 10, value: 3 }, { time: 20, value: 8 }]);
            assert.deepEqual(result.emas, { '20': [{ time: 10, value: 10.5 }] });
            for (const payload of [null, undefined, {}, { candles: 'unavailable' }]) {
              assert.deepEqual(normalizeKlineData(payload), { candles: [], volume: [], emas: {} });
            }
        """)

    def test_polling_transitions_match_snapshots_and_do_not_rewrite_unchanged_history(self):
        self.run_node("""
            import assert from 'node:assert/strict';
            import { syncSeriesData } from './src/lib/kline.js';
            const bar = (time, value = 10) => ({ time, value });
            let actual = [];
            let replacements = 0;
            let updates = 0;
            const series = {
              setData(rows) { actual = structuredClone(rows); replacements++; },
              update(row) {
                assert.ok(actual.length, 'incremental update needs a seeded series');
                assert.ok(row.time >= actual.at(-1).time, 'updates cannot rewrite older bars');
                if (row.time === actual.at(-1).time) actual[actual.length - 1] = structuredClone(row);
                else actual.push(structuredClone(row));
                updates++;
              },
            };
            let previous = [];
            const poll = (snapshot, reset = false) => {
              const changed = syncSeriesData(series, previous, snapshot, reset);
              assert.deepEqual(actual, snapshot);
              previous = structuredClone(snapshot);
              return changed;
            };
            poll([bar(10), bar(20)]);
            assert.equal(poll([bar(10), bar(20)]), false);
            poll([bar(10), bar(20, 11), bar(30)]);
            assert.equal(replacements, 1, 'a normal tick plus new bar keeps existing history');
            assert.equal(updates, 2);
            poll([bar(10, 9), bar(20, 11), bar(30)]); // Exchange corrects history.
            poll([bar(20, 11), bar(30), bar(40)]); // Oldest bar leaves the API window.
            poll([bar(20, 11)]); // Snapshot shrinks.
            poll([]);
            poll([bar(100)]);
            poll([bar(100)], true); // Different instrument with coincident timestamps.
            assert.equal(replacements, 7);
        """)

    def test_monthly_and_gapped_windows_preserve_history_or_follow_latest(self):
        self.run_node("""
            import assert from 'node:assert/strict';
            import { preserveKlineRange } from './src/lib/kline.js';
            const months = ['2026-01-01', '2026-02-01', '2026-03-01',
              '2026-05-01', '2026-06-01', '2026-07-01']
              .map(date => ({ time: Date.parse(date) / 1000 }));
            const before = months.slice(0, 5);
            const after = months.slice(1);
            const history = preserveKlineRange({ from: 1.5, to: 3.5 }, before, after);
            assert.deepEqual(history, { from: 0.5, to: 2.5 });
            assert.equal(before[2].time, after[1].time, 'same calendar candle remains centered');
            assert.deepEqual(preserveKlineRange({ from: 2, to: 7 }, before, after),
              { from: 2, to: 7 });
            assert.deepEqual(preserveKlineRange({ from: 2, to: 7 }, before, months),
              { from: 3, to: 8 });
            assert.equal(preserveKlineRange({ from: 0, to: 1 }, before, [{ time: 1 }]), null);
            assert.equal(preserveKlineRange(null, before, after), null);
            assert.equal(preserveKlineRange({ from: 0, to: 1 }, before, []), null);
        """)

    def test_recent_view_and_price_format_work_for_mobile_and_small_coin_prices(self):
        self.run_node("""
            import assert from 'node:assert/strict';
            import { klinePriceFormat, recentKlineRange } from './src/lib/kline.js';
            const mobile = recentKlineRange(500, 320);
            const desktop = recentKlineRange(500, 1200);
            assert.ok(mobile.to >= 499 && desktop.to >= 499);
            assert.ok(mobile.to - mobile.from < desktop.to - desktop.from);
            for (const length of [0, 1, 8, 500]) {
              for (const width of [0, 320, 600, 1200]) {
                const range = recentKlineRange(length, width);
                assert.ok(Number.isFinite(range.from) && Number.isFinite(range.to));
                assert.ok(range.to > range.from);
              }
            }
            for (const close of [65000.12, 1.2345, 0.00001234, 0.000000001234]) {
              const format = klinePriceFormat([{ close }]);
              const shown = Number(close.toFixed(format.precision));
              assert.ok(shown > 0, 'small coin prices must not display as zero');
              assert.ok(Math.abs(shown - close) <= format.minMove / 2 + Number.EPSILON);
              assert.equal(format.minMove, 10 ** -format.precision);
            }
            assert.ok(Number.isFinite(klinePriceFormat([]).minMove));
        """)

    def test_history_backfill_keeps_the_visible_candle_at_the_same_offset(self):
        self.run_node("""
            import assert from 'node:assert/strict';
            import { preserveKlineRange } from './src/lib/kline.js';
            const before = [10, 30, 40].map(time => ({ time }));
            const after = [10, 20, 30, 40].map(time => ({ time }));
            const viewport = { from: 1, to: 1.8 };
            const updated = preserveKlineRange(viewport, before, after);
            assert.equal(updated.from, 2, 'the first visible candle is still time 30');
            assert.equal(after[updated.from].time, before[viewport.from].time);
            assert.ok(Math.abs((updated.to - updated.from) - (viewport.to - viewport.from)) < 1e-10);
        """)


if __name__ == "__main__":
    unittest.main()
