import json

import pytest

from agentscope_integration.shadow_clone import state_machine


class _FakeRedis:
    def __init__(self):
        self.store = {}
        self.ttl = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return False
        self.store[key] = value
        if ex is not None:
            self.ttl[key] = ex
        return True

    async def get(self, key, default=None):
        return self.store.get(key, default)

    async def delete(self, key):
        self.store.pop(key, None)
        self.ttl.pop(key, None)
        return 1

    async def expire(self, key, ttl):
        if key not in self.store:
            return 0
        self.ttl[key] = ttl
        return 1

    async def eval_script(self, script, keys, args):
        key = keys[0]
        raw = self.store.get(key)

        if "local incoming_raw = ARGV[1]" in script:
            incoming = json.loads(args[0])
            ttl_value = int(args[1])
            now_iso = args[2]
            incoming["updated_at"] = now_iso

            if raw is not None:
                current = json.loads(raw)
                current_status = str(current.get("status") or "")
                incoming_status = str(incoming.get("status") or "")
                current_epoch = int(current.get("execution_epoch") or 0)
                incoming_epoch = int(incoming.get("execution_epoch") or 0)
                terminal_statuses = {
                    "completed",
                    "failed",
                    "cancelled",
                    "timeout",
                    "denied",
                }

                if current_status in terminal_statuses:
                    if incoming_status not in terminal_statuses:
                        return raw
                    if incoming_epoch < current_epoch:
                        return raw
                    if (
                        incoming_epoch == current_epoch
                        and incoming_status != current_status
                    ):
                        return raw

            encoded = json.dumps(incoming, ensure_ascii=False)
            self.store[key] = encoded
            self.ttl[key] = ttl_value
            return encoded

        if raw is None:
            if "shadow_clone_stage_proposal_state" in script:
                return ""
            if "shadow_clone_update_environment" in script:
                return ""
            if "shadow_clone_update_live_activity" in script:
                return ""
            return 0
        state = json.loads(raw)

        if "shadow_clone_stage_proposal_state" in script:
            total, subtasks_json, dependencies_json, ttl_value, now_iso = args
            state["proposal"] = {
                "subtasks": json.loads(subtasks_json),
                "dependencies": json.loads(dependencies_json),
            }
            state["total"] = int(total)
            state["updated_at"] = now_iso
            self.store[key] = json.dumps(state, ensure_ascii=False)
            self.ttl[key] = int(ttl_value)
            return self.store[key]

        if "shadow_clone_update_environment" in script:
            (
                status_mode,
                status_value,
                ready_mode,
                ready_value,
                last_error_mode,
                last_error_value,
                prepared_at_mode,
                prepared_at_value,
                manifest_mode,
                manifest_json,
                ttl_value,
                now_iso,
            ) = args
            environment = state.setdefault("environment", {})
            merged_manifest = environment.get("manifest") or {}
            if manifest_mode == "SET":
                merged_manifest = {
                    **merged_manifest,
                    **json.loads(manifest_json),
                }
            if status_mode == "SET":
                environment["status"] = status_value
            if ready_mode == "SET":
                environment["ready"] = ready_value == "1"
            if last_error_mode == "SET":
                environment["last_error"] = last_error_value or None
            if prepared_at_mode == "SET":
                environment["prepared_at"] = prepared_at_value or None
            environment["manifest"] = merged_manifest
            state["environment"] = environment
            state["updated_at"] = now_iso
            self.store[key] = json.dumps(state, ensure_ascii=False)
            self.ttl[key] = int(ttl_value)
            return self.store[key]

        if "shadow_clone_update_live_activity" in script:
            scope, phase, reason, subtask_id, epoch_mode, epoch_value, ttl_value, now_iso = args
            resolved_epoch = (
                int(epoch_value)
                if epoch_mode == "SET"
                else int(state.get("execution_epoch", 0))
            )
            state["live_activity"] = {
                "scope": scope,
                "phase": phase,
                "reason": reason or None,
                "subtask_id": subtask_id or None,
                "epoch": resolved_epoch,
                "updated_at": now_iso,
            }
            state["updated_at"] = now_iso
            self.store[key] = json.dumps(state, ensure_ascii=False)
            self.ttl[key] = int(ttl_value)
            return self.store[key]

        if "expected" in script and len(args) == 4:
            from_status, to_status, _, now_iso = args
            ttl = int(args[2])
            if state.get("status") != from_status:
                return 0
            state["status"] = to_status
            state["updated_at"] = now_iso
            self.store[key] = json.dumps(state, ensure_ascii=False)
            self.ttl[key] = ttl
            return 1

        if 'sub["owner_token"] = owner_token' in script:
            subtask_id, role, _, now_iso, expected_epoch, owner_token = args
            ttl = int(args[2])
            if int(state.get("execution_epoch", 0)) != int(expected_epoch):
                return -1
            subagents = state.setdefault("subagents", {})
            item = subagents.setdefault(subtask_id, {})
            current_owner = item.get("owner_token")
            if item.get("status") == "running" and current_owner not in (None, owner_token):
                return -2
            if item.get("status") not in (None, "", "pending", "running"):
                return -2
            item["status"] = "running"
            item["owner_token"] = owner_token
            if role:
                item["role"] = role
            item.setdefault("started_at", now_iso)
            state["completed"] = sum(
                1 for value in subagents.values() if value.get("status") == "completed"
            )
            state["failed"] = sum(
                1 for value in subagents.values() if value.get("status") == "failed"
            )
            state["running"] = sum(
                1 for value in subagents.values() if value.get("status") == "running"
            )
            state["total"] = max(int(state.get("total", 0)), len(subagents))
            state["updated_at"] = now_iso
            self.store[key] = json.dumps(state, ensure_ascii=False)
            self.ttl[key] = ttl
            return 1

        if 'current_owner == "" or current_owner ~= owner_token' in script:
            subtask_id, status, role, result_summary, _, now_iso, expected_epoch, owner_token = args
            ttl = int(args[4])
            if int(state.get("execution_epoch", 0)) != int(expected_epoch):
                return -1
            subagents = state.setdefault("subagents", {})
            item = subagents.get(subtask_id)
            if not item or item.get("owner_token") != owner_token:
                return -2
            item["status"] = status
            item["owner_token"] = None
            if role:
                item["role"] = role
            if result_summary:
                item["result_summary"] = result_summary
            item["finished_at"] = now_iso
            state["completed"] = sum(
                1 for value in subagents.values() if value.get("status") == "completed"
            )
            state["failed"] = sum(
                1 for value in subagents.values() if value.get("status") == "failed"
            )
            state["running"] = sum(
                1 for value in subagents.values() if value.get("status") == "running"
            )
            state["total"] = max(int(state.get("total", 0)), len(subagents))
            state["updated_at"] = now_iso
            self.store[key] = json.dumps(state, ensure_ascii=False)
            self.ttl[key] = ttl
            return 1

        subtask_id, status, role, result_summary, _, now_iso = args[:6]
        ttl = int(args[4])
        subagents = state.setdefault("subagents", {})
        item = subagents.setdefault(subtask_id, {})
        item["status"] = status
        if role:
            item["role"] = role
        if result_summary:
            item["result_summary"] = result_summary
        if status in {"pending", "completed", "failed"}:
            item["owner_token"] = None
        state["completed"] = sum(
            1 for value in subagents.values() if value.get("status") == "completed"
        )
        state["failed"] = sum(
            1 for value in subagents.values() if value.get("status") == "failed"
        )
        state["running"] = sum(
            1 for value in subagents.values() if value.get("status") == "running"
        )
        state["total"] = max(int(state.get("total", 0)), len(subagents))
        state["updated_at"] = now_iso
        self.store[key] = json.dumps(state, ensure_ascii=False)
        self.ttl[key] = ttl
        return 1


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(state_machine.redis_service, "set", fake.set)
    monkeypatch.setattr(state_machine.redis_service, "get", fake.get)
    monkeypatch.setattr(state_machine.redis_service, "delete", fake.delete)
    monkeypatch.setattr(state_machine.redis_service, "expire", fake.expire)
    monkeypatch.setattr(state_machine.redis_service, "eval_script", fake.eval_script)
    return fake


