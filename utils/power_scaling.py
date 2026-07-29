"""Per-key rank/quantile rescaling of quantized power values.

The per-POI service/capability powers (sp/cp) cluster tightly, so raw values are
hard to tell apart. This maps each key's values onto ~[0, 1] by their empirical
rank (quantile transform / histogram equalization): the k-th smallest value lands
near k / N, so a bunched distribution is spread out evenly. Each key (each
service, each capability) is scaled on its OWN distribution.

Everything is done on the already-quantized integer values the shard writer
stores (q = round(value * SCALE)), so the memory footprint is one small
histogram per key (counts indexed by q), not the tens of millions of raw values
-- fixed, bounded memory regardless of how many POIs are processed.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any


class PerKeyQuantileScaler:
    """Fit an empirical-quantile remap per key, then transform quantized values.

    Usage:
        s = PerKeyQuantileScaler(scale=10000)
        for (key, q) in observations: s.observe(key, q)   # q > 0, integer
        s.fit()
        q_scaled = s.transform(key, q)                     # integer in [0, scale]
    """

    def __init__(self, scale: int = 10000):
        self.scale = int(scale)
        # key -> {quantized value q: count}
        self._hist: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        # key -> {q: q_scaled}, built by fit()
        self._remap: dict[str, dict[int, int]] = {}
        self._total: dict[str, int] = {}

    def observe(self, key: str, q: int) -> None:
        """Record one quantized value (as written to the store) for `key`."""
        if q > 0:
            self._hist[key][q] += 1

    def fit(self) -> None:
        """Build the per-key value -> scaled-value lookup from the histograms.

        Uses the mid-rank convention -- scaled(q) = (values strictly below q +
        half the values equal to q) / total -- so the smallest value maps near 0
        and the largest near 1, with ties sharing one scaled value.
        """
        self._remap = {}
        for key, hist in self._hist.items():
            total = sum(hist.values())
            self._total[key] = total
            if total == 0:
                self._remap[key] = {}
                continue
            remap: dict[int, int] = {}
            cum_below = 0
            for q in sorted(hist):
                count = hist[q]
                midrank = (cum_below + count / 2.0) / total
                remap[q] = int(round(midrank * self.scale))
                cum_below += count
            self._remap[key] = remap

    def transform(self, key: str, q: int) -> int:
        """Map a quantized value to its scaled quantized value (identity if unseen)."""
        remap = self._remap.get(key)
        if not remap:
            return q
        return remap.get(q, q)

    # ── Reporting (for the scaling dashboard) ────────────────────────────────
    def report(self, nbins: int = 100) -> dict[str, Any]:
        """Summarize each key's raw distribution and its raw->scaled mapping.

        Returns, per key, `nbins` equal-width bins over the observed raw-value
        range: each bin carries its raw [lo, hi) interval, the count of values in
        it, and the scaled [lo, hi] interval those values map to. Enough for a
        dashboard to show how the values were spread and to merge/drop ranges.
        Values are real (divided by `scale`), not the internal integers.
        """
        out: dict[str, Any] = {"scale": self.scale, "keys": {}}
        for key, hist in self._hist.items():
            total = self._total.get(key, sum(hist.values()))
            if total == 0 or not hist:
                out["keys"][key] = {"total": 0, "bins": []}
                continue
            qmin, qmax = min(hist), max(hist)
            vmin, vmax = qmin / self.scale, qmax / self.scale
            remap = self._remap.get(key, {})
            width = (qmax - qmin) or 1
            step = width / nbins
            bins = []
            for b in range(nbins):
                lo_q = qmin + b * step
                hi_q = qmin + (b + 1) * step
                count = 0
                scaled_vals: list[int] = []
                for q, c in hist.items():
                    # include the top edge in the last bin
                    if lo_q <= q < hi_q or (b == nbins - 1 and q == qmax):
                        count += c
                        if q in remap:
                            scaled_vals.append(remap[q])
                if count == 0:
                    continue
                bins.append(
                    {
                        "lo": round(lo_q / self.scale, 6),
                        "hi": round(hi_q / self.scale, 6),
                        "count": count,
                        "scaled_lo": round(min(scaled_vals) / self.scale, 6) if scaled_vals else None,
                        "scaled_hi": round(max(scaled_vals) / self.scale, 6) if scaled_vals else None,
                    }
                )
            out["keys"][key] = {
                "total": total,
                "raw_min": round(vmin, 6),
                "raw_max": round(vmax, 6),
                "bins": bins,
            }
        return out
