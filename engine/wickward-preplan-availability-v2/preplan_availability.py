"""Supplied, as-of scheduled constraints for the frozen pre-day planner.

This is a separate planning interface, not a navigation handoff schema. No I/O
except explicitly loading source-locked cloud dependencies and optional CLI JSON.
"""
from __future__ import annotations

import builtins
import copy
from dataclasses import dataclass, replace
from bisect import bisect_left
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from types import ModuleType

HERE = Path(__file__).resolve().parent
PRODUCER = HERE.parent / 'wickward-preplan-handoff-v1'
PRODUCER_SHA256 = 'adfa65e9fe7eb741ed4b433b39175ef4a6879131798dd23c5b6f0446298e6a39'
SCHEMA_VERSION = 'wickward.preplan-availability.v2'
MAX_EXTRA_FORECAST_BOUNDARIES = 16


def _load_producer():
    path = PRODUCER / 'preplan_handoff.py'
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PRODUCER_SHA256:
        raise RuntimeError('Frozen preplan producer source hash mismatch')
    name = '_scheduled_availability_frozen_producer'
    module = ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    return module


producer = _load_producer()
kernel, boundary, adapter = producer.kernel, producer.boundary, producer._adapter
InputError = kernel.InputError
instant, mins, diff = kernel.instant, kernel.mins, kernel.diff


@dataclass(frozen=True)
class Availability:
    entry_windows: tuple | None
    closed_intervals: tuple
    finish_by: datetime | None
    provenance: dict


@dataclass(frozen=True)
class Constraints:
    by_attraction: dict
    park_start: datetime | None
    park_exit: datetime | None
    source_envelope: dict


def _keys(row, allowed, required, path):
    if not isinstance(row, dict):
        raise InputError(f'{path}: object required')
    unsupported = set(row) - set(allowed)
    missing = set(required) - set(row)
    if unsupported or missing:
        raise InputError(f'{path}: unsupported fields {sorted(unsupported)}; missing fields {sorted(missing)}')


def _provenance(row, asof, path):
    for key in ('source',):
        if not isinstance(row.get(key), str) or not row[key].strip():
            raise InputError(f'{path}.{key}: nonempty source required')
    received = instant(row.get('received_at'), path + '.received_at')
    issued = instant(row.get('issued_at'), path + '.issued_at')
    published = instant(row['published_at'], path + '.published_at') if row.get('published_at') is not None else None
    if not received <= issued <= asof or (published is not None and published > received):
        raise InputError(f'{path}: require received_at <= issued_at <= information_as_of and known published_at <= received_at; future-issued or late-received records cannot be backfilled')
    return {key: copy.deepcopy(row[key]) for key in ('issued_at', 'published_at', 'received_at', 'source') if key in row}


def _intervals(rows, path):
    if not isinstance(rows, list) or len(rows) > 64:
        raise InputError(f'{path}: array of at most 64 intervals required')
    result = []
    for index, row in enumerate(rows):
        at = f'{path}[{index}]'
        _keys(row, {'start_at', 'end_at'}, {'start_at', 'end_at'}, at)
        start, end = instant(row['start_at'], at + '.start_at'), instant(row['end_at'], at + '.end_at')
        if start >= end:
            raise InputError(f'{at}: require start_at < end_at')
        result.append((start, end))
    result.sort()
    if any(a[1] > b[0] for a, b in zip(result, result[1:])):
        raise InputError(f'{path}: overlapping intervals must be resolved by the supplier')
    return tuple(result)


