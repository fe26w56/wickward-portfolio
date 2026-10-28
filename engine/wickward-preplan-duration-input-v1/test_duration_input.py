"""Fictional correctness fixtures; never official receipts or historical data."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest

import duration_input as d

T = datetime(2035, 4, 10, 9, tzinfo=timezone.utc)


def ts(minutes=0):
    return (T + timedelta(minutes=minutes)).isoformat()


def fixture():
    request = {'schema_version': 'wickward.generic-input.v1', 'now': ts(),
        'information_as_of': ts(-1440), 'exit_deadline': ts(240), 'park_timezone': 'UTC',
        'current_location_id': 'gate', 'exit_location_id': 'gate',
        'locations': [{'id': 'gate', 'identity_verified': True, 'identity_source': 'fictional fixture'}],
        'walking_edges': [], 'attractions': [{'id': 'a', 'location_id': 'gate',
            'ride_minutes': 10, 'utility': 8, 'utility_source': 'fictional supplied preference'}],
        'observations': [], 'forecasts': [{'attraction_id': 'a', 'issued_at': ts(-1440),
            'valid_from': ts(), 'valid_until': ts(240), 'wait_minutes': 0,
            'source': 'fictional supplied forecast', 'scenario_id': 'synthetic'}],
        'must_ride_ids': ['a'], 'visited_attraction_ids': [], 'action_state': 'idle',
        'commitments': [], 'flexible_breaks': [], 'legacy_non_preemptive_rests': [],
        'policy': {'wait_buffer_minutes': 0, 'wait_buffer_per_future_minute': 0,
            'boarding_buffer_minutes': 0, 'exit_buffer_minutes': 0,
            'queue_admission_buffer_minutes': 0, 'freshness_minutes': 10}}
    public = {'attraction_id': 'a', 'duration_kind': 'approximate',
        'duration_nominal_minutes': 20, 'duration_lower_minutes': None,
        'duration_upper_minutes': None, 'source_url': 'https://example.invalid/fictional/a',
        'source_title': 'Fictional duration source', 'source_field_label': 'Experience duration',
        'source_text': 'Approximately 20 minutes', 'received_at': ts(-1450),
        'published_at': None, 'source_sha256': '0' * 64,
        'source_artifact': 'fictional-not-an-actual-source.txt'}
    overhead = {'attraction_id': 'a', 'overhead_minutes': 5, 'scenario_id': 'fictional_fixed5',
        'assumption': 'Supplied 5 minutes for otherwise unmeasured occupancy overhead'}
    return {'schema_version': d.SCHEMA_VERSION,
        'availability_envelope': {'schema_version': d.availability.SCHEMA_VERSION,
            'generic_request': request, 'scheduled_availability': []},
        'public_duration_records': [public], 'overhead_assumptions': [overhead]}


def run(envelope):
    return d.create_plan(envelope, route_evaluation_budget=100)


def request(envelope):
    return envelope['availability_envelope']['generic_request']


def linked(envelope, duration=25, kind='appointment'):
    row = {'id': 'booking', 'kind': kind, 'attraction_id': 'a', 'location_id': 'gate',
           'duration_minutes': duration, 'arrival_buffer_minutes': 0}
    if kind == 'dpa':
        row.update(entry_start=ts(10), entry_end=ts(40))
    else:
        row['start_at'] = ts(10)
    request(envelope)['commitments'].append(row)


class DurationInputTests(unittest.TestCase):
    def test_fixed_and_approximate_nominal_plus_overhead(self):
        for kind in ('fixed', 'approximate'):
            e = fixture(); e['public_duration_records'][0]['duration_kind'] = kind
            z = run(e)
            self.assertEqual(z['status'], 'duration_scenario_plan_ready')
            self.assertEqual(z['primary_result']['steps'][0]['planning_finish_at'], ts(25))
            self.assertTrue(z['final_validation']['feasible'])
            p = z['duration_input']['duration_provenance'][0]
            self.assertEqual(p['derived_service_minutes'], 25)
            self.assertIsNone(p['measured_total_occupancy_minutes'])
            self.assertIsNone(p['total_occupancy_hard_upper_bound_minutes'])
            self.assertEqual(p['public_duration_record'], e['public_duration_records'][0])

    def test_fractional_decimal_addition(self):
        e = fixture(); e['public_duration_records'][0]['duration_nominal_minutes'] = 1.5
        e['overhead_assumptions'][0]['overhead_minutes'] = 0.1
        self.assertEqual(d.prepare_duration_input(e)['duration_provenance'][0]['derived_service_minutes'], 1.6)

    def test_unknown_durations_never_fall_back_to_original_ten(self):
        for kind in ('range', 'variable', 'unpublished'):
            e = fixture(); row = e['public_duration_records'][0]
            row['duration_kind'] = kind; row['duration_nominal_minutes'] = None
            if kind == 'range':
                row.update(duration_lower_minutes=1.5, duration_upper_minutes=3)
            else:
                row['source_text'] = None; row['source_field_label'] = None
            z = run(e)
            self.assertEqual(z['status'], 'needs_duration_input')
            self.assertIsNone(z['duration_input']['transformed_availability_envelope'])
            self.assertIsNone(z['plan'])
            self.assertIsNone(z['duration_input']['duration_provenance'][0]['derived_service_minutes'])

    def test_missing_duration_record_is_unresolved_even_for_optional(self):
        e = fixture(); e['public_duration_records'] = []; request(e)['must_ride_ids'] = []
        self.assertEqual(run(e)['status'], 'needs_duration_input')

    def test_unknown_and_missing_overhead_are_unresolved(self):
        for missing in (False, True):
            e = fixture()
            if missing:
                e['overhead_assumptions'] = []
            else:
                e['overhead_assumptions'][0]['overhead_minutes'] = None
            self.assertEqual(run(e)['status'], 'needs_duration_input')

    def test_explicit_zero_overhead_is_valid_scenario_not_measurement(self):
        e = fixture(); e['overhead_assumptions'][0]['overhead_minutes'] = 0
        z = run(e)
        self.assertEqual(z['primary_result']['steps'][0]['planning_finish_at'], ts(20))
        self.assertEqual(z['duration_input']['duration_provenance'][0]['overhead_assumption']['overhead_minutes'], 0)
        self.assertIsNone(z['duration_input']['duration_provenance'][0]['measured_total_occupancy_minutes'])

    def test_source_receipt_equal_cutoff_passes_future_fails(self):
        e = fixture(); row = e['public_duration_records'][0]
        row['received_at'] = ts(-1440)
        self.assertEqual(run(e)['status'], 'duration_scenario_plan_ready')
        row['received_at'] = ts(-1439)
        self.assertEqual(run(e)['status'], 'needs_input')

    def test_publication_cannot_follow_receipt(self):
        e = fixture(); e['public_duration_records'][0]['published_at'] = ts(-1449)
        self.assertEqual(run(e)['status'], 'needs_input')

    def test_fixed_meal_and_exit_conflict_is_respected(self):
        e = fixture(); q = request(e); q['exit_deadline'] = ts(30)
        q['commitments'] = [{'id': 'meal', 'kind': 'meal', 'location_id': 'gate',
            'start_at': ts(15), 'duration_minutes': 10, 'arrival_buffer_minutes': 0}]
        baseline = d.availability.create_plan(e['availability_envelope'], route_evaluation_budget=100)
        self.assertEqual(baseline['status'], 'scheduled_plan_ready')
        self.assertEqual(run(e)['status'], 'no_verified_plan')
        replay = d.evaluate_sequence(e, [{'kind': 'ride', 'id': 'a'}, {'kind': 'commitment', 'id': 'meal'}])
        self.assertFalse(replay['feasible'])

    def test_deadline_uses_derived_service_duration(self):
        e = fixture(); request(e)['exit_deadline'] = ts(24)
        self.assertEqual(run(e)['status'], 'no_verified_plan')
        request(e)['exit_deadline'] = ts(25)
        self.assertEqual(run(e)['status'], 'duration_scenario_plan_ready')

    def test_linked_booking_mismatch_shorter_or_longer_rejected(self):
        for duration in (10, 30):
            for kind in ('appointment', 'dpa'):
                e = fixture(); linked(e, duration, kind)
                z = run(e)
                self.assertEqual(z['status'], 'needs_duration_input')
                mismatch = z['duration_input']['unresolved'][0]
                self.assertEqual(mismatch['commitment_id'], 'booking')
                self.assertEqual(mismatch['required_scenario_service_minutes'], 25)
                self.assertEqual(request(e)['commitments'][0]['duration_minutes'], duration)
                self.assertEqual(d.evaluate_sequence(e, [{'kind': 'commitment', 'id': 'booking'}])['status'], 'needs_duration_input')

    def test_linked_booking_exact_match_retains_deadline_constraint(self):
        e = fixture(); linked(e); request(e)['exit_deadline'] = ts(35)
        z = run(e)
        self.assertEqual(z['status'], 'duration_scenario_plan_ready')
        self.assertEqual(z['primary_result']['steps'][0]['planning_finish_at'], ts(35))
        request(e)['exit_deadline'] = ts(34)
        self.assertEqual(run(e)['status'], 'no_verified_plan')

    def test_source_and_caller_are_not_mutated_or_aliased(self):
        e = fixture(); before = copy.deepcopy(e)
        z = run(e)
        self.assertEqual(e, before)
        self.assertEqual(z['plan']['input_envelope'], before)
        z['duration_input']['duration_provenance'][0]['public_duration_record']['source_text'] = 'changed output'
        z['duration_input']['transformed_availability_envelope']['generic_request']['attractions'][0]['ride_minutes'] = 100
        self.assertEqual(e, before)

    def test_only_ride_minutes_changes_in_strict_envelope(self):
        e = fixture(); before = copy.deepcopy(e['availability_envelope'])
        transformed = d.prepare_duration_input(e)['transformed_availability_envelope']
        before['generic_request']['attractions'][0]['ride_minutes'] = 25.0
        self.assertEqual(transformed, before)

    def test_existing_boarding_and_exit_buffers_remain_additive(self):
        e = fixture(); q = request(e); q['policy']['boarding_buffer_minutes'] = 3
        q['policy']['exit_buffer_minutes'] = 5; q['exit_deadline'] = ts(32)
        self.assertEqual(run(e)['status'], 'no_verified_plan')
        q['exit_deadline'] = ts(33)
        z = run(e)
        self.assertEqual(z['status'], 'duration_scenario_plan_ready')
        self.assertEqual(z['primary_result']['steps'][0]['planning_finish_at'], ts(28))
        self.assertEqual(z['duration_input']['policy_buffers']['ordinary_ride_additional_boarding_buffer_minutes'], 3)

    def test_policy_default_metadata_matches_frozen_defaults(self):
        e = fixture(); del request(e)['policy']['boarding_buffer_minutes']; del request(e)['policy']['exit_buffer_minutes']
        p = d.prepare_duration_input(e)['policy_buffers']
        self.assertEqual(p['ordinary_ride_additional_boarding_buffer_minutes'], 3)
        self.assertEqual(p['exit_buffer_minutes'], 15)

    def test_declared_closure_still_blocks_source_duration_ride(self):
        e = fixture(); e['availability_envelope']['scheduled_availability'] = [{
            'attraction_id': 'a', 'issued_at': ts(-1445), 'received_at': ts(-1450),
            'source': 'fictional closure', 'closed_intervals': [{'start_at': ts(), 'end_at': ts(240)}]}]
        self.assertEqual(run(e)['status'], 'no_verified_plan')

    def test_missing_forecast_does_not_become_zero(self):
        e = fixture(); request(e)['forecasts'] = []
        self.assertEqual(run(e)['status'], 'no_verified_plan')

    def test_strict_extra_keys(self):
        for where in ('root', 'source', 'overhead', 'attraction'):
            e = fixture()
            row = {'root': e, 'source': e['public_duration_records'][0],
                'overhead': e['overhead_assumptions'][0], 'attraction': request(e)['attractions'][0]}[where]
            row['unknown_key'] = True
            self.assertEqual(run(e)['status'], 'needs_input', where)

    def test_unknown_and_duplicate_ids(self):
        for field in ('public_duration_records', 'overhead_assumptions'):
            e = fixture(); e[field].append(copy.deepcopy(e[field][0]))
            self.assertEqual(run(e)['status'], 'needs_input')
            e = fixture(); e[field][0]['attraction_id'] = 'not_a'
            self.assertEqual(run(e)['status'], 'needs_input')

    def test_nonfinite_boolean_negative_and_overflow_numbers(self):
        for field, value in [('duration_nominal_minutes', float('nan')),
                             ('duration_nominal_minutes', float('inf')),
                             ('duration_nominal_minutes', True),
                             ('duration_nominal_minutes', -1),
                             ('duration_nominal_minutes', 0),
                             ('duration_nominal_minutes', 2161)]:
            e = fixture(); e['public_duration_records'][0][field] = value
            self.assertEqual(run(e)['status'], 'needs_input')
        for value in (True, -1, float('inf'), 2161):
            e = fixture(); e['overhead_assumptions'][0]['overhead_minutes'] = value
            self.assertEqual(run(e)['status'], 'needs_input')
        e = fixture(); e['public_duration_records'][0]['duration_nominal_minutes'] = 2160
        self.assertEqual(run(e)['status'], 'needs_input')

    def test_inconsistent_duration_kind_values_rejected(self):
        for patch in ({'duration_kind': 'unpublished'}, {'duration_nominal_minutes': None},
                      {'duration_lower_minutes': 19}, {'duration_kind': 'range', 'duration_lower_minutes': 1.5, 'duration_upper_minutes': 3}):
            e = fixture(); e['public_duration_records'][0].update(patch)
            self.assertEqual(run(e)['status'], 'needs_input')

    def test_naive_time_invalid_hash_missing_source_and_bad_url_rejected(self):
        for field, value in [('received_at', '2035-04-09T00:00:00'),
                             ('received_at', '0001-01-01T00:00:00+23:59'),
                             ('received_at', 'not-a-time'), ('source_sha256', 'bad'),
                             ('source_url', 'http://example.invalid/a'),
                             ('source_url', 'https://exa mple.invalid/a'),
                             ('source_url', 'https://user:pass@example.invalid/a'),
                             ('source_title', ''), ('source_text', '')]:
            e = fixture(); e['public_duration_records'][0][field] = value
            self.assertEqual(run(e)['status'], 'needs_input')

    def test_json_duplicate_keys_and_nonfinite_rejected(self):
        self.assertEqual(d.run_json(b'{"a":1,"a":2}')['status'], 'needs_input')
        self.assertEqual(d.run_json(b'{"a":NaN}')['status'], 'needs_input')
        self.assertEqual(d.run_json(b'\xff')['status'], 'needs_input')
        self.assertEqual(d.run_json(json.dumps(fixture()).encode(), route_evaluation_budget=100)['status'], 'duration_scenario_plan_ready')

    def test_direct_replay_same_scenario_and_strict_options(self):
        e = fixture(); z = d.evaluate_sequence(e, [{'kind': 'ride', 'id': 'a'}])
        self.assertTrue(z['feasible'])
        self.assertEqual(z['steps'][0]['planning_finish_at'], ts(25))
        self.assertEqual(d.evaluate_sequence(e, [], complete_remaining=1)['status'], 'needs_input')

    def test_frozen_dependency_hash_unchanged(self):
        self.assertEqual(hashlib.sha256(d.AVAILABILITY.read_bytes()).hexdigest(), d.AVAILABILITY_SHA256)


if __name__ == '__main__':
    unittest.main()
