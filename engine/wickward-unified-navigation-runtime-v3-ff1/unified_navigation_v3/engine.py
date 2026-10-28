"""Single-request orchestration; no historical policy, execution or live inputs."""
from __future__ import annotations

import copy
from dataclasses import replace

from .context import (BudgetStop, Proof, fingerprint, prove, timing_for, validate_binding,
                      normalize_step)
from .kernel import instant, mins, diff

POLICY = 'generic_explicit_then_fallback_then_timed_then_partial_v3'


def action_key(kind, key):
    return {'kind': kind, 'id': key}


def empty_result(request=None, ledger=None):
    visible = None
    if ledger is not None:
        visible = {key: list(ledger[key]) for key in ('completed_commitment_ids',
            'visited_attraction_ids', 'all_reserved_attraction_ids', 'missed_commitment_ids')}
        visible['original_unresolved_must_ride_ids'] = list(ledger['unresolved_must_ride_ids'])
        visible['original_unresolved_commitment_ids'] = list(ledger['unresolved_commitment_ids'])
    return {
        'schema_version': 'wickward.navigation-result.v3',
        'input_contract_version': 'wickward.strict-input.v1',
        'status': 'no_verified_action',
        'planning_now': request['now'] if request is not None else None,
        'information_as_of': request['information_as_of'] if request is not None else None,
        'validation_complete': ledger is not None,
        'next_action': None, 'alternatives': [], 'verification_only': [], 'unranked_options': [],
        'timing': None, 'ledger': visible,
        'exclusions': {'actions': [], 'observations': [], 'forecasts': []},
        'replan': {
            'at': None, 'at_basis': None,
            'events': ['actual_action_completed', 'actual_wait_completed', 'status_or_wait_changed',
                       'delay_or_location_changed', 'reservation_changed', 'action_lock_changed'],
            'same_target_arrival_revalidation_required': False,
            'actual_arrival_overrides_modeled_timestamp': True,
            'new_information_only_when_genuinely_received': True,
        },
        'budget': {},
        'ranking': {
            'policy': POLICY, 'domain': 'current_decision_only_not_full_day_utility_optimization',
            'candidate_extraction_complete': False, 'ordinary_complete': False,
            'fallback_tier_complete': False, 'full_current_scan_complete': False,
            'full_current_scan_required': False, 'required_only_check_complete': False,
            'partial_attempted': False, 'partial_complete': False,
            'rank_established': False, 'ranking_complete': False, 'selection_basis': None,
            'checked_actions': [], 'unchecked_actions': [], 'current_action_scan': [],
            'timing_not_evaluated_actions': [],
        },
        'master_ledger_unchanged': True, 'completion_not_inferred': True,
        'full_plan_verified': False, 'overall_success_possible':
            False if ledger is not None and ledger['missed_commitment_ids'] else None,
        'infeasibility_proven': False, 'physical_admission_guaranteed': False,
        'scenario_only': True,
    }


def effective_deadline(c, key):
    anchor = c.anchors[key]
    latest = anchor['latest']
    if anchor.get('finish_by'):
        latest = min(latest, anchor['finish_by'] - mins(anchor['duration_minutes']))
    buffer = 0 if anchor['kind'] == 'flexible_break' else anchor.get('arrival_buffer_minutes', 5)
    return latest - mins(buffer)


def state_issues(c):
    problems = []
    for key, a in sorted(c.anchors.items()):
        reason = None
        linked = a.get('attraction_id')
        if linked in c.visited:
            reason = 'pending_linked_service_with_visited_attraction'
        buffer = 0 if a['kind'] == 'flexible_break' else a.get('arrival_buffer_minutes', 5)
        earliest_possible = max(c.now + mins(buffer), a['earliest'])
        if a['kind'] == 'dpa' and earliest_possible >= a['latest']:
            reason = 'uncompleted_dpa_past_prospective_entry_boundary'
        elif a['kind'] == 'flexible_break' and earliest_possible > a['latest']:
            reason = 'uncompleted_flexible_break_past_latest_start'
        elif a['kind'] in {'meal', 'appointment'} and c.now + mins(buffer) > a['earliest']:
            reason = 'uncompleted_fixed_service_past_arrival_margin'
        if a.get('finish_by') and earliest_possible + mins(a['duration_minutes']) > a['finish_by']:
            reason = 'uncompleted_service_finish_by_unavoidably_exceeded'
        if reason:
            problems.append({**action_key('flexible_break' if a['kind'] == 'flexible_break' else 'commitment', key),
                             'reason': reason, 'miss_or_completion_not_inferred': True})
    return problems


