"""Point-in-time feature engine (Phase 2+). Not implemented in Phase 1.

Every feature obeys the PIT contract: computing a feature at bar t may read only
bars <= t. Enforced by the future-invariance test in tests/test_leakage_guards.py.
"""
