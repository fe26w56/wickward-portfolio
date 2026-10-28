"""Cloud-only historical replay bridge over the audited generic constraints.

No history truth, preferences, forecasts, physical locations, or reservations are
inferred here. Numeric clocks are minutes from the supplied day's JST midnight.
A received observation is not automatically a genuine source poll. The explicit
``synthetic_poll`` option is a *replay assumption*, never historical evidence that
an actual API poll or receipt occurred at those times.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
import json
import math
from numbers import Real, Integral
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from generic_io.adapter import (
    InputError, PracticalContext, SCHEMA_VERSION,
    evaluate_sequence,
)
from generic_io.recommendations import WallTimeBudgetExceeded, _bounded_shape

ROOT = Path(__file__).resolve().parents[1]
JST = ZoneInfo("Asia/Tokyo")
EXIT_INDEX = 34
EXIT_DEADLINE_MINUTE = 1080.0
EXIT_RESERVE_MINUTES = 15.0
MAX_RECEIVED_OBSERVATIONS = 10000
BRIDGE_VERSION = "wickward.fixed-replay-bridge.v1"
_catalog = json.loads((ROOT / "data/prior/facility_crosswalk.json").read_text())
if len(_catalog) != 34 or [row["index"] for row in _catalog] != list(range(34)):
    raise RuntimeError("The bridge requires the preserved 34-facility index mapping")
CANONICAL_IDS = tuple(row["id"] for row in _catalog)
if len(set(CANONICAL_IDS)) != 34:
    raise RuntimeError("Canonical facility IDs must be unique")
CANONICAL_NAMES = tuple(row["name"] for row in _catalog)
LOCATION_IDS = tuple(f"historical-node:{aid}" for aid in CANONICAL_IDS) + ("historical-node:exit34",)
del _catalog


@dataclass(frozen=True)
class ReceivedObservation:
    index: int
    observation: Any  # Read-only src.history.Observation, or the same attribute protocol.
    received_minute: float
    source_time_kind: str = "changed"
    source: str = "historical source row; receipt is an explicit replay assumption"
    changed_minute: float | None = None


@dataclass(frozen=True)
class FixedCommitment:
    id: str
    kind: str
    location_index: int
    duration_minutes: float
    start_minute: float | None = None
    entry_start_minute: float | None = None
    entry_end_minute: float | None = None
    attraction_index: int | None = None
    arrival_buffer_minutes: float = 0.0
    finish_by_minute: float | None = None


@dataclass(frozen=True)
class FlexibleBreak:
    id: str
    earliest_start_minute: float
    latest_start_minute: float
    duration_minutes: float
    location_index: int | None = None
    finish_by_minute: float | None = None


@dataclass(frozen=True)
class FixedScenario:
    commitments: tuple[FixedCommitment, ...] = ()
    must_ride_indices: tuple[int, ...] = ()
    ride_minutes: float = 10.0
    freshness_minutes: float = 30.0
    label: str = "fictional fixed commitments; no real booking or product offer"
    flexible_breaks: tuple[FlexibleBreak, ...] = ()


def _finite(value, field, minimum=0.0, maximum=2160.0):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise InputError(f"{field}: a finite numeric value is required")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise InputError(f"{field}: must be between {minimum} and {maximum}")
    return result


def _index(value, field, *, allow_exit=False):
    if isinstance(value, bool) or not isinstance(value, Integral) or not 0 <= value <= (34 if allow_exit else 33):
        raise InputError(f"{field}: an index from 0 to {34 if allow_exit else 33} is required")
    return int(value)


def _day(value):
    if isinstance(value, datetime):
        raise InputError("day: supply an ISO local date, not an implicit timestamp")
    try:
        return date.fromisoformat(value) if isinstance(value, str) else value if isinstance(value, date) else date.fromisoformat("")
    except (TypeError, ValueError) as exc:
        raise InputError("day: an ISO local date is required") from exc


def minute_datetime(day, minute):
    """Convert a numeric replay minute to an aware JST instant."""
    return datetime.combine(_day(day), time(), tzinfo=JST) + timedelta(
        minutes=_finite(minute, "minute", -2160, 2160))


def minute_of_day(day, instant):
    """Inverse conversion, retaining fractional minutes and explicit time zones."""
    if isinstance(instant, str):
        try:
            instant = datetime.fromisoformat(instant.replace("Z", "+00:00"))
        except ValueError as exc:
            raise InputError("instant: invalid ISO timestamp") from exc
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        raise InputError("instant: an aware datetime is required")
    return (instant - minute_datetime(day, 0)).total_seconds() / 60.0


def _stamp(day, minute):
    # Do not round to whole seconds before applying exact entry boundaries.
    return minute_datetime(day, minute).isoformat()


def _bounded_sequence(values, field, limit):
    try:
        size = len(values)
    except TypeError as exc:
        raise InputError(f"{field}: a bounded concrete sequence is required") from exc
    if isinstance(values, (str, bytes, Mapping)) or size > limit:
        raise InputError(f"{field}: at most {limit} items are supported")
    return values


def _ids(values, known, field):
    _bounded_sequence(values, field, 100)
    if any(not isinstance(value, str) or value not in known for value in values):
        raise InputError(f"{field}: every ID must reference a scenario commitment")
    if len(set(values)) != len(values):
        raise InputError(f"{field}: duplicate IDs are not allowed")
    return set(values)


def _normalize_observation(record, day):
    if not isinstance(record, ReceivedObservation):
        raise InputError("received_observations must contain ReceivedObservation values with an explicit receipt clock")
    index = _index(record.index, "observation.index")
    receipt = _finite(record.received_minute, "observation.received_minute", -2160, 2160)
    o = record.observation
    try:
        state, wait, source_minute, raw = o.status, o.wait, o.source_minute, o.raw_status
    except AttributeError as exc:
        raise InputError("observation requires status, wait, source_minute and raw_status") from exc
    if not isinstance(state, str) or state not in {"open", "closed", "down", "unknown", "stale"}:
        raise InputError("Observation.status must be explicitly normalized by the caller")
    if not isinstance(record.source_time_kind, str) or record.source_time_kind not in {"changed", "poll", "synthetic_poll", "unknown"}:
        raise InputError("source_time_kind must be changed, poll, synthetic_poll or unknown")
    if not isinstance(raw, str):
        raise InputError("Observation.raw_status must be a source status string")
    if not isinstance(record.source, str) or not record.source.strip():
        raise InputError("observation.source must describe the source/receipt assumption")
    if source_minute is not None:
        source_minute = _finite(source_minute, "observation.source_minute", -2160, 2160)
        if source_minute > receipt:
            raise InputError("observation source_minute may not follow its receipt")
    if wait is not None:
        wait = _finite(wait, "observation.wait")
    # A stale record retains only explicitly supplied values. No lost wait is
    # restored from the historical grid. Freshness is recomputed by the core.
    status = raw if state == "stale" and raw in {"open", "closed", "down", "unknown"} else "unknown" if state == "stale" else state
    observed = source_minute if record.source_time_kind in {"poll", "synthetic_poll"} else None
    changed = source_minute if record.source_time_kind == "changed" else record.changed_minute
    if changed is not None:
        changed = _finite(changed, "observation.changed_minute", -2160, 2160)
        if changed > receipt or (observed is not None and changed > observed):
            raise InputError("changed_minute may not follow the source poll or receipt")
    source = record.source
    if record.source_time_kind == "synthetic_poll":
        source += "; SYNTHETIC POLL/RECEIPT SCENARIO, not verified historical API freshness"
    return {"attraction_id": CANONICAL_IDS[index], "status": status,
            "wait_minutes": wait, "observed_at": _stamp(day, observed) if observed is not None else None,
            "changed_at": _stamp(day, changed) if changed is not None else None,
            "fetched_at": _stamp(day, receipt), "source": source}, {
                "attraction_id": CANONICAL_IDS[index], "source_minute": source_minute,
                "received_minute": receipt, "source_time_kind": record.source_time_kind,
                "supplied_status": state, "raw_status": str(raw)}


def compile_request(*, day, now, current_location_index, visited_indices,
                    received_observations, utilities, nominal_walking_matrix,
                    observed_walking_multiplier, learned_wait_overrun_margin,
                    scenario: FixedScenario, completed_ids=(), missed_ids=(),
                    information_as_of=None, max_schedule_states=100000) -> dict:
    """Compile explicit replay inputs into the unchanged generic JSON contract.

    Missing walking matrix entries (None or +inf) create no edge. Zero is only
    valid on the diagonal: distinct nodes never receive an invented free route.
    All 34 real canonical facility IDs are kept, including closed/unknown ones.
    Source and receipt clocks are retained separately; future receipts are passed
    to the generic exclusion audit and never become usable evidence.
    """
    day = _day(day).isoformat()
    now = _finite(now, "now", 0, EXIT_DEADLINE_MINUTE)
    if now >= EXIT_DEADLINE_MINUTE:
        raise InputError("now must be before the fixed 18:00 JST exit deadline")
    cutoff = now if information_as_of is None else _finite(information_as_of, "information_as_of", -2160, now)
    current = _index(current_location_index, "current_location_index", allow_exit=True)
    if not isinstance(scenario, FixedScenario):
        raise InputError("scenario must be FixedScenario")
    _bounded_sequence(scenario.commitments, "scenario.commitments", 100)
    _bounded_sequence(scenario.must_ride_indices, "scenario.must_ride_indices", 34)
    _bounded_sequence(scenario.flexible_breaks, "scenario.flexible_breaks", 100)
    if not isinstance(scenario.label, str) or not scenario.label.strip():
        raise InputError("scenario.label must explicitly identify the supplied scenario")
    ride_duration = _finite(scenario.ride_minutes, "scenario.ride_minutes", .01)
    ttl = _finite(scenario.freshness_minutes, "scenario.freshness_minutes", .01)
    multiplier = _finite(observed_walking_multiplier, "observed_walking_multiplier", .1, 10)
    overrun = _finite(learned_wait_overrun_margin, "learned_wait_overrun_margin")
    if isinstance(max_schedule_states, bool) or not isinstance(max_schedule_states, Integral) or not 1 <= max_schedule_states <= 250000:
        raise InputError("max_schedule_states must be an integer from 1 to 250000")
    _bounded_sequence(visited_indices, "visited_indices", 34)
    visited = {_index(i, "visited_indices") for i in visited_indices}
    required = {_index(i, "must_ride_indices") for i in scenario.must_ride_indices}
    _bounded_sequence(utilities, "utilities", 34)
    if len(utilities) != 34:
        raise InputError("utilities must contain exactly 34 explicitly supplied values or nulls")
    utility_values = [None if value is None else _finite(value, "utility", 0, 1e6) for value in utilities]
    _bounded_sequence(nominal_walking_matrix, "nominal_walking_matrix", 35)
    if len(nominal_walking_matrix) != 35:
        raise InputError("nominal_walking_matrix must be 35 by 35, with exit index 34")
    edges = []
    for i, row in enumerate(nominal_walking_matrix):
        _bounded_sequence(row, "walking matrix row", 35)
        if len(row) != 35:
            raise InputError("nominal_walking_matrix must be 35 by 35")
        for j, value in enumerate(row):
            if i == j:
                if value is not None and _finite(value, "walking matrix diagonal") != 0:
                    raise InputError("walking matrix diagonal must be zero or null")
                continue
            if value is None or isinstance(value, Real) and not isinstance(value, bool) and float(value) == math.inf:
                continue
            minutes = _finite(value, "walking matrix edge", .01)
            edges.append({"from": LOCATION_IDS[i], "to": LOCATION_IDS[j], "minutes": minutes,
                          "quality": "estimate", "source": "supplied coarse nominal matrix; unverified real walking paths or geometry"})
    commitments = []
    known_ids = set()
    all_reserved = set()
    for item in scenario.commitments:
        if not isinstance(item, FixedCommitment):
            raise InputError("scenario.commitments must contain FixedCommitment values")
        if not isinstance(item.id, str) or not item.id or item.id in known_ids or item.id in CANONICAL_IDS:
            raise InputError("Commitment IDs must be unique, nonempty and separate from facility IDs")
        known_ids.add(item.id)
        loc = _index(item.location_index, "commitment.location_index", allow_exit=True)
        if not isinstance(item.kind, str) or item.kind not in {"meal", "appointment", "dpa"}:
            raise InputError("Only fixed meals, appointments and fictional DPA-like timed-entry scenarios are supported")
        row = {"id": item.id, "kind": item.kind, "location_id": LOCATION_IDS[loc],
               "duration_minutes": _finite(item.duration_minutes, "commitment.duration_minutes", .01),
               "arrival_buffer_minutes": _finite(item.arrival_buffer_minutes, "commitment.arrival_buffer_minutes")}
        if item.kind == "dpa":
            if item.start_minute is not None:
                raise InputError("DPA-like service uses an entry window, not start_minute")
            start = _finite(item.entry_start_minute, "entry_start_minute", 0, 2160)
            end = _finite(item.entry_end_minute, "entry_end_minute", 0, 2160)
            if end <= start:
                raise InputError("entry_end_minute must be after entry_start_minute")
            row.update(entry_start=_stamp(day, start), entry_end=_stamp(day, end))
        else:
            if item.entry_start_minute is not None or item.entry_end_minute is not None:
                raise InputError("Fixed meal/appointment uses start_minute, not an entry window")
            row["start_at"] = _stamp(day, _finite(item.start_minute, "start_minute", 0, 2160))
        if item.attraction_index is not None:
            ai = _index(item.attraction_index, "commitment.attraction_index")
            if loc != ai:
                raise InputError("Reserved attraction must use its existing canonical matrix node")
            row["attraction_id"] = CANONICAL_IDS[ai]
            all_reserved.add(ai)
        if item.finish_by_minute is not None:
            row["finish_by"] = _stamp(day, _finite(item.finish_by_minute, "finish_by_minute", 0, 2160))
        commitments.append(row)
    flexible_breaks = []
    for item in scenario.flexible_breaks:
        if not isinstance(item, FlexibleBreak):
            raise InputError("scenario.flexible_breaks must contain FlexibleBreak values")
        if not isinstance(item.id, str) or not item.id or item.id in known_ids or item.id in CANONICAL_IDS:
            raise InputError("Flexible break IDs must be unique and separate from facility/commitment IDs")
        known_ids.add(item.id)
        earliest = _finite(item.earliest_start_minute, "earliest_start_minute", 0, 2160)
        latest = _finite(item.latest_start_minute, "latest_start_minute", 0, 2160)
        if latest < earliest:
            raise InputError("latest_start_minute must be at or after earliest_start_minute")
        row = {"id": item.id, "earliest_start": _stamp(day, earliest),
               "latest_start": _stamp(day, latest),
               "duration_minutes": _finite(item.duration_minutes, "break.duration_minutes", .01),
               "location_id": None if item.location_index is None else LOCATION_IDS[_index(item.location_index, "break.location_index", allow_exit=True)]}
        if item.finish_by_minute is not None:
            row["finish_by"] = _stamp(day, _finite(item.finish_by_minute, "break.finish_by_minute", 0, 2160))
        flexible_breaks.append(row)
    completed = _ids(completed_ids, known_ids, "completed_ids")
    missed = _ids(missed_ids, known_ids, "missed_ids")
    if completed & missed:
        raise InputError("An obligation cannot be both completed and missed")
    # A completed linked attraction is an accomplished visit even if the caller
    # omitted it from visited_indices. Never make that reserved ride optional.
    for row in commitments:
        row["completed"] = row["id"] in completed
        if row["completed"] and row.get("attraction_id"):
            visited.add(CANONICAL_IDS.index(row["attraction_id"]))
    for row in flexible_breaks:
        row["completed"] = row["id"] in completed
    active_reserved = {CANONICAL_IDS.index(row["attraction_id"]) for row in commitments if not row["completed"] and row.get("attraction_id")}
    pending_count = len(known_ids - completed) + len(required - visited - active_reserved)
    if pending_count > 6:
        raise InputError("At most six pending required rides and fixed commitments are supported")
    _bounded_sequence(received_observations, "received_observations", MAX_RECEIVED_OBSERVATIONS)
    observations, clocks = [], []
    for record in received_observations:
        row, clock = _normalize_observation(record, day)
        observations.append(row)
        clocks.append(clock)
    return {
        "schema_version": SCHEMA_VERSION, "now": _stamp(day, now),
        "information_as_of": _stamp(day, cutoff), "park_timezone": "Asia/Tokyo",
        "exit_deadline": _stamp(day, EXIT_DEADLINE_MINUTE),
        "current_location_id": LOCATION_IDS[current], "exit_location_id": LOCATION_IDS[EXIT_INDEX],
        "locations": [{"id": loc, "identity_verified": True,
                       "identity_source": "identity within preserved canonical matrix index mapping only; physical geometry unverified"}
                      for loc in LOCATION_IDS],
        "walking_edges": edges, "walking_time_multiplier": multiplier,
        "attractions": [{"id": aid, "location_id": LOCATION_IDS[i], "ride_minutes": ride_duration,
                         "utility": utility_values[i], "utility_source": "explicit caller-supplied utility; no tuning or inferred user preference",
                         "ride_finishes_by": _stamp(day, EXIT_DEADLINE_MINUTE)}
                        for i, aid in enumerate(CANONICAL_IDS)],
        "observations": observations, "visited_attraction_ids": [CANONICAL_IDS[i] for i in sorted(visited)],
        "must_ride_ids": [CANONICAL_IDS[i] for i in sorted(required)],
        "action_state": "idle", "commitments": commitments, "flexible_breaks": flexible_breaks,
        "forecasts": [], "legacy_non_preemptive_rests": [],
        "policy": {"freshness_minutes": ttl, "wait_buffer_minutes": overrun,
                   "wait_buffer_per_future_minute": 0, "boarding_buffer_minutes": 0,
                   "queue_admission_buffer_minutes": 0, "exit_buffer_minutes": EXIT_RESERVE_MINUTES,
                   "max_schedule_states": int(max_schedule_states)},
        "provenance": {"bridge_version": BRIDGE_VERSION, "scenario": scenario.label,
                       "day": day, "source_and_receipt_clocks": clocks,
                       "completed_commitment_ids": sorted(completed), "missed_commitment_ids": sorted(missed),
                       "all_reserved_attraction_ids": [CANONICAL_IDS[i] for i in sorted(all_reserved)],
                       "fictional_meal_locations": "explicit scenario co-location at an existing matrix node; no real restaurant coordinates",
                       "timed_entry": "fictional service scenario; no claim a real paid product was offered or booked",
                       "walking": "all coarse matrix edges are unverified estimates; identity flags verify IDs only",
                       "service": "supplied ride/commitment durations are assumptions independent of entry windows",
                       "queue_admission": "actual queue admission cutoffs unknown; no operating guarantee"},
    }


class _BoundedPracticalContext(PracticalContext):
    """Cooperative deadline only; ride, anchor and schedule constraints are inherited."""
    def __init__(self, request, *, wall_deadline, monotonic_clock, forecast_provider=None):
        self.wall_deadline = wall_deadline
        self.monotonic_clock = monotonic_clock
        self.check_time()
        super().__init__(request, forecast_provider=forecast_provider)
        self.check_time()

    def stamp(self, value):
        # Preserve actual fractional walking boundaries in replay handoff. The
        # generic default renders only seconds; this changes serialization only,
        # never the inherited timing or admission constraints.
        return value.astimezone(self.zone).isoformat()

    def check_time(self):
        if self.monotonic_clock() >= self.wall_deadline:
            raise WallTimeBudgetExceeded("bridge cooperative wall-time budget reached")

    def ride(self, aid, t, loc, override_wait=None):
        pinned = getattr(self, "_live_first_action", None)
        if override_wait is None and pinned is not None and aid == pinned[0]:
            step, reason = super().ride(aid, t, loc, override_wait=pinned[1])
            if step is not None:
                step = replace(step, conditions=tuple(x for x in step.conditions if x != "override_wait_is_supplied_scenario"),
                               detail={**step.detail, "first_action_wait_basis": "current_fresh_posted_wait"})
            return step, reason
        return super().ride(aid, t, loc, override_wait=override_wait)

    def _schedule(self, *args):
        self.check_time()
        return super()._schedule(*args)


@dataclass(frozen=True)
class BridgeContext:
    _request: dict
    context: _BoundedPracticalContext | None
    budget_seconds: float
    started: float
    clock: Callable[[], float]
    build_failure_reason: str | None = None
    _completed_ledger: tuple[str, ...] = ()
    _missed_ledger: tuple[str, ...] = ()

    @property
    def request(self):
        """A copy suitable for the existing generic/pre-day consumer."""
        return copy.deepcopy(self._request)

    @property
    def completed_ids(self):
        return self._completed_ledger

    @property
    def missed_ids(self):
        return self._missed_ledger

    @property
    def reserved_indices(self):
        return frozenset(CANONICAL_IDS.index(aid) for aid in self._request.get("provenance", {}).get("all_reserved_attraction_ids", ()))


def _validate_request_ledger(request):
    """Validate bridge accounting even when the numeric compiler was bypassed.

    This is bridge ledger validation, not a second scheduling constraint kernel.
    Missing ledger entries must not revive completed or missed reservations.
    """
    provenance = request.get("provenance", {})
    if not isinstance(provenance, dict):
        raise InputError("provenance must be an object")
    for field in ("commitments", "flexible_breaks", "attractions", "visited_attraction_ids"):
        if not isinstance(request.get(field, []), list):
            raise InputError(f"{field}: a concrete JSON array is required")
    obligations = {}
    linked_attractions = set()
    completed_linked = set()
    for field in ("commitments", "flexible_breaks"):
        for row in request.get(field, []):
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                raise InputError(f"{field}: every obligation needs a string ID")
            key = row["id"]
            if key in obligations:
                raise InputError("Obligation IDs must be unique across commitments and flexible breaks")
            completed = row.get("completed", False)
            if not isinstance(completed, bool):
                raise InputError(f"{key}.completed must be a boolean")
            obligations[key] = completed
            aid = row.get("attraction_id") if field == "commitments" else None
            if aid is not None:
                if not isinstance(aid, str) or aid not in CANONICAL_IDS:
                    raise InputError(f"{key}: reserved attraction must reference a canonical facility")
                linked_attractions.add(aid)
                if completed:
                    completed_linked.add(aid)
    completed = _ids(provenance.get("completed_commitment_ids", ()), obligations,
                     "provenance.completed_commitment_ids")
    missed = _ids(provenance.get("missed_commitment_ids", ()), obligations,
                  "provenance.missed_commitment_ids")
    if completed & missed:
        raise InputError("An obligation cannot be both completed and missed")
    flagged_completed = {key for key, value in obligations.items() if value}
    if completed != flagged_completed:
        raise InputError("Completed ledger must match every obligation's completed flag")
    supplied_attractions = set()
    for row in request.get("attractions", []):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise InputError("attractions: every attraction needs a string ID")
        supplied_attractions.add(row["id"])
    reserved = _ids(provenance.get("all_reserved_attraction_ids", ()),
                    set(CANONICAL_IDS) & supplied_attractions,
                    "provenance.all_reserved_attraction_ids")
    if reserved != linked_attractions:
        raise InputError("Reserved ledger must match all linked scenario attractions, including completed/missed ones")
    visited_values = request.get("visited_attraction_ids", [])
    if any(not isinstance(value, str) for value in visited_values):
        raise InputError("visited_attraction_ids must contain string IDs")
    if not completed_linked <= set(visited_values):
        raise InputError("Completed reserved attractions must also be marked visited")


def build_bounded_context(request=None, *, wall_time_budget_seconds=0.8,
                          forecast_provider=None, monotonic_clock=None, **compile_inputs) -> BridgeContext:
    """Build from explicit numeric inputs, or an already compiled shared request.

    The cooperative wall budget covers construction and all checks using this
    context. Build a new context for each real decision/information cutoff.
    Search caches may mutate internally; caller input is never changed.
    """
    if request is not None and compile_inputs:
        raise InputError("Supply a shared request or numeric compile inputs, not both")
    budget = _finite(wall_time_budget_seconds, "wall_time_budget_seconds", .001, 3)
    clock = monotonic_clock or perf_counter
    started = clock()
    deadline = started + budget
    # Preserve the explicit outcome ledger even if construction runs out of time
    # before a full context can be made. A budget cannot erase a known miss.
    provenance = request.get("provenance", {}) if isinstance(request, dict) else {}
    if not isinstance(provenance, dict):
        raise InputError("provenance must be an object")
    completed_values = provenance.get("completed_commitment_ids", ()) if request is not None else compile_inputs.get("completed_ids", ())
    missed_values = provenance.get("missed_commitment_ids", ()) if request is not None else compile_inputs.get("missed_ids", ())
    for values, field in ((completed_values, "completed_ids"), (missed_values, "missed_ids")):
        _bounded_sequence(values, field, 100)
        if any(not isinstance(value, str) for value in values):
            raise InputError(f"{field}: string IDs are required")
    completed_ledger, missed_ledger = tuple(sorted(completed_values)), tuple(sorted(missed_values))
    compiled = {}
    def check():
        if clock() >= deadline:
            raise WallTimeBudgetExceeded("bridge construction budget reached")
    try:
        check()
        if request is None:
            request = compile_request(**compile_inputs)
        check()
        _bounded_shape(request, check)
        _validate_request_ledger(request)
        check()
        compiled = copy.deepcopy(request)
        context = _BoundedPracticalContext(compiled, wall_deadline=deadline, monotonic_clock=clock,
                                           forecast_provider=forecast_provider)
        return BridgeContext(compiled, context, budget, started, clock, None, completed_ledger, missed_ledger)
    except WallTimeBudgetExceeded:
        return BridgeContext(compiled, None, budget, started, clock, "wall_time_budget_exceeded_during_context_build", completed_ledger, missed_ledger)


def _blocked_required(c, t, loc, remaining_rides, remaining_anchors):
    """Diagnose current individual blockers without claiming a full infeasibility proof."""
    blockers = []
    for key in sorted(remaining_anchors):
        a = c.anchors[key]
        destination = a["location_id"] if a["location_id"] is not None else loc
        if c.walk(loc, destination) is None:
            blockers.append({"id": key, "reason": "missing_walking_route_from_current_witness_endpoint"})
            continue
        aid = a.get("attraction_id")
        if aid and c.evidence(aid)["kind"] == "unavailable":
            blockers.append({"id": key, "attraction_id": aid, "reason": f"fresh_{c.evidence(aid)['status']}_reserved_facility"})
            continue
        if c.anchor(key, t, loc) is None:
            blockers.append({"id": key, "reason": "commitment_window_or_finish_constraint_unresolved_from_current_witness_endpoint"})
    for aid in sorted(remaining_rides):
        step, reason = c.ride(aid, t, loc)
        if step is None:
            blockers.append({"id": aid, "reason": reason,
                             "scope": "current_witness_endpoint; future deliberate waits not searched"})
    return blockers


def check_next_ride(bridge: BridgeContext, next_index=None, *, wait_until_minute=None,
                    phase="decision", recovery=False) -> dict:
    """Verify an optional next ride, every pending fixed obligation, and exit.

    next_index=None supplies the required-only fallback. Optional explicit virtual
    waiting uses the generic evaluator and never refreshes information_as_of.
    phase labels initial validation versus a later decision; neither a missing
    witness nor a bounded-search stop is called a proof of infeasibility.
    """
    if not isinstance(bridge, BridgeContext):
        raise InputError("check_next_ride requires BridgeContext")
    if not isinstance(recovery, bool):
        raise InputError("recovery must be a boolean")
    if not isinstance(phase, str) or phase not in {"initial", "decision"}:
        raise InputError("phase must be initial or decision")
    result = {"schema_version": BRIDGE_VERSION, "status": "not_verified", "reason": None,
              "phase": phase, "scenario_feasibility_verified": False, "feasible": False,
              "overall_success_possible": False if bridge.missed_ids else None,
              "feasibility_scope": "remaining_only_after_miss" if recovery and bridge.missed_ids else "all_required_obligations",
              "infeasibility_proven": False, "witness": None, "candidate_step": None,
              "required_only": next_index is None, "completed_commitment_ids": list(bridge.completed_ids),
              "missed_commitment_ids": list(bridge.missed_ids),
              "guarantee": "conditional supplied-scenario witness only; not real operating, queue-admission, or walking guarantees"}
    c = bridge.context
    def finish(reason, status="not_verified"):
        result.update(status=status, reason=reason)
        result["budget"] = {"wall_time_budget_seconds": bridge.budget_seconds,
                            "elapsed_seconds": max(0.0, bridge.clock() - bridge.started),
                            "schedule_states": c.schedule_states if c is not None else 0,
                            "semantics": "cooperative context-build and recursive-search deadline; not hard real-time preemption"}
        return result
    if c is None:
        return finish(bridge.build_failure_reason, "budget")
    result.update(planning_now=c.stamp(c.now), information_as_of=c.stamp(c.information_as_of),
                  remaining_commitment_ids=sorted(c.anchors), remaining_must_ride_ids=sorted(c.required),
                  excluded_observations=copy.deepcopy(c.excluded_observations),
                  excluded_forecasts=copy.deepcopy(c.excluded_forecasts))
    if bridge.missed_ids and not recovery:
        result["diagnostic_category"] = "already_missed_required_obligation"
        return finish("missed_required_obligations")
    if recovery and bridge.missed_ids:
        # A remaining must-ride cannot silently turn a lost timed-entry booking
        # into an executable standby recovery. Both checker paths must abstain
        # until an explicit supported recovery contract resolves this overlap.
        missed_dpa_attractions = {row["attraction_id"]
            for row in bridge._request.get("commitments", [])
            if row["id"] in bridge.missed_ids and row["kind"] == "dpa" and row.get("attraction_id")}
        unvisited_required = set(bridge._request.get("must_ride_ids", [])) - c.visited
        reserved = set(bridge._request.get("provenance", {}).get("all_reserved_attraction_ids", []))
        unsupported = unvisited_required & missed_dpa_attractions & reserved
        if unsupported:
            result["unsupported_recovery_attraction_ids"] = sorted(unsupported)
            result["remaining_must_ride_ids"] = sorted(set(result["remaining_must_ride_ids"]) | unsupported)
            result["diagnostic_category"] = "unsupported_reserved_must_ride_recovery"
            return finish("reserved_must_ride_recovery_not_supported")
    try:
        c.check_time()
        if recovery and bridge.missed_ids:
            # Explicit recovery scope: remove lost obligations from this separate
            # search only. Original request and missed ledger stay unchanged;
            # no missed item is ever relabeled completed.
            request = bridge.request
            missed = set(bridge.missed_ids)
            request["commitments"] = [row for row in request.get("commitments", []) if row["id"] not in missed]
            request["flexible_breaks"] = [row for row in request.get("flexible_breaks", []) if row["id"] not in missed]
            provider = c.forecast_provider
            c = _BoundedPracticalContext(request, wall_deadline=c.wall_deadline, monotonic_clock=c.monotonic_clock)
            c.forecast_provider = provider
            result["remaining_commitment_ids"] = sorted(c.anchors)
        sequence = []
        if wait_until_minute is not None:
            day = bridge._request.get("provenance", {}).get("day", c.now.astimezone(JST).date().isoformat())
            sequence.append({"kind": "wait_until", "until": _stamp(day, wait_until_minute)})
        if next_index is not None:
            index = _index(next_index, "next_index")
            aid = CANONICAL_IDS[index]
            result["attraction_id"] = aid
            result["attraction_index"] = index
            if aid not in c.rides:
                raise InputError("next_index is absent from the supplied request's catalog")
            if aid in c.visited:
                return finish("visited_attraction_excluded")
            if aid in c.reserved or index in bridge.reserved_indices:
                return finish("reserved_attraction_excluded_from_optional_choices")
            result["evidence"] = c.evidence(aid)
            sequence.append(aid)
            evidence = c.evidence(aid)
            c._live_first_action = (aid, evidence["wait_minutes"]) if wait_until_minute is None and evidence["kind"] == "fresh_open" else None
        # The audited evaluator invokes the same ride/anchor/schedule methods.
        # No bridge copy of meal, DPA, exit or forecast constraints is maintained.
        try:
            witness = evaluate_sequence(c, sequence, complete_remaining=True)
        finally:
            c._live_first_action = None
        c.check_time()
        result["witness"] = witness
        result["generic_status"] = witness["status"]
        result["remaining_commitment_ids"] = witness["remaining_commitment_ids"]
        result["remaining_must_ride_ids"] = witness["remaining_must_ride_ids"]
        result["conditions"] = witness["conditions"]
        result["excluded_forecasts"] = witness["excluded_forecasts"]
        if witness["status"] == "search_budget_exceeded_not_verified":
            return finish(witness.get("reason", "bounded_required_order_search_exhausted"), "budget")
        if not witness["feasible"]:
            steps = witness["steps"]
            t = datetime.fromisoformat(steps[-1]["planning_finish_at"]) if steps else c.now
            loc = steps[-1]["location_id"] if steps else c.current
            result["required_blockers"] = _blocked_required(c, t, loc, witness["remaining_must_ride_ids"], witness["remaining_commitment_ids"])
            result["diagnostic_category"] = "initial_scenario_not_verified" if phase == "initial" else "current_required_plan_not_verified"
            return finish(witness.get("reason") or witness["status"])
        result.update(scenario_feasibility_verified=True, feasible=True,
                      overall_success_possible=not bool(bridge.missed_ids),
                      conditional=witness["conditional"],
                      planned_exit_at=witness["planned_exit_at"],
                      spare_minutes_after_exit_buffer=witness["spare_minutes_after_exit_buffer"])
        if next_index is not None:
            result["candidate_step"] = next(s for s in witness["sequence_steps"] if s["kind"] == "ride")
            detail = result["candidate_step"]["detail"]
            result["first_action_wait_basis"] = detail.get("first_action_wait_basis", "supplied_forecast_scenario" if "wait_forecast" in detail else "explicit_fallback_or_frozen_observation")
        result["next_required_step"] = witness["completion_steps"][0] if witness["completion_steps"] else None
        if not witness["utility_total_is_complete"]:
            result["conditions"] = sorted(set(result["conditions"]) | {"supplied_utility_unresolved"})
            result["conditional"] = True
        return finish("all_remaining_obligations_and_exit_have_scenario_witness",
                      "conditional" if result["conditional"] else "feasible")
    except WallTimeBudgetExceeded:
        return finish("wall_time_budget_exceeded_not_verified", "budget")
