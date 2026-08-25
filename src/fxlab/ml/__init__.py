"""ML setup-quality filter (Phase 5). Not in P1.

Estimates calibrated P(TP before SL | state) and FILTERS setups by expected value.
Starts with logistic regression; complex models must beat the simpler baseline
out-of-sample or they are cut. AI never bypasses the risk engine.
"""
