"""One known required action, explicitly partial, with every timed anchor retained.

No master ledger mutation, future reopening, complete score, or full-plan claim.
This additive API uses the same canonical-ID/ledger contract as common_bridge.
"""
from __future__ import annotations

import copy
import hashlib
import json
from time import perf_counter

from generic_io import InputError, evaluate_sequence
from generic_io.adapter import diff, instant, mins, number
from generic_io.recommendations import WallTimeBudgetExceeded, _bounded_shape
from integration_round1.common_bridge import (
    _BoundedPracticalContext, _validate_request_ledger,
    build_bounded_context, check_next_ride,
)


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _ledger(request):
    """Keep the master IDs visible even when a later timed search is interrupted."""
    _bounded_shape(request, lambda: None)
    _validate_request_ledger(request)
    visited = request.get('visited_attraction_ids', [])
    must = request.get('must_ride_ids', [])
    if any(not isinstance(key, str) for key in must):
        raise InputError('must_ride_ids must contain string IDs')
    provenance = request.get('provenance', {})
    missed = sorted(set(provenance.get('missed_commitment_ids', [])))
    outstanding = sorted(row['id'] for field in ('commitments', 'flexible_breaks')
                         for row in request.get(field, []) if not row.get('completed', False))
    return {
        'original_outstanding_commitment_ids': outstanding,
        'unresolved_commitment_ids': list(outstanding),
        'original_unvisited_must_ride_ids': sorted(set(must) - set(visited)),
        'unresolved_must_ride_ids': sorted(set(must) - set(visited)),
        'completed_commitment_ids': sorted(set(provenance.get('completed_commitment_ids', []))),
        'missed_commitment_ids': missed,
        'all_reserved_attraction_ids': sorted(set(provenance.get('all_reserved_attraction_ids', []))),
        'visited_attraction_ids': copy.deepcopy(visited),
    }


def _fresh_operating(c, aid):
    # Use exactly the validated latest admitted record. Do not search old opens,
    # use rounded display ages, or mistake a field-change time for a source poll.
    row = c.last_reported.get(aid)
    return bool(row and row['observed'] is not None and row['status'] == 'open'
                and 0 <= diff(c.now, row['observed']) <= c.ttl)


def _partial_witness(raw, ledger, preserved, deferred):
    result = copy.deepcopy(raw)
    result['schema_version'] = 'wickward.partial-required-witness.v1'
    result['status'] = 'partial_subset_' + result['status']
    result['subset_feasible'] = result.pop('feasible')
    result['full_plan_verified'] = False
    result['infeasibility_proven'] = False
    result['complete_experience_score_claimed'] = False
    result['guarantee'] = 'Partial supplied-scenario timing only; all original obligations remain in the master ledger; no future admission guarantee'
    result['feasibility_scope'] = 'one_required_first_action_plus_all_pending_timed_anchors_and_exit_only'
    result['projected_subset_utility'] = result.pop('utility_total')
    result['projected_subset_known_utility_subtotal'] = result.pop('known_utility_subtotal')
    result['projected_subset_utility_known'] = result.pop('utility_total_is_complete')
    result['projected_attraction_ids'] = result.pop('awarded_attraction_ids')
    result['projection_remaining_must_ride_ids'] = result.pop('remaining_must_ride_ids')
    result['projection_remaining_commitment_ids'] = result.pop('remaining_commitment_ids')
    result.update(copy.deepcopy(ledger))
    result['preserved_pending_anchor_ids'] = list(preserved)
    result['deferred_unreserved_must_ride_ids'] = list(deferred)
    result['completion_not_inferred'] = True
    return result