@pytest.mark.asyncio
async def test_init_and_get_state(fake_redis):
    await state_machine.init_state(
        "run-1",
        "auto",
        total=3,
        subtasks=[
            {
                "id": "task-1",
                "role": "researcher",
                "task_description": "Inspect the environment",
            }
        ],
        dependencies=[{"from_id": "task-1", "to_id": "task-2"}],
    )
    payload = await state_machine.get_state("run-1")
    assert payload["status"] == "pending"
    assert payload["mode"] == "auto"
    assert payload["total"] == 3
    assert payload["environment"]["status"] == "pending"
    assert payload["environment"]["ready"] is False
    assert payload["completion_mode"] is None
    assert payload["terminal_reason"] is None
    assert payload["proposal"]["subtasks"][0]["task_description"] == "Inspect the environment"
    assert payload["proposal"]["dependencies"][0]["to_id"] == "task-2"
    assert fake_redis.ttl[state_machine._state_key("run-1")] == state_machine.STATE_TTL_SECONDS


@pytest.mark.asyncio
async def test_transition_valid_and_invalid(fake_redis):
    await state_machine.init_state("run-2", "auto")
    assert await state_machine.transition("run-2", "pending", "confirming") is True
    assert await state_machine.transition("run-2", "pending", "running") is False
    assert await state_machine.transition("run-2", "confirming", "running") is True
    assert await state_machine.transition("run-2", "running", "completed") is True


