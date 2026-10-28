"""Precise clock and shared cooperative accounting around unchanged kernels."""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace
import hashlib
import json

from .kernel import PracticalContext, InputError, Step, instant, mins, diff, _step_json


class BudgetStop(RuntimeError):
    def __init__(self, reason, stage, scope):
        super().__init__(reason)
        self.reason, self.stage, self.scope = reason, stage, scope


class Budget:
    def __init__(self, clock, started, limit=2.4, state_limit=100000):
        self.clock, self.started, self.limit = clock, started, limit
        self.deadline = started + limit
        self.state_limit, self.states = state_limit, 0
        self.stage, self.stage_deadline = 'input', None
        self.stopped_stage = self.stop_reason = self.stop_scope = None
        self.completed_stages = []

    def tighten(self, limit):
        self.limit, self.deadline = limit, self.started + limit
        self.check_total()

    def stop(self, reason, scope):
        self.stopped_stage, self.stop_reason, self.stop_scope = self.stage, reason, scope
        raise BudgetStop(reason, self.stage, scope)

    def check_total(self):
        if self.clock() >= self.deadline:
            self.stop('total_deadline_reached', 'total')

    def check(self):
        self.check_total()
        if self.stage_deadline is not None and self.clock() >= self.stage_deadline:
            self.stop('search_stage_cap_reached', 'stage')

    def begin(self, stage):
        self.check_total()
        now = self.clock()
        if self.deadline - now < .001:
            self.stop('insufficient_remaining_budget_for_new_stage', 'total')
        self.stage, self.stage_deadline = stage, min(self.deadline, now + .8)

    def complete(self):
        self.check()
        self.completed_stages.append(self.stage)
        self.stage_deadline = None

    def state(self):
        self.check()
        if self.states >= self.state_limit:
            self.stop('aggregate_schedule_state_limit', 'states')
        self.states += 1

    def diagnostics(self, retained=False):
        return {
            'total_limit_seconds': self.limit, 'stage_cap_seconds': .8,
            'schedule_state_limit': self.state_limit, 'schedule_states': self.states,
            'elapsed_seconds': max(0., self.clock() - self.started),
            'elapsed_endpoint': 'result_metadata_assembly_before_final_encode',
            'state': 'stopped' if self.stop_reason else 'within_total_deadline',
            'stopped_stage': self.stopped_stage, 'reason': self.stop_reason,
            'stop_scope': self.stop_scope, 'completed_stages': list(self.completed_stages),
            'retained_previously_verified_action': retained,
            'semantics': 'one cooperative parse-through-encode deadline; no hard preemption or latency certification',
        }


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def same_instant(left, right):
    return instant(left, 'binding.left') == instant(right, 'binding.right')


class Context(PracticalContext):
    """Only display precision, globally barred rides and budget accounting change."""
    def __init__(self, request, ledger, budget):
        self.budget = budget
        budget.check()
        projection = copy.deepcopy(request)
        missed = set(ledger['missed_commitment_ids'])
        for field in ('commitments', 'flexible_breaks'):
            projection[field] = [r for r in projection.get(field, []) if r['id'] not in missed]
        # These are private views, never replacement master input or completion.
        super().__init__(projection)
        self.reserved = set(ledger['all_reserved_attraction_ids'])
        self.required = set(ledger['unresolved_must_ride_ids']) - self.reserved
        self.original_request_sha256 = fingerprint(request)
        self.master_ledger_sha256 = fingerprint(ledger)
        self.master_ledger = copy.deepcopy(ledger)
        budget.check()

    def stamp(self, t):
        # Preserve supported microseconds; frozen display truncation is not used.
        return t.astimezone(self.zone).isoformat(timespec='microseconds')

    def _schedule(self, t, loc, remaining_rides, remaining_anchors):
        self.budget.state()
        return super()._schedule(t, loc, remaining_rides, remaining_anchors)

    def fresh_operating(self, aid):
        row = self.last_reported.get(aid)
        return bool(row and row['observed'] is not None and row['status'] == 'open'
                    and 0 <= diff(self.now, row['observed']) <= self.ttl)

    def first_step(self, kind, key, t=None, *, permit_future=False):
        self.budget.check()
        t = self.now if t is None else t
        if kind == 'ride':
            if key in self.visited or key in self.reserved:
                return None, 'visited_or_globally_reserved'
            evidence = self.evidence(key)
            departure = t
            route = self.walk(self.current, self.rides[key]['location_id'])
            opening = self.rides[key].get('opens_at')
            if permit_future and opening and route:
                departure = max(t, opening - mins(route[0]))
            if evidence['kind'] == 'fresh_open':
                step, reason = self.ride(key, departure, self.current, override_wait=evidence['wait_minutes'])
                if step is not None:
                    step = replace(step, conditions=tuple(f for f in step.conditions
                                   if f != 'override_wait_is_supplied_scenario'),
                                   detail={**step.detail, 'first_action_wait_basis': 'current_fresh_posted_wait'})
            else:
                step, reason = self.ride(key, departure, self.current)
            return step, reason
        return self.anchor(key, t, self.current), 'commitment_window_or_route_unresolved'


@dataclass
class Proof:
    kind: str
    key: str
    step: Step
    tail: object
    timing: dict
    request_sha256: str
    ledger_sha256: str
    witness_sha256: str
    partial: bool