def validate_envelope(envelope):
    # The reviewed shape checker rejects cycles, non-JSON/nonfinite values, and
    # oversize/nested objects before deepcopy, sort or field access.
    kernel._validation._shape(envelope)
    _keys(envelope, {'schema_version', 'generic_request', 'scheduled_availability', 'park_hours'},
          {'schema_version', 'generic_request'}, 'envelope')
    if envelope['schema_version'] != SCHEMA_VERSION:
        raise InputError(f'schema_version must be {SCHEMA_VERSION}')
    request = envelope['generic_request']
    ledger = boundary.validate_request(request)
    asof = instant(request['information_as_of'], 'information_as_of')
    planning_now = instant(request['now'], 'now')
    ttl = request.get('policy', {}).get('freshness_minutes', 10)
    # A scheduled statement is not an operational observation. Strict rejection
    # makes stale evidence visible instead of silently reviving it for tomorrow.
    for index, row in enumerate(request.get('observations', [])):
        at = f'generic_request.observations[{index}]'
        times = [instant(row[key], at + '.' + key) for key in ('observed_at', 'changed_at', 'fetched_at') if row.get(key) is not None]
        if any(t > asof for t in times):
            raise InputError(f'{at}: future operational evidence after information_as_of')
        if row.get('observed_at') is None:
            raise InputError(f'{at}: operational polling time missing; changed/fetched time cannot establish freshness')
        if diff(planning_now, instant(row['observed_at'], at + '.observed_at')) > ttl:
            raise InputError(f'{at}: stale operational observation at planning start; supply an explicit forecast instead')
    for index, row in enumerate(request.get('forecasts', [])):
        if instant(row['issued_at'], 'forecast.issued_at') > asof:
            raise InputError(f'generic_request.forecasts[{index}]: issued after information_as_of')
    rows = envelope.get('scheduled_availability', [])
    if not isinstance(rows, list) or len(rows) > 40:
        raise InputError('scheduled_availability: array of at most 40 attraction records required')
    attractions = {row['id'] for row in request.get('attractions', [])}
    parsed = {}
    provenance_keys = {'issued_at', 'published_at', 'received_at', 'source'}
    required_provenance = {'issued_at', 'received_at', 'source'}
    for index, row in enumerate(rows):
        at = f'scheduled_availability[{index}]'
        _keys(row, provenance_keys | {'attraction_id', 'entry_windows', 'closed_intervals', 'finish_by'},
              required_provenance | {'attraction_id'}, at)
        aid = row['attraction_id']
        if not isinstance(aid, str) or aid not in attractions or aid in parsed:
            raise InputError(f'{at}.attraction_id: known, unique attraction required')
        provenance = _provenance(row, asof, at)
        if not set(row) & {'entry_windows', 'closed_intervals', 'finish_by'}:
            raise InputError(f'{at}: at least one explicit constraint required')
        windows = _intervals(row['entry_windows'], at + '.entry_windows') if 'entry_windows' in row else None
        closed = _intervals(row.get('closed_intervals', []), at + '.closed_intervals')
        finish = instant(row['finish_by'], at + '.finish_by') if 'finish_by' in row else None
        parsed[aid] = Availability(windows, closed, finish, provenance)
    start = end = None
    if 'park_hours' in envelope:
        row = envelope['park_hours']
        _keys(row, provenance_keys | {'entry_start_at', 'exit_by'}, required_provenance | {'entry_start_at', 'exit_by'}, 'park_hours')
        _provenance(row, asof, 'park_hours')
        start, end = instant(row['entry_start_at'], 'park_hours.entry_start_at'), instant(row['exit_by'], 'park_hours.exit_by')
        if start >= end:
            raise InputError('park_hours: entry_start_at must precede exit_by')
    if start and start > planning_now:
        for index, row in enumerate(request.get('observations', [])):
            if diff(start, instant(row['observed_at'], 'observation.observed_at')) > ttl:
                raise InputError(f'generic_request.observations[{index}]: stale operational observation at effective park start')
    return copy.deepcopy(request), Constraints(parsed, start, end, copy.deepcopy(envelope)), ledger


