"""Bounded next-choice recommendations over the separately audited constraint API.

No network, UI, publication, future truth, or full-day optimization. The cooperative
wall deadline is checked between candidates and at every required-search recursion.
"""
from __future__ import annotations

import math
from dataclasses import replace
from time import perf_counter

from .adapter import (InputError, PracticalContext, SearchBudgetExceeded, _step_json,
                      diff, number)

RESULT_VERSION = "wickward.bounded-recommendations.v1"
MAX_RESULTS = 3


class WallTimeBudgetExceeded(RuntimeError):
    pass


class TimedPracticalContext(PracticalContext):
    def __init__(self, request, *, wall_deadline, monotonic_clock):
        self.wall_deadline = wall_deadline
        self.monotonic_clock = monotonic_clock
        self.check_time()
        super().__init__(request)
        self.check_time()

    def check_time(self):
        if self.monotonic_clock() >= self.wall_deadline:
            raise WallTimeBudgetExceeded("Cooperative next-choice search deadline reached")

    def _schedule(self, t, loc, remaining_rides, remaining_anchors):
        self.check_time()
        return super()._schedule(t, loc, remaining_rides, remaining_anchors)


def _bounded_shape(request, check_time):
    if not isinstance(request, dict):
        raise InputError("The request must be an object")
    # Prevent unbounded copying/validation before cooperative search can check time.
    # These are entrypoint limits, not changes to the shared adapter.
    limits = {"locations": 256, "walking_edges": 10000, "observations": 10000,
              "forecasts": 10000, "attractions": 40, "commitments": 100,
              "flexible_breaks": 100, "visited_attraction_ids": 100,
              "must_ride_ids": 100}
    for name, limit in limits.items():
        rows = request.get(name, [])
        if not isinstance(rows, list) or len(rows) > limit:
            raise InputError(f"{name}: bounded recommendation input requires an array of at most {limit} items")
    stack, nodes, characters = [(request, 0)], 0, 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes % 64 == 0:
            check_time()
        if nodes > 100000 or depth > 12:
            raise InputError("Recommendation input exceeds 100,000 JSON nodes or nesting depth 12")
        if isinstance(value, dict):
            if len(value) > 10000 or any(not isinstance(k, str) for k in value):
                raise InputError("Input objects require string keys and at most 10,000 members")
            stack.extend((v, depth + 1) for v in value.values())
            stack.extend((k, depth + 1) for k in value.keys())
        elif isinstance(value, list):
            if len(value) > 10000:
                raise InputError("Input arrays have at most 10,000 members")
            stack.extend((v, depth + 1) for v in value)
        elif isinstance(value, str):
            characters += len(value)
            if len(value) > 16384 or characters > 2000000:
                raise InputError("Input strings exceed the 16,384 individual / 2,000,000 total character bound")
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise InputError("Recommendation input must contain only JSON value types")
        elif isinstance(value, float) and not math.isfinite(value):
            raise InputError("Recommendation input numbers must be finite")


