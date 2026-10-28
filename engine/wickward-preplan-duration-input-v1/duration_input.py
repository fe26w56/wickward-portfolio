"""Source-provenanced duration scenarios over the source-locked availability v2.

Pure supplied-input adapter. Public experience durations are not measurements
or upper bounds for total occupancy; missing values never become zero.
"""
from __future__ import annotations

import copy
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from types import ModuleType
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
AVAILABILITY = HERE.parent / 'wickward-preplan-availability-v2' / 'preplan_availability.py'
AVAILABILITY_SHA256 = '90375dff1c69434f50464b521d444b6d7fb23039d8dbb1ec32b1e11c6720597a'
SCHEMA_VERSION = 'wickward.preplan-duration-input.v1'
OUTPUT_VERSION = 'wickward.duration-input-result.v1'
RECORD_FIELDS = {
    'attraction_id', 'duration_kind', 'duration_nominal_minutes',
    'duration_lower_minutes', 'duration_upper_minutes', 'source_url',
    'source_title', 'source_field_label', 'source_text', 'received_at',
    'published_at', 'source_sha256', 'source_artifact',
}
OVERHEAD_FIELDS = {'attraction_id', 'overhead_minutes', 'scenario_id', 'assumption'}
KINDS = {'fixed', 'approximate', 'range', 'variable', 'unpublished'}
UNKNOWN_COMPONENTS = ['boarding', 'alighting', 'preshow', 'exit']
CONDITIONS = [
    'public_experience_duration_is_not_measured_total_occupancy_or_hard_upper_bound',
    'occupancy_overhead_is_explicit_scenario_assumption_not_measurement',
]


def _load_availability():
    raw = AVAILABILITY.read_bytes()
    if hashlib.sha256(raw).hexdigest() != AVAILABILITY_SHA256:
        raise RuntimeError('Frozen availability v2 source hash mismatch')
    name = '_duration_input_frozen_availability_v2'
    module = ModuleType(name)
    module.__file__ = str(AVAILABILITY)
    sys.modules[name] = module
    exec(compile(raw, str(AVAILABILITY), 'exec'), module.__dict__)
    return module


availability = _load_availability()
InputError = availability.InputError


def _keys(row, fields, path):
    availability._keys(row, fields, fields, path)


def _text(value, path):
    if not isinstance(value, str) or not value.strip():
        raise InputError(f'{path}: nonempty string required')


def _minutes(value, path, *, minimum=0.01):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f'{path}: finite JSON number required')
    try:
        valid = math.isfinite(value) and minimum <= value <= 2160
    except OverflowError:
        valid = False
    if not valid:
        raise InputError(f'{path}: finite number between {minimum} and 2160 required')


def _instant(value, path):
    try:
        return availability.instant(value, path)
    except (OverflowError, ValueError) as exc:
        raise InputError(f'{path}: timestamp exceeds supported UTC range') from exc


def _indexed(rows, fields, ids, path):
    if not isinstance(rows, list) or len(rows) > 40:
        raise InputError(f'{path}: array of at most 40 records required')
    result = {}
    for i, row in enumerate(rows):
        at = f'{path}[{i}]'
        _keys(row, fields, at)
        aid = row['attraction_id']
        if not isinstance(aid, str) or aid not in ids or aid in result:
            raise InputError(f'{at}.attraction_id: known, unique attraction required')
        result[aid] = row
    return result


def _validate_record(row, asof):
    at = 'public_duration_records.' + row['attraction_id']
    kind = row['duration_kind']
    if not isinstance(kind, str) or kind not in KINDS:
        raise InputError(f'{at}.duration_kind: unsupported duration kind')
    for field in ('source_url', 'source_title', 'source_sha256', 'source_artifact'):
        _text(row[field], at + '.' + field)
    try:
        url = urlsplit(row['source_url'])
        valid_url = (url.scheme == 'https' and bool(url.hostname) and url.username is None
                     and url.password is None and not any(c.isspace() for c in row['source_url']))
    except ValueError:
        valid_url = False
    if not valid_url:
        raise InputError(f'{at}.source_url: absolute HTTPS source URL without credentials required')
    if not re.fullmatch(r'[0-9a-f]{64}', row['source_sha256']):
        raise InputError(f'{at}.source_sha256: lowercase SHA-256 required')
    for field in ('source_field_label', 'source_text'):
        if kind in {'fixed', 'approximate', 'range'}:
            _text(row[field], at + '.' + field)
        elif row[field] is not None and not isinstance(row[field], str):
            raise InputError(f'{at}.{field}: string or null required')
    received = _instant(row['received_at'], at + '.received_at')
    published = (_instant(row['published_at'], at + '.published_at')
                 if row['published_at'] is not None else None)
    if received > asof or (published is not None and published > received):
        raise InputError(f'{at}: require actual received_at <= information_as_of and known published_at <= received_at; no historical backfill')
    nominal, lower, upper = (row[key] for key in (
        'duration_nominal_minutes', 'duration_lower_minutes', 'duration_upper_minutes'))
    for field in ('duration_nominal_minutes', 'duration_lower_minutes', 'duration_upper_minutes'):
        if row[field] is not None:
            _minutes(row[field], at + '.' + field)
    if kind in {'fixed', 'approximate'}:
        if nominal is None or lower is not None or upper is not None:
            raise InputError(f'{at}: fixed/approximate requires nominal and null bounds; nominal is not a hard bound')
    elif kind == 'range':
        if nominal is not None or lower is None or upper is None or lower >= upper:
            raise InputError(f'{at}: range requires null nominal and 0 < lower < upper; no invented midpoint')
    elif any(value is not None for value in (nominal, lower, upper)):
        raise InputError(f'{at}: variable/unpublished requires null duration values')


