"""Tests for toolkit_adapter.py — Phase 2: dynamic required-params and execution gate."""

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))


def _import_gate_functions():
    """Import the real functions from toolkit_adapter module."""
    import importlib.util

    module_path = (
        BACKEND_ROOT
        / "agentscope_integration"
        / "tools"
        / "toolkit_adapter.py"
    )
    spec = importlib.util.spec_from_file_location(
        "toolkit_adapter_gate_test", module_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["toolkit_adapter_gate_test"] = module
    spec.loader.exec_module(module)
    return module


_MODULE = _import_gate_functions()
_extract_required_params = _MODULE._extract_required_params
_check_missing_params = _MODULE._check_missing_params


# ── Test _extract_required_params ──────────────────────────────────────────


def test_extract_required_params_all_required():
    def f(a, b):
        pass

    assert _extract_required_params(f) == ["a", "b"]


def test_extract_required_params_some_optional():
    def f(a, b, c=1, d="x"):
        pass

    assert _extract_required_params(f) == ["a", "b"]


def test_extract_required_params_all_optional():
    def f(a=1, b=2, c=3):
        pass

    assert _extract_required_params(f) == []


def test_extract_required_params_no_params():
    def f():
        pass

    assert _extract_required_params(f) == []


def test_extract_required_params_skips_self():
    class C:
        def m(self, required_param, optional_param=None):
            pass

    assert _extract_required_params(C.m) == ["required_param"]


def test_extract_required_params_skips_var_args():
    def f(a, *args, b=1, **kwargs):
        pass

    assert _extract_required_params(f) == ["a"]


def test_extract_required_params_builtin_safe():
    result = _extract_required_params(len)
    assert isinstance(result, list)


# ── Test _check_missing_params ─────────────────────────────────────────────


def test_check_missing_params_all_present():
    assert _check_missing_params("test", {"a": 1, "b": "hello"}, ["a", "b"]) == []


def test_check_missing_params_some_missing():
    assert _check_missing_params("test", {"a": 1}, ["a", "b"]) == ["b"]


def test_check_missing_params_empty_string():
    assert _check_missing_params("test", {"command": ""}, ["command"]) == ["command"]


def test_check_missing_params_whitespace_string():
    assert _check_missing_params("test", {"command": "   "}, ["command"]) == [
        "command"
    ]


def test_check_missing_params_none_value():
    assert _check_missing_params("test", {"path": None}, ["path"]) == ["path"]


def test_check_missing_params_empty_list():
    assert _check_missing_params("test", {"items": []}, ["items"]) == ["items"]


def test_check_missing_params_empty_dict():
    assert _check_missing_params("test", {"config": {}}, ["config"]) == ["config"]


def test_check_missing_params_zero_is_valid():
    assert _check_missing_params("test", {"count": 0}, ["count"]) == []


def test_check_missing_params_false_is_valid():
    assert _check_missing_params("test", {"flag": False}, ["flag"]) == []


def test_check_missing_params_non_empty_list():
    assert _check_missing_params("test", {"items": [1, 2]}, ["items"]) == []


def test_check_missing_params_no_required():
    assert _check_missing_params("test", {}, []) == []