@pytest.mark.asyncio
async def test_update_subagent_rolls_up_counts(fake_redis):
    await state_machine.init_state("run-3", "on", total=2)
    await state_machine.update_subagent("run-3", "task_a", "running", role="researcher")
    await state_machine.update_subagent("run-3", "task_a", "completed")
    await state_machine.update_subagent("run-3", "task_b", "failed", result_summary="err")
    payload = await state_machine.get_state("run-3")
    assert payload["completed"] == 1
    assert payload["failed"] == 1
    assert payload["running"] == 0
    assert payload["subagents"]["task_a"]["role"] == "researcher"
    assert payload["subagents"]["task_b"]["result_summary"] == "err"


@pytest.mark.asyncio
async def test_cleanup_retains_latest_state_snapshot_until_ttl_expiry(fake_redis):
    await state_machine.init_state("run-4", "off")
    key = state_machine._state_key("run-4")

    await state_machine.cleanup("run-4")

    payload = await state_machine.get_state("run-4")
    assert payload is not None
    assert payload["status"] == "pending"
    assert fake_redis.ttl[key] == state_machine.STATE_TTL_SECONDS


@pytest.mark.asyncio
async def test_update_environment_persists_manifest(fake_redis):
    await state_machine.init_state("run-5", "auto")
    await state_machine.update_environment(
        "run-5",
        status="ready",
        ready=True,
        prepared_at="2026-03-20T00:00:00+00:00",
        manifest={"sandbox": {"id": "sbx-1"}},
    )
    payload = await state_machine.get_state("run-5")
    assert payload["environment"]["status"] == "ready"
    assert payload["environment"]["ready"] is True
    assert payload["environment"]["prepared_at"] == "2026-03-20T00:00:00+00:00"
    assert payload["environment"]["manifest"]["sandbox"]["id"] == "sbx-1"


@pytest.mark.asyncio
async def test_stage_proposal_state_survives_environment_and_live_activity_updates(fake_redis):
    await state_machine.init_state("run-proposal", "auto")

    await state_machine.stage_proposal_state(
        "run-proposal",
        total=2,
        subtasks=[
            {"id": "task-1", "role": "researcher", "task_description": "Inspect"},
            {"id": "task-2", "role": "designer", "task_description": "Draft"},
        ],
        dependencies=[{"from_id": "task-1", "to_id": "task-2"}],
    )
    await state_machine.update_environment(
        "run-proposal",
        status="preparing",
        ready=False,
        manifest={"bootstrap": {"status": "pending"}},
    )
    await state_machine.update_live_activity(
        "run-proposal",
        scope="shadow_clone_main",
        phase="confirming",
        reason="proposal_created",
    )

    payload = await state_machine.get_state("run-proposal")
    assert payload["total"] == 2
    assert payload["proposal"]["subtasks"][0]["id"] == "task-1"
    assert payload["proposal"]["dependencies"][0]["to_id"] == "task-2"
    assert payload["environment"]["status"] == "preparing"
    assert payload["environment"]["manifest"]["bootstrap"]["status"] == "pending"
    assert payload["live_activity"]["phase"] == "confirming"


@pytest.mark.asyncio
async def test_update_terminal_metadata_records_completion_mode_and_reason(fake_redis):
    await state_machine.init_state("run-terminal-meta", "auto")
    assert await state_machine.transition("run-terminal-meta", "pending", "completed") is True

    payload = await state_machine.update_terminal_metadata(
        "run-terminal-meta",
        completion_mode="aggregate",
        terminal_reason="aggregate_complete",
    )

    assert payload["status"] == "completed"
    assert payload["completion_mode"] == "aggregate"
    assert payload["terminal_reason"] == "aggregate_complete"