def prepare_duration_input(envelope):
    """Validate and return a copied, transformed v2 envelope, or unresolved input.

    Invalid input raises InputError. A valid but incomplete scenario returns
    status=unresolved_duration_input and no transformed envelope. All attraction
    rows require a resolvable public nominal and explicit aggregate overhead.
    """
    availability.kernel._validation._shape(envelope)
    _keys(envelope, {'schema_version', 'availability_envelope',
                     'public_duration_records', 'overhead_assumptions'}, 'envelope')
    if envelope['schema_version'] != SCHEMA_VERSION:
        raise InputError(f'schema_version must be {SCHEMA_VERSION}')
    original = envelope['availability_envelope']
    request, _, _ = availability.validate_envelope(original)
    ids = {row['id'] for row in request.get('attractions', [])}
    sources = _indexed(envelope['public_duration_records'], RECORD_FIELDS, ids, 'public_duration_records')
    overhead = _indexed(envelope['overhead_assumptions'], OVERHEAD_FIELDS, ids, 'overhead_assumptions')
    asof = availability.instant(request['information_as_of'], 'information_as_of')
    for row in sources.values():
        _validate_record(row, asof)
    for aid, row in overhead.items():
        _text(row['scenario_id'], f'overhead_assumptions.{aid}.scenario_id')
        _text(row['assumption'], f'overhead_assumptions.{aid}.assumption')
        if row['overhead_minutes'] is not None:
            _minutes(row['overhead_minutes'], f'overhead_assumptions.{aid}.overhead_minutes', minimum=0)

    transformed = copy.deepcopy(original)
    provenance, unresolved, totals = [], [], {}
    for attraction in transformed['generic_request'].get('attractions', []):
        aid = attraction['id']
        source, assumption = sources.get(aid), overhead.get(aid)
        reasons = []
        if source is None:
            reasons.append('missing_public_duration_record')
        elif source['duration_kind'] not in {'fixed', 'approximate'}:
            reasons.append('public_duration_' + source['duration_kind'] + '_has_no_resolvable_nominal')
        if assumption is None or assumption['overhead_minutes'] is None:
            reasons.append('unknown_occupancy_overhead_requires_explicit_scenario_assumption')
        derived = None
        if not reasons:
            derived = float(Decimal(str(source['duration_nominal_minutes'])) +
                            Decimal(str(assumption['overhead_minutes'])))
            _minutes(derived, f'derived_service_minutes.{aid}')
            attraction['ride_minutes'] = derived
            totals[aid] = derived
        else:
            unresolved.append({'attraction_id': aid, 'reasons': reasons})
        provenance.append({
            'attraction_id': aid,
            'public_duration_record': copy.deepcopy(source),
            'overhead_assumption': copy.deepcopy(assumption),
            'original_ride_minutes': next(row['ride_minutes'] for row in request['attractions'] if row['id'] == aid),
            'derived_service_minutes': derived,
            'unmeasured_occupancy_components': list(UNKNOWN_COMPONENTS),
            'measured_total_occupancy_minutes': None,
            'total_occupancy_hard_upper_bound_minutes': None,
            'scenario_only': True,
        })

    # Anchors bypass attraction.ride_minutes in the frozen planner. Refuse any
    # mismatch instead of silently retaining an underlong linked booking or
    # rewriting a caller's commitment. Completed anchors also remain strict.
    for row in request.get('commitments', []):
        aid = row.get('attraction_id')
        if aid in totals and row['duration_minutes'] != totals[aid]:
            unresolved.append({'attraction_id': aid, 'commitment_id': row['id'],
                'reasons': ['linked_booking_duration_mismatch_requires_explicit_reconciliation'],
                'supplied_booking_duration_minutes': row['duration_minutes'],
                'required_scenario_service_minutes': totals[aid]})
    if not unresolved:
        availability.validate_envelope(transformed)
    policy = request.get('policy', {})
    return {
        'schema_version': OUTPUT_VERSION,
        'status': 'unresolved_duration_input' if unresolved else 'duration_inputs_ready',
        'input_envelope': copy.deepcopy(envelope),
        'input_envelope_sha256': availability.producer.request_sha256(envelope),
        'transformed_availability_envelope': None if unresolved else transformed,
        'duration_provenance': provenance,
        'unresolved': unresolved,
        'conditions': list(CONDITIONS),
        'policy_buffers': {
            'ordinary_ride_additional_boarding_buffer_minutes': policy.get('boarding_buffer_minutes', 3),
            'boarding_buffer_applies_to_linked_bookings': False,
            'exit_buffer_minutes': policy.get('exit_buffer_minutes', 15),
            'policy_unchanged': True,
        },
        'contract': {
            'frozen_availability_source_sha256': AVAILABILITY_SHA256,
            'source_artifact_hash_verified_by_adapter': False,
            'actual_source_receipt_cutoff_checked': True,
            'operating_status_evidence_created': False,
            'historical_backfill_performed': False,
            'empirical_quality_gate_passed': False,
            'linked_booking_policy': 'require_exact_derived_service_duration_or_unresolved',
            'original_ride_minutes_used_as_fallback': False,
            'all_requested_attractions_require_resolved_duration': True,
        },
    }


