from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


LOAD_PROBE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "phase3_regular_load_probe.py"
)


def _load_probe_module():
    spec = importlib.util.spec_from_file_location("phase3_regular_load_probe", LOAD_PROBE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_run_probe_matrix_uses_default_phase3_targets(monkeypatch):
    module = _load_probe_module()
    seen_targets: list[int] = []

    async def _fake_run_probe(*, target_active_runs: int, **_kwargs):
        seen_targets.append(target_active_runs)
        return {"target_active_runs": target_active_runs}

    monkeypatch.setattr(module, "run_probe", _fake_run_probe)

    results = await module.run_probe_matrix()

    assert seen_targets == [25, 50, 100]
    assert [result["target_active_runs"] for result in results] == [25, 50, 100]
