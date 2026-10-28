"""Pure, closed-key validation in front of unchanged scheduling implementations.

This is not a feasibility checker. Only provided values are checked here; frozen
engines retain their missing-field, reference, timing and ledger semantics.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from generic_io import InputError
from generic_io.adapter import instant, number
from generic_io.recommendations import _bounded_shape

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def verify_source_lock():
    """Refuse silent drift in the reviewed contract and frozen dependencies."""
    lock = json.loads((HERE / 'source_lock.json').read_text())
    checks = [(HERE / 'schema.json', lock['schema_sha256']),
              (HERE / 'CONTRACT_PROPOSAL.txt', lock['protocol_sha256']),
              (HERE / 'CLI_PROTOCOL.txt', lock['cli_protocol_sha256']),
              (HERE / 'RESOURCE_PROTOCOL.txt', lock['resource_protocol_sha256'])]
    checks += [(ROOT / name, expected) for name, expected in lock['source_sha256'].items()]
    for path, expected in checks:
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f'Strict input source/contract lock mismatch: {path.name}')
    return lock


_LOCK = verify_source_lock()
_SCHEMA = json.loads((HERE / 'schema.json').read_text())
CONTRACT_VERSION = _SCHEMA['contract_version']


def _fail(path, message):
    raise InputError(f'{path}: {message}')


def _shape(request):
    # Handles non-string keys, non-JSON types, nonfinite floats, cycles and depth
    # before copying, sorting keys or serializing. This function does not mutate.
    _bounded_shape(request, lambda: None)
    try:
        size = 0
        for chunk in json.JSONEncoder(allow_nan=False, ensure_ascii=False).iterencode(request):
            size += len(chunk.encode('utf-8'))
            if size > _SCHEMA['serialization_max_bytes']:
                _fail('request', 'serialized JSON exceeds the strict 16 MiB resource limit')
    except InputError:
        raise
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError) as exc:
        raise InputError('request: JSON serialization limits exceeded') from exc


def _value(value, spec, path, row):
    kind = spec['type']
    if value is None and spec.get('nullable', False):
        return
    if kind in {'number', 'integer'}:
        if kind == 'integer' and (isinstance(value, bool) or not isinstance(value, int)):
            _fail(path, 'a JSON integer is required')
        try:
            number(value, path, spec['minimum'], spec['maximum'])
        except OverflowError as exc:
            raise InputError(f'{path}: number exceeds supported finite range') from exc
    elif kind in {'string', 'id', 'label', 'enum', 'timestamp'}:
        if not isinstance(value, str):
            _fail(path, 'a string is required')
        if kind == 'id' and not value:
            _fail(path, 'a nonempty string ID is required')
        if kind == 'label' and not value.strip():
            _fail(path, 'a nonempty source/ID string is required')
        if kind == 'enum' and value not in spec['values']:
            _fail(path, f'expected one of {spec["values"]}')
        if kind == 'timestamp':
            instant(value, path)
    elif kind == 'boolean':
        if not isinstance(value, bool):
            _fail(path, 'a JSON boolean is required')
    elif kind == 'paired_source':
        active = row.get(spec['paired_value']) is not None
        if value is None and not active:
            return
        if not isinstance(value, str) or (active and not value.strip()):
            _fail(path, 'an explicit string source is required for a supplied value')
    elif kind == 'ids':
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            _fail(path, 'an array of string IDs is required')
    elif kind == 'empty_array':
        if not isinstance(value, list) or value:
            _fail(path, 'only an empty array is supported; legacy rests use a separate kernel')
    elif kind == 'rows':
        if not isinstance(value, list):
            _fail(path, 'an array of objects is required')
        for index, item in enumerate(value):
            _object(item, spec['model'], f'{path}[{index}]')
    elif kind == 'object':
        _object(value, spec['model'], path)
    elif kind == 'provenance':
        if not isinstance(value, dict):
            _fail(path, 'an extensible JSON object is required')
        for key in _SCHEMA['reserved_provenance_ids']:
            if key in value:
                _value(value[key], {'type': 'ids'}, f'{path}.{key}', value)
    elif kind == 'external_provider':
        if value is not None and not callable(getattr(value, 'forecast', None)):
            _fail(path, 'must implement forecast() or be None')
    else:
        raise RuntimeError(f'Unknown frozen schema type: {kind}')


def _object(row, model, path):
    if not isinstance(row, dict):
        _fail(path, 'an object is required')
    if any(not isinstance(key, str) for key in row):
        _fail(path, 'object keys must be strings')
    specs = _SCHEMA['models'][model]
    unsupported = sorted(set(row) - set(specs))
    if unsupported:
        _fail(path, f'unsupported fields {unsupported}; constraints cannot be silently ignored')
    for name, value in row.items():
        _value(value, specs[name], f'{path}.{name}', row)
    if model == 'commitment' and 'kind' in row:
        # The string/enum check has already run, so unhashable kinds cannot leak
        # TypeError. Inapplicable timing constraints are never silently ignored.
        inapplicable = sorted(set(row) & set(_SCHEMA['commitment_kind_disallowed'][row['kind']]))
        if inapplicable:
            _fail(path, f'{row["kind"]} does not support fields {inapplicable}')


def validate_request_keys(request):
    """Check all supplied request keys/types before state/cutoff filtering.

    Returns None; does not copy or modify caller input. Arbitrary provenance is
    descriptive except the three explicitly reserved, existing bridge ledgers.
    Absence of required fields is checked by the underlying engine on execution.
    """
    _shape(request)
    _object(request, 'request', 'request')


def validate_sequence_keys(sequence):
    """Validate the entire sequence before any locked-action/early-failure exit."""
    if not isinstance(sequence, (list, tuple)):
        _fail('sequence', 'an array or tuple of ride IDs / step objects is required')
    # Preserve the existing Python tuple interface without modifying the input.
    _shape({'sequence': list(sequence)})
    for index, step in enumerate(sequence):
        path = f'sequence[{index}]'
        if isinstance(step, str):
            continue
        if not isinstance(step, dict):
            _fail(path, 'a string ID or step object is required')
        kind = step.get('kind')
        if not isinstance(kind, str):
            _fail(path + '.kind', 'a string is required')
        model = 'sequence_wait' if kind == 'wait_until' else 'sequence_action'
        _object(step, model, path)
        required = {'kind', 'until'} if kind == 'wait_until' else {'kind', 'id'}
        missing = sorted(required - set(step))
        if missing:
            _fail(path, f'missing fields {missing}')


def validate_options(operation, options):
    specs = _SCHEMA['wrapper_options'][operation]
    unsupported = sorted(set(options) - set(specs))
    if unsupported:
        _fail(operation, f'unsupported options {unsupported}')
    for name, value in options.items():
        _value(value, specs[name], f'{operation}.{name}', options)
