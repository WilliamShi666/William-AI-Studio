import sys
import time
import types

import pytest

from sandbox import sandbox as sandbox_module


@pytest.mark.asyncio
async def test_delete_sandbox_attach_timeout_is_bounded(monkeypatch):
    class _SlowSandbox:
        def __init__(self, sandbox_id: str):
            self.sandbox_id = sandbox_id
            time.sleep(0.05)

        def kill(self):
            return None

    monkeypatch.setattr(sandbox_module, "_get_delete_timeout_seconds", lambda: 0.01)
    monkeypatch.setitem(sys.modules, "e2b_desktop", types.SimpleNamespace(Sandbox=_SlowSandbox))
    monkeypatch.setitem(sys.modules, "e2b_code_interpreter", types.SimpleNamespace(Sandbox=_SlowSandbox))

    start = time.perf_counter()
    with pytest.raises(Exception, match="Failed to delete sandbox sb-slow"):
        await sandbox_module.delete_sandbox("sb-slow")
    elapsed = time.perf_counter() - start

    assert elapsed < 0.2
