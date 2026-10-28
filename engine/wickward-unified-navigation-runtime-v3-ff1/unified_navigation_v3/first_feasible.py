"""First complete feasible tail; no minimum-exit or active-duration objective.

The traversal/terminal formulas below are copied from the unchanged revision5
Context._schedule (source SHA8868c004a67fc57da51044590d0b71958ab5f012fbf1581fe86cd0394a96d613).
Only successful-tail enumeration terminates earlier. Ride, anchor, walking,
forecast, interval and exit-reserve semantics are unchanged.
"""
from __future__ import annotations

from .context import Context
from .kernel import Schedule, SearchBudgetExceeded, mins


class FirstFeasibleContext(Context):
    witness_selection = 'first_feasible_depth_first_exact_state_v1'

    def _schedule(self, t, loc, remaining_rides, remaining_anchors):
        # Context construction wraps this bound method in a per-instance exact
        # lru_cache. Exceptions propagate, so they are never cached as None.
        self.budget.state()
        self.schedule_states += 1
        if self.schedule_states > self.max_states:
            raise SearchBudgetExceeded('Bounded mandatory-order feasibility search exhausted its state budget')
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
        # Public entry callers provide sorted unique tuples; removing one node
        # retains that deterministic order and the ride/anchor namespace split.
        options = [('ride', aid) for aid in remaining_rides] + [('anchor', key) for key in remaining_anchors]
        for kind, key in options:
            if kind == 'ride':
                start = t
                declared_open = self.rides[key].get('opens_at')
                route = self.walk(loc, self.rides[key]['location_id'])
                if declared_open and route is not None:
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
            # A tail exists only after every remaining node and exit was
            # verified. This returns no incomplete/prefix schedule.
            return Schedule(tail.exit_at, step.walking + tail.walking, step.active + tail.active,
                            (step,) + tail.steps, tuple(sorted(set(step.conditions + tail.conditions))))
        # Every permitted branch completed unsuccessfully. Only now may the
        # exact memo store None; there is deliberately no budget-exception catch.
        return None
