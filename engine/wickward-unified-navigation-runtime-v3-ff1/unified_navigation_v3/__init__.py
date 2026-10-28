"""Offline, stateless Wickward day-of navigation entry v3."""
from .api import decide_json
from .kernel import InputError

__all__ = ['decide_json', 'InputError']