def normalize_step(c, step):
    out = _step_json(c, step)
    if out['kind'] in {'meal', 'appointment', 'dpa'}:
        out['service_kind'], out['kind'] = out['kind'], 'commitment'
    return out


def timing_for(c, steps, tail, partial):
    awarded = set()
    covered_anchors = set()
    for step in steps:
        if step.detail['kind'] == 'ride':
            awarded.add(step.key)
        elif step.detail['kind'] != 'wait_until':
            covered_anchors.add(step.key)
            aid = c.anchors[step.key].get('attraction_id')
            if aid is not None and aid not in c.visited:
                awarded.add(aid)
    unknown = sorted(a for a in awarded if c.rides[a].get('utility') is None)
    subtotal = sum(c.rides[a]['utility'] for a in awarded if c.rides[a].get('utility') is not None)
    conditions = set(tail.conditions)
    for step in steps:
        conditions.update(step.conditions)
        if step.detail.get('duration_is_supplied_assumption'):
            conditions.add('supplied_service_duration')
    if unknown:
        conditions.add('unknown_individual_utility')
    original_must = set(c.master_ledger['unresolved_must_ride_ids'])
    missed = c.master_ledger['missed_commitment_ids']
    result = {
        'planning_now': c.stamp(c.now), 'information_as_of': c.stamp(c.information_as_of),
        'coverage': 'required_subset_and_all_pending_timed' if partial else
                    'all_still_pending_after_reported_miss' if missed else 'all_original_outstanding',
        'verified_under_supplied_assumptions': True,
        'full_plan_verified': not partial and not missed,
        'covered_must_ride_ids': sorted(original_must & awarded),
        'covered_pending_commitment_ids': sorted(covered_anchors),
        'deferred_must_ride_ids': sorted(original_must - awarded),
        'steps': [normalize_step(c, s) for s in steps],
        'planned_exit_at': c.stamp(tail.exit_at),
        'exit_buffer_minutes': c.exit_buffer,
        'exit_reserve_until': c.stamp(tail.exit_at + mins(c.exit_buffer)),
        'exit_deadline': c.stamp(c.deadline), 'conditions': sorted(conditions),
        'unknown_utility_ids': unknown, 'awarded_attraction_ids': sorted(awarded),
        'scenario_only': True,
    }
    if getattr(c, 'witness_selection', None) is not None:
        result.update(witness_selection=c.witness_selection,
                      earliest_exit_verified=False, minimum_active_duration_verified=False,
                      witness_guarantee='complete supplied-scenario witness for the declared coverage scope; not a minimum-exit or best-tail claim')
    if partial:
        result.update(projected_subset_utility=None if unknown else subtotal,
                      projected_subset_known_utility_subtotal=subtotal,
                      projected_subset_utility_known=not unknown)
    else:
        result.update(utility_total=None if unknown else subtotal, known_utility_subtotal=subtotal,
                      utility_total_is_complete=not unknown)
    return result


def prove(c, kind, key, *, partial=False, t=None, permit_future=False, prefix=()):
    step, reason = c.first_step(kind, key, t, permit_future=permit_future)
    if step is None:
        return None, reason
    rides = set() if partial else set(c.required)
    anchors = set(c.anchors)
    if kind == 'ride':
        rides.discard(key)
    else:
        anchors.discard(key)
    tail = c.schedule(step.end, step.location, tuple(sorted(rides)), tuple(sorted(anchors)))
    c.budget.check()
    if tail is None:
        return None, 'required_completion_or_exit_not_verified'
    steps = tuple(prefix) + (step,) + tail.steps
    timing = timing_for(c, steps, tail, partial)
    return Proof(kind, key, step, tail, timing, c.original_request_sha256,
                 c.master_ledger_sha256, fingerprint(timing), partial), None


def validate_binding(proof, request, ledger, *, immediate=True):
    if (proof.request_sha256 != fingerprint(request) or proof.ledger_sha256 != fingerprint(ledger)
            or proof.witness_sha256 != fingerprint(proof.timing)
            or not same_instant(proof.timing['planning_now'], request['now'])
            or not same_instant(proof.timing['information_as_of'], request['information_as_of'])):
        raise InputError('Internal proof is not bound to the unchanged request/ledger/clocks')
    first = proof.timing['steps'][0]
    selected = proof.timing['steps'][1] if first['kind'] == 'wait_until' else first
    selected_kind = ('commitment' if proof.step.detail['kind'] in {'meal', 'appointment', 'dpa'}
                     else proof.step.detail['kind'])
    if (selected['id'] != proof.key or proof.step.key != proof.key or selected_kind != proof.kind
            or selected['kind'] != proof.kind
            or instant(selected['start_at'], 'bound.start') != proof.step.start
            or instant(selected['planning_finish_at'], 'bound.finish') != proof.step.end
            or instant(selected['expected_finish_at'], 'bound.expected') != proof.step.expected_end):
        raise InputError('Internal selected action differs from its precise witness step')
    if immediate and not same_instant(first['departure_at'], request['now']):
        raise InputError('Internal immediate first-action departure differs from planning now')
    pending = sorted(r['id'] for f in ('commitments', 'flexible_breaks') for r in request.get(f, [])
                     if not r.get('completed', False) and r['id'] not in ledger['missed_commitment_ids'])
    actual = [s['id'] for s in proof.timing['steps'] if s['kind'] in {'commitment', 'flexible_break'}]
    if sorted(actual) != pending or len(actual) != len(set(actual)):
        raise InputError('Internal proof changed pending timed anchors')
    return True