def is_current(c, proof):
    first = proof.timing['steps'][0]
    if instant(first['departure_at'], 'departure') != c.now:
        return False, 'first_departure_is_future'
    if proof.kind == 'ride':
        if c.evidence(proof.key)['kind'] != 'fresh_open':
            return False, 'first_operating_status_or_numeric_wait_requires_verification'
    else:
        a = c.anchors[proof.key]
        if a.get('attraction_id') and not c.fresh_operating(a['attraction_id']):
            return False, 'linked_service_requires_fresh_operating_open'
        # At a future fixed service, co-location/clock passage do not supply
        # the missing arrival or service state. The private partial may be used
        # by the separately verified five-minute guard before this public gate.
        if proof.step.walking == 0 and proof.step.start > c.now:
            return False, 'co_located_future_service_state_unrepresented'
        if a['kind'] in {'meal', 'appointment'} and proof.step.walking == 0:
            return False, 'fixed_service_arrival_or_entry_state_requires_resolution'
    return True, None


def option(c, proof, tier, *, diagnostic_reason=None, compact=False):
    step = normalize_step(c, proof.step)
    ride = proof.kind == 'ride'
    raw = c.rides[proof.key].get('utility') if ride else None
    effective, source, _ = c.utility(proof.key) if ride else (None, None, ())
    assumed = effective if ride and raw is None and effective is not None else None
    elapsed = diff(proof.step.end, c.now)
    conditional_reasons = set()
    conditions = set(proof.timing['conditions'])
    if conditions:
        conditional_reasons.add('witness_action_or_tail_conditions')
    if proof.timing['unknown_utility_ids']:
        conditional_reasons.add('factual_utility_unresolved')
    if proof.partial or c.master_ledger['missed_commitment_ids']:
        conditional_reasons.add('partial_or_remaining_recovery_scope')
    if assumed is not None:
        conditional_reasons.add('explicit_global_utility_fallback')
    conditional_reasons.add('arrival_or_entry_revalidation_required')
    if diagnostic_reason:
        conditional_reasons.add(diagnostic_reason)
    out = {
        **step, 'kind': proof.kind, 'id': proof.key,
        'supplied_utility': raw, 'utility_source': c.rides[proof.key].get('utility_source') if ride else None,
        'assumed_ranking_value': assumed,
        'assumed_ranking_source': 'request.policy.unknown_utility_fallback' if assumed is not None else None,
        'ranking_value_used': effective, 'ranking_tier': tier,
        'ranking_is_conditional': assumed is not None,
        'next_choice_score': effective / max(1., elapsed) if effective is not None else None,
        'planning_minutes_to_first_completion': elapsed,
        'conditional': bool(conditional_reasons), 'conditional_reasons': sorted(conditional_reasons),
        'conditions': sorted(conditions), 'full_plan_verified': proof.timing['full_plan_verified'],
        'completion_not_inferred': True,
        'scenario_feasibility_verified': True,
        'dispatch': 'travel_then_revalidate' if proof.step.walking > 0 else 'verify_here_before_service',
        'requires_new_decision_at_actual_arrival': True,
        'automatic_wait_or_service_authorized': False,
        'same_target_entry_revalidation_required': ride or bool(c.anchors[proof.key].get('attraction_id')),
    }
    if ride:
        out['first_action_wait_basis'] = proof.step.detail.get('first_action_wait_basis')
        out['evidence'] = c.evidence(proof.key)
    else:
        out['effective_arrival_deadline'] = c.stamp(effective_deadline(c, proof.key))
        arrival = instant(step['arrival_at'], 'arrival')
        out.update(witness_service_start_at=step['start_at'],
                   witness_service_finish_at=step['planning_finish_at'],
                   modeled_pre_service_wait_minutes=diff(proof.step.start, arrival))
    if proof.step.walking > 0:
        out.update(action_scope='travel_only', action_start_at=step['departure_at'],
                   expected_finish_at=step['arrival_at'], planning_finish_at=step['arrival_at'])
    else:
        out['action_scope'] = 'verify_before_entry'
    if diagnostic_reason:
        out.update(dispatch='verification_only', diagnostic_only=True, reason=diagnostic_reason)
    if compact:
        keep = {'kind', 'id', 'supplied_utility', 'assumed_ranking_value', 'assumed_ranking_source',
                'ranking_value_used', 'ranking_tier', 'ranking_is_conditional', 'conditional',
                'conditional_reasons', 'scenario_feasibility_verified', 'reason', 'dispatch', 'diagnostic_only',
                'full_plan_verified'}
        out = {k: v for k, v in out.items() if k in keep}
    else:
        out['timing'] = copy.deepcopy(proof.timing)
    return out