@pytest.mark.asyncio
async def test_force_terminal_state_clears_completion_mode_and_sets_terminal_reason(
    fake_redis,
):
    await state_machine.init_state("run-force-terminal", "auto")
    assert await state_machine.transition("run-force-terminal", "pending", "completed") is True
    await state_machine.update_terminal_metadata(
        "run-force-terminal",
        completion_mode="aggregate",
        terminal_reason="aggregate_complete",
    )

    payload = await state_machine.force_terminal_state(
        "run-force-terminal",
        status="failed",
        reason="external_stop_failed",
        error_message="boom",
    )

    assert payload["status"] == "failed"
    assert payload["completion_mode"] is None
    assert payload["terminal_reason"] == "external_stop_failed"
    assert payload["environment"]["last_error"] == "boom"


@pytest.mark.asyncio
async def test_force_terminal_state_from_aggregating_external_stop_preserves_completed_subagents(
    fake_redis,
):
    await state_machine.init_state(
        "run-force-stop-aggregating",
        "auto",
        total=2,
        subtasks=[
            {"id": "task-1", "role": "researcher"},
            {"id": "task-2", "role": "analyst"},
        ],
        dependencies=[],
    )
    assert (
        await state_machine.transition(
            "run-force-stop-aggregating",
            "pending",
            "confirming",
        )
        is True
    )
    assert (
        await state_machine.transition(
            "run-force-stop-aggregating",
            "confirming",
            "running",
        )
        is True
    )
    assert (
        await state_machine.transition(
            "run-force-stop-aggregating",
            "running",
            "aggregating",
        )
        is True
    )
    await state_machine.update_subagent(
        "run-force-stop-aggregating",
        "task-1",
        "completed",
        role="researcher",
    )
    await state_machine.update_subagent(
        "run-force-stop-aggregating",
        "task-2",
        "completed",
        role="analyst",
    )

    payload = await state_machine.force_terminal_state(
        "run-force-stop-aggregating",
        status="cancelled",
        reason="external_stop",
    )

    assert payload["status"] == "cancelled"
    assert payload["completion_mode"] is None
    assert payload["terminal_reason"] == "external_stop"
    assert payload["environment"]["status"] == "cancelled"
    assert payload["environment"]["ready"] is False
    assert payload["live_activity"]["phase"] == "cancelled"
    assert payload["live_activity"]["reason"] == "external_stop"
    assert payload["completed"] == 2
    assert payload["failed"] == 0
    assert payload["running"] == 0
    assert payload["subagents"]["task-1"]["status"] == "completed"
    assert payload["subagents"]["task-2"]["status"] == "completed"


@pytest.mark.asyncio
async def test_claim_and_finalize_subagent_for_epoch(fake_redis):
    await state_machine.init_state("run-6", "auto")

    claimed = await state_machine.claim_subagent_for_epoch(
        "run-6",
        "task-1",
        expected_epoch=0,
        owner_token="owner-1",
        role="researcher",
    )
    assert claimed is True

    duplicate_claim = await state_machine.claim_subagent_for_epoch(
        "run-6",
        "task-1",
        expected_epoch=0,
        owner_token="owner-2",
        role="researcher",
    )
    assert duplicate_claim is False

    finalized = await state_machine.finalize_subagent_for_epoch(
        "run-6",
        "task-1",
        "completed",
        expected_epoch=0,
        owner_token="owner-1",
        result_summary="done",
        role="researcher",
    )
    assert finalized is True

    payload = await state_machine.get_state("run-6")
    assert payload["subagents"]["task-1"]["status"] == "completed"
    assert payload["subagents"]["task-1"]["result_summary"] == "done"
    assert payload["subagents"]["task-1"].get("owner_token") is None


@pytest.mark.asyncio
async def test_finalize_subagent_rejects_owner_mismatch(fake_redis):
    await state_machine.init_state("run-7", "auto")
    await state_machine.claim_subagent_for_epoch(
        "run-7",
        "task-1",
        expected_epoch=0,
        owner_token="owner-1",
    )

    finalized = await state_machine.finalize_subagent_for_epoch(
        "run-7",
        "task-1",
        "completed",
        expected_epoch=0,
        owner_token="owner-2",
        result_summary="wrong owner",
    )
    assert finalized is False

    payload = await state_machine.get_state("run-7")
    assert payload["subagents"]["task-1"]["status"] == "running"


@pytest.mark.asyncio
async def test_initialize_subagent_attempts_sets_defaults(fake_redis):
    await state_machine.init_state("run-8", "auto")
    await state_machine.update_subagent("run-8", "task-1", "pending", role="researcher")

    await state_machine.initialize_subagent_attempts(
        "run-8",
        subtask_ids=["task-1"],
    )

    payload = await state_machine.get_state("run-8")
    assert payload["subagents"]["task-1"]["attempt_index"] == 1
    assert payload["subagents"]["task-1"]["attempt_history"] == []
    assert payload["subagents"]["task-1"]["failure_class"] is None
    assert payload["subagents"]["task-1"]["last_error"] is None
    assert payload["subagents"]["task-1"]["recovery"] == {
        "mode": None,
        "phase": None,
        "reason": None,
        "wake_attempts": 0,
        "replacement_attempts": 0,
        "replacement_context_id": None,
        "handoff_summary": None,
        "last_command_id": None,
        "updated_at": None,
    }