class AvailabilityContext(adapter.PracticalContext):
    """Constraint checks run in every search expansion and final replay."""
    def __init__(self, request, constraints):
        self.has_constraints = bool(constraints.by_attraction or constraints.park_start or constraints.park_exit)
        super().__init__(request)
        self.availability = constraints.by_attraction
        self.original_now, self.original_deadline = self.now, self.deadline
        self.now = max(self.now, constraints.park_start) if constraints.park_start else self.now
        self.deadline = min(self.deadline, constraints.park_exit) if constraints.park_exit else self.deadline
        self.forecast_boundaries = self._forecast_boundaries()
        self.forecast_boundary_sets = {aid: set(rows) for aid, rows in self.forecast_boundaries.items()}
        self.forecast_boundary_stats = {'candidate_calls': 0, 'extra_candidates_evaluated': 0,
            'maximum_added_per_call': 0, 'truncated_calls': 0}

    def _forecast_boundaries(self):
        # A sweep keeps only boundaries with actual supplied forecast coverage.
        # Half-open forecast ends remove coverage before admission at that time.
        by_id = {}
        for fc in self.forecast_provider.forecasts:
            if fc.attraction_id not in self.availability or fc.issued_at > self.information_as_of:
                continue
            events = by_id.setdefault(fc.attraction_id, {})
            events[fc.valid_from] = events.get(fc.valid_from, 0) + 1
            events[fc.valid_until] = events.get(fc.valid_until, 0) - 1
        output = {}
        for aid, events in by_id.items():
            coverage = 0
            boundaries = []
            for at, delta in sorted(events.items()):
                coverage += delta
                if coverage > 0 and self.now <= at < self.deadline and self._entry_allowed(aid, at):
                    boundaries.append(at)
            output[aid] = tuple(boundaries)
        return output

    def _entry_allowed(self, aid, at):
        attraction, record = self.rides[aid], self.availability[aid]
        if attraction.get('opens_at') and at < attraction['opens_at']:
            return False
        if attraction.get('queue_closes_at') and at + mins(self.admission_buffer) >= attraction['queue_closes_at']:
            return False
        if record.entry_windows is not None and not any(lo <= at and at + mins(self.admission_buffer) < hi for lo, hi in record.entry_windows):
            return False
        return not any(lo <= at < hi for lo, hi in record.closed_intervals)

    def _extra_forecast_candidates(self, aid, earliest, existing):
        boundaries = self.forecast_boundaries.get(aid, ())
        index = bisect_left(boundaries, earliest)
        selected = []
        truncated = False
        for at in boundaries[index:]:
            if at in existing:
                continue
            if len(selected) == MAX_EXTRA_FORECAST_BOUNDARIES:
                truncated = True
                break
            selected.append(at)
        return tuple(selected), truncated

    def boundary_report(self):
        return {'max_extra_boundaries_per_ride_call': MAX_EXTRA_FORECAST_BOUNDARIES,
            'selection': 'earliest source-available covered forecast start/end boundaries compatible with entry constraints',
            'tie_break': 'earliest planning finish, then earliest entry',
            'eligible_boundary_count_by_attraction': {aid: len(rows) for aid, rows in sorted(self.forecast_boundaries.items())},
            **self.forecast_boundary_stats,
            'global_intentional_wait_horizon_unchanged': True,
            'not_exhaustive_or_globally_optimal': True}

    def stamp(self, t):
        # Preserve baseline output exactly when the envelope is unconstrained.
        if not self.has_constraints:
            return super().stamp(t)
        return t.astimezone(self.zone).isoformat()

    def _entry_candidates(self, aid, earliest):
        record = self.availability.get(aid)
        candidates = {earliest}
        opening = self.rides[aid].get('opens_at')
        if opening:
            candidates.add(max(earliest, opening))
        if record:
            candidates.update(start for start, end in record.entry_windows or () if start >= earliest)
            candidates.update(end for start, end in record.closed_intervals if end >= earliest)
        return sorted(t for t in candidates if earliest <= t < self.deadline)

    def _allowed(self, aid, step, *, linked=False):
        record = self.availability.get(aid)
        start, end = step.start, step.end
        attraction = self.rides[aid]
        if linked:
            if attraction.get('opens_at') and start < attraction['opens_at']:
                return False
            if attraction.get('queue_closes_at') and start + mins(self.admission_buffer) >= attraction['queue_closes_at']:
                return False
            if attraction.get('ride_finishes_by') and end > attraction['ride_finishes_by']:
                return False
        if record is None:
            return True
        if record.entry_windows is not None and not any(lo <= start and start + mins(self.admission_buffer) < hi for lo, hi in record.entry_windows):
            return False
        if any(lo <= start < hi or (start < hi and lo < end) for lo, hi in record.closed_intervals):
            return False
        if record.finish_by is not None and end > record.finish_by:
            return False
        return True

    def _annotate(self, aid, step, original_start):
        if aid not in self.availability:
            return step
        detail = copy.deepcopy(step.detail)
        detail['scheduled_availability'] = {
            'source': copy.deepcopy(self.availability[aid].provenance),
            'entry_window_is_not_finish_by': True,
            'added_wait_minutes': max(0, diff(step.start, original_start)),
            'never_operating_status_evidence': True,
        }
        return replace(step, detail=detail, conditions=tuple(sorted(set(step.conditions) | {
            'scheduled_availability_is_constraint_not_operating_evidence',
            'scheduled_hours_do_not_guarantee_service_completion',
        })))

    def ride(self, aid, t, loc, override_wait=None):
        if aid not in self.availability:
            return super().ride(aid, t, loc, override_wait=override_wait)
        leg = self.walk(loc, self.rides[aid]['location_id'])
        if leg is None:
            return None, 'missing_walking_route'
        first_entry = t + mins(leg[0])
        reason = 'scheduled_availability_no_verified_entry'
        original = self._entry_candidates(aid, first_entry)
        # An explicit override is not a changing JSON forecast; preserve v1.
        extra, truncated = self._extra_forecast_candidates(aid, first_entry, set(original)) if override_wait is None else ((), False)
        self.forecast_boundary_stats['candidate_calls'] += 1
        self.forecast_boundary_stats['extra_candidates_evaluated'] += len(extra)
        self.forecast_boundary_stats['maximum_added_per_call'] = max(self.forecast_boundary_stats['maximum_added_per_call'], len(extra))
        self.forecast_boundary_stats['truncated_calls'] += int(truncated)
        best = None
        candidates = sorted(set(original) | set(extra))
        compare_forecast_candidates = override_wait is None and any(
            entry in self.forecast_boundary_sets.get(aid, set()) for entry in candidates)
        for entry in candidates:
            step, failure = super().ride(aid, entry - mins(leg[0]), loc, override_wait=override_wait)
            if step is not None and self._allowed(aid, step):
                if not compare_forecast_candidates:
                    return self._annotate(aid, step, first_entry), None
                if best is None or (step.end, step.start) < (best.end, best.start):
                    best = step
            if failure:
                reason = failure
        if best is not None:
            result = self._annotate(aid, best, first_entry)
            detail = copy.deepcopy(result.detail)
            detail['forecast_boundary_search'] = {'extra_candidate_count': len(extra),
                'extra_candidate_entries': [self.stamp(at) for at in extra],
                'truncated': truncated, 'selection': 'earliest_planning_finish_then_entry',
                'forecast_boundary_comparison_active': compare_forecast_candidates,
                'selected_entry_is_extra_forecast_boundary': best.start in extra}
            return replace(result, detail=detail), None
        return None, reason

    def anchor(self, key, t, loc):
        anchor = self.anchors[key]
        aid = anchor.get('attraction_id')
        if not aid or aid not in self.availability:
            return super().anchor(key, t, loc)
        leg = self.walk(loc, anchor['location_id'])
        if leg is None:
            return None
        offset = mins(leg[0] + anchor.get('arrival_buffer_minutes', 5))
        first_entry = max(t + offset, anchor['earliest'])
        for entry in self._entry_candidates(aid, first_entry):
            step = super().anchor(key, max(t, entry - offset), loc)
            if step is not None and self._allowed(aid, step, linked=True):
                return self._annotate(aid, step, first_entry)
        return None


