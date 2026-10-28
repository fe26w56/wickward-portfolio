"""The only public decision entry. Buffers output before the return deadline gate."""
from __future__ import annotations

import json
from time import perf_counter

from .boundary import parse_envelope, validate_request
from .context import Budget, BudgetStop, fingerprint
from .first_feasible import FirstFeasibleContext as Context
from .engine import Decision, empty_result
from .kernel import InputError

CLOCK = perf_counter
MAX_OUTPUT_BYTES = 16 * 1024 * 1024


def _encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


ENCODE = _encode


def _abstain(request, ledger, budget, *, status='budget_abstention', reason=None):
    out = empty_result(request if ledger is not None else None, ledger)
    out['status'], out['reason'] = status, reason or budget.stop_reason
    out['budget'] = budget.diagnostics()
    if reason is not None:
        out['budget']['reason'] = reason
    return out


def decide_json(payload: bytes) -> bytes:
    """Validate one bounded JSON envelope and return one bounded decision JSON.

    Invalid input raises InputError. Cooperative deadline/resource abstention
    returns JSON without dispatch. The immutable caller input is bytes; no input,
    projection or internal proof is exposed as an execution authorization.
    """
    clock = CLOCK
    started = clock()
    budget = Budget(clock, started)
    request = ledger = None
    try:
        budget.check_total()
        request, options = parse_envelope(payload)
        budget.tighten(options['total_budget_seconds'])
        budget.stage = 'strict_validation_and_ledger'
        ledger = validate_request(request)
        budget.check_total()
        original_hash, ledger_hash = fingerprint(request), fingerprint(ledger)
        budget.state_limit = request.get('policy', {}).get('max_schedule_states', 100000)
        budget.stage = 'context'
        c = Context(request, ledger, budget)
        result = Decision(request, ledger, c, options, budget).run()
        budget.stage_deadline = None
        budget.stage = 'result_binding_and_encoding'
        budget.check_total()
        if fingerprint(request) != original_hash or fingerprint(ledger) != ledger_hash:
            raise InputError('Internal projection mutated the original request or ledger')
        result['original_request_sha256'] = original_hash
        result['original_payload_sha256'] = __import__('hashlib').sha256(payload).hexdigest()
        result['budget'] = budget.diagnostics(bool(result['next_action'] and budget.stop_reason))
        encoded = ENCODE(result)
        budget.check_total()  # Required AFTER encoding and before returning any bytes.
        if len(encoded) > MAX_OUTPUT_BYTES:
            result = _abstain(request, ledger, budget, status='no_verified_action', reason='response_resource_limit')
            result['budget']['response_limit_bytes'] = MAX_OUTPUT_BYTES
            encoded = ENCODE(result)
            budget.check_total()
        return encoded
    except BudgetStop:
        # The cooperative abstention may itself finish late, but can never
        # contain an actionable prefix or an invented validated empty ledger.
        return _encode(_abstain(request, ledger, budget))
    except (OverflowError, RecursionError, UnicodeError) as exc:
        raise InputError('Input exceeds supported numeric, calendar or encoding range') from exc