def recommend_required_recovery(request, *, wall_time_budget_seconds=.8):
    """Return only a bounded, explicitly partial next action, never execute it.

    Fixed/flexible obligations are never waived. An explicit earlier miss stays
    missed and its reserved attraction cannot silently become a standby choice.
    Unknown utility suspends the ride-only ranking; reservations use deadlines.
    """
    budget = number(wall_time_budget_seconds, 'wall_time_budget_seconds', .001, 3)
    started = perf_counter()
    ledger = _ledger(request)
    original_hash = _fingerprint(request)
    deadline = started + budget
    result = {
        'schema_version': 'wickward.partial-required-recovery.v1',
        'status': 'pending', 'next_action': None, 'alternatives': [], 'unranked_options': [],
        'full_plan_verified': False, 'infeasibility_proven': False,
        'complete_experience_score_claimed': False,
        'completion_not_inferred': True, 'master_ledger_unchanged': True,
        'original_request_sha256': original_hash,
        'overall_success_possible': False if ledger['missed_commitment_ids'] else None,
        'scope': 'partial first action only; deferred must-rides are not canceled or completed',
        'ranking_complete': False, 'selected_action_rank_established': False,
        'checked_candidates': [], 'excluded': [],
        **copy.deepcopy(ledger),
    }
    def finish(status, reason):
        result.update(status=status, reason=reason)
        result['budget'] = {'wall_time_budget_seconds': budget,
                            'elapsed_seconds': perf_counter() - started,
                            'semantics': 'cooperative deadline; not hard real-time preemption'}
        assert _fingerprint(request) == original_hash
        return result
    def check_time():
        if perf_counter() >= deadline:
            raise WallTimeBudgetExceeded('partial recovery deadline reached')
    try:
        check_time()
        bridge = build_bounded_context(request, wall_time_budget_seconds=max(.001, deadline-perf_counter()))
        c = bridge.context
        if c is None:
            return finish('budget_not_verified', 'context_build_budget_exhausted')
        # Use the original overall deadline, not a renewed budget per candidate.
        c.wall_deadline = deadline
        result['planning_now'] = c.stamp(c.now)
        result['information_as_of'] = c.stamp(c.information_as_of)
        result['excluded_observations'] = copy.deepcopy(c.excluded_observations)
        result['excluded_forecasts'] = copy.deepcopy(c.excluded_forecasts)
        if c.raw.get('action_state', 'idle') != 'idle':
            return finish('current_action_locked', 'finish_or_explicitly_end_current_action')
        full = check_next_ride(bridge, recovery=bool(ledger['missed_commitment_ids']))
        result['original_full_check_status'] = full['status']
        result['original_full_check_reason'] = full['reason']
        if full['status'] == 'budget':
            return finish('budget_not_verified', 'full_check_budget_is_not_a_failure_proof')
        if full['feasible'] and not ledger['missed_commitment_ids']:
            return finish('not_applicable_full_witness_available', 'use_the_regular_all_required_checker')

        missed = set(ledger['missed_commitment_ids'])
        all_reserved = set(ledger['all_reserved_attraction_ids'])
        pending = sorted(set(c.anchors) - missed)
        unresolved_must = set(ledger['unresolved_must_ride_ids'])
        projection = copy.deepcopy(request)
        projection['must_ride_ids'] = []
        projection['commitments'] = [row for row in projection.get('commitments', []) if row['id'] not in missed]
        projection['flexible_breaks'] = [row for row in projection.get('flexible_breaks', []) if row['id'] not in missed]
        projection.setdefault('provenance', {})['partial_projection'] = {
            'not_a_master_ledger_update': True,
            'excluded_already_missed_ids': sorted(missed),
            'deferred_must_ride_ids': sorted(unresolved_must),
            'all_pending_timed_anchors_retained': pending,
        }
        candidates = []
        for key in pending:
            anchor = c.anchors[key]
            aid = anchor.get('attraction_id')
            if aid and (aid in c.visited or not _fresh_operating(c, aid)):
                result['excluded'].append({'kind': 'commitment', 'id': key,
                    'reason': 'linked_attraction_visited_or_no_fresh_operating_open',
                    'evidence': c.evidence(aid)})
                continue
            latest = anchor['latest']
            if anchor.get('finish_by'):
                latest = min(latest, anchor['finish_by']-mins(anchor['duration_minutes']))
            buffer = 0 if anchor['kind'] == 'flexible_break' else anchor.get('arrival_buffer_minutes', 5)
            effective = latest - mins(buffer)
            candidates.append({'kind': 'flexible_break' if anchor['kind'] == 'flexible_break' else 'commitment',
                'id': key, 'attraction_id': aid, 'effective_deadline': effective,
                'standby_wait_known': c.evidence(aid).get('wait_minutes') is not None if aid else None})
        for aid in sorted(unresolved_must):
            evidence = c.evidence(aid)
            if aid in all_reserved or aid in c.visited or evidence['kind'] != 'fresh_open':
                result['excluded'].append({'kind': 'ride', 'id': aid,
                    'reason': 'reserved_visited_or_no_fresh_open_numeric_wait', 'evidence': evidence})
                continue
            candidates.append({'kind': 'ride', 'id': aid, 'attraction_id': aid,
                               'wait_minutes': evidence['wait_minutes']})
        verified = []
        for rec in candidates:
            check_time()
            pc = _BoundedPracticalContext(projection, wall_deadline=deadline, monotonic_clock=perf_counter)
            if rec['kind'] == 'ride':
                pc._live_first_action = (rec['id'], rec['wait_minutes'])
            raw = evaluate_sequence(pc, [{'kind': rec['kind'], 'id': rec['id']}], complete_remaining=True)
            check_time()
            result['checked_candidates'].append({'kind': rec['kind'], 'id': rec['id'],
                'partial_subset_status': raw['status'], 'subset_feasible': raw['feasible'], 'full_plan_verified': False})
            if raw['status'] == 'search_budget_exceeded_not_verified':
                return finish('budget_not_verified', 'subset_search_budget_not_infeasibility')
            if not raw['feasible']:
                continue
            step = raw['sequence_steps'][0]
            utility, utility_source, utility_flags = c.utility(rec['attraction_id']) if rec['attraction_id'] else (None, None, ())
            elapsed = diff(instant(step['planning_finish_at'], 'finish'), c.now)
            deferred = sorted(unresolved_must - all_reserved - ({rec['id']} if rec['kind'] == 'ride' else set()))
            item = {'kind': rec['kind'], 'id': rec['id'], 'step': step,
                    'full_plan_verified': False, 'conditional': True,
                    'completion_not_inferred': True, 'must_revalidate_at_actual_arrival': True,
                    'supplied_utility': utility, 'utility_source': utility_source,
                    'utility_conditions': list(utility_flags),
                    'planning_minutes_to_first_completion': elapsed,
                    'first_action_efficiency': utility/max(1., elapsed) if utility is not None else None,
                    'all_original_unresolved_ids': copy.deepcopy(ledger),
                    'deferred_unreserved_must_ride_ids': deferred,
                    'witness': _partial_witness(raw, ledger, pending, deferred)}
            if rec['kind'] != 'ride':
                item['effective_arrival_deadline'] = c.stamp(rec['effective_deadline'])
                item['standby_wait_known'] = rec['standby_wait_known']
                item['selection_basis'] = 'pending_commitment_effective_deadline_before_ride_utility'
                key = (0, rec['effective_deadline'], instant(step['planning_finish_at'], 'finish'), rec['id'])
            else:
                item['selection_basis'] = 'explicit_required_utility_per_first_action_planning_minute'
                key = ((2, rec['id']) if utility is None else
                       (1, -item['first_action_efficiency'], -utility, elapsed, rec['id']))
            verified.append((key, item))
        check_time()
        verified.sort(key=lambda pair: pair[0])
        if not verified:
            result['ranking_complete'] = True
            return finish('no_verified_partial_action', 'no_candidate_preserves_all_pending_timed_anchors_and_exit')
        unknown_options = [dict(item, ranking_status='unranked') for _, item in verified
                           if item['kind'] == 'ride' and item['supplied_utility'] is None]
        result['unranked_options'] = sorted(unknown_options, key=lambda item: item['id'])
        known_options = [pair for pair in verified
                         if not (pair[1]['kind'] == 'ride' and pair[1]['supplied_utility'] is None)]
        result['ranking_complete'] = not unknown_options
        only_rides = all(item['kind'] == 'ride' for _, item in verified)
        if only_rides and unknown_options:
            result['alternatives'] = [item for _, item in known_options[:3]]
            return finish('partial_options_require_explicit_utility', 'unknown_utility_prevents_automatic_ride_only_ranking')
        result['next_action'] = known_options[0][1]
        result['selected_action_rank_established'] = True
        result['alternatives'] = [item for _, item in known_options[1:3]]
        return finish('partial_next_action_verified_under_supplied_assumptions',
                      'only_first_action_plus_timed_anchors_and_exit_verified; other_must_rides_unresolved')
    except WallTimeBudgetExceeded:
        result['next_action'] = None
        result['alternatives'] = []
        result['unranked_options'] = []
        result['ranking_complete'] = False
        return finish('budget_not_verified', 'cooperative_budget_exhausted_not_proven_infeasible')