def _engine(constraints, observed):
    """Execute byte-identical engine text in one call-private dependency scope."""
    generic = ModuleType('_availability_generic_contract')
    generic.build_context = lambda request: AvailabilityContext(request, constraints)
    for name in ('evaluate_sequence', 'InputError', 'SearchBudgetExceeded'):
        setattr(generic, name, getattr(adapter, name))
    imports = {'generic_io': generic, 'route_problem': producer.problem, 'route_search': producer.search}
    def local_import(name, globals=None, locals=None, fromlist=(), level=0):
        if not level and name in imports:
            return imports[name]
        return builtins.__import__(name, globals, locals, fromlist, level)
    path = PRODUCER / '_vendor/engine_api/plan_common.py'
    raw = path.read_bytes()
    expected = json.loads((PRODUCER / 'VENDORED_SOURCES.json').read_text())[str(path.relative_to(PRODUCER))]
    if hashlib.sha256(raw).hexdigest() != expected:
        raise RuntimeError('Frozen plan_common source hash mismatch')
    module = ModuleType('_availability_call_private_plan')
    module.__file__ = str(path)
    module.__dict__['__builtins__'] = {**vars(builtins), '__import__': local_import}
    old_path = list(sys.path)
    try:
        exec(compile(raw, str(path), 'exec'), module.__dict__)
    finally:
        sys.path[:] = old_path
    class ObservedProblem(producer.problem.RouteProblem):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.feasible_candidates = {}
            observed.append(self)
        def evaluate(self, route, charge=True):
            result = super().evaluate(route, charge=charge)
            if charge and result.feasible:
                self.feasible_candidates.setdefault(tuple(route), result)
            return result
    module.RouteProblem = ObservedProblem
    return module


