"""Original bounded time-dependent weighted orienteering heuristics.

Pure Python; forecasts and replay states remain external. No network or database writes.
Every proposed sequence is simulated causally from the start, including time-dependent
waits and delayed non-preemptive fixed breaks. No constant-cost delta approximations.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Result:
    feasible: bool
    score: float
    finish: float
    waits: float
    walking: float
    rides: int
    arrivals: tuple[float, ...] = ()


class BudgetExceeded(Exception):
    pass


class Problem:
    def __init__(
        self,
        ids,
        weights,
        waits,
        walks,
        *,
        start=600.0,
        end=1080.0,
        ride=10.0,
        breaks=((690.0, 45.0), (900.0, 20.0)),
        exit_buffer=15.0,
        wait_scale=1.0,
        walk_scale=1.0,
        budget=30000,
        start_location=None,
        wait_function=None,
    ):
        self.ids = tuple(ids)
        self.weights = weights
        self.waits = waits
        self.walks = walks
        self.start = start
        self.end = end
        self.ride = ride
        self.breaks = breaks
        self.exit_buffer = exit_buffer
        self.wait_scale = wait_scale
        self.walk_scale = walk_scale
        self.budget = budget
        self.evaluations = 0
        self.start_location = start_location
        self.wait_function = wait_function

    def travel(self, a, b):
        return self.walks[a, b] * self.walk_scale

    def rests_due(self, t, index):
        while index < len(self.breaks) and t >= self.breaks[index][0]:
            t += self.breaks[index][1]
            index += 1
        return t, index

    def finish_time(self, t, loc, rest_index):
        # At the end, perform every remaining scheduled rest, then leave.
        for earliest, duration in self.breaks[rest_index:]:
            t = max(t, earliest) + duration
        return t + self.travel(loc, None) + self.exit_buffer

    def evaluate(self, route, *, charge=True):
        if charge:
            if self.evaluations >= self.budget:
                raise BudgetExceeded
            self.evaluations += 1
        if len(set(route)) != len(route) or any(i not in self.weights for i in route):
            return Result(False, 0.0, math.inf, 0.0, 0.0, 0)
        t, loc, rest_index = self.start, self.start_location, 0
        total_wait, walking, score = 0.0, 0.0, 0.0
        arrivals = []
        for aid in route:
            t, rest_index = self.rests_due(t, rest_index)
            walk = self.travel(loc, aid)
            t += walk
            walking += walk
            arrivals.append(t)
            wait = (self.wait_function(aid, t) if self.wait_function is not None
                    else self.waits.get((aid, int(t // 30) * 30)))
            if wait is None or not math.isfinite(wait) or wait < 0:
                return Result(
                    False,
                    score,
                    math.inf,
                    total_wait,
                    walking,
                    len(arrivals) - 1,
                    tuple(arrivals),
                )
            wait *= self.wait_scale
            total_wait += wait
            t += wait + self.ride
            score += self.weights[aid]
            loc = aid
        finish = self.finish_time(t, loc, rest_index)
        walking += self.travel(loc, None)
        return Result(
            finish <= self.end + 1e-9,
            score,
            finish,
            total_wait,
            walking,
            len(route),
            tuple(arrivals),
        )


def rank(result):
    return (
        (round(result.score, 10), -result.finish)
        if result.feasible
        else (-math.inf, -math.inf)
    )


def static_greedy(problem, ordered):
    route = ()
    for aid in ordered:
        attempt = route + (aid,)
        if problem.evaluate(attempt).feasible:
            route = attempt
    return route


def greedy_score_time(problem):
    route = ()
    current = problem.evaluate(route)
    while True:
        offers = []
        for aid in problem.ids:
            if aid in route:
                continue
            candidate = route + (aid,)
            result = problem.evaluate(candidate)
            if result.feasible:
                # Completion time can be fixed by a pending break; use marginal
                # activity cost excluding forced idle time, never a negative denominator.
                marginal = (
                    result.waits
                    + result.walking
                    + result.rides * problem.ride
                    - (current.waits + current.walking + current.rides * problem.ride)
                )
                offers.append(
                    (
                        problem.weights[aid] / max(1.0, marginal),
                        rank(result),
                        aid,
                        candidate,
                        result,
                    )
                )
        if not offers:
            return route
        _, _, _, route, current = max(offers)


def insertion(problem, route=(), *, randomized=False, rng=None):
    current = problem.evaluate(route)
    while True:
        best = None
        ids = [i for i in problem.ids if i not in route]
        if randomized:
            rng.shuffle(ids)
        for aid in ids:
            for position in range(len(route) + 1):
                candidate = route[:position] + (aid,) + route[position:]
                result = problem.evaluate(candidate)
                if result.feasible:
                    delta = result.finish - current.finish
                    # Non-FIFO waiting can make an insertion shorten the tour.
                    key = (problem.weights[aid] / max(1.0, delta), rank(result))
                    if randomized:
                        key = (key[0] * rng.uniform(0.80, 1.20), key[1])
                    if best is None or key > best[0]:
                        best = key, candidate, result
        if best is None:
            return route
        _, route, current = best


def local_search(problem, route):
    """First-improvement relocate/swap/2-opt, all costs fully recomputed."""
    current = problem.evaluate(route)
    while True:
        improvement = None
        n = len(route)
        for i in range(n):
            if improvement is not None:
                break
            for j in range(n):
                if i == j:
                    continue
                moved = list(route)
                aid = moved.pop(i)
                moved.insert(j, aid)
                candidate = tuple(moved)
                result = problem.evaluate(candidate)
                if rank(result) > rank(current):
                    improvement = candidate, result
                    break
        if improvement is None:
            for i in range(n):
                if improvement is not None:
                    break
                for j in range(i + 1, n):
                    swapped = list(route)
                    swapped[i], swapped[j] = swapped[j], swapped[i]
                    candidates = [
                        tuple(swapped),
                        route[:i] + tuple(reversed(route[i : j + 1])) + route[j + 1 :],
                    ]
                    for candidate in candidates:
                        result = problem.evaluate(candidate)
                        if rank(result) > rank(current):
                            improvement = candidate, result
                            break
                    if improvement is not None:
                        break
        if improvement is None:
            return route
        route, current = improvement


def ils(problem, initial_routes, seed=20261005):
    rng = random.Random(seed)
    best = ()
    best_result = problem.evaluate(best)
    for candidate in initial_routes:
        result = problem.evaluate(candidate)
        if rank(result) > rank(best_result):
            best, best_result = candidate, result
    current = best
    stagnation = 0
    while True:
        try:
            current = local_search(problem, current)
            current = insertion(problem, current, randomized=stagnation > 0, rng=rng)
            result = problem.evaluate(current)
            if rank(result) > rank(best_result):
                best, best_result = current, result
                stagnation = 0
            else:
                stagnation += 1
            # Remove 1–4 visits, with adaptive perturbation and occasional restart.
            current = list(best if rng.random() < 0.7 else current)
            remove = min(len(current), 1 + stagnation // 3, 4)
            for _ in range(remove):
                current.pop(rng.randrange(len(current)))
            if stagnation > 0 and stagnation % 8 == 0:
                current = []
            current = tuple(current)
        except BudgetExceeded:
            return best
