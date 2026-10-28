"""Versioned, offline input contract over the unchanged revision-5 constraints.

Importing this module does not access the network, user devices, or raw feeds.
The provider is caller-supplied and must be a pure, as-of-safe scenario lookup.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import importlib.util
from pathlib import Path
import sys
from typing import Protocol

SCHEMA_VERSION = "wickward.generic-input.v1"
FROZEN_SHA256 = "8868c004a67fc57da51044590d0b71958ab5f012fbf1581fe86cd0394a96d613"
_FROZEN_PATH = Path(__file__).resolve().parents[1] / "original_revision5/wickward-next-ride-prototype/wickward/engine.py"
if hashlib.sha256(_FROZEN_PATH.read_bytes()).hexdigest() != FROZEN_SHA256:
    raise RuntimeError("Frozen revision5 engine changed; review/reversion the adapter explicitly")
_spec = importlib.util.spec_from_file_location("_wickward_frozen_revision5_generic_io", _FROZEN_PATH)
assert _spec is not None and _spec.loader is not None
_core = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _core
_spec.loader.exec_module(_core)
InputError = _core.InputError
SearchBudgetExceeded = _core.SearchBudgetExceeded
Step = _core.Step
Schedule = _core.Schedule
instant, number, mins, diff = _core.instant, _core.number, _core.mins, _core.diff


def _label(value, field):
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{field}: a nonempty source/ID string is required")
    return value


def _aware(value, field):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InputError(f"{field}: an aware datetime is required")
    return value.astimezone(timezone.utc)


def _array(request, field):
    values = request.get(field, [])
    if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
        raise InputError(f"{field}: an array of objects is required")
    return values


@dataclass(frozen=True)
class WaitForecast:
    """One supplied wait scenario; valid interval [valid_from, valid_until)."""
    attraction_id: str
    issued_at: datetime
    valid_from: datetime
    valid_until: datetime
    wait_minutes: float
    source: str
    scenario_id: str = "supplied_scenario"

    @classmethod
    def from_json(cls, row):
        if not isinstance(row, dict):
            raise InputError("forecast must be an object")
        return cls(row.get("attraction_id"), instant(row.get("issued_at"), "forecast.issued_at"),
                   instant(row.get("valid_from"), "forecast.valid_from"),
                   instant(row.get("valid_until"), "forecast.valid_until"),
                   row.get("wait_minutes"), row.get("source"), row.get("scenario_id", "supplied_scenario"))


def _validate_forecast(value):
    if not isinstance(value, WaitForecast):
        raise InputError("forecast provider must return WaitForecast or None")
    _label(value.attraction_id, "forecast.attraction_id")
    _label(value.source, "forecast.source")
    _label(value.scenario_id, "forecast.scenario_id")
    issued = _aware(value.issued_at, "forecast.issued_at")
    start = _aware(value.valid_from, "forecast.valid_from")
    end = _aware(value.valid_until, "forecast.valid_until")
    if end <= start:
        raise InputError("forecast.valid_until must be after valid_from")
    return replace(value, issued_at=issued, valid_from=start, valid_until=end,
                   wait_minutes=number(value.wait_minutes, "forecast.wait_minutes"))


class ForecastProvider(Protocol):
    def forecast(self, *, attraction_id: str, arrival_at: datetime,
                 information_as_of: datetime) -> WaitForecast | None:
        """Return a scenario for this arrival, using only information available by cutoff."""
        ...


class JSONForecastProvider:
    """Pure interval lookup. Latest allowable issue wins, never a later issue."""
    def __init__(self, rows):
        if not isinstance(rows, list):
            raise InputError("forecasts must be an array")
        self.forecasts = tuple(_validate_forecast(WaitForecast.from_json(r)) for r in rows)

    def forecast(self, *, attraction_id, arrival_at, information_as_of):
        rows = [r for r in self.forecasts if r.attraction_id == attraction_id
                and r.issued_at <= information_as_of and r.valid_from <= arrival_at < r.valid_until]
        if not rows:
            return None
        newest_issue = max(r.issued_at for r in rows)
        newest = [r for r in rows if r.issued_at == newest_issue]
        if len({r.wait_minutes for r in newest}) > 1:
            raise InputError(f"{attraction_id}: conflicting overlapping forecasts at the same issue time")
        return sorted(newest, key=lambda r: (r.valid_from, r.valid_until, r.source, r.scenario_id))[-1]


class PracticalContext(_core.Context):
    """Explicitly versioned subclass. All input objects are copied before parsing."""
    def __init__(self, request, forecast_provider=None):
        if not isinstance(request, dict):
            raise InputError("The request must be an object")
        request = copy.deepcopy(request)
        supported = {"schema_version", "information_as_of", "now", "exit_deadline", "park_timezone",
                     "locations", "current_location_id", "exit_location_id", "walking_edges",
                     "walking_time_multiplier", "attractions", "observations", "visited_attraction_ids",
                     "must_ride_ids", "action_state", "commitments", "policy", "flexible_breaks",
                     "forecasts", "legacy_non_preemptive_rests", "provenance"}
        unknown_fields = set(request) - supported
        if unknown_fields:
            raise InputError(f"Unsupported input fields: {sorted(unknown_fields)}; constraints cannot be silently ignored")
        if request.get("schema_version") != SCHEMA_VERSION:
            raise InputError(f"schema_version must be {SCHEMA_VERSION!r}")
        self.information_as_of = instant(request.get("information_as_of"), "information_as_of")
        planning_now = instant(request.get("now"), "now")
        if self.information_as_of > planning_now:
            raise InputError("information_as_of must not be later than planning now")
        if request.get("legacy_non_preemptive_rests", []) != []:
            raise InputError("legacy_non_preemptive_rests require the separate historical rest kernel; "
                             "they are not fixed appointments or flexible breaks")
        self.last_reported = {}
        self.excluded_observations = []
        observations = _array(request, "observations")
        for row in observations:
            aid = _label(row.get("attraction_id"), "observation.attraction_id")
            if "source_updated_at" in row:
                raise InputError("Use observed_at for actual observation/poll time and changed_at for value-change time; "
                                 "source_updated_at is ambiguous in this schema")
            fetched = instant(row.get("fetched_at"), "observation.fetched_at")
            observed = instant(row["observed_at"], "observation.observed_at") if row.get("observed_at") is not None else None
            changed = instant(row["changed_at"], "observation.changed_at") if row.get("changed_at") is not None else None
            if any(t is not None and t > fetched for t in (observed, changed)):
                raise InputError("observed_at/changed_at may not be later than fetched_at")
            if observed is not None and changed is not None and changed > observed:
                raise InputError("changed_at may not be later than this snapshot's observed_at")
            if any(t is not None and t > self.information_as_of for t in (observed, changed, fetched)):
                self.excluded_observations.append({"attraction_id": aid, "reason": "after_information_as_of"})
                continue
            if not isinstance(row.get("status"), str) or row.get("status") not in {"open", "closed", "down", "unknown"}:
                raise InputError("Observation status must be open, closed, down, or unknown")
            if row.get("wait_minutes") is not None:
                number(row["wait_minutes"], "observation.wait_minutes")
            _label(row.get("source"), "observation.source")
            parsed = {**row, "observed": observed, "changed": changed, "fetched": fetched}
            # A later retrieval alone cannot refresh or supersede a newer source state.
            rank = (observed or changed or datetime.min.replace(tzinfo=timezone.utc), fetched)
            previous = self.last_reported.get(aid)
            if previous is not None and rank == previous["rank"] and (row["status"], row.get("wait_minutes")) != (previous["status"], previous.get("wait_minutes")):
                raise InputError(f"{aid}: conflicting observations have identical evidence timestamps")
            if previous is None or rank > previous["rank"]:
                self.last_reported[aid] = {**parsed, "rank": rank}
        # Only true source observation times enter the frozen engine's freshness clock.
        request["observations"] = [
            {"attraction_id": aid, "status": row["status"], "wait_minutes": row.get("wait_minutes"),
             "source_updated_at": row["observed"].isoformat(), "fetched_at": row["fetched"].isoformat(),
             "source": row["source"]}
            for aid, row in self.last_reported.items() if row["observed"] is not None]
        super().__init__(request)
        if any(row["attraction_id"] not in self.rides for row in observations):
            raise InputError("Observation references an unknown attraction")
        if not isinstance(self.raw.get("action_state", "idle"), str) or self.raw.get("action_state", "idle") not in {"idle", "in_queue", "riding"}:
            raise InputError("action_state must be idle, in_queue, or riding")
        if "flexible_breaks" in request:
            breaks = self.unique(request["flexible_breaks"], "flexible_breaks")
        else:
            breaks = {}
        for key, row in breaks.items():
            if key in self.anchors:
                raise InputError(f"{key}: duplicate commitment/flexible break ID")
            if not isinstance(row.get("completed", False), bool):
                raise InputError(f"{key}.completed must be a JSON boolean")
            if row.get("completed", False):
                continue
            loc = row.get("location_id")
            if loc is not None:
                self.location(loc, f"{key}.location_id")
            earliest = instant(row.get("earliest_start"), f"{key}.earliest_start")
            latest = instant(row.get("latest_start"), f"{key}.latest_start")
            if latest < earliest:
                raise InputError(f"{key}: latest_start must be at or after earliest_start")
            duration = number(row.get("duration_minutes"), f"{key}.duration_minutes", .01)
            finish = instant(row["finish_by"], f"{key}.finish_by") if row.get("finish_by") is not None else None
            if "kind" in row and row["kind"] != "flexible_break":
                raise InputError(f"{key}: flexible_breaks must not disguise another commitment kind")
            self.anchors[key] = {**row, "kind": "flexible_break", "earliest": earliest, "latest": latest,
                                 "duration_minutes": duration, "location_id": loc, "finish_by": finish}
        if len(self.required) + len(self.anchors) > 6:
            raise InputError("At most six pending commitments, flexible breaks and unreserved must-rides are supported")
        if forecast_provider is not None and request.get("forecasts"):
            raise InputError("Supply JSON forecasts or a forecast_provider, not both")
        self.excluded_forecasts = []
        allowed_forecasts = []
        for row in _array(request, "forecasts"):
            issued = instant(row.get("issued_at"), "forecast.issued_at")
            aid = row.get("attraction_id")
            if not isinstance(aid, str) or aid not in self.rides:
                raise InputError("Forecast references an unknown attraction")
            if issued > self.information_as_of:
                self.excluded_forecasts.append({"attraction_id": aid, "issued_at": issued.isoformat(),
                                                "reason": "issued_after_information_as_of"})
            else:
                allowed_forecasts.append(row)
        self.forecast_provider = forecast_provider if forecast_provider is not None else JSONForecastProvider(allowed_forecasts)
        if not callable(getattr(self.forecast_provider, "forecast", None)):
            raise InputError("forecast_provider must implement forecast()")

    def evidence(self, aid):
        row = self.last_reported.get(aid)
        if row is None:
            return {"kind": "missing", "status": "unknown", "age_minutes": None, "wait_minutes": None,
                    "observed_at": None, "changed_at": None, "poll_freshness_known": False}
        details = {"observed_at": self.stamp(row["observed"]) if row["observed"] else None,
                   "changed_at": self.stamp(row["changed"]) if row["changed"] else None,
                   "fetched_at": self.stamp(row["fetched"]), "source": row["source"],
                   "last_reported_status": row["status"], "last_reported_wait_minutes": row.get("wait_minutes"),
                   "change_age_minutes": diff(self.now, row["changed"]) if row["changed"] else None,
                   "poll_freshness_known": row["observed"] is not None}
        if row["observed"] is None:
            return {"kind": "unverified_last_reported", "status": "unknown", "age_minutes": None,
                    "wait_minutes": None, **details,
                    "warning": "Value-change age and fetch time do not establish source polling freshness"}
        return {**super().evidence(aid), **details}

    def _forecast(self, aid, arrival):
        value = self.forecast_provider.forecast(attraction_id=aid, arrival_at=arrival,
                                                information_as_of=self.information_as_of)
        if value is None:
            return None
        if not isinstance(value, WaitForecast):
            raise InputError("forecast provider must return WaitForecast or None")
        if value.attraction_id != aid:
            raise InputError("forecast provider returned the wrong attraction ID")
        issued = _aware(value.issued_at, "forecast.issued_at")
        if issued > self.information_as_of:
            excluded = {"attraction_id": aid, "issued_at": issued.isoformat(),
                        "reason": "issued_after_information_as_of"}
            if excluded not in self.excluded_forecasts:
                self.excluded_forecasts.append(excluded)
            return None
        value = _validate_forecast(value)
        if not value.valid_from <= arrival < value.valid_until:
            raise InputError("forecast provider returned an interval not covering arrival")
        return value

    def ride(self, aid, t, loc, override_wait=None):
        forecast = None
        if override_wait is not None:
            override_wait = number(override_wait, "override_wait")
        else:
            route = self.walk(loc, self.rides[aid]["location_id"])
            if route is not None:
                forecast = self._forecast(aid, t + mins(route[0]))
                if forecast is not None:
                    override_wait = forecast.wait_minutes
        step, reason = super().ride(aid, t, loc, override_wait=override_wait)
        if step is None:
            return None, reason
        flags, detail = set(step.conditions), copy.deepcopy(step.detail)
        if forecast is not None:
            flags.add("forecast_wait_is_scenario_not_open_status_evidence")
            detail["wait_forecast"] = {"issued_at": self.stamp(forecast.issued_at),
                "valid_from": self.stamp(forecast.valid_from), "valid_until": self.stamp(forecast.valid_until),
                "source": forecast.source, "scenario_id": forecast.scenario_id}
        elif override_wait is not None:
            flags.add("override_wait_is_supplied_scenario")
        return replace(step, conditions=tuple(sorted(flags)), detail=detail), None

    def ceiling(self, aid):
        # Revision5's static-snapshot binary search relies on monotone feasibility.
        # Time-varying forecasts can make a smaller wait enter a later ride's spike.
        raise InputError("ceiling is disabled in the generic forecast context: feasibility may be non-monotone; "
                         "evaluate each explicit wait scenario instead of claiming an at-or-below threshold")

    def anchor(self, key, t, loc):
        a = self.anchors[key]
        if a["kind"] != "flexible_break":
            return super().anchor(key, t, loc)
        destination = a["location_id"] if a["location_id"] is not None else loc
        route = self.walk(loc, destination)
        if route is None:
            return None
        walking, flags, sources = route
        arrive = t + mins(walking)
        start = max(arrive, a["earliest"])
        if start > a["latest"]:  # A break's latest START is inclusive; not a finish deadline.
            return None
        end = start + mins(a["duration_minutes"])
        if a.get("finish_by") and end > a["finish_by"]:
            return None
        return Step(key, destination, start, end, end, walking, walking + a["duration_minutes"], flags,
                    {"kind": "flexible_break", "commitment_id": key, "arrive_at": self.stamp(arrive),
                     "service_start_at": self.stamp(start), "service_finish_at": self.stamp(end),
                     "latest_start_is_inclusive": True, "location_id": destination,
                     "location_free": a["location_id"] is None, "walking_sources": sorted(set(sources)),
                     "duration_is_supplied_assumption": True})


def build_context(request: dict, forecast_provider: ForecastProvider | None = None) -> PracticalContext:
    return PracticalContext(request, forecast_provider=forecast_provider)


def _step_json(c, step):
    arrival = instant(step.detail["arrive_at"], "step.arrive_at") if "arrive_at" in step.detail else step.start
    return {"id": step.key, "kind": step.detail["kind"], "location_id": step.location,
            "departure_at": c.stamp(arrival - mins(step.walking)), "arrival_at": c.stamp(arrival),
            "start_at": c.stamp(step.start), "expected_finish_at": c.stamp(step.expected_end),
            "planning_finish_at": c.stamp(step.end), "walking_minutes": step.walking,
            "active_minutes": step.active, "conditions": list(step.conditions), "detail": copy.deepcopy(step.detail)}


def evaluate_sequence(request_or_context, sequence, *, complete_remaining=True, forecast_provider=None):
    """Evaluate explicit ordered steps, optionally append every remaining obligation.

    Strings are attraction IDs; objects are {kind: ride|commitment|flexible_break, id: ...}.
    Explicit idle steps are {kind: wait_until, until: aware ISO timestamp}.
    No anchor is inserted between supplied steps. Duplicate/visited/reserved rides fail.
    A feasible result is only feasibility under supplied scenario assumptions.
    """
    if isinstance(request_or_context, PracticalContext):
        if forecast_provider is not None:
            raise InputError("A context already has its forecast provider")
        c = request_or_context
    else:
        c = build_context(request_or_context, forecast_provider=forecast_provider)
    if not isinstance(sequence, (list, tuple)):
        raise InputError("sequence must be an array of ride IDs or explicit step objects")
    if not isinstance(complete_remaining, bool):
        raise InputError("complete_remaining must be a JSON boolean")
    c.schedule.cache_clear()
    c.schedule_states = 0
    selected = set(c.visited)
    remaining_rides, remaining_anchors = set(c.required), set(c.anchors)
    steps, completion = [], []
    t, loc = c.now, c.current
    result = {"schema_version": SCHEMA_VERSION, "information_as_of": c.stamp(c.information_as_of),
              "planning_now": c.stamp(c.now), "feasible": False,
              "guarantee": "scenario_feasibility_only; no operating or queue-admission guarantee"}

    def finish(status, *, tail=None, failed_index=None, reason=None):
        all_steps = steps + completion
        awarded = set()
        for step in all_steps:
            if step.detail["kind"] == "ride":
                awarded.add(step.key)
            elif step.detail["kind"] != "wait_until":
                aid = c.anchors[step.key].get("attraction_id")
                if aid is not None and aid not in c.visited:
                    awarded.add(aid)
        unknown = sorted(aid for aid in awarded if c.utility(aid)[0] is None)
        unknown_required = sorted(aid for aid in c.required | {a["attraction_id"] for a in c.anchors.values() if a.get("attraction_id")} if c.utility(aid)[0] is None)
        known_total = sum(c.utility(aid)[0] for aid in awarded if c.utility(aid)[0] is not None)
        flags = {flag for s in all_steps for flag in s.conditions}
        flags.update(flag for aid in awarded for flag in c.utility(aid)[2])
        if tail is not None:
            flags.update(tail.conditions)
        result.update(status=status, sequence_steps=[_step_json(c, s) for s in steps],
                      completion_steps=[_step_json(c, s) for s in completion],
                      steps=[_step_json(c, s) for s in all_steps],
                      remaining_must_ride_ids=sorted(remaining_rides), remaining_commitment_ids=sorted(remaining_anchors),
                      unknown_utility_ids=unknown, unknown_required_utility_ids=unknown_required,
                      utility_total=None if unknown or unknown_required or not result["feasible"] else known_total, known_utility_subtotal=known_total,
                      utility_total_is_complete=result["feasible"] and not unknown and not unknown_required, awarded_attraction_ids=sorted(awarded),
                      conditions=sorted(flags), conditional=bool(flags),
                      excluded_observations=copy.deepcopy(c.excluded_observations),
                      excluded_forecasts=copy.deepcopy(c.excluded_forecasts),
                      schedule_states=c.schedule_states)
        waits = [step for step in all_steps if step.detail["kind"] == "wait_until"]
        if waits:
            result["explicit_waiting_minutes"] = sum(step.detail["waiting_minutes"] for step in waits)
            result["waiting_does_not_advance_information_as_of"] = True
        if failed_index is not None:
            result["failed_index"] = failed_index
        if reason is not None:
            result["reason"] = reason
        if tail is not None:
            result.update(planned_exit_at=c.stamp(tail.exit_at), exit_deadline=c.stamp(c.deadline),
                          exit_walk_start_at=c.stamp(all_steps[-1].end if all_steps else c.now),
                          exit_walking_minutes=tail.walking - sum(s.walking for s in completion),
                          exit_reserve_until=c.stamp(tail.exit_at + mins(c.exit_buffer)),
                          exit_buffer_minutes=c.exit_buffer,
                          spare_minutes_after_exit_buffer=diff(c.deadline, tail.exit_at) - c.exit_buffer,
                          total_walking_minutes=sum(s.walking for s in steps) + tail.walking,
                          planning_elapsed_minutes=diff(tail.exit_at, c.now))
        return result

    if c.raw.get("action_state", "idle") != "idle":
        return finish("current_action_locked", reason="finish_or_explicitly_end_current_action")
    try:
        for index, item in enumerate(sequence):
            if isinstance(item, str):
                kind, key = "ride", item
            elif isinstance(item, dict):
                if item.get("kind") == "wait_until":
                    if set(item) - {"kind", "until"}:
                        raise InputError("wait_until steps support only kind and until")
                    until = instant(item.get("until"), "wait_until.until")
                    if until < t:
                        raise InputError("wait_until.until must be at or after the current sequence time")
                    if until > c.deadline:
                        result["attempted_wait_until"] = c.stamp(until)
                        return finish("sequence_not_verified_feasible", failed_index=index,
                                      reason="wait_extends_beyond_exit_deadline")
                    duration = diff(until, t)
                    step = Step(f"wait_until@{index}", loc, t, until, until, 0., duration,
                                ("waiting_does_not_refresh_observations_or_forecasts",),
                                {"kind": "wait_until", "arrive_at": c.stamp(t),
                                 "wait_start_at": c.stamp(t), "wait_until": c.stamp(until),
                                 "waiting_minutes": duration, "location_unchanged": True,
                                 "information_as_of": c.stamp(c.information_as_of)})
                    steps.append(step)
                    t = until
                    continue
                if set(item) - {"kind", "id"}:
                    raise InputError("Sequence objects support only kind and id; extra timing/constraint fields are not implemented")
                kind, key = item.get("kind"), item.get("id")
            else:
                raise InputError("sequence items must be strings or objects")
            if not isinstance(key, str):
                raise InputError("sequence step id must be a string")
            if not isinstance(kind, str):
                raise InputError("sequence step kind must be a string")
            if kind == "ride":
                if key not in c.rides:
                    raise InputError(f"Unknown sequence attraction {key}")
                if key in selected or key in c.reserved:
                    return finish("invalid_sequence", failed_index=index, reason="duplicate_visited_or_reserved_ride")
                route = c.walk(loc, c.rides[key]["location_id"])
                declared_open = c.rides[key].get("opens_at")
                departure = max(t, declared_open - mins(route[0])) if declared_open and route else t
                step, reason = c.ride(key, departure, loc)
                if step is None:
                    return finish("sequence_not_verified_feasible", failed_index=index, reason=reason)
                selected.add(key)
                remaining_rides.discard(key)
            elif kind in {"commitment", "flexible_break"}:
                if key not in c.anchors:
                    raise InputError(f"Unknown pending sequence commitment {key}")
                if kind == "flexible_break" and c.anchors[key]["kind"] != "flexible_break":
                    raise InputError("A flexible_break step must reference a flexible break")
                if key not in remaining_anchors:
                    return finish("invalid_sequence", failed_index=index, reason="duplicate_commitment")
                step = c.anchor(key, t, loc)
                if step is None:
                    return finish("sequence_not_verified_feasible", failed_index=index, reason="commitment_window_or_route_unresolved")
                remaining_anchors.remove(key)
            else:
                raise InputError("sequence.kind must be ride, commitment, flexible_break, or wait_until")
            steps.append(step)
            t, loc = step.end, step.location
        if not complete_remaining and (remaining_rides or remaining_anchors):
            return finish("incomplete_obligations", reason="explicit_sequence_leaves_required_nodes")
        tail = c.schedule(t, loc, tuple(sorted(remaining_rides)), tuple(sorted(remaining_anchors)))
        if tail is None:
            return finish("remaining_obligations_or_exit_not_verified_feasible")
        completion.extend(tail.steps)
        remaining_rides.clear()
        remaining_anchors.clear()
        result["feasible"] = True
        out = finish("scenario_feasible", tail=tail)
        if not out["utility_total_is_complete"]:
            out["status"] = "scenario_feasible_utility_unresolved"
        return out
    except SearchBudgetExceeded:
        return finish("search_budget_exceeded_not_verified", reason="bounded_required_order_search_exhausted")
