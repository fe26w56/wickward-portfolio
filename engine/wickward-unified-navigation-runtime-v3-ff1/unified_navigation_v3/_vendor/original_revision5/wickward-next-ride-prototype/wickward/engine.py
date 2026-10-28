"""Pure decision function: explicit inputs in, evidence-labelled suggestions out.

No network, storage, GPS, user inference, wall clock, or randomness. The two-step
utility-density objective is a next-choice heuristic, not a full-day optimizer.
All timing checks use a configurable stress scenario, not a calibrated guarantee.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import heapq
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class InputError(ValueError):
    pass


class SearchBudgetExceeded(RuntimeError):
    pass


def instant(value, field):
    if not isinstance(value, str):
        raise InputError(f"{field}: an ISO 8601 timestamp with UTC offset is required")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputError(f"{field}: invalid timestamp") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise InputError(f"{field}: UTC offset is required; local clock strings are ambiguous")
    return dt.astimezone(timezone.utc)


def number(value, field, minimum=0, maximum=2160):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise InputError(f"{field}: a finite number from {minimum} to {maximum} is required")
    return float(value)


def mins(value):
    return timedelta(minutes=value)


def diff(a, b):
    return (a - b).total_seconds() / 60


@dataclass(frozen=True)
class Step:
    key: str
    location: str
    start: datetime
    end: datetime
    expected_end: datetime
    walking: float
    active: float
    conditions: tuple[str, ...]
    detail: dict


@dataclass(frozen=True)
class Schedule:
    exit_at: datetime
    walking: float
    active: float
    steps: tuple[Step, ...]
    conditions: tuple[str, ...]


class Context:
    def __init__(self, request):
        if not isinstance(request, dict):
            raise InputError("The request must be an object")
        self.raw = request
        self.now = instant(request.get("now"), "now")
        self.deadline = instant(request.get("exit_deadline"), "exit_deadline")
        if not 0 < diff(self.deadline, self.now) <= 2160:
            raise InputError("exit_deadline must be after now and at most 36 hours away")
        try:
            self.zone = ZoneInfo(request.get("park_timezone", "Asia/Tokyo"))
        except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
            raise InputError("park_timezone must name an IANA time zone") from exc
        self.locations = self.unique(request.get("locations", []), "locations")
        if not self.locations:
            raise InputError("locations must not be empty")
        self.current = self.location(request.get("current_location_id"), "current_location_id")
        self.exit = self.location(request.get("exit_location_id"), "exit_location_id")
        self.config = request.get("policy", {})
        if not isinstance(self.config, dict):
            raise InputError("policy must be an object")
        for field in ("observations", "walking_edges", "visited_attraction_ids", "must_ride_ids"):
            if not isinstance(request.get(field, []), list):
                raise InputError(f"{field} must be an array")
        for field in ("visited_attraction_ids", "must_ride_ids"):
            if any(not isinstance(value, str) for value in request.get(field, [])):
                raise InputError(f"{field} must contain string IDs")
        for field in ("observations", "walking_edges"):
            if any(not isinstance(value, dict) for value in request.get(field, [])):
                raise InputError(f"{field} must contain objects")
        self.schedule_states = 0
        self.max_states = self.config.get("max_schedule_states", 100000)
        if isinstance(self.max_states, bool) or not isinstance(self.max_states, int) or not 1 <= self.max_states <= 250000:
            raise InputError("max_schedule_states must be an integer from 1 to 250000")
        self.ttl = number(self.config.get("freshness_minutes", 10), "freshness_minutes", 0.01)
        self.exit_buffer = number(self.config.get("exit_buffer_minutes", 15), "exit_buffer_minutes")
        self.uncertainty = number(self.config.get("wait_buffer_minutes", 10), "wait_buffer_minutes")
        self.growth = number(self.config.get("wait_buffer_per_future_minute", .15), "wait_buffer_per_future_minute", maximum=10)
        self.boarding = number(self.config.get("boarding_buffer_minutes", 3), "boarding_buffer_minutes")
        self.admission_buffer = number(self.config.get("queue_admission_buffer_minutes", 5), "queue_admission_buffer_minutes")
        self.pace = number(request.get("walking_time_multiplier", 1), "walking_time_multiplier", .1, 10)
        self.fallback_utility = self.config.get("unknown_utility_fallback")
        if self.fallback_utility is not None:
            self.fallback_utility = number(self.fallback_utility, "unknown_utility_fallback", maximum=1e6)
        self.rides = self.unique(request.get("attractions", []), "attractions")
        if len(self.rides) > 40:
            raise InputError("This bounded prototype supports at most 40 attractions")
        for aid, a in self.rides.items():
            self.location(a.get("location_id"), f"{aid}.location_id")
            number(a.get("ride_minutes"), f"{aid}.ride_minutes", .01)
            if a.get("utility") is not None:
                number(a["utility"], f"{aid}.utility", maximum=1e6)
                if not isinstance(a.get("utility_source"), str) or not a["utility_source"].strip():
                    raise InputError(f"{aid}: utility_source must identify the supplied priority or proxy")
            if a.get("fallback_wait_minutes") is not None:
                number(a["fallback_wait_minutes"], f"{aid}.fallback_wait_minutes")
                if not isinstance(a.get("fallback_wait_source"), str) or not a["fallback_wait_source"].strip():
                    raise InputError(f"{aid}: fallback_wait_source is required")
            for f in ("opens_at", "queue_closes_at", "ride_finishes_by"):
                if a.get(f) is not None:
                    a[f] = instant(a[f], f"{aid}.{f}")
        self.visited = set(request.get("visited_attraction_ids", []))
        self.required = set(request.get("must_ride_ids", [])) - self.visited
        if (self.visited | self.required) - self.rides.keys():
            raise InputError("visited_attraction_ids and must_ride_ids must reference attraction IDs")
        self.anchors = self.unique(request.get("commitments", []), "commitments")
        for key, item in self.anchors.items():
            if not isinstance(item.get("completed", False), bool):
                raise InputError(f"{key}.completed must be a JSON boolean")
        self.anchors = {key: val for key, val in self.anchors.items() if not val.get("completed", False)}
        self.reserved = set()
        for key, a in self.anchors.items():
            self.location(a.get("location_id"), f"{key}.location_id")
            if not isinstance(a.get("kind"), str) or a["kind"] not in {"dpa", "meal", "appointment"}:
                raise InputError(f"{key}.kind must be dpa, meal, or appointment")
            number(a.get("duration_minutes"), f"{key}.duration_minutes", .01)
            number(a.get("arrival_buffer_minutes", 5), f"{key}.arrival_buffer_minutes")
            if a["kind"] == "dpa":
                a["earliest"] = instant(a.get("entry_start"), f"{key}.entry_start")
                a["latest"] = instant(a.get("entry_end"), f"{key}.entry_end")
                if a["latest"] <= a["earliest"]:
                    raise InputError(f"{key}: entry_end must be after entry_start")
            else:
                a["earliest"] = a["latest"] = instant(a.get("start_at"), f"{key}.start_at")
            if a.get("finish_by") is not None:
                a["finish_by"] = instant(a["finish_by"], f"{key}.finish_by")
            if a.get("attraction_id") is not None:
                if not isinstance(a["attraction_id"], str) or a["attraction_id"] not in self.rides:
                    raise InputError(f"{key}: unknown attraction_id")
                if self.rides[a["attraction_id"]]["location_id"] != a["location_id"]:
                    raise InputError(f"{key}: commitment and attraction locations disagree")
                if a["attraction_id"] in self.reserved:
                    raise InputError("Duplicate active reservations for one attraction need manual resolution")
                self.reserved.add(a["attraction_id"])
        self.required -= self.reserved
        if len(self.required) + len(self.anchors) > 6:
            raise InputError("At most six pending commitments and unreserved must-rides are supported")
        self.graph = {key: [] for key in self.locations}
        seen_edges = set()
        for edge in request.get("walking_edges", []):
            start = self.location(edge.get("from"), "walking_edges.from")
            end = self.location(edge.get("to"), "walking_edges.to")
            length = number(edge.get("minutes"), "walking_edges.minutes", .01)
            if (start, end) in seen_edges:
                raise InputError("Duplicate directed walking edge")
            seen_edges.add((start, end))
            if not isinstance(edge.get("quality"), str) or edge["quality"] not in {"verified", "estimate"} or not isinstance(edge.get("source"), str) or not edge["source"].strip():
                raise InputError("Every walking edge needs quality=verified|estimate and source")
            self.graph[start].append((end, length * self.pace, edge["quality"], edge["source"]))
        self.observations = {}
        self.ignored_future = []
        for obs in request.get("observations", []):
            aid = obs.get("attraction_id")
            if not isinstance(aid, str) or aid not in self.rides:
                raise InputError("Observation references an unknown attraction")
            updated = instant(obs.get("source_updated_at"), "source_updated_at")
            fetched = instant(obs.get("fetched_at"), "fetched_at")
            if updated > fetched:
                raise InputError("source_updated_at may not be later than fetched_at")
            if updated > self.now or fetched > self.now:
                self.ignored_future.append(aid)
                continue
            if not isinstance(obs.get("status"), str) or obs["status"] not in {"open", "closed", "down", "unknown"}:
                raise InputError("Observation status must be open, closed, down, or unknown")
            if obs.get("wait_minutes") is not None:
                number(obs["wait_minutes"], "wait_minutes")
            if not isinstance(obs.get("source"), str) or not obs["source"].strip():
                raise InputError("Every observation needs a source label")
            row = {**obs, "updated": updated, "fetched": fetched}
            prev = self.observations.get(aid)
            # Source event time wins over fetch time: a late retrieval cannot revive old data.
            if prev is not None and (updated, fetched) == (prev["updated"], prev["fetched"]):
                if (row["status"], row.get("wait_minutes")) != (prev["status"], prev.get("wait_minutes")):
                    raise InputError(f"{aid}: conflicting observations have identical source and fetch timestamps")
            if prev is None or (updated, fetched) > (prev["updated"], prev["fetched"]):
                self.observations[aid] = row
        self.walk = lru_cache(maxsize=None)(self._walk)
        self.schedule = lru_cache(maxsize=None)(self._schedule)

    @staticmethod
    def unique(items, field):
        if not isinstance(items, list):
            raise InputError(f"{field} must be an array")
        out = {}
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                raise InputError(f"Every {field} item needs a nonempty string id")
            if item["id"] in out:
                raise InputError(f"Duplicate {field} ID: {item['id']}")
            out[item["id"]] = dict(item)
        return out

    def location(self, loc, field):
        if not isinstance(loc, str) or loc not in self.locations:
            raise InputError(f"{field}: unknown location ID")
        if self.locations[loc].get("identity_verified") is not True:
            raise InputError(f"{field}: location identity must be verified; resolve it manually first")
        return loc

    def stamp(self, t):
        return t.astimezone(self.zone).isoformat(timespec="seconds")

    def _walk(self, start, end):
        if start == end:
            return (0., (), ())
        queue = [(0., start, (), ())]
        best = {start: 0.}
        while queue:
            cost, node, assumptions, sources = heapq.heappop(queue)
            if cost != best.get(node):
                continue
            if node == end:
                return cost, assumptions, sources
            for nxt, weight, quality, source in self.graph[node]:
                trial = cost + weight
                if trial < best.get(nxt, math.inf):
                    best[nxt] = trial
                    flags = assumptions + (("walking_times_estimated",) if quality == "estimate" else ())
                    heapq.heappush(queue, (trial, nxt, tuple(sorted(set(flags))), sources + (source,)))
        return None

    def evidence(self, aid):
        obs = self.observations.get(aid)
        if not obs:
            return {"kind": "missing", "status": "unknown", "age_minutes": None, "wait_minutes": None}
        age = diff(self.now, obs["updated"])
        kind = ("stale" if age > self.ttl else
                "fresh_open" if obs["status"] == "open" and obs.get("wait_minutes") is not None else
                "unavailable" if obs["status"] in {"closed", "down"} else "unknown")
        return {"kind": kind, "status": obs["status"], "age_minutes": round(age, 2),
                "wait_minutes": obs.get("wait_minutes"), "source_updated_at": self.stamp(obs["updated"]),
                "fetched_at": self.stamp(obs["fetched"]), "source": obs["source"]}

    def utility(self, aid):
        a = self.rides[aid]
        if a.get("utility") is not None:
            return a["utility"], a["utility_source"], ()
        if self.fallback_utility is not None:
            return self.fallback_utility, "explicit_policy_fallback", ("unknown_utility_uses_explicit_fallback",)
        return None, None, ()

    def ride(self, aid, t, loc, override_wait=None):
        a = self.rides[aid]
        route = self.walk(loc, a["location_id"])
        if route is None:
            return None, "missing_walking_route"
        walking, walk_flags, sources = route
        entry = t + mins(walking)
        evidence = self.evidence(aid)
        if evidence["kind"] == "unavailable":
            return None, f"fresh_{evidence['status']}"
        conditions = list(walk_flags)
        if override_wait is not None:
            wait = override_wait
            if evidence["kind"] != "fresh_open":
                conditions.append("verify_open_status_and_new_posted_wait")
        elif evidence["kind"] == "fresh_open":
            wait = evidence["wait_minutes"]
        elif a.get("fallback_wait_minutes") is not None:
            wait = a["fallback_wait_minutes"]
            conditions += ["verify_open_status_and_new_posted_wait", "wait_uses_explicit_scenario_fallback"]
        else:
            return None, f"{evidence['kind']}_wait_needs_verification"
        if a.get("opens_at") and entry < a["opens_at"]:
            return None, "before_declared_opening"
        if a.get("queue_closes_at"):
            if entry + mins(self.admission_buffer) >= a["queue_closes_at"]:
                return None, "queue_admission_cutoff_too_close"
        else:
            conditions.append("queue_admission_cutoff_unknown")
        horizon = max(0., diff(entry, self.now))
        reserve = self.uncertainty + self.growth * horizon
        expected_end = entry + mins(wait + a["ride_minutes"] + self.boarding)
        end = expected_end + mins(reserve)
        if a.get("ride_finishes_by") and end > a["ride_finishes_by"]:
            return None, "ride_finish_deadline_exceeded"
        if evidence["kind"] == "fresh_open" and diff(entry, self.observations[aid]["updated"]) > self.ttl:
            conditions.append("posted_wait_outlives_freshness_before_arrival")
        if horizon > 0:
            conditions.append("future_wait_is_not_observed")
        detail = {"attraction_id": aid, "kind": "ride", "queue_entry_at": self.stamp(entry),
                  "expected_wait_minutes": round(wait, 2), "wait_buffer_minutes": round(reserve, 2),
                  "planning_wait_minutes": round(wait + reserve, 2), "ride_minutes": a["ride_minutes"],
                  "boarding_buffer_minutes": self.boarding, "walking_sources": sorted(set(sources)),
                  "evidence": evidence}
        return Step(aid, a["location_id"], entry, end, expected_end, walking,
                    diff(end, t), tuple(sorted(set(conditions))), detail), None

    def anchor(self, key, t, loc):
        a = self.anchors[key]
        route = self.walk(loc, a["location_id"])
        if route is None:
            return None
        walking, flags, sources = route
        if a.get("attraction_id"):
            evidence = self.evidence(a["attraction_id"])
            if evidence["kind"] == "unavailable":
                return None
            if evidence["kind"] != "fresh_open":
                flags += ("verify_reserved_attraction_operating_status",)
        arrive = t + mins(walking)
        buffer = a.get("arrival_buffer_minutes", 5)
        start = max(arrive + mins(buffer), a["earliest"])
        if a.get("attraction_id"):
            obs = self.observations.get(a["attraction_id"])
            if obs and diff(start, obs["updated"]) > self.ttl:
                flags += ("reserved_attraction_status_expires_before_entry",)
        if (a["kind"] == "dpa" and start >= a["latest"]) or (a["kind"] != "dpa" and start > a["latest"]):
            return None
        end = start + mins(a["duration_minutes"])
        if a.get("finish_by") and end > a["finish_by"]:
            return None
        return Step(key, a["location_id"], start, end, end, walking,
                    walking + buffer + a["duration_minutes"], flags,
                    {"kind": a["kind"], "commitment_id": key,
                     "arrive_at": self.stamp(arrive), "service_start_at": self.stamp(start),
                     "service_finish_at": self.stamp(end), "walking_sources": sorted(set(sources)),
                     "duration_is_supplied_assumption": True})

    def _schedule(self, t, loc, remaining_rides, remaining_anchors):
        self.schedule_states += 1
        if self.schedule_states > self.max_states:
            raise SearchBudgetExceeded("Bounded mandatory-order feasibility search exhausted its state budget")
        if t > self.deadline:
            return None
        if not remaining_rides and not remaining_anchors:
            route = self.walk(loc, self.exit)
            if route is None:
                return None
            walking, flags, _ = route
            exit_at = t + mins(walking)
            if exit_at + mins(self.exit_buffer) > self.deadline:
                return None
            return Schedule(exit_at, walking, walking, (), flags)
        best = None
        options = [("ride", aid) for aid in remaining_rides] + [("anchor", key) for key in remaining_anchors]
        for kind, key in options:
            if kind == "ride":
                start = t
                declared_open = self.rides[key].get("opens_at")
                route = self.walk(loc, self.rides[key]["location_id"])
                if declared_open and route is not None:
                    # A required future visit may wait before walking to opening.
                    # Earlier availability must not be less feasible than later.
                    # Current fresh closed/down evidence still blocks ride().
                    start = max(t, declared_open - mins(route[0]))
                step, _ = self.ride(key, start, loc)
                after_rides = tuple(i for i in remaining_rides if i != key)
                after_anchors = remaining_anchors
            else:
                step = self.anchor(key, t, loc)
                after_rides = remaining_rides
                after_anchors = tuple(i for i in remaining_anchors if i != key)
            if step is None:
                continue
            tail = self.schedule(step.end, step.location, after_rides, after_anchors)
            if tail is None:
                continue
            combined = Schedule(tail.exit_at, step.walking + tail.walking, step.active + tail.active,
                                (step,) + tail.steps, tuple(sorted(set(step.conditions + tail.conditions))))
            if best is None or (combined.exit_at, combined.active) < (best.exit_at, best.active):
                best = combined
        return best

    def after(self, step, selected):
        return self.schedule(step.end, step.location, tuple(sorted(self.required - set(selected))), tuple(sorted(self.anchors)))

    def ceiling(self, aid):
        # Feasibility in this deterministic stress scenario is monotone in this
        # candidate's queue wait. The result is a verification threshold, not a forecast.
        def feasible(wait):
            step, _ = self.ride(aid, self.now, self.current, override_wait=wait)
            return step is not None and self.after(step, (aid,)) is not None
        if not feasible(0):
            return None
        low, high = 0, math.floor(diff(self.deadline, self.now))
        while low < high:
            middle = (low + high + 1) // 2
            if feasible(middle):
                low = middle
            else:
                high = middle - 1
        # The public "at or below" value is tested at that exact integer,
        # including strict DPA and queue-entry boundaries. No epsilon rounding.
        return low if feasible(low) else None

    def summary(self, schedule):
        return {"planned_exit_at": self.stamp(schedule.exit_at),
                "exit_buffer_minutes": self.exit_buffer,
                "spare_minutes_after_exit_buffer": round(diff(self.deadline, schedule.exit_at) - self.exit_buffer, 2),
                "commitments_and_must_rides": [
                    {**s.detail, "planning_finish_at": self.stamp(s.end), "conditions": list(s.conditions)}
                    for s in schedule.steps],
                "conditions": list(schedule.conditions)}


def _recommend(request):
    """Return a new JSON-serializable result without mutating the caller's input."""
    c = Context(request)
    action_state = request.get("action_state", "idle")
    if not isinstance(action_state, str) or action_state not in {"idle", "in_queue", "riding"}:
        raise InputError("action_state must be idle, in_queue, or riding")
    if action_state != "idle":
        return {"schema_version": "1.0", "status": "current_action_locked",
                "recommendations": [], "conditional_recommendations": [], "verification_watchlist": [],
                "next_action": {"kind": "finish_or_explicitly_end_current_action",
                                "reason": "Do not infer queue abandonment or a completed ride. Recompute from an idle state afterward."}}
    base = c.schedule(c.now, c.current, tuple(sorted(c.required)), tuple(sorted(c.anchors)))
    result = {"schema_version": "1.0", "generated_for": c.stamp(c.now),
              "park_timezone": str(c.zone), "exit_deadline": c.stamp(c.deadline),
              "ranking_objective": "two_step_supplied_utility_per_planning_minute",
              "guarantee": "scenario_feasibility_only; not full-day optimality or guaranteed queue admission",
              "recommendations": [], "conditional_recommendations": [], "verification_watchlist": [],
              "excluded": [], "warnings": [
                  "Recompute after every completed ride, changed wait/status, reservation, or delay.",
                  "Queue admission and future waits can change; confirm at the entrance.",
                  "Wait buffers are explicit stress assumptions, not calibrated confidence intervals.",
                  "Walking estimates and supplied commitment durations can be wrong."],
              "ignored_future_observation_ids": sorted(set(c.ignored_future)),
              "effective_policy": {"freshness_minutes": c.ttl, "exit_buffer_minutes": c.exit_buffer,
                  "wait_buffer_minutes": c.uncertainty, "wait_buffer_per_future_minute": c.growth,
                  "boarding_buffer_minutes": c.boarding, "queue_admission_buffer_minutes": c.admission_buffer,
                  "walking_time_multiplier": c.pace, "unknown_utility_fallback": c.fallback_utility}}
    if base is None:
        result["status"] = "no_verified_feasible_commitment_plan"
        result["next_action"] = {"kind": "resolve_commitments_or_missing_inputs",
                                 "reason": "No scenario-feasible path preserves all pending commitments, must-rides and the exit reserve. Check expired windows, unavailable rides, missing wait/path data, and timing."}
        return result
    result["baseline_required_plan"] = c.summary(base)
    available, conditional = [], []
    for aid in sorted(c.rides):
        if aid in c.visited:
            result["excluded"].append({"attraction_id": aid, "reason": "already_visited"})
            continue
        if aid in c.reserved:
            result["excluded"].append({"attraction_id": aid, "reason": "preserved_as_reserved_commitment"})
            continue
        utility, utility_source, utility_flags = c.utility(aid)
        step, reason = c.ride(aid, c.now, c.current)
        if reason and reason.startswith("fresh_"):
            result["excluded"].append({"attraction_id": aid, "reason": reason, "evidence": c.evidence(aid)})
            continue
        if utility is None or step is None:
            ceiling = c.ceiling(aid)
            result["verification_watchlist"].append({"attraction_id": aid, "name": c.rides[aid].get("name", aid),
                "reason": "unknown_utility_requires_explicit_input_or_fallback" if utility is None else reason,
                "evidence": c.evidence(aid), "max_updated_wait_minutes_if_open": ceiling,
                "condition": "Verify open status, queue admission, a new posted wait at or below the ceiling, and supply missing utility before choosing. A null ceiling means no feasible path was found even at zero wait."})
            continue
        tail = c.after(step, (aid,))
        if tail is None:
            result["excluded"].append({"attraction_id": aid, "reason": "would_endanger_commitment_must_ride_or_exit_reserve",
                                        "evidence": c.evidence(aid)})
            continue
        flags = tuple(sorted(set(step.conditions + utility_flags + tail.conditions)))
        record = {"aid": aid, "step": step, "tail": tail, "utility": utility, "utility_source": utility_source,
                  "flags": flags, "utility_flags": utility_flags}
        if c.evidence(aid)["kind"] == "fresh_open" and not utility_flags:
            available.append(record)
        else:
            conditional.append(record)
    # Second-step candidates require fresh known-open status and explicit utility.
    # Uncertain alternatives cannot quietly boost the main list's score.
    second_ids = [i for i in sorted(c.rides) if i not in c.visited | c.reserved
                  and c.evidence(i)["kind"] == "fresh_open" and c.rides[i].get("utility") is not None]
    for rec in available + conditional:
        aid, first, tail = rec["aid"], rec["step"], rec["tail"]
        def objective(last, completion, value):
            onward_detour = max(0., completion.walking - base.walking)
            occupied = diff(last.end, c.now) + onward_detour
            return value / max(1., occupied), occupied, onward_detour
        score, cost, detour = objective(first, tail, rec["utility"])
        best_second = None
        best_tail = tail
        for bid in second_ids:
            if bid == aid:
                continue
            second, _ = c.ride(bid, first.end, first.location)
            if second is None:
                continue
            completion = c.after(second, (aid, bid))
            if completion is None:
                continue
            value = rec["utility"] + c.utility(bid)[0]
            trial, trial_cost, trial_detour = objective(second, completion, value)
            if trial > score + 1e-12:
                score, cost, detour = trial, trial_cost, trial_detour
                best_second, best_tail = second, completion
        flags = set(rec["flags"])
        if best_second:
            flags.update(best_second.conditions + best_tail.conditions)
        raw_value = rec["utility"] + (c.utility(best_second.key)[0] if best_second else 0)
        rec["output"] = {"attraction_id": aid, "name": c.rides[aid].get("name", aid),
              "conditional": bool(flags),
              "availability_evidence": c.evidence(aid), "supplied_utility": c.rides[aid].get("utility"),
              "effective_utility": rec["utility"], "utility_source": rec["utility_source"],
              "walking_minutes": round(first.walking, 2), **first.detail,
              "expected_elapsed_minutes": round(diff(first.expected_end, c.now), 2),
              "planning_elapsed_minutes": round(diff(first.end, c.now), 2),
              "expected_finish_at": c.stamp(first.expected_end), "planning_finish_at": c.stamp(first.end),
              "max_updated_wait_minutes_if_open": c.ceiling(aid),
              "next_choice_heuristic_score": round(score, 6),
              "score_breakdown": {"lookahead_utility": raw_value, "planning_opportunity_minutes": round(cost, 3),
                                  "extra_onward_walking_minutes": round(detour, 3),
                                  "formula": "lookahead_utility / max(1, planning_opportunity_minutes)"},
              "lookahead_next_attraction_id": best_second.key if best_second else None,
              "lookahead_is_tentative": bool(best_second),
              "conditions": sorted(flags),
              "preserved_required_plan_after_first_ride": c.summary(tail),
              "lookahead_required_plan": c.summary(best_tail) if best_second else None,
              "reasons": [f"Supplied value {rec['utility']:g}; immediate walk + queue + ride + boarding is {diff(first.expected_end, c.now):.1f} minutes before the stress buffer.",
                          f"The planning scenario retains every required commitment and {c.exit_buffer:g} minutes before the exit deadline.",
                          (f"A tentative second ride ({best_second.key}) raises the two-step utility-per-minute heuristic; refresh its wait before following it."
                           if best_second else "The immediate choice has the best tested one/two-step efficiency for this first ride."),
                          "This local heuristic does not maximize total full-day experience value."]}
        rec["score"] = score
    sort_key = lambda r: (-r["score"], -r["utility"], r["step"].end, r["aid"])
    result["recommendations"] = [r["output"] for r in sorted(available, key=sort_key)[:3]]
    result["conditional_recommendations"] = [r["output"] for r in sorted(conditional, key=sort_key)[:3]]
    result["status"] = "recommendations_available" if available else "conditional_options_only" if conditional else "no_feasible_ride_now"
    if base.steps:
        first_anchor = base.steps[0]
        result["next_required_action"] = {**first_anchor.detail, "location_id": first_anchor.location,
                                           "planning_finish_at": c.stamp(first_anchor.end)}
    else:
        result["next_required_action"] = {"kind": "exit", "location_id": c.exit,
                                           "latest_arrival_at": c.stamp(c.deadline - mins(c.exit_buffer))}
    if not available:
        result["next_action"] = {"kind": "verify_conditional_option_or_follow_required_plan" if conditional else "follow_required_plan_or_verify_watchlist",
                                 "required_action": result["next_required_action"]}
    return result


def recommend(request):
    """Pure public entry point. An exhausted search returns no unverified ranking."""
    try:
        return _recommend(request)
    except OverflowError as exc:
        raise InputError("Input timing exceeds the supported calendar range") from exc
    except SearchBudgetExceeded as exc:
        return {"schema_version": "1.0", "status": "search_budget_exceeded_not_verified",
                "recommendations": [], "conditional_recommendations": [],
                "verification_watchlist": [], "reason": str(exc),
                "next_action": {"kind": "simplify_request_or_increase_bounded_search_budget",
                                "reason": "This is not evidence of infeasibility. No candidate ranking was returned."}}
