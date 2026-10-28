"""Load the byte-identical reviewed kernels into an isolated private namespace.

The vendor tree contains a few inert files because the unchanged strict source
lock hashes them. Those files are never imported. No source-study path, History
module, canonical catalog, global ``generic_io`` alias, or sys.path edit is used.
"""
from __future__ import annotations

import builtins
import hashlib
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace


_HERE = Path(__file__).resolve().parent
_VENDOR = _HERE / "_vendor"
_PREFIX = __package__ + "._vendor"


def _verify_vendored_sources():
    manifest = json.loads((_HERE / "VENDORED_SOURCES.json").read_text("utf-8"))
    expected = {}
    for entry in manifest["files"]:
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "_vendor":
            raise RuntimeError("Invalid portable kernel manifest path")
        path = _HERE / relative
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise RuntimeError(f"Portable kernel source hash mismatch: {relative}")
        expected[path.resolve()] = entry["sha256"]
    return expected


class _VerifiedSourceLoader(importlib.machinery.SourceFileLoader):
    """Compile only freshly verified source; never read or write bytecode caches."""

    def get_code(self, fullname):
        path = Path(self.path).resolve()
        source = path.read_bytes()
        expected = _SOURCE_HASHES.get(path)
        if expected is None or hashlib.sha256(source).hexdigest() != expected:
            raise RuntimeError(f"Portable kernel source hash mismatch: {path.name}")
        return compile(source, str(path), "exec", dont_inherit=True)


def _package(name, path):
    module = ModuleType(name)
    module.__path__ = [str(path)]
    module.__package__ = name
    sys.modules[name] = module
    return module


def _private_core_spec(name, location, *args, **kwargs):
    # The unchanged adapter gives its frozen core a process-global legacy name.
    # Remap only that module name; its actual bytes and loader are unchanged.
    if name == "_wickward_frozen_revision5_generic_io":
        name = _PREFIX + ".frozen_revision5"
    kwargs["loader"] = _VerifiedSourceLoader(name, str(location))
    return importlib.util.spec_from_file_location(name, location, *args, **kwargs)


_adapter_importlib = SimpleNamespace(util=SimpleNamespace(
    spec_from_file_location=_private_core_spec,
    module_from_spec=importlib.util.module_from_spec,
))


def _kernel_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and (name == "generic_io" or name.startswith("generic_io.")):
        target = _PREFIX + "." + name
        module = sys.modules[target]
        return module if fromlist else sys.modules[_PREFIX + ".generic_io"]
    if (level == 0 and name == "importlib.util" and globals is not None
            and globals.get("__name__") == _PREFIX + ".generic_io.adapter"):
        return _adapter_importlib.util if fromlist else _adapter_importlib
    return builtins.__import__(name, globals, locals, fromlist, level)


def _load(name, relative, *, package=False):
    path = _VENDOR / relative
    spec = importlib.util.spec_from_file_location(
        name, path, loader=_VerifiedSourceLoader(name, str(path)),
        submodule_search_locations=[str(path.parent)] if package else None)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load reviewed kernel: {relative}")
    module = importlib.util.module_from_spec(spec)
    module.__dict__["__builtins__"] = {**vars(builtins), "__import__": _kernel_import}
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


_SOURCE_HASHES = _verify_vendored_sources()
_package(_PREFIX, _VENDOR)
_package(_PREFIX + ".generic_io", _VENDOR / "generic_io")
_adapter = _load(_PREFIX + ".generic_io.adapter", "generic_io/adapter.py")
_load(_PREFIX + ".generic_io", "generic_io/__init__.py", package=True)
_load(_PREFIX + ".generic_io.recommendations", "generic_io/recommendations.py")
_package(_PREFIX + ".strict_input_v1", _VENDOR / "strict_input_v1")
_validation = _load(_PREFIX + ".strict_input_v1.validation", "strict_input_v1/validation.py")

PracticalContext = _adapter.PracticalContext
InputError = _adapter.InputError
SearchBudgetExceeded = _adapter.SearchBudgetExceeded
Step = _adapter.Step
Schedule = _adapter.Schedule
instant = _adapter.instant
mins = _adapter.mins
diff = _adapter.diff
number = _adapter.number
_step_json = _adapter._step_json
validate_request_keys = _validation.validate_request_keys

__all__ = ["PracticalContext", "InputError", "SearchBudgetExceeded", "Step", "Schedule",
           "instant", "mins", "diff", "number", "_step_json", "validate_request_keys"]
