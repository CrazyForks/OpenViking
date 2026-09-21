# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Central TTL resolution: map an object URI to its expiry.

This is the single seam that turns a canonical Viking URI plus the cluster TTL
config into a frozen ``expires_at`` at object-creation time. Every writer that
freezes TTL (events via the memory path, sessions via SessionMeta) and the
background cleanup scanner go through here so the scope rules stay in one place.

TTL is default OFF and strictly scoped to three directory kinds:

- ``user_events``  -> ``viking://user/{uid}/memories/events/...``
- ``peer_events``  -> ``viking://user/{uid}/peers/{pid}/memories/events/...``
- ``sessions``     -> ``viking://user/{uid}/sessions/{sid}...``

Day granularity is expressed as ``ttl_days`` whole days after ``received_at``
(N x 24h in UTC). ``expires_at`` is authoritative for both the read barrier and
the cleanup scan.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from openviking.core.namespace import uri_parts
from openviking.storage.expr import RawDSL
from openviking.utils.time_utils import format_iso8601, parse_iso_datetime
from openviking_cli.utils.config import TTLConfig, TTLScope, get_openviking_config

# Object-type tags used by lifecycle records / cleanup, kept next to the scope
# rules so callers do not re-derive them.
OBJECT_TYPE_EVENT = "event"
OBJECT_TYPE_SESSION = "session"


def ttl_scope_for_uri(uri: str) -> Optional[TTLScope]:
    """Classify a canonical URI into a TTL scope, or ``None`` when unscoped.

    Only user events, peer events, and sessions are in scope. Anything else
    (preferences, resources, entities, skills, non-event memories, ...) returns
    ``None`` so TTL never touches it.
    """
    try:
        parts = uri_parts(uri)
    except ValueError:
        return None
    if len(parts) < 3 or parts[0] != "user":
        return None
    # sessions: viking://user/{uid}/sessions/...
    if parts[2] == "sessions":
        return "sessions"
    # peer events: viking://user/{uid}/peers/{pid}/memories/events/...
    if (
        len(parts) >= 6
        and parts[2] == "peers"
        and parts[4] == "memories"
        and parts[5] == "events"
    ):
        return "peer_events"
    # user events: viking://user/{uid}/memories/events/...
    if len(parts) >= 4 and parts[2] == "memories" and parts[3] == "events":
        return "user_events"
    return None


def object_type_for_scope(scope: TTLScope) -> str:
    """Return the lifecycle object_type tag for a resolved TTL scope."""
    return OBJECT_TYPE_SESSION if scope == "sessions" else OBJECT_TYPE_EVENT


def resolve_ttl_days(uri: str, config: Optional[TTLConfig] = None) -> Optional[int]:
    """Resolve the effective ``ttl_days`` for a URI, or ``None`` when TTL is off."""
    scope = ttl_scope_for_uri(uri)
    if scope is None:
        return None
    ttl_config = config if config is not None else _current_ttl_config()
    if ttl_config is None:
        return None
    return ttl_config.resolve_scope(scope)


def compute_expires_at(received_at: datetime, ttl_days: int) -> datetime:
    """Return the frozen expiry: ``received_at`` plus ``ttl_days`` whole days."""
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    return received_at + timedelta(days=ttl_days)


def freeze_ttl_fields(
    uri: str,
    *,
    received_at: Optional[datetime] = None,
    config: Optional[TTLConfig] = None,
) -> Optional[dict]:
    """Compute the frozen TTL snapshot for a new object, or ``None`` when off.

    Returns a dict with RFC 3339 ``received_at``/``expires_at`` strings and the
    integer ``ttl_days`` actually applied. Callers persist this snapshot verbatim
    at creation time and never recompute it from later config changes.
    """
    ttl_days = resolve_ttl_days(uri, config)
    if ttl_days is None:
        return None
    received = received_at or datetime.now(timezone.utc)
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    expires = compute_expires_at(received, ttl_days)
    return {
        "ttl_days": ttl_days,
        "received_at": format_iso8601(received),
        "expires_at": format_iso8601(expires),
    }


def is_expired(expires_at: Optional[str], *, now: Optional[datetime] = None) -> bool:
    """Return whether an ``expires_at`` timestamp is at or past ``now`` (UTC).

    Absent/blank/unparseable expiry means "no TTL" and is never expired, matching
    the read barrier's absent-field-visible rule.
    """
    if not expires_at:
        return False
    try:
        expires = parse_iso_datetime(expires_at)
    except Exception:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return expires <= current


def ttl_enabled() -> bool:
    """Whether the TTL read barrier is active. Cheap gate for read paths.

    When this is ``False`` (the default), callers must behave exactly as before:
    no expiry filtering, no extra metadata reads. It is the single switch that
    keeps a disabled TTL config fully inert.
    """
    config = _current_ttl_config()
    return config is not None and config.enabled


def hidden_by_ttl(expires_at: Optional[str], *, now: Optional[datetime] = None) -> bool:
    """Whether a read/compute path should treat ``expires_at`` as logically gone.

    Scalar counterpart to :func:`expiry_filter_now` for code paths that already
    hold a frozen ``expires_at`` (session load/list, the idle scan) instead of
    issuing a vector query. Gated on TTL being enabled so disabling TTL lifts the
    barrier uniformly — a frozen expiry never hides an object while TTL is off.
    """
    if not ttl_enabled():
        return False
    return is_expired(expires_at, now=now)


def _current_ttl_config() -> Optional[TTLConfig]:
    try:
        return get_openviking_config().ttl
    except Exception:
        # Config not initialized (e.g. unit tests, bootstrap). Fail closed to OFF.
        return None


def expiry_filter_now(*, now: Optional[datetime] = None) -> Optional[RawDSL]:
    """Return the read-barrier filter that hides expired objects, or ``None``.

    When TTL is disabled the barrier is skipped entirely (``None``), so nothing
    is filtered and existing behaviour is untouched. When enabled, the predicate
    keeps an object visible while ``expires_at`` is absent or strictly in the
    future, and hides it once ``expires_at <= now``:

        ``{"op": "range_out", "field": "expires_at", "lte": <now RFC3339>}``

    ``range_out`` is the negation of ``range``: a row matches when ``expires_at``
    is *outside* ``(-inf, now]`` (i.e. still in the future). Rows without an
    ``expires_at`` value never satisfy the inner range, so ``range_out`` keeps
    them visible — matching :func:`is_expired`'s absent-is-not-expired rule. The
    barrier is injected as a raw DSL node because the typed filter AST compiles
    ``Range``/``TimeRange`` down to ``range`` and has no ``range_out`` variant.
    """
    if not ttl_enabled():
        return None
    current = now or datetime.now(timezone.utc)
    return RawDSL(
        {
            "op": "range_out",
            "field": "expires_at",
            "lte": format_iso8601(current),
        }
    )