def _state_resolution(request, ledger):
    if ledger['missed_commitment_ids']:
        return {'status': 'needs_state_resolution', 'feasible': False,
                'reason': 'Reported missed commitments remain unresolved',
                'missed_commitment_ids': ledger['missed_commitment_ids']}
    pending = sorted(row['id'] for row in request.get('commitments', [])
                     if not row.get('completed', False) and row.get('attraction_id') in ledger['visited_attraction_ids'])
    if pending:
        return {'status': 'needs_state_resolution', 'feasible': False,
                'reason': 'Visited linked attractions have pending commitments',
                'unresolved_linked_commitment_ids': pending}
    return None


def evaluate_sequence(envelope, sequence, *, complete_remaining=False):
    """Authoritative standalone evaluator; supplied routes cannot bypass rules."""
    request, constraints, ledger = validate_envelope(envelope)
    state = _state_resolution(request, ledger)
    if state:
        return state
    c = AvailabilityContext(request, constraints)
    if c.now >= c.deadline:
        return {'status': 'sequence_not_verified_feasible', 'feasible': False,
                'reason': 'park_start_at_or_after_effective_exit_deadline'}
    result = adapter.evaluate_sequence(c, sequence, complete_remaining=complete_remaining)
    if constraints.by_attraction:
        result['forecast_boundary_search'] = c.boundary_report()
    return result


def create_plan(envelope, *, route_evaluation_budget=5000, max_intentional_wait_minutes=60):
    """Produce a conditional plan under supplied schedules, never a proof of operation."""
    try:
        request, constraints, ledger = validate_envelope(envelope)
        state = _state_resolution(request, ledger)
        if state:
            return {**state, 'plan': None}
        context = AvailabilityContext(request, constraints)
        if context.now >= context.deadline:
            return {'status': 'no_verified_plan', 'plan': None,
                    'reason': 'Park start is at or after effective exit deadline; no plan verified under supplied constraints'}
        instances = []
        engine = _engine(constraints, instances)
        primary = engine.plan(request, route_evaluation_budget=route_evaluation_budget,
                              max_intentional_wait_minutes=max_intentional_wait_minutes)
        if primary.get('status') != 'conditional_model_plan' or not primary.get('feasible'):
            return {'status': primary.get('status', 'no_verified_plan'), 'plan': None, 'primary_result': primary,
                    'forecast_boundary_search': (instances[0].c if instances else context).boundary_report()}
        fresh = evaluate_sequence(envelope, primary['route'])
        if not fresh['feasible']:
            return {'status': 'internal_validation_failed', 'plan': None, 'primary_result': primary}
        original = constraints.source_envelope
        issued = datetime.now(timezone.utc).isoformat()
        result = {'status': 'scheduled_plan_ready', 'plan': {
            'schema_version': 'wickward.scheduled-preplan.v2',
            'input_envelope': original, 'input_envelope_sha256': producer.request_sha256(original),
            'ordered_route': copy.deepcopy(primary['route']),
            'issued_at': issued, 'plan_information_as_of': request['information_as_of'],
            'provenance': {'producer': 'wickward-preplan-availability-v2',
                'constraint_protocol': SCHEMA_VERSION,
                'operating_status_evidence_created': False,
                'supplied_utility_unchanged': True,
                'scheduled_hours_guarantee_service_completion': False,
                'historical_backfill_performed': False,
                'requires_same_constraints_for_revalidation': True,
                'not_accepted_by_navigation_handoff_v2': True,
                'empirical_quality_gate_passed': False}},
            'primary_result': primary, 'final_validation': fresh,
            'effective_park_limits': {'planning_start_at': context.stamp(context.now),
                                      'exit_by': context.stamp(context.deadline)},
            'forecast_boundary_search': instances[0].c.boundary_report(),
            'statistics': {'route_evaluations': instances[0].evaluations,
                           'distinct_feasible_candidates_observed': len(instances[0].feasible_candidates)}}
        return result
    except (InputError, kernel.SearchBudgetExceeded) as exc:
        return {'status': 'needs_input', 'plan': None, 'reason': str(exc)}


def run_json(payload, **options):
    try:
        return create_plan(producer.parse_request_json(payload), **options)
    except InputError as exc:
        return {'status': 'needs_input', 'plan': None, 'reason': str(exc)}


if __name__ == '__main__':
    json.dump(run_json(sys.stdin.buffer.read(boundary.MAX_INPUT_BYTES + 1)), sys.stdout,
              ensure_ascii=False, allow_nan=False, indent=2)
    print()
