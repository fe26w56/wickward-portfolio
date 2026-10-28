"""Internal opt-in partial-only fixed5 guard, prospectively recovered semantics.

Source: partial_wait_v1/controller.py sha256
b452a11ca5248b8b7e2e83d7c47c6b6e977e22e6cc348b83aa7ec8143f2d4581
Walking amendment sha256 02bfa048be105043cfa20678e286dc5b649a93811db130ba7d266fd8f2f600c0.
No historical execution/performance acceptance is implied by source provenance.
"""
from __future__ import annotations

import copy
from .context import BudgetStop, prove, validate_binding, same_instant
from .kernel import Step, InputError, mins, diff, instant

WAIT_US = 300_000_000


def micros(delta):
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def guard(c, original, request, ledger):
    validate_binding(original, request, ledger)
    if not original.partial:
        raise InputError('fixed5 requires a same-request verified partial candidate')
    check = {'status': 'wait_not_applicable', 'full_plan_verified': False}
    if original.kind != 'commitment':
        return None, check
    target = c.anchors[original.key]
    if target['kind'] not in {'meal', 'appointment'} or target.get('attraction_id') is not None:
        return None, check
    step = original.timing['steps'][0]
    departure = instant(step['departure_at'], 'departure')
    arrival = instant(step['arrival_at'], 'arrival')
    if departure != c.now or arrival < departure:
        raise InputError('fixed5 requires a precise immediate first leg')
    walk_us = micros(arrival - departure)
    bound = target['earliest'] - mins(target.get('arrival_buffer_minutes', 5))
    available_us = micros(bound - c.now)
    base_pass = available_us - walk_us >= WAIT_US
    travel_pass = 2 * WAIT_US + 3 * walk_us <= 2 * available_us
    assumption = {
        'version': 'wickward.uncalibrated-first-leg-assumption.v1', 'factor': 1.5,
        'source': 'uncalibrated prospective engineering assumption; not fitted or verified physical bound',
        'current_planned_first_leg_minutes': walk_us / 60_000_000,
        'assumed_max_first_leg_minutes': 3 * walk_us / 120_000_000,
        'assumed_max_first_leg_microseconds_numerator': 3 * walk_us,
        'assumed_max_first_leg_microseconds_denominator': 2,
        'planned_duration_already_includes_supplied_or_learned_pace': True,
        'fixed_arrival_deadline': c.stamp(bound),
        'five_minute_deferral_within_assumed_bound': travel_pass,
        'physical_upper_bound_verified': False, 'calibrated_probability_claimed': False,
        'above_assumed_bound_or_location_error_can_still_cause_miss': True,
        'additional_assumption_does_not_advance_information_cutoff': True,
    }
    check.update(avoidable_destination_idle_minutes=(available_us - walk_us) / 60_000_000,
                 base_idle_gate_passed=base_pass, walking_uncertainty_assumption=assumption)
    if not base_pass:
        check['status'] = 'wait_not_needed_below_quantum'
        return None, check
    if not travel_pass:
        check['status'] = 'wait_not_selected_insufficient_assumed_travel_slack'
        return None, check
    c.budget.begin('optional_fixed5_wait')
    until = c.now + mins(5)
    waitstep = Step('wait_until@0', c.current, c.now, until, until, 0., 5.,
        ('waiting_does_not_refresh_observations_or_forecasts', 'uncalibrated_first_leg_assumption'),
        {'kind': 'wait_until', 'arrive_at': c.stamp(c.now), 'wait_start_at': c.stamp(c.now),
         'wait_until': c.stamp(until), 'waiting_minutes': 5., 'location_unchanged': True,
         'information_as_of': c.stamp(c.information_as_of)})
    try:
        proof, reason = prove(c, 'commitment', original.key, partial=True, t=until, prefix=(waitstep,))
        c.budget.check()
        validate_binding(original, request, ledger)
        if proof is None:
            c.budget.complete()
            check.update(status='wait_not_verified', reason=reason)
            return None, check
        validate_binding(proof, request, ledger)
        protected = proof.timing['steps'][1]
        if (len(proof.timing['steps']) < 2 or protected['kind'] != 'commitment'
                or protected['id'] != original.key
                or not same_instant(protected['start_at'], step['start_at'])
                or not same_instant(protected['planning_finish_at'], step['planning_finish_at'])
                or not same_instant(protected['departure_at'], c.stamp(until))
                or any(s['kind'] == 'ride' for s in proof.timing['steps'])):
            raise InputError('fixed5 witness changed the protected service or timed-only subset')
        c.budget.complete()
        check.update(status='partial_wait_verified', same_cutoff_and_all_pending_timed_witness_verified=True)
        reasons = {'new_decision_required_after_wait', 'partial_or_remaining_recovery_scope',
                   'uncalibrated_walking_assumption', 'witness_action_or_tail_conditions'}
        if proof.timing['unknown_utility_ids']:
            reasons.add('factual_utility_unresolved')
        action = {
            'kind': 'wait_until', 'until': c.stamp(until), 'duration_minutes': 5,
            'location_id': c.current, 'dispatch': 'wait_locally_then_new_decision',
            'action_scope': 'explicit_verified_local_wait', 'full_plan_verified': False,
            'completion_not_inferred': True, 'requires_new_ordinary_decision_after_wait': True,
            'automatic_protected_service_authorized': False,
            'protected_candidate': {'kind': 'commitment', 'service_kind': target['kind'], 'id': original.key,
                'diagnostic_only': True, 'conditional': True, 'ranking_is_conditional': False,
                'conditional_reasons': ['diagnostic_protected_candidate_not_executable', 'partial_scope']},
            'walking_uncertainty_assumption': copy.deepcopy(assumption),
            'conditional': True, 'conditional_reasons': sorted(reasons),
            'ranking_is_conditional': False, 'ranking_tier': 'partial_fixed5_wait',
            'conditions': list(proof.timing['conditions']), 'timing': copy.deepcopy(proof.timing),
        }
        return (action, proof.timing), check
    except BudgetStop as exc:
        if exc.scope == 'total':
            raise
        # This optional failure cannot invalidate a still-bound immediate
        # candidate, but it never permits a fresh search after the stop.
        c.budget.stage_deadline = None
        c.budget.check_total()
        validate_binding(original, request, ledger)
        check.update(status='wait_not_verified_budget', reason=exc.reason)
        return None, check