class Decision:
    def __init__(self, request, ledger, c, options, budget):
        self.request, self.ledger, self.c, self.options, self.budget = request, ledger, c, options, budget
        self.result = empty_result(request, ledger)
        self.result['exclusions']['observations'] = copy.deepcopy(c.excluded_observations)
        self.result['exclusions']['forecasts'] = copy.deepcopy(c.excluded_forecasts)
        self.checked, self.proofs = set(), {}
        self.ineligible_accounted = set()
        # This bounded identity inventory survives interrupted extraction. It
        # requires no new timing/evidence search after a budget stop.
        self.all_keys = [('ride', aid) for aid in sorted(c.rides) if aid not in c.visited | c.reserved]
        self.all_keys += [('flexible_break' if a['kind'] == 'flexible_break' else 'commitment', key)
                         for key, a in sorted(c.anchors.items())]
        self.candidates, self.retained = [], []
        self.active = None
        covered_linked = {a.get('attraction_id') for a in c.anchors.values()}
        self.uncovered_reserved_must = set(ledger['unresolved_must_ride_ids']) & c.reserved - covered_linked

    def exclude(self, kind, key, reason):
        self.result['exclusions']['actions'].append({**action_key(kind, key), 'reason': reason})

    def extract(self):
        c = self.c
        for aid in sorted(c.rides):
            self.budget.check()
            if aid in c.visited:
                self.exclude('ride', aid, 'already_visited')
                continue
            if aid in c.reserved:
                self.exclude('ride', aid, 'reserved_attraction_excluded_from_standby')
                continue
            evidence = c.evidence(aid)
            if evidence['kind'] == 'unavailable':
                self.exclude('ride', aid, 'fresh_' + evidence['status'])
                continue
            raw = c.rides[aid].get('utility')
            value = c.utility(aid)[0]
            tier = ('individual_supplied_utility' if raw is not None else
                    'explicit_global_utility_fallback' if value is not None else 'unknown_priority')
            self.candidates.append(('ride', aid, tier, evidence['kind'] == 'fresh_open'))
        for key, a in sorted(c.anchors.items()):
            kind = 'flexible_break' if a['kind'] == 'flexible_break' else 'commitment'
            eligible = not a.get('attraction_id') or c.fresh_operating(a['attraction_id'])
            self.candidates.append((kind, key, 'timed_service', eligible))
        for key in self.ledger['missed_commitment_ids']:
            self.exclude('commitment', key, 'reported_missed_retained_unresolved')
        self.all_keys = [(k, i) for k, i, _, _ in self.candidates]
        self.result['ranking']['candidate_extraction_complete'] = True

    def check_candidate(self, rec):
        kind, key, tier, eligible = rec
        pair = (kind, key)
        if pair in self.proofs:
            return self.proofs[pair]
        self.active = pair
        self.budget.check()
        if self.uncovered_reserved_must:
            proof, reason = None, 'unresolved_must_linked_only_to_reported_missed_service'
        else:
            proof, reason = prove(self.c, kind, key, permit_future=True)
        current, current_reason = is_current(self.c, proof) if proof else (False, reason)
        self.checked.add(pair)
        self.active = None
        self.proofs[pair] = (proof, current, current_reason)
        self.result['ranking']['current_action_scan'].append({**action_key(kind, key),
            'timing_check_performed': not bool(self.uncovered_reserved_must),
            'full_timing_verified': (proof is not None) if not self.uncovered_reserved_must else None,
            'current_action_eligible': current,
            'status': 'verified_current' if current else 'verification_only' if proof else 'not_verified',
            'reason': current_reason})
        if proof is None:
            self.exclude(kind, key, reason)
        elif not current:
            self.add_diagnostic(proof, tier, current_reason)
        return proof, current, current_reason

    def account_ineligible(self, rec):
        """Checking current evidence does not assert that timing was searched."""
        kind, key, _, _ = rec
        self.budget.check()
        reason = ('first_operating_status_or_numeric_wait_requires_verification' if kind == 'ride'
                  else 'linked_service_requires_fresh_operating_open')
        self.ineligible_accounted.add((kind, key))
        self.result['ranking']['current_action_scan'].append({**action_key(kind, key),
            'current_action_eligible': False, 'timing_check_performed': False,
            'full_timing_verified': None, 'status': 'not_evaluated_not_current', 'reason': reason})
        self.result['ranking']['timing_not_evaluated_actions'].append(action_key(kind, key))
        self.exclude(kind, key, reason)

    def required_diagnostic(self, schedule):
        """Reuse the one required-only witness without forced optional searches."""
        if not schedule.steps or len(self.result['verification_only']) >= 3:
            return
        first = schedule.steps[0]
        kind = 'commitment' if first.detail['kind'] in {'meal', 'appointment', 'dpa'} else first.detail['kind']
        tail = replace(schedule, steps=schedule.steps[1:], walking=schedule.walking-first.walking,
                       active=schedule.active-first.active)
        timing = timing_for(self.c, schedule.steps, schedule, False)
        proof = Proof(kind, first.key, first, tail, timing, self.c.original_request_sha256,
                      self.c.master_ledger_sha256, fingerprint(timing), False)
        validate_binding(proof, self.request, self.ledger, immediate=False)
        self.add_diagnostic(proof, 'required_only_timing_witness',
                            'required_only_first_action_requires_verification')

    def exit_reachable(self):
        self.budget.begin('exit_reachability_precheck')
        route = self.c.walk(self.c.current, self.c.exit)
        self.budget.complete()
        self.result['exit_route_precheck'] = {
            'scope': 'supplied_directed_graph_only', 'route_available': route is not None,
            'timing_or_admission_verified': False, 'physical_infeasibility_proven': False}
        if route is not None:
            return True
        reason = 'no_supplied_directed_path_from_current_location_to_exit'
        self.result['reason'] = reason
        self.exclude('exit', self.c.exit, reason)
        return False

    def add_diagnostic(self, proof, tier, reason):
        if len(self.result['verification_only']) < 3:
            self.result['verification_only'].append(option(self.c, proof, tier, diagnostic_reason=reason))

    def sort_proofs(self, proofs, tier):
        if tier == 'timed_service':
            return sorted(proofs, key=lambda p: (effective_deadline(self.c, p.key), p.step.end, p.key))
        return sorted(proofs, key=lambda p: (-self.c.utility(p.key)[0] / max(1., diff(p.step.end, self.c.now)),
                                            -self.c.utility(p.key)[0], diff(p.step.end, self.c.now), p.key))

    def select(self, proofs, tier, complete=True):
        ordered = self.sort_proofs(proofs, tier)[:3]
        for p in ordered:
            validate_binding(p, self.request, self.ledger)
        selected = ordered[0]
        self.retained = ordered
        out = self.result
        out['next_action'] = option(self.c, selected, tier)
        out['alternatives'] = [option(self.c, p, tier) for p in ordered[1:]]
        out['timing'] = copy.deepcopy(selected.timing)
        out['full_plan_verified'] = selected.timing['full_plan_verified']
        out['status'] = ('partial_required_action' if selected.partial else
                         'remaining_timing_action_after_reported_miss' if self.ledger['missed_commitment_ids'] else
                         'full_timing_action')
        out['ranking'].update(rank_established=complete, ranking_complete=complete,
                              selection_basis=tier)
        self.set_replan()

    def set_replan(self):
        action = self.result['next_action']
        if not action:
            return
        replan = self.result['replan']
        replan['same_target_arrival_revalidation_required'] = bool(
            action.get('requires_new_decision_at_actual_arrival', False))
        if action['kind'] == 'exit':
            replan['automatic_follow_up_required'] = action['movement_required']
            if not action['movement_required']:
                replan.update(at=None, at_basis=None, events=[])
                return
        if action['kind'] == 'wait_until':
            replan.update(at=action['until'], at_basis='explicitly_verified_wait_completion')
        elif action.get('dispatch') in {'travel_then_revalidate', 'travel_to_exit'}:
            replan.update(at=action['arrival_at'], at_basis='modeled_arrival_actual_arrival_overrides')

    def tier(self, name, completed_key):
        self.budget.begin(name)
        found = []
        for rec in self.candidates:
            if rec[2] != name or not rec[3]:
                continue
            proof, current, _ = self.check_candidate(rec)
            if current:
                found.append(proof)
                self.retained = self.sort_proofs(found, name)
        self.budget.complete()
        self.result['ranking'][completed_key] = True
        if found:
            self.select(found, name)
        return bool(found)

    def full_scan(self):
        self.result['ranking']['full_current_scan_required'] = True
        self.budget.begin('full_current_action_scan')
        timed, unknown = [], []
        for rec in self.candidates:
            if not rec[3]:
                self.account_ineligible(rec)
                continue
            proof, current, _ = self.check_candidate(rec)
            if current and rec[2] == 'timed_service':
                timed.append(proof)
            elif current and rec[2] == 'unknown_priority':
                unknown.append(proof)
        self.result['unranked_options'] = [option(self.c, p, 'unknown_priority',
            diagnostic_reason='unknown_utility_requires_explicit_input_or_fallback', compact=True)
            for p in sorted(unknown, key=lambda p: p.key)]
        required = None
        if not self.uncovered_reserved_must:
            required = self.c.schedule(self.c.now, self.c.current, tuple(sorted(self.c.required)),
                                       tuple(sorted(self.c.anchors)))
        self.result['ranking']['required_only_check_complete'] = True
        self.budget.complete()
        self.result['ranking']['full_current_scan_complete'] = True
        if timed:
            self.select(timed, 'timed_service')
            return True, required
        if unknown:
            self.result['status'] = 'choice_input_required'
            return True, required
        if required is not None and not self.c.required and not self.c.anchors and not self.ledger['unresolved_must_ride_ids']:
            # Exit requires literally no unresolved original obligation, including a miss.
            if not self.ledger['unresolved_commitment_ids']:
                self.exit(required)
                return True, required
        return False, required

    def exit(self, tail):
        c, out = self.c, self.result
        timing = timing_for(c, (), tail, False)
        conditions = timing['conditions']
        action = {'kind': 'exit', 'id': c.exit, 'location_id': c.exit,
            'departure_at': c.stamp(c.now), 'arrival_at': c.stamp(tail.exit_at),
            'expected_finish_at': c.stamp(tail.exit_at), 'planning_finish_at': c.stamp(tail.exit_at),
            'dispatch': 'travel_to_exit', 'movement_required': tail.exit_at > c.now,
            'action_scope': 'travel_only' if tail.exit_at > c.now else 'exit',
            'requires_new_decision_at_actual_arrival': tail.exit_at > c.now,
            'automatic_wait_or_service_authorized': False,
            'completion_not_inferred': True,
            'conditional': bool(conditions) or tail.exit_at > c.now,
            'conditional_reasons': sorted(set(conditions) | ({'arrival_revalidation_required'} if tail.exit_at > c.now else set())),
            'ranking_is_conditional': False, 'ranking_tier': 'verified_exit', 'full_plan_verified': True,
            'timing': timing}
        out.update(next_action=action, timing=timing, status='full_timing_action', full_plan_verified=True)
        out['ranking'].update(rank_established=True, ranking_complete=True, selection_basis='verified_exit')
        self.set_replan()

    def partial(self):
        out, c = self.result, self.c
        out['ranking']['partial_attempted'] = True
        self.budget.begin('partial_required_recovery')
        proofs = []
        for kind, key, tier, eligible in self.candidates:
            if not eligible or kind == 'ride' and key not in self.ledger['unresolved_must_ride_ids']:
                continue
            self.active = (kind, key)
            proof, _ = prove(c, kind, key, partial=True)
            self.active = None
            if proof is not None:
                proofs.append(proof)
        self.budget.complete()
        out['ranking']['partial_complete'] = True
        timed = [p for p in proofs if p.kind != 'ride']
        rides = [p for p in proofs if p.kind == 'ride']
        unknown = [p for p in rides if c.utility(p.key)[0] is None]
        out['unranked_options'] = [option(c, p, 'partial_unknown_priority',
            diagnostic_reason='unknown_utility_prevents_automatic_ride_only_ranking', compact=True)
            for p in sorted(unknown, key=lambda p: p.key)]
        if timed:
            chosen = sorted(timed, key=lambda p: (effective_deadline(c, p.key), p.step.end, p.key))
            tier = 'partial_timed_effective_deadline'
        elif unknown:
            out['status'] = 'choice_input_required'
            return
        elif rides:
            chosen = self.sort_proofs(rides, 'partial_required_utility')
            tier = 'partial_required_utility'
        else:
            return
        # Partial services precede required rides. Unknown ride priorities cannot
        # displace a separately justified timed-service choice.
        p = chosen[0]
        validate_binding(p, self.request, self.ledger)
        self.retained = []
        wait = None
        if self.options['allow_fixed5_partial_wait']:
            from .wait_guard import guard
            wait, diagnostic = guard(c, p, self.request, self.ledger)
            out['wait_check'] = diagnostic
        if wait is not None:
            action, timing = wait
            out.update(next_action=action, alternatives=[], timing=timing, status='partial_fixed5_wait')
            out['ranking'].update(rank_established=True, ranking_complete=not unknown,
                                  selection_basis='partial_timed_effective_deadline_then_internal_fixed5_guard')
            self.set_replan()
            return
        current, reason = is_current(c, p)
        if not current:
            self.add_diagnostic(p, tier, reason)
            out.update(status='verification_required', timing=copy.deepcopy(p.timing))
            return
        dispatchable = [v for v in chosen if is_current(c, v)[0]]
        # Use a timed sort for partial timed choices while retaining explicit tier metadata.
        sorting_tier = 'timed_service' if p.kind != 'ride' else 'partial_required_utility'
        self.select(dispatchable, sorting_tier)
        out['ranking']['ranking_complete'] = not unknown
        out['ranking']['selection_basis'] = tier
        for action in [out['next_action']] + out['alternatives']:
            action['ranking_tier'] = tier

    def finish(self):
        rank = self.result['ranking']
        rank['checked_actions'] = [action_key(*p) for p in sorted(self.checked)]
        rank['unchecked_actions'] = [action_key(*p) for p in sorted(
            set(self.all_keys) - self.checked - self.ineligible_accounted)]
        if self.active:
            rank['interrupted_action'] = action_key(*self.active)
        self.result['budget'] = self.budget.diagnostics(
            retained=bool(self.result['next_action'] and self.budget.stop_reason))
        return self.result

    def run(self):
        try:
            if self.request.get('action_state', 'idle') != 'idle':
                self.result['status'] = 'current_action_locked'
                return self.finish()
            issues = state_issues(self.c)
            if issues:
                self.result['status'] = 'state_resolution_required'
                self.result['exclusions']['actions'].extend(issues)
                return self.finish()
            if not self.exit_reachable():
                return self.finish()
            self.budget.begin('candidate_extraction')
            self.extract()
            self.budget.complete()
            if self.tier('individual_supplied_utility', 'ordinary_complete'):
                return self.finish()
            if self.tier('explicit_global_utility_fallback', 'fallback_tier_complete'):
                return self.finish()
            found, required = self.full_scan()
            if found:
                return self.finish()
            if required is not None and not self.ledger['missed_commitment_ids']:
                self.result['status'] = 'verification_required'
                self.result['reason'] = 'full_required_timing_witness_vetoes_partial'
                if self.result['timing'] is None:
                    self.result['timing'] = timing_for(self.c, required.steps, required, False)
                self.required_diagnostic(required)
                return self.finish()
            if self.options['allow_partial_recovery']:
                self.partial()
            elif self.result['verification_only']:
                self.result['status'] = 'verification_required'
            return self.finish()
        except BudgetStop as exc:
            self.result['ranking'].update(rank_established=False, ranking_complete=False)
            self.result['reason'] = exc.reason
            if exc.scope != 'total' and self.retained:
                p = self.retained[0]
                tier = ('individual_supplied_utility' if self.c.rides.get(p.key, {}).get('utility') is not None
                        else 'explicit_global_utility_fallback')
                self.budget.stage_deadline = None
                self.budget.check_total()
                self.select(self.retained, tier, complete=False)
            else:
                self.result.update(status='budget_abstention', next_action=None, alternatives=[], timing=None)
            return self.finish()
