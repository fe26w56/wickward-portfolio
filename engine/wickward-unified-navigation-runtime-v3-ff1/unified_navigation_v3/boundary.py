"""Strict bytes-only envelope and immutable, generic-ID obligation admission.

All supplied rows are checked before cutoff, completion, miss or action-lock
filtering. Timing feasibility remains the unchanged scheduling kernel's job.
"""
from __future__ import annotations

from datetime import datetime, timezone
import heapq
import json
import math

from .kernel import InputError, PracticalContext, instant, number, validate_request_keys


SCHEMA_VERSION = "wickward.navigation-entry.v3"
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_CONTAINER_NESTING = 14
_DEFAULT_OPTIONS = {
    "total_budget_seconds": 2.4,
    "allow_partial_recovery": True,
    "allow_fixed5_partial_wait": False,
}


def _fail(path, message):
    raise InputError(f"{path}: {message}")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("JSON", f"duplicate object key {key!r}")
        result[key] = value
    return result


def _constant(value):
    _fail("JSON", f"nonfinite constant {value} is not supported")


def _float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        _fail("JSON", "numbers must be finite")
    return parsed


def _bounded_nesting(text):
    depth, in_string, escaped = 0, False, False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
            if depth > MAX_CONTAINER_NESTING:
                _fail("JSON", "container nesting exceeds the 14-container resource limit")
        elif char in "}]":
            depth -= 1


def parse_envelope(payload: bytes):
    """Decode a strict v3 envelope and return its request plus defaulted options.

    Structural/relational request admission is performed by validate_request.
    No caller-provided object, forecast callback, witness or proof is accepted.
    """
    if not isinstance(payload, bytes):
        _fail("payload", "bytes are required")
    if len(payload) > MAX_INPUT_BYTES:
        _fail("payload", "JSON exceeds the strict 16 MiB resource limit")
    try:
        text = payload.decode("utf-8", errors="strict")
        _bounded_nesting(text)
        envelope = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant,
                              parse_float=_float)
    except InputError:
        raise
    except (UnicodeError, ValueError, RecursionError, OverflowError) as exc:
        raise InputError("payload: a supported UTF-8 JSON document is required") from exc
    if not isinstance(envelope, dict):
        _fail("envelope", "an object is required")
    unknown = sorted(set(envelope) - {"schema_version", "request", "options"})
    if unknown:
        _fail("envelope", f"unsupported fields {unknown}")
    if envelope.get("schema_version") != SCHEMA_VERSION:
        _fail("schema_version", f"must be {SCHEMA_VERSION!r}")
    request = envelope.get("request")
    if not isinstance(request, dict):
        _fail("request", "an object is required")
    supplied = envelope.get("options", {})
    if not isinstance(supplied, dict):
        _fail("options", "an object is required")
    unknown = sorted(set(supplied) - set(_DEFAULT_OPTIONS))
    if unknown:
        _fail("options", f"unsupported fields {unknown}")
    options = {**_DEFAULT_OPTIONS, **supplied}
    try:
        options["total_budget_seconds"] = number(options["total_budget_seconds"],
                                                 "options.total_budget_seconds", .001, 2.4)
    except OverflowError as exc:
        raise InputError("options.total_budget_seconds: finite supported number required") from exc
    for field in ("allow_partial_recovery", "allow_fixed5_partial_wait"):
        if not isinstance(options[field], bool):
            _fail("options." + field, "a JSON boolean is required")
    if options["allow_fixed5_partial_wait"] and not options["allow_partial_recovery"]:
        _fail("options.allow_fixed5_partial_wait", "requires allow_partial_recovery=true")
    return request, options


def _required(row, fields, path):
    missing = sorted(set(fields) - set(row))
    if missing:
        _fail(path, f"missing required fields {missing}")


def _unique(rows, path):
    found = {}
    for index, row in enumerate(rows):
        _required(row, {"id"}, f"{path}[{index}]")
        key = row["id"]
        if key in found:
            _fail(path, f"duplicate ID {key!r}")
        found[key] = row
    return found


