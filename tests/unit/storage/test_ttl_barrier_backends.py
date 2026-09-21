# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Tests for how the TTL ``expires_at`` barrier crosses the vector backends.

Two backend seams must agree on the ``range_out`` semantics the barrier relies
on (absent -> visible, future -> visible, at/past -> hidden):

- the commercial VikingDB collection rewrites numeric ``range`` on date_time
  fields to ``time_range`` but must pass ``range_out`` (the barrier) through so
  the negation is preserved and ``expires_at`` is recognised as a date_time
  field; and
- the local cuVS matcher evaluates ``range_out`` directly.
"""

from __future__ import annotations

from openviking.storage.vectordb.collection.volcengine_collection import (
    VolcengineCollection,
)
from openviking.storage.vectordb.index.cuvs_index import matches_filter


# ── Commercial data-plane normalization ─────────────────────────────────────


def test_commercial_rewrites_range_on_expires_at_to_time_range():
    node = {"op": "range", "field": "expires_at", "lte": "2026-01-01T00:00:00.000Z"}
    out = VolcengineCollection._normalize_date_time_filter(node)
    assert out["op"] == "time_range"
    assert out["field"] == "expires_at"


def test_commercial_passes_range_out_barrier_through_unchanged():
    # The read barrier is emitted as range_out; it must survive normalization so
    # the negation (and absent-is-visible) is preserved on the commercial plane.
    node = {"op": "range_out", "field": "expires_at", "lte": "2026-01-01T00:00:00.000Z"}
    out = VolcengineCollection._normalize_date_time_filter(node)
    assert out == node


def test_commercial_leaves_numeric_range_untouched():
    node = {"op": "range", "field": "active_count", "gte": 1}
    assert VolcengineCollection._normalize_date_time_filter(node) == node


def test_commercial_normalizes_nested_expires_at_range():
    node = {
        "op": "and",
        "conds": [
            {"op": "range", "field": "created_at", "gte": "2026-01-01T00:00:00.000Z"},
            {"op": "must", "field": "uri", "conds": ["viking://user/u1"]},
        ],
    }
    out = VolcengineCollection._normalize_date_time_filter(node)
    assert out["conds"][0]["op"] == "time_range"
    assert out["conds"][1]["op"] == "must"


# ── Local cuVS matcher: range_out semantics the barrier depends on ───────────

_FIELD_TYPES = {"expires_at": "int64"}


def _barrier(now_ms: int) -> dict:
    return {"op": "range_out", "field": "expires_at", "lte": now_ms}


def test_local_matcher_absent_expiry_is_visible():
    assert matches_filter({}, _barrier(100), _FIELD_TYPES) is True


def test_local_matcher_future_expiry_is_visible():
    assert matches_filter({"expires_at": 150}, _barrier(100), _FIELD_TYPES) is True


def test_local_matcher_past_and_equal_expiry_is_hidden():
    assert matches_filter({"expires_at": 50}, _barrier(100), _FIELD_TYPES) is False
    assert matches_filter({"expires_at": 100}, _barrier(100), _FIELD_TYPES) is False
