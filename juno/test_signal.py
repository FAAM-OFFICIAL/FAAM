"""JUNO Signal's exported trees must score exactly like scikit-learn's model.

FAAM scores the trees in plain Python (app.py: _signal_raw / signal_score), so
this checks the export against sklearn on data with missing values, including
the Platt shrink. Run with JUNO's Python:

    ~/.faam/juno/venv/bin/python -m unittest juno/test_signal.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("FAAM_DATA_DIR", tempfile.mkdtemp(prefix="juno-signal-test-"))
import signal_train as st  # noqa: E402


class TestExport(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.faam = st.load_faam()
        rng = np.random.default_rng(0)
        n, k = 6000, len(cls.faam.SIGNAL_FEATURES)
        X = rng.normal(size=(n, k))
        X[rng.random((n, k)) < 0.1] = np.nan               # some signals missing, as in real data
        y = ((X[:, 0] > 0) ^ (rng.random(n) < 0.3)).astype(int)
        cls.X, cls.y = X, y
        cls.days = np.array([f"{2018 + i * 7 // n}-{1 + i % 12:02d}-01" for i in range(n)])

    def model_json(self, clf, ab=(1.0, 0.0)):
        names = list(self.faam.SIGNAL_FEATURES)
        return {"features": names, "horizons": {"21": {**st.export_trees(clf, names), "platt": list(ab)}}}

    def rows(self, n=400):
        names = list(self.faam.SIGNAL_FEATURES)
        return [{k: (None if np.isnan(v) else float(v)) for k, v in zip(names, x)} for x in self.X[:n]]

    def test_raw_scores_match_sklearn(self):
        clf = st.HistGradientBoostingClassifier(**{**st.PARAMS, "max_iter": 40}).fit(self.X, self.y)
        m = self.model_json(clf)
        ours = [self.faam._signal_raw(m["horizons"]["21"], [f[k] for k in m["features"]]) for f in self.rows()]
        np.testing.assert_allclose(ours, clf.decision_function(self.X[:400]), rtol=0, atol=1e-6)

    def test_calibrated_odds_match(self):
        dates = self.days
        end = dates.copy()
        clf, ab, n = st.fit_calibrated(self.X, self.y, dates, end)
        m = self.model_json(clf, ab)
        ours = [self.faam.signal_score(f, 21, m) for f in self.rows()]
        np.testing.assert_allclose(ours, st.calibrated(clf, ab, self.X[:400]), rtol=0, atol=1e-6)
        self.assertGreaterEqual(n, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