def _unresolved(prepared):
    return {'status': 'needs_duration_input', 'plan': None, 'feasible': False,
            'reason': 'Public duration or overhead or linked booking duration remains unresolved',
            'duration_input': prepared}


def create_plan(envelope, *, route_evaluation_budget=5000, max_intentional_wait_minutes=60):
    """Plan under supplied duration assumptions; never verify real occupancy."""
    try:
        prepared = prepare_duration_input(envelope)
        if prepared['unresolved']:
            return _unresolved(prepared)
        result = availability.create_plan(prepared['transformed_availability_envelope'],
            route_evaluation_budget=route_evaluation_budget,
            max_intentional_wait_minutes=max_intentional_wait_minutes)
        result['duration_input'] = prepared
        for key in ('primary_result', 'final_validation'):
            if key in result:
                result[key]['conditions'] = sorted(set(result[key].get('conditions', [])) | set(CONDITIONS))
        if result.get('plan') is not None:
            scheduled_plan = result['plan']
            result['status'] = 'duration_scenario_plan_ready'
            result['plan'] = {
                'schema_version': 'wickward.duration-preplan.v1',
                'input_envelope': copy.deepcopy(envelope),
                'input_envelope_sha256': prepared['input_envelope_sha256'],
                'scheduled_plan': scheduled_plan,
                'duration_provenance': copy.deepcopy(prepared['duration_provenance']),
                'conditions': list(CONDITIONS),
                'requires_duration_adapter_for_revalidation': True,
                'not_accepted_by_navigation_handoff_v2': True,
            }
        return result
    except InputError as exc:
        return {'status': 'needs_input', 'plan': None, 'reason': str(exc)}


def evaluate_sequence(envelope, sequence, *, complete_remaining=False):
    """Replay through exactly the same duration transformation and constraints."""
    try:
        if not isinstance(complete_remaining, bool):
            raise InputError('complete_remaining: boolean required')
        prepared = prepare_duration_input(envelope)
        if prepared['unresolved']:
            return _unresolved(prepared)
        result = availability.evaluate_sequence(prepared['transformed_availability_envelope'],
            sequence, complete_remaining=complete_remaining)
        result['duration_input'] = prepared
        result['conditions'] = sorted(set(result.get('conditions', [])) | set(CONDITIONS))
        return result
    except InputError as exc:
        return {'status': 'needs_input', 'feasible': False, 'reason': str(exc)}


def run_json(payload, **options):
    try:
        return create_plan(availability.producer.parse_request_json(payload), **options)
    except InputError as exc:
        return {'status': 'needs_input', 'plan': None, 'reason': str(exc)}


if __name__ == '__main__':
    json.dump(run_json(sys.stdin.buffer.read(availability.boundary.MAX_INPUT_BYTES + 1)),
              sys.stdout, ensure_ascii=False, allow_nan=False, indent=2)
    print()