def recommend(request, *, wall_time_budget_seconds=0.8):
    """Return at most three proven scenario-feasible next choices.

    Fresh observed open+wait and explicit utility are required for the ordinary
    list. Forecast/fallback/unknown input choices belong only to a separately
    labelled conditional verification list. Total choices across both lists <= 3.
    The exact immediate score is supplied utility / stress-scenario elapsed time;
    all required obligations and the exit reserve must have a completion witness.
    """
    wall_time_budget_seconds = number(wall_time_budget_seconds, "wall_time_budget_seconds", .001, 3)
    clock = perf_counter
    started = clock()
    deadline = started + wall_time_budget_seconds
    context = None
    candidates = []
    checked_ids = []
    active_id = None
    result = {
        "schema_version": RESULT_VERSION,
        "status": "pending",
        "recommendations": [], "conditional_verification_list": [],
        "verification_watchlist": [], "excluded": [],
        "ranking_objective": "ordinary_fresh_evidence_first_then_supplied_immediate_utility_per_stress_scenario_elapsed_minute",
        "evidence_class_priority": "ordinary fresh observed inputs before conditional checks, regardless of cross-class score; conditional checks fill only remaining slots",
        "ranking_scope": "bounded_next_choice; not full-day score optimization",
        "guarantee": "Each returned choice has a scenario-feasible required-completion and exit witness; no operational guarantee",
        "ranking_complete": False, "candidate_extraction_complete": False,
        "warnings": ["Recompute after any wait, status, completed action, delay or reservation change.",
                     "Forecasts and fallback waits never establish operating status.",
                     "No at-or-below safe wait ceiling is calculated.",
                     "A budget stop or missing witness is not a proof of infeasibility."],
    }

    def finish(status):
        result["status"] = status
        result["checked_candidate_ids"] = list(checked_ids)
        selected = {r["attraction_id"] for r in result["recommendations"] + result["conditional_verification_list"]}
        result["unchecked_candidate_ids"] = sorted({r["aid"] for r in candidates} - set(checked_ids) - selected)
        if active_id is not None and active_id not in checked_ids:
            result["interrupted_candidate_id"] = active_id
        result["budget"] = {"wall_time_budget_seconds": wall_time_budget_seconds,
                            "elapsed_seconds": max(0., clock() - started),
                            "schedule_states": context.schedule_states if context is not None else 0,
                            "max_schedule_states": context.max_states if context is not None else None,
                            "semantics": "cooperative search deadline, checked at each recursive schedule entry; not a hard real-time preemption guarantee"}
        if context is not None:
            result["information_as_of"] = context.stamp(context.information_as_of)
            result["planning_now"] = context.stamp(context.now)
            result["excluded_observations"] = list(context.excluded_observations)
            result["excluded_forecasts"] = list(context.excluded_forecasts)
        return result

    try:
        def check_entry_time():
            if clock() >= deadline:
                raise WallTimeBudgetExceeded("Deadline reached during bounded input validation")
        _bounded_shape(request, check_entry_time)
        context = TimedPracticalContext(request, wall_deadline=deadline, monotonic_clock=clock)
        c = context
        if c.raw.get("action_state", "idle") != "idle":
            result["next_action"] = "finish_or_explicitly_end_current_action"
            return finish("current_action_locked")
        # Compute the exact immediate ranking key first. More expensive mandatory
        # completion checks then run in ranking order, preserving verified choices.
        for aid in sorted(c.rides):
            c.check_time()
            if aid in c.visited or aid in c.reserved:
                result["excluded"].append({"attraction_id": aid, "reason": "already_visited_or_reserved"})
                continue
            utility, source, utility_flags = c.utility(aid)
            evidence = c.evidence(aid)
            if evidence["kind"] == "unavailable":
                result["excluded"].append({"attraction_id": aid, "reason": f"fresh_{evidence['status']}"})
                continue
            if utility is None:
                result["verification_watchlist"].append({"attraction_id": aid,
                    "reason": "unknown_utility_requires_explicit_input_or_fallback", "evidence": evidence,
                    "feasibility_verified": False})
                continue
            if evidence["kind"] == "fresh_open":
                # Explicitly use the current posted wait for the live-efficiency
                # first action. Time-varying forecasts may still inform the tail.
                step, reason = c.ride(aid, c.now, c.current, override_wait=evidence["wait_minutes"])
                if step is not None:
                    step = replace(step, conditions=tuple(flag for flag in step.conditions
                                   if flag != "override_wait_is_supplied_scenario"),
                                   detail={**step.detail, "first_action_wait_basis": "current_fresh_posted_wait"})
            else:
                step, reason = c.ride(aid, c.now, c.current)
                if step is not None:
                    step = replace(step, detail={**step.detail, "first_action_wait_basis":
                                   "supplied_forecast_scenario" if "wait_forecast" in step.detail else "explicit_fallback_scenario"})
            if step is None:
                result["verification_watchlist"].append({"attraction_id": aid, "reason": reason,
                    "evidence": evidence, "feasibility_verified": False})
                continue
            elapsed = diff(step.end, c.now)
            ordinary = evidence["kind"] == "fresh_open" and not utility_flags and "wait_forecast" not in step.detail
            candidates.append({"aid": aid, "utility": utility, "source": source,
                               "utility_flags": utility_flags, "step": step, "evidence": evidence,
                               "elapsed": elapsed, "score": utility / max(1., elapsed), "ordinary": ordinary})
        result["candidate_extraction_complete"] = True
        candidates.sort(key=lambda r: (not r["ordinary"], -r["score"], -r["utility"], r["elapsed"], r["aid"]))
        for index, rec in enumerate(candidates):
            c.check_time()
            if len(result["recommendations"]) + len(result["conditional_verification_list"]) >= MAX_RESULTS:
                # Remaining candidates have lower scores within their evidence class.
                # Their individual feasibility is untested, but top-3 selection is done.
                result["ranking_complete"] = True
                break
            active_id = rec["aid"]
            step = rec["step"]
            tail = c.schedule(step.end, step.location, tuple(sorted(c.required - {rec["aid"]})),
                              tuple(sorted(c.anchors)))
            checked_ids.append(rec["aid"])
            active_id = None
            if tail is None:
                result["excluded"].append({"attraction_id": rec["aid"],
                    "reason": "no_verified_required_completion_for_this_candidate_under_supplied_inputs"})
                continue
            conditions = tuple(sorted(set(step.conditions + rec["utility_flags"] + tail.conditions)))
            entry = {
                "attraction_id": rec["aid"], "utility": rec["utility"], "utility_source": rec["source"],
                "first_action_wait_basis": step.detail["first_action_wait_basis"],
                "next_choice_score": rec["score"],
                "reason": "Highest remaining supplied immediate utility per planning minute within this evidence class; ordinary fresh evidence is prioritized over conditional checks; every required obligation and exit reserve is preserved",
                "score_breakdown": {"supplied_utility": rec["utility"], "walking_minutes": step.walking,
                                    "expected_wait_minutes": step.detail["expected_wait_minutes"],
                                    "wait_buffer_minutes": step.detail["wait_buffer_minutes"],
                                    "ride_minutes": step.detail["ride_minutes"],
                                    "boarding_buffer_minutes": step.detail["boarding_buffer_minutes"],
                                    "planning_elapsed_minutes": rec["elapsed"],
                                    "formula": "supplied_utility / max(1, walking + wait + ride + explicit buffers)"},
                "evidence": rec["evidence"], "conditional": bool(conditions) or not rec["ordinary"],
                "conditions": list(conditions), "candidate_step": _step_json(c, step),
                "required_completion_witness": c.summary(tail),
                "required_attraction_ids": sorted(c.required),
                "required_commitment_ids": sorted(c.anchors),
                "candidate_satisfies_must_ride": rec["aid"] in c.required,
                "unknown_required_utility_ids": sorted(aid for aid in c.required | {a["attraction_id"] for a in c.anchors.values() if a.get("attraction_id")} if c.utility(aid)[0] is None),
                "scenario_feasibility_verified": True,
            }
            if rec["ordinary"]:
                entry["evidence_category"] = "fresh_observed_open_wait_and_supplied_utility"
                result["recommendations"].append(entry)
            else:
                entry["evidence_category"] = "conditional_inputs_require_confirmation"
                entry["verification_needed"] = "Verify current operating status, actual wait/admission and any fallback priority; then recompute"
                result["conditional_verification_list"].append(entry)
        else:
            result["ranking_complete"] = True
        c.check_time()
        if not result["recommendations"] and not result["conditional_verification_list"]:
            tail = c.schedule(c.now, c.current, tuple(sorted(c.required)), tuple(sorted(c.anchors)))
            if tail is not None:
                result["required_only_witness"] = c.summary(tail)
                result["next_action"] = "attend_next_required_task_or_exit"
                return finish("no_optional_choice_required_plan_verified")
            result["next_action"] = "resolve_required_constraints_or_missing_inputs"
            return finish("no_verified_choice_or_required_plan")
        return finish("verified_choices")
    except WallTimeBudgetExceeded:
        result["ranking_complete"] = False
        result["next_action"] = "Use only an already verified choice, or pause and recompute with updated inputs"
        return finish("wall_time_budget_exceeded_verified_partial" if result["recommendations"] or result["conditional_verification_list"]
                      else "wall_time_budget_exceeded_not_verified")
    except SearchBudgetExceeded:
        result["ranking_complete"] = False
        result["next_action"] = "Use only an already verified choice, or pause; untested candidates are not proven impossible"
        return finish("search_budget_exceeded_verified_partial" if result["recommendations"] or result["conditional_verification_list"]
                      else "search_budget_exceeded_not_verified")