@pytest.mark.asyncio
async def test_record_recovery_decision_persists_public_fields(fake_redis):
    await state_machine.init_state("run-9", "auto")
    await state_machine.request_recovery(
        "run-9",
        kind="layer_review",
        error_code="ERR",
        message="review needed",
        source="test",
        scope="layer:0",
        failed_subtasks=[{"subtask_id": "task-1"}],
        retryable_subtasks=["task-1"],
    )

    await state_machine.record_recovery_decision(
        "run-9",
        decision="retry_failed_subtasks",
        decision_source="shadow_clone_failure_review_agent",
        scope="layer:0",
        failed_subtasks=[{"subtask_id": "task-1"}],
        retryable_subtasks=["task-1"],
    )

    payload = await state_machine.get_state("run-9")
    assert payload["recovery"]["pending"] is False
    assert payload["recovery"]["decision"] == "retry_failed_subtasks"
    assert payload["recovery"]["decision_source"] == "shadow_clone_failure_review_agent"
    assert payload["recovery"]["scope"] == "layer:0"
    assert payload["recovery"]["failed_subtasks"] == [{"subtask_id": "task-1"}]
    assert payload["recovery"]["retryable_subtasks"] == ["task-1"]
    assert payload["recovery"]["decision_at"] is not None


@pytest.mark.asyncio
async def test_prepare_subagents_for_retry_increments_attempt_index(fake_redis):
    await state_machine.init_state("run-10", "auto")
    await state_machine.update_subagent("run-10", "task-1", "failed", role="researcher")
    await state_machine.initialize_subagent_attempts(
        "run-10",
        subtask_ids=["task-1"],
    )
    await state_machine.record_subagent_failures(
        "run-10",
        failed_subtasks=[
            {
                "subtask_id": "task-1",
                "error": "SubAgent timed out after 300 seconds.",
                "failure_class": "timeout",
            }
        ],
    )

    await state_machine.prepare_subagents_for_retry(
        "run-10",
        ["task-1"],
        expected_epoch=0,
        roles={"task-1": "researcher"},
    )

    payload = await state_machine.get_state("run-10")
    assert payload["subagents"]["task-1"]["status"] == "pending"
    assert payload["subagents"]["task-1"]["attempt_index"] == 2
    assert payload["subagents"]["task-1"]["failure_class"] is None
    assert payload["subagents"]["task-1"]["last_error"] is None
    assert payload["subagents"]["task-1"]["attempt_history"] == [
        {
            "attempt_index": 1,
            "status": "failed",
            "result_summary": None,
            "error": "SubAgent timed out after 300 seconds.",
            "failure_class": "timeout",
            "started_at": None,
            "finished_at": None,
        }
    ]


@pytest.mark.asyncio
async def test_update_subagent_recovery_persists_runtime_recovery_fields(fake_redis):
    await state_machine.init_state("run-11", "auto")
    await state_machine.update_subagent("run-11", "task-1", "pending", role="researcher")
    await state_machine.initialize_subagent_attempts(
        "run-11",
        subtask_ids=["task-1"],
    )

    await state_machine.update_subagent_recovery(
        "run-11",
        "task-1",
        expected_epoch=0,
        mode="replacement",
        phase="replacement_started",
        reason="SubAgent timed out after 300 seconds.",
        wake_attempts=1,
        replacement_attempts=1,
        replacement_context_id="ctx-123",
        handoff_summary="Use the partial draft and continue from the outline stage.",
        last_command_id="cmd-789",
    )

    payload = await state_machine.get_state("run-11")
    recovery = payload["subagents"]["task-1"]["recovery"]
    assert recovery["mode"] == "replacement"
    assert recovery["phase"] == "replacement_started"
    assert recovery["reason"] == "SubAgent timed out after 300 seconds."
    assert recovery["wake_attempts"] == 1
    assert recovery["replacement_attempts"] == 1
    assert recovery["replacement_context_id"] == "ctx-123"
    assert recovery["handoff_summary"] == "Use the partial draft and continue from the outline stage."
    assert recovery["last_command_id"] == "cmd-789"
    assert recovery["updated_at"] is not None