def _label(row, field, path):
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        _fail(path + "." + field, "a nonempty source/ID string is required")


def _validate_request(request):
    validate_request_keys(request)
    _required(request, {"schema_version", "information_as_of", "now", "exit_deadline",
                        "current_location_id", "exit_location_id"}, "request")
    locations = _unique(request.get("locations", []), "locations")
    attractions = _unique(request.get("attractions", []), "attractions")
    commitments = _unique(request.get("commitments", []), "commitments")
    breaks = _unique(request.get("flexible_breaks", []), "flexible_breaks")
    if set(commitments) & set(breaks):
        _fail("obligations", "IDs must be unique across commitments and flexible breaks")

    def location(key, path):
        if key not in locations:
            _fail(path, "unknown location ID")
        if locations[key].get("identity_verified") is not True:
            _fail(path, "location identity must be verified")

    for key, row in attractions.items():
        _required(row, {"location_id", "ride_minutes"}, f"attractions[{key!r}]")
        location(row["location_id"], f"{key}.location_id")
        if row.get("utility") is not None:
            _label(row, "utility_source", key)
        if row.get("fallback_wait_minutes") is not None:
            _label(row, "fallback_wait_source", key)

    all_reserved, completed_linked, active_reserved = set(), set(), set()
    for key, row in commitments.items():
        _required(row, {"kind", "location_id", "duration_minutes"}, f"commitments[{key!r}]")
        location(row["location_id"], f"{key}.location_id")
        if row["kind"] == "dpa":
            _required(row, {"entry_start", "entry_end"}, key)
            if instant(row["entry_end"], key + ".entry_end") <= instant(row["entry_start"], key + ".entry_start"):
                _fail(key, "entry_end must be after entry_start")
        else:
            _required(row, {"start_at"}, key)
        aid = row.get("attraction_id")
        if aid is not None:
            if aid not in attractions:
                _fail(key + ".attraction_id", "unknown attraction ID")
            if row["location_id"] != attractions[aid]["location_id"]:
                _fail(key, "commitment and attraction locations disagree")
            all_reserved.add(aid)
            if row.get("completed", False):
                completed_linked.add(aid)
            else:
                if aid in active_reserved:
                    _fail(key, "duplicate active reservations for one attraction need manual resolution")
                active_reserved.add(aid)

    for key, row in breaks.items():
        _required(row, {"earliest_start", "latest_start", "duration_minutes"}, f"flexible_breaks[{key!r}]")
        if row.get("location_id") is not None:
            location(row["location_id"], key + ".location_id")
        if instant(row["latest_start"], key + ".latest_start") < instant(row["earliest_start"], key + ".earliest_start"):
            _fail(key, "latest_start must be at or after earliest_start")

    observation_values = {}
    for index, row in enumerate(request.get("observations", [])):
        path = f"observations[{index}]"
        _required(row, {"attraction_id", "fetched_at", "status", "source"}, path)
        if row["attraction_id"] not in attractions:
            _fail(path + ".attraction_id", "unknown attraction ID")
        fetched = instant(row["fetched_at"], path + ".fetched_at")
        observed = instant(row["observed_at"], path + ".observed_at") if row.get("observed_at") is not None else None
        changed = instant(row["changed_at"], path + ".changed_at") if row.get("changed_at") is not None else None
        if any(t is not None and t > fetched for t in (observed, changed)):
            _fail(path, "observed_at/changed_at may not be later than fetched_at")
        if observed is not None and changed is not None and changed > observed:
            _fail(path, "changed_at may not be later than observed_at")
        evidence_key = (row["attraction_id"], observed or changed or datetime.min.replace(tzinfo=timezone.utc), fetched)
        value = (row["status"], row.get("wait_minutes"))
        if evidence_key in observation_values and observation_values[evidence_key] != value:
            _fail(path, "conflicting observations have identical evidence timestamps")
        observation_values[evidence_key] = value

    forecast_groups = {}
    for index, row in enumerate(request.get("forecasts", [])):
        path = f"forecasts[{index}]"
        _required(row, {"attraction_id", "issued_at", "valid_from", "valid_until", "wait_minutes", "source"}, path)
        if row["attraction_id"] not in attractions:
            _fail(path + ".attraction_id", "unknown attraction ID")
        start = instant(row["valid_from"], path + ".valid_from")
        end = instant(row["valid_until"], path + ".valid_until")
        if end <= start:
            _fail(path, "valid_until must be after valid_from")
        issue = instant(row["issued_at"], path + ".issued_at")
        forecast_groups.setdefault((row["attraction_id"], issue), []).append(
            (start, end, row["wait_minutes"], index))
    # Include future issues and rows not queried by a selected witness. The
    # interval sweep is O(n log n); touching half-open intervals do not conflict.
    for group in forecast_groups.values():
        active, wait_counts = [], {}
        for start, end, wait, index in sorted(group):
            while active and active[0][0] <= start:
                _, old_wait = heapq.heappop(active)
                wait_counts[old_wait] -= 1
                if not wait_counts[old_wait]:
                    del wait_counts[old_wait]
            if wait_counts and wait not in wait_counts:
                _fail(f"forecasts[{index}]", "conflicting overlapping forecasts at the same issue time")
            heapq.heappush(active, (end, wait))
            wait_counts[wait] = wait_counts.get(wait, 0) + 1

    obligations = {**commitments, **breaks}
    completed = {key for key, row in obligations.items() if row.get("completed", False)}
    outstanding = set(obligations) - completed
    visited = set(request.get("visited_attraction_ids", []))
    must = set(request.get("must_ride_ids", []))
    if (visited | must) - set(attractions):
        _fail("request", "visited_attraction_ids and must_ride_ids must reference attraction IDs")
    if not completed_linked <= visited:
        _fail("visited_attraction_ids", "completed linked attractions must be explicitly visited")
    provenance = request.get("provenance", {})
    for name in ("completed_commitment_ids", "all_reserved_attraction_ids", "missed_commitment_ids"):
        if name in provenance and len(set(provenance[name])) != len(provenance[name]):
            _fail("provenance." + name, "duplicate IDs are not allowed")
    if "completed_commitment_ids" in provenance and set(provenance["completed_commitment_ids"]) != completed:
        _fail("provenance.completed_commitment_ids", "must exactly match completed flags")
    if "all_reserved_attraction_ids" in provenance and set(provenance["all_reserved_attraction_ids"]) != all_reserved:
        _fail("provenance.all_reserved_attraction_ids", "must exactly match all linked attractions")
    missed = set(provenance.get("missed_commitment_ids", []))
    if not missed <= outstanding:
        _fail("provenance.missed_commitment_ids", "must reference known uncompleted obligations")
    unvisited_must = must - visited
    # Count the original admission set before any miss can remove a timing node.
    if len(outstanding) + len(unvisited_must - active_reserved) > 6:
        _fail("request", "at most six original uncompleted timed rows and unvisited unreserved must-rides are supported")

    # The unchanged constructor validates ordinary graph and policy relations.
    # It deep-copies its input. All rows it filters have already passed preflight.
    PracticalContext(request)
    return {
        "original_outstanding_commitment_ids": sorted(outstanding),
        "unresolved_commitment_ids": sorted(outstanding),
        "original_unvisited_must_ride_ids": sorted(unvisited_must),
        "unresolved_must_ride_ids": sorted(unvisited_must),
        "completed_commitment_ids": sorted(completed),
        "missed_commitment_ids": sorted(missed),
        "all_reserved_attraction_ids": sorted(all_reserved),
        "visited_attraction_ids": sorted(visited),
    }


def validate_request(request):
    """Validate every original row and return fresh, sorted ledger lists.

    The supplied request is never changed. Recorded misses remain unresolved;
    linked visited-but-pending service remains an engine state-resolution issue.
    """
    try:
        return _validate_request(request)
    except InputError:
        raise
    except (OverflowError, ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise InputError("request: unsupported structural value or timestamp range") from exc


__all__ = ["parse_envelope", "validate_request", "MAX_INPUT_BYTES"]
