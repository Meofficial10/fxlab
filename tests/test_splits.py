"""Chronological splitting and purged + embargoed walk-forward."""

from __future__ import annotations

import pandas as pd

from fxlab.validation.splits import chronological_split, to_utc_ts
from fxlab.validation.walkforward import PurgedWalkForward, purge_train_times


def test_to_utc_ts_localizes_naive():
    assert str(to_utc_ts("2021-01-01").tz) == "UTC"


def test_chronological_split_boundaries_and_partition():
    idx = pd.date_range("2015-01-01", "2025-01-01", freq="D", tz="UTC")
    sp = chronological_split(idx, "2021-12-31", "2023-12-31")
    te, ve = to_utc_ts("2021-12-31"), to_utc_ts("2023-12-31")

    assert sp.train.max() <= te
    assert sp.val.min() > te and sp.val.max() <= ve
    assert sp.test.min() > ve
    # a strict, exhaustive partition — no bar lost or double-counted
    assert sum(sp.counts().values()) == len(idx)


def test_purge_and_embargo_drop_leaking_train_events():
    starts = pd.date_range("2020-01-01", periods=100, freq="D", tz="UTC")
    t1 = pd.Series(starts + pd.Timedelta("5D"), index=starts)  # 5-day label horizon
    test_start = starts[50]
    embargo = pd.Timedelta("2D")

    kept = purge_train_times(t1, test_start, embargo)

    # No kept event's label may reach into the test window...
    assert (t1.loc[kept] < test_start).all()
    # ...and none may start inside the embargo band just before it.
    assert not (((kept >= test_start - embargo) & (kept < test_start)).any())


def test_walkforward_folds_have_no_leakage():
    starts = pd.date_range("2020-01-01", periods=200, freq="D", tz="UTC")
    t1 = pd.Series(starts + pd.Timedelta("3D"), index=starts)
    wf = PurgedWalkForward(n_splits=4, embargo=pd.Timedelta("1D"))

    folds = list(wf.split(t1))
    assert len(folds) >= 2
    for train_times, test_times in folds:
        test_start = test_times.min()
        # every training label ends strictly before the test block begins
        assert (t1.loc[train_times] < test_start).all()
        # training is drawn only from the past
        assert train_times.max() < test_start
