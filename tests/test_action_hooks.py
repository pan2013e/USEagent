import asyncio
import copy
import inspect
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest
import pytest_asyncio
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

import useagent.action_hooks as action_hooks_module
import useagent.action_hook_scheduler as scheduler_module
import useagent.common.constants as constants
from useagent.agents.meta import agent as meta_agent_module
from useagent.action_hooks import (
    ACTION_HOOK_MANAGER,
    ActionCheckpoint,
    ActionHookEvent,
    ActionHookManager,
    ActionHookPolicy,
    ActionHookResourceReleaseError,
    ActionHookSchedulerError,
    ActionInterventionRequest,
    HookCancellationToken,
    HookDecision,
    HookOptions,
    OrderedHookSchedulerConfig,
    cleanup_filesystem_snapshot,
    create_filesystem_snapshot,
    load_action_hook_spec,
    parse_action_hook_spec_list,
    register_action_hook_specs,
    restore_filesystem_snapshot,
    restore_task_state_from_checkpoint,
    restore_task_state_from_snapshot,
)
from useagent.pydantic_models.task_state import TaskState
from useagent.state.git_repo import GitRepository
from useagent.state.usage_tracker import UsageTracker
from useagent.tasks.test_task import TestTask
from useagent.tools import meta


@pytest_asyncio.fixture(autouse=True)
async def clear_global_hooks():
    await ACTION_HOOK_MANAGER.cancel_and_close(clean_snapshots=True)
    ACTION_HOOK_MANAGER.clear_hooks()
    yield
    await ACTION_HOOK_MANAGER.cancel_and_close(clean_snapshots=True)
    ACTION_HOOK_MANAGER.clear_hooks()


def make_context(tmp_path) -> RunContext[TaskState]:
    task = TestTask(root=tmp_path)
    state = TaskState(task=task, git_repo=GitRepository(local_path=tmp_path))
    context = RunContext(
        deps=state,
        model=TestModel(),
        usage=RunUsage(),
        messages=[],
    )
    context.tool_call_id = "test-tool-call"
    return context


def git(tmp_path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=tmp_path,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def ordered_config(**overrides) -> OrderedHookSchedulerConfig:
    values = {
        "max_concurrent_runs": 2,
        "max_unretired_actions": 2,
        "run_timeout_seconds": 1.0,
        "post_action_patience_seconds": 0.0,
        "intervention_quiesce_seconds": 1.0,
        "cleanup_seconds": 1.0,
        "finalize_seconds": 1.0,
        "snapshot_budget_mib": 64.0,
    }
    values.update(overrides)
    return OrderedHookSchedulerConfig(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_concurrent_runs", True),
        ("max_unretired_actions", 1.5),
        ("run_timeout_seconds", float("nan")),
        ("post_action_patience_seconds", -1),
        ("intervention_quiesce_seconds", 0),
        ("cleanup_seconds", "30"),
        ("finalize_seconds", float("inf")),
        ("snapshot_budget_mib", 0),
    ],
)
def test_ordered_config_rejects_invalid_programmatic_values(field, value):
    with pytest.raises(ValueError):
        ordered_config(**{field: value})


@pytest.mark.parametrize(
    "decision",
    [
        {"kind": "unknown"},
        {"kind": 1},
        {"reason": 1},
        {"instruction": 1},
        {"instruction": "unused no-op instruction"},
        {"kind": "intervene"},
        {"kind": "intervene", "instruction": "   "},
        {"additional_knowledge": []},
        {"additional_knowledge": {"key": 1}},
        {"additional_knowledge": {1: "value"}},
        {"restore_to_checkpoint": 1},
    ],
)
def test_hook_decision_rejects_invalid_runtime_values(decision):
    with pytest.raises(ValueError):
        HookDecision(**decision)


def test_hook_decision_accepts_valid_runtime_values():
    assert HookDecision.noop("nothing to do") == HookDecision(
        kind="noop",
        reason="nothing to do",
    )
    assert HookDecision.intervene(
        "Revise the edit.",
        reason="guardrail violation",
        additional_knowledge={"candidate_id": "candidate-1"},
        restore_to_checkpoint=False,
    ) == HookDecision(
        kind="intervene",
        reason="guardrail violation",
        instruction="Revise the edit.",
        additional_knowledge={"candidate_id": "candidate-1"},
        restore_to_checkpoint=False,
    )


def test_hook_decision_factory_does_not_coerce_invalid_runtime_values():
    with pytest.raises(ValueError, match="additional_knowledge"):
        HookDecision.intervene("Revise the edit.", additional_knowledge=[])


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "noop", "reason": 1},
        {"kind": "intervene", "instruction": "Revise", "reason": 1},
        {
            "kind": "intervene",
            "instruction": "Revise",
            "additional_knowledge": {"candidate_id": 1},
        },
        {
            "kind": "intervene",
            "instruction": "Revise",
            "restore_to_checkpoint": "false",
        },
    ],
)
def test_command_decision_payload_does_not_coerce_invalid_values(payload):
    with pytest.raises(ValueError):
        action_hooks_module._decision_from_payload(payload)


def set_tool_call(ctx: RunContext[TaskState], tool_call_id: str) -> None:
    ctx.tool_call_id = tool_call_id


async def wait_for_intervention(
    manager: ActionHookManager,
    timeout: float = 1.0,
) -> ActionInterventionRequest:
    async def poll() -> ActionInterventionRequest:
        while True:
            request = manager.pop_intervention()
            if request is not None:
                return request
            await asyncio.sleep(0)

    return await asyncio.wait_for(poll(), timeout)


def make_ordered_command_event(
    tmp_path,
    ctx: RunContext[TaskState],
    *,
    action_name="edit_code",
    action_args=None,
    result="diff_0",
) -> ActionHookEvent:
    analysis_workspace = tmp_path / f"revision-{action_name}"
    analysis_workspace.mkdir(exist_ok=True)
    checkpoint = ActionCheckpoint(
        id="checkpoint-1",
        action_name=action_name,
        task_state=ctx.deps,
        messages=[],
        bash_history_length=0,
        generation=1,
        session_id="session-1",
        epoch=1,
        action_seq=1,
        tool_call_id="tool-1",
    )
    return ActionHookEvent(
        action_name=action_name,
        action_args=action_args or {},
        result=result,
        error=None,
        checkpoint=checkpoint,
        current_task_state=ctx.deps,
        current_bash_history_length=0,
        session_id="session-1",
        epoch=1,
        action_seq=1,
        hook_job_id="job-1",
        workspace_revision_id="revision-1",
        analysis_workspace=analysis_workspace,
    )


@pytest.mark.asyncio
async def test_load_action_hook_spec_from_external_file(tmp_path):
    hook_file = tmp_path / "external_hooks.py"
    hook_file.write_text(
        "\n".join(
            [
                "from useagent.action_hooks import HookDecision",
                "",
                "async def external_hook(event, token):",
                "    return HookDecision.noop('loaded')",
            ]
        )
    )

    hook = load_action_hook_spec(f"{hook_file}:external_hook")
    ctx = make_context(tmp_path)
    checkpoint = ActionCheckpoint(
        id="checkpoint",
        action_name="search_code",
        task_state=ctx.deps,
        messages=[],
        bash_history_length=0,
        generation=0,
    )
    decision_or_awaitable = hook(
        ActionHookEvent(
            action_name="search_code",
            action_args={},
            result=[],
            error=None,
            checkpoint=checkpoint,
            current_task_state=ctx.deps,
            current_bash_history_length=0,
        ),
        HookCancellationToken(),
    )
    if inspect.isawaitable(decision_or_awaitable):
        decision = await decision_or_awaitable
    else:
        decision = decision_or_awaitable

    assert decision == HookDecision.noop("loaded")


def test_register_action_hook_spec_uses_callable_metadata(tmp_path):
    hook_file = tmp_path / "metadata_hook.py"
    hook_file.write_text(
        "\n".join(
            [
                "from useagent.action_hooks import HookOptions",
                "",
                "async def metadata_hook(event, token):",
                "    return None",
                "",
                "metadata_hook.__useagent_hook_options__ = HookOptions(",
                "    id='metadata-hook',",
                "    actions=frozenset({'edit_code'}),",
                "    failure_policy='intervene',",
                ")",
            ]
        )
    )

    assert register_action_hook_specs([f"{hook_file}:metadata_hook"]) == 1
    registration = ACTION_HOOK_MANAGER._registrations[-1]
    assert registration.id == "metadata-hook"
    assert registration.options.actions == frozenset({"edit_code"})
    assert registration.options.failure_policy == "intervene"


def test_parse_action_hook_spec_list():
    assert parse_action_hook_spec_list("pkg.mod:hook, /tmp/hooks.py:other ,,") == [
        "pkg.mod:hook",
        "/tmp/hooks.py:other",
    ]


@pytest.mark.asyncio
async def test_intervention_limit_ignores_future_interventions(monkeypatch, tmp_path):
    ctx = make_context(tmp_path)
    warnings: list[str] = []
    logger_stub = SimpleNamespace(
        warning=warnings.append,
        info=lambda *_args, **_kwargs: None,
        exception=lambda *_args, **_kwargs: None,
    )

    monkeypatch.setattr(constants, "MAX_ACTION_HOOK_INTERVENTIONS", 0)
    monkeypatch.setattr(meta_agent_module, "logger", logger_stub)
    monkeypatch.setattr(action_hooks_module, "logger", logger_stub)

    request = ActionInterventionRequest(
        checkpoint=ActionCheckpoint(
            id="over-limit",
            action_name="search_code",
            task_state=ctx.deps,
            messages=[],
            bash_history_length=0,
            generation=0,
        ),
        decision=HookDecision.intervene("Apply the over-limit intervention."),
    )

    (
        intervention_count,
        intervention_prompt,
    ) = await meta_agent_module._apply_action_hook_intervention(
        request,
        ctx.deps,
        intervention_count=0,
    )

    assert intervention_count == 1
    assert intervention_prompt is None
    assert any(
        "Future interventions will be ignored" in warning for warning in warnings
    )

    assert ACTION_HOOK_MANAGER.pop_intervention() is None


@pytest.mark.asyncio
async def test_intervention_limit_resumes_agent_loop_from_live_safe_boundary(
    monkeypatch,
    tmp_path,
):
    ctx = make_context(tmp_path)
    checkpoint_state = copy.deepcopy(ctx.deps)
    ctx.deps.additional_knowledge["current-trajectory"] = "preserved"
    initial = ModelRequest(parts=[])
    completed_call = ModelResponse(
        parts=[
            ToolCallPart(
                tool_name="edit_code",
                args={"instruction": "completed edit"},
                tool_call_id="call-completed-edit",
            )
        ]
    )
    completed_return = ModelRequest(
        parts=[
            ToolReturnPart(
                tool_name="edit_code",
                content="diff_0",
                tool_call_id="call-completed-edit",
            )
        ]
    )
    current_response = ModelResponse(
        parts=[TextPart(content="Continue from the completed edit.")]
    )
    pending_call = ModelResponse(
        parts=[
            ToolCallPart(
                tool_name="search_code",
                args={"query": "next action"},
                tool_call_id="call-pending-search",
            )
        ]
    )
    live_messages = [
        initial,
        completed_call,
        completed_return,
        current_response,
        pending_call,
    ]
    request = ActionInterventionRequest(
        checkpoint=ActionCheckpoint(
            id="over-limit-live-loop",
            action_name="edit_code",
            task_state=checkpoint_state,
            messages=[initial, completed_call],
            bash_history_length=0,
            generation=0,
            tool_call_id="call-completed-edit",
        ),
        decision=HookDecision.intervene("Roll back and revise the edit."),
        replay_messages=live_messages,
    )
    final_messages = [
        *live_messages[:-1],
        ModelResponse(parts=[TextPart(content="Done.")]),
    ]
    final_output = SimpleNamespace(doubts=None)
    final_result = SimpleNamespace(
        output=final_output,
        usage=lambda: RunUsage(),
        all_messages=lambda: final_messages,
    )
    turn_inputs = []

    async def run_turn(_agent, prompt, _task_state, message_history):
        turn_inputs.append((prompt, message_history))
        if len(turn_inputs) == 1:
            raise action_hooks_module.ActionIntervention(request)
        return final_result

    monkeypatch.setattr(constants, "MAX_ACTION_HOOK_INTERVENTIONS", 0)
    monkeypatch.setattr(
        meta_agent_module,
        "init_agent",
        lambda **_kwargs: SimpleNamespace(name="intervention-limit-test"),
    )
    monkeypatch.setattr(meta_agent_module, "init_bash_tool", lambda *_a, **_k: None)
    monkeypatch.setattr(meta_agent_module, "init_edit_tools", lambda *_a, **_k: None)
    monkeypatch.setattr(meta_agent_module, "_run_meta_agent_turn", run_turn)
    monkeypatch.setattr(
        meta_agent_module.ConfigSingleton,
        "is_initialized",
        classmethod(lambda _cls: False),
    )

    output, _usage, messages = await meta_agent_module.agent_loop(ctx.deps)

    assert output is final_output
    assert messages == final_messages
    assert turn_inputs == [
        ("Invoke tools to complete the task.", None),
        (None, live_messages[:-1]),
    ]
    assert ctx.deps.additional_knowledge == {"current-trajectory": "preserved"}


def test_restore_task_state_from_checkpoint_restores_in_memory_state(tmp_path):
    ctx = make_context(tmp_path)
    checkpoint = ActionCheckpoint(
        id="checkpoint",
        action_name="search_code",
        task_state=copy.deepcopy(ctx.deps),
        messages=[],
        bash_history_length=0,
        generation=0,
    )

    ctx.deps.additional_knowledge["speculative"] = "value"

    restore_task_state_from_checkpoint(ctx.deps, checkpoint)

    assert "speculative" not in ctx.deps.additional_knowledge


def test_restore_task_state_from_snapshot_preserves_post_action_state(tmp_path):
    ctx = make_context(tmp_path)
    snapshot = TaskState(task=ctx.deps._task, git_repo=ctx.deps._git_repo)
    snapshot.additional_knowledge["post_action"] = "preserved"
    ctx.deps.additional_knowledge["post_action"] = "speculative"

    restore_task_state_from_snapshot(ctx.deps, snapshot, bash_history_length=None)

    assert ctx.deps.additional_knowledge == {"post_action": "preserved"}


def test_restore_filesystem_snapshot_restores_project_files(tmp_path):
    ctx = make_context(tmp_path)
    keep_file = tmp_path / "keep.txt"
    keep_file.write_text("after action")
    original_head = git(tmp_path, "rev-parse", "HEAD")
    original_branch = git(tmp_path, "branch", "--show-current")
    snapshot = create_filesystem_snapshot(ctx.deps)
    assert snapshot is not None
    assert snapshot.strategy == "git"
    assert snapshot.git_snapshot is not None

    try:
        keep_file.write_text("later action")
        (tmp_path / "later.txt").write_text("remove me")
        git(tmp_path, "checkout", "-b", "later-branch")
        committed_file = tmp_path / "committed.txt"
        committed_file.write_text("committed after snapshot")
        git(tmp_path, "add", "committed.txt")
        git(tmp_path, "commit", "-m", "Commit after snapshot")
        git(tmp_path, "tag", "later-tag")
        assert git(tmp_path, "rev-parse", "HEAD") != original_head
        staged_file = tmp_path / "staged.txt"
        staged_file.write_text("staged after snapshot")
        git(tmp_path, "add", "staged.txt")
        assert "staged.txt" in git(tmp_path, "status", "--short")

        restore_filesystem_snapshot(snapshot)

        assert keep_file.read_text() == "after action"
        assert not (tmp_path / "later.txt").exists()
        assert not committed_file.exists()
        assert not staged_file.exists()
        assert (tmp_path / ".git").exists()
        assert git(tmp_path, "rev-parse", "HEAD") == original_head
        assert git(tmp_path, "branch", "--show-current") == original_branch
        assert git(tmp_path, "branch", "--list", "later-branch") == ""
        assert git(tmp_path, "tag", "--list", "later-tag") == ""
        assert git(tmp_path, "status", "--short") == "?? keep.txt"
    finally:
        cleanup_filesystem_snapshot(snapshot)


def test_restore_intervention_message_history_preserves_completed_action(tmp_path):
    ctx = make_context(tmp_path)
    initial_user_message = ModelRequest(parts=[])
    probe_tool_call = ModelResponse(
        parts=[
            ToolCallPart(
                tool_name="probe_environment",
                args={},
                tool_call_id="call-probe",
            ),
        ]
    )
    probe_tool_return = ModelRequest(
        parts=[
            ToolReturnPart(
                tool_name="probe_environment",
                content="Environment detected.",
                tool_call_id="call-probe",
            ),
        ]
    )
    pending_next_action = ModelResponse(
        parts=[
            ToolCallPart(
                tool_name="search_code",
                args={"instruction": "find relevant code"},
                tool_call_id="call-next",
            ),
        ]
    )
    request = ActionInterventionRequest(
        checkpoint=ActionCheckpoint(
            id="checkpoint",
            action_name="probe_environment",
            task_state=ctx.deps,
            messages=[initial_user_message],
            bash_history_length=0,
            generation=0,
        ),
        decision=HookDecision.intervene("Use the probe result before searching."),
        replay_messages=[
            initial_user_message,
            probe_tool_call,
            probe_tool_return,
            pending_next_action,
        ],
    )

    assert meta_agent_module._message_history_for_action_hook_intervention(request) == [
        initial_user_message,
        probe_tool_call,
        probe_tool_return,
    ]


def test_restore_intervention_message_history_stops_at_triggering_repeated_action(
    tmp_path,
):
    ctx = make_context(tmp_path)
    initial_user_message = ModelRequest(parts=[])
    first_search_call = ModelResponse(
        parts=[
            ToolCallPart(
                tool_name="search_code",
                args={"instruction": "first search"},
                tool_call_id="call-search-1",
            ),
        ]
    )
    first_search_return = ModelRequest(
        parts=[
            ToolReturnPart(
                tool_name="search_code",
                content="First search result.",
                tool_call_id="call-search-1",
            ),
        ]
    )
    later_search_call = ModelResponse(
        parts=[
            ToolCallPart(
                tool_name="search_code",
                args={"instruction": "later search"},
                tool_call_id="call-search-2",
            ),
        ]
    )
    later_search_return = ModelRequest(
        parts=[
            ToolReturnPart(
                tool_name="search_code",
                content="Later search result.",
                tool_call_id="call-search-2",
            ),
        ]
    )
    request = ActionInterventionRequest(
        checkpoint=ActionCheckpoint(
            id="checkpoint",
            action_name="search_code",
            task_state=ctx.deps,
            messages=[initial_user_message],
            bash_history_length=0,
            generation=0,
        ),
        decision=HookDecision.intervene("Use the first search result."),
        replay_messages=[
            initial_user_message,
            first_search_call,
            first_search_return,
            later_search_call,
            later_search_return,
        ],
    )

    assert meta_agent_module._message_history_for_action_hook_intervention(request) == [
        initial_user_message,
        first_search_call,
        first_search_return,
    ]


def test_non_restore_intervention_keeps_completed_message_history(tmp_path):
    ctx = make_context(tmp_path)
    checkpoint_message = ModelRequest(parts=[])
    completed_messages = [
        checkpoint_message,
        ModelResponse(parts=[TextPart(content="Completed action.")]),
    ]
    request = ActionInterventionRequest(
        checkpoint=ActionCheckpoint(
            id="checkpoint",
            action_name="search_code",
            task_state=ctx.deps,
            messages=[checkpoint_message],
            bash_history_length=0,
            generation=0,
        ),
        decision=HookDecision.intervene(
            "Continue from current state.",
            restore_to_checkpoint=False,
        ),
    )

    assert (
        meta_agent_module._message_history_for_action_hook_intervention(
            request,
            completed_messages,
        )
        == completed_messages
    )


def test_non_restore_intervention_uses_interrupted_message_history(tmp_path):
    ctx = make_context(tmp_path)
    checkpoint_message = ModelRequest(parts=[])
    completed_probe_message = ModelResponse(parts=[TextPart(content="Probe complete.")])
    pending_next_action = ModelResponse(
        parts=[
            ToolCallPart(
                tool_name="search_code",
                args={"instruction": "find relevant code"},
                tool_call_id="call-next",
            )
        ]
    )
    request = ActionInterventionRequest(
        checkpoint=ActionCheckpoint(
            id="checkpoint",
            action_name="probe_environment",
            task_state=ctx.deps,
            messages=[checkpoint_message],
            bash_history_length=0,
            generation=0,
        ),
        decision=HookDecision.intervene(
            "Continue from current state.",
            restore_to_checkpoint=False,
        ),
        replay_messages=[
            checkpoint_message,
            completed_probe_message,
            pending_next_action,
        ],
    )

    assert meta_agent_module._message_history_for_action_hook_intervention(request) == [
        checkpoint_message,
        completed_probe_message,
    ]


@pytest.mark.asyncio
async def test_command_action_hook_spec_returns_intervention(tmp_path):
    hook_file = tmp_path / "command_hook.py"
    hook_file.write_text(
        "\n".join(
            [
                "import json",
                "import sys",
                "payload = json.load(sys.stdin)",
                "print(json.dumps({",
                "    'kind': 'intervene',",
                "    'instruction': 'Command hook says continue carefully.',",
                "    'reason': payload['event']['action_name'],",
                "    'additional_knowledge': {'command_hook': 'ran'},",
                "    'restore_to_checkpoint': False,",
                "}))",
            ]
        )
    )
    hook = load_action_hook_spec(f"command:{sys.executable} {hook_file}")
    assert getattr(hook, "__useagent_hook_options__").execution == "process"
    ctx = make_context(tmp_path)

    decision_or_awaitable = hook(
        make_ordered_command_event(
            tmp_path,
            ctx,
            action_name="search_code",
            action_args={"instruction": "find code"},
            result=[],
        ),
        HookCancellationToken(),
    )
    assert inspect.isawaitable(decision_or_awaitable)
    decision = await decision_or_awaitable

    assert decision == HookDecision.intervene(
        "Command hook says continue carefully.",
        reason="search_code",
        additional_knowledge={"command_hook": "ran"},
        restore_to_checkpoint=False,
    )


@pytest.mark.asyncio
async def test_command_action_hook_receives_ordered_identity_and_workspace(tmp_path):
    hook_file = tmp_path / "ordered_command_hook.py"
    hook_file.write_text(
        "\n".join(
            [
                "import json",
                "import sys",
                "event = json.load(sys.stdin)['event']",
                "print(json.dumps({",
                "    'kind': 'noop',",
                "    'reason': '|'.join([",
                "        event['session_id'],",
                "        str(event['epoch']),",
                "        str(event['action_seq']),",
                "        event['hook_job_id'],",
                "        event['workspace_revision_id'],",
                "        event['analysis_workspace'],",
                "        event['checkpoint']['tool_call_id'],",
                "    ]),",
                "}))",
            ]
        )
    )
    hook = load_action_hook_spec(f"command:{sys.executable} {hook_file}")
    ctx = make_context(tmp_path)
    analysis_workspace = tmp_path / "revision"
    analysis_workspace.mkdir()
    checkpoint = ActionCheckpoint(
        id="checkpoint",
        action_name="edit_code",
        task_state=ctx.deps,
        messages=[],
        bash_history_length=0,
        generation=4,
        session_id="session-1",
        epoch=4,
        action_seq=7,
        tool_call_id="tool-8",
    )

    decision_or_awaitable = hook(
        ActionHookEvent(
            action_name="edit_code",
            action_args={},
            result="diff_0",
            error=None,
            checkpoint=checkpoint,
            current_task_state=ctx.deps,
            current_bash_history_length=0,
            session_id="session-1",
            epoch=4,
            action_seq=7,
            hook_job_id="job-9",
            workspace_revision_id="revision-10",
            analysis_workspace=analysis_workspace,
        ),
        HookCancellationToken(),
    )
    assert inspect.isawaitable(decision_or_awaitable)
    decision = await decision_or_awaitable

    assert decision == HookDecision.noop(
        f"session-1|4|7|job-9|revision-10|{analysis_workspace}|tool-8"
    )


@pytest.mark.asyncio
async def test_command_action_hook_nonzero_exit_is_failure(tmp_path):
    hook_file = tmp_path / "failing_command_hook.py"
    hook_file.write_text(
        "import sys\nprint('hook failed', file=sys.stderr)\nraise SystemExit(23)\n"
    )
    hook = load_action_hook_spec(f"command:{sys.executable} {hook_file}")
    event = make_ordered_command_event(tmp_path, make_context(tmp_path))

    decision_or_awaitable = hook(event, HookCancellationToken())
    assert inspect.isawaitable(decision_or_awaitable)
    with pytest.raises(RuntimeError, match="failed with exit code 23"):
        await decision_or_awaitable


@pytest.mark.asyncio
async def test_command_action_hook_invalid_json_is_failure(tmp_path):
    hook_file = tmp_path / "invalid_json_command_hook.py"
    hook_file.write_text("print('not-json')\n")
    hook = load_action_hook_spec(f"command:{sys.executable} {hook_file}")
    event = make_ordered_command_event(tmp_path, make_context(tmp_path))

    decision_or_awaitable = hook(event, HookCancellationToken())
    assert inspect.isawaitable(decision_or_awaitable)
    with pytest.raises(RuntimeError, match="command hook returned invalid JSON"):
        await decision_or_awaitable


@pytest.mark.asyncio
async def test_command_action_hook_timeout_is_failure(monkeypatch, tmp_path):
    hook_file = tmp_path / "timed_out_command_hook.py"
    hook_file.write_text("import time\ntime.sleep(60)\n")
    hook = load_action_hook_spec(f"command:{sys.executable} {hook_file}")
    event = make_ordered_command_event(tmp_path, make_context(tmp_path))
    monkeypatch.setattr(constants, "ACTION_HOOK_COMMAND_TIMEOUT_SECONDS", 0.02)

    decision_or_awaitable = hook(event, HookCancellationToken())
    assert inspect.isawaitable(decision_or_awaitable)
    with pytest.raises(RuntimeError, match="command hook timed out"):
        await decision_or_awaitable


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="uses /proc for process audit")
async def test_command_action_hook_cancellation_kills_descendants(tmp_path):
    hook_file = tmp_path / "blocking_command_hook.py"
    pid_file = tmp_path / "descendant.pid"
    hook_file.write_text(
        "\n".join(
            [
                "import pathlib",
                "import subprocess",
                "import sys",
                "import time",
                "child = subprocess.Popen([",
                "    sys.executable, '-c', 'import time; time.sleep(60)'",
                "])",
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid))",
                "time.sleep(60)",
            ]
        )
    )
    hook = load_action_hook_spec(f"command:{sys.executable} {hook_file} {pid_file}")
    ctx = make_context(tmp_path)
    decision_or_awaitable = hook(
        make_ordered_command_event(tmp_path, ctx),
        HookCancellationToken(),
    )
    assert inspect.isawaitable(decision_or_awaitable)
    task = asyncio.create_task(decision_or_awaitable)

    async def descendant_pid() -> int:
        while not pid_file.exists():
            await asyncio.sleep(0.01)
        return int(pid_file.read_text())

    child_pid = await asyncio.wait_for(descendant_pid(), timeout=2)
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        child_proc = f"/proc/{child_pid}"
        for _ in range(100):
            if not os.path.exists(child_proc):
                break
            await asyncio.sleep(0.01)
        assert not os.path.exists(child_proc)
    finally:
        if os.path.exists(f"/proc/{child_pid}"):
            os.kill(child_pid, 9)


@pytest.mark.asyncio
async def test_uncaught_top_level_action_exception_schedules_hook(
    monkeypatch,
    tmp_path,
):
    ctx = make_context(tmp_path)
    meta.USAGE_TRACKER = UsageTracker()
    seen_error: list[BaseException | None] = []
    seen = asyncio.Event()

    class FailingSearchAgent:
        name = "SEARCH"

        async def run(self, *args, **kwargs):
            raise RuntimeError("search failed")

    async def hook(event, token):
        seen_error.append(event.error)
        seen.set()
        return HookDecision.noop("observed error")

    monkeypatch.setattr(meta, "init_search_code_agent", lambda: FailingSearchAgent())
    ACTION_HOOK_MANAGER.register(hook)

    with pytest.raises(ModelRetry, match="recorded the failure") as exc_info:
        await meta.search_code(ctx, "find relevant code")

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    await asyncio.wait_for(seen.wait(), 1)
    assert isinstance(seen_error[0], RuntimeError)


@pytest.mark.asyncio
async def test_edit_code_success_finalization_failure_is_not_finalized_twice(
    monkeypatch,
    tmp_path,
):
    ctx = make_context(tmp_path)
    meta.USAGE_TRACKER = UsageTracker()
    checkpoint = object()
    finish_calls: list[dict[str, object]] = []

    class SuccessfulEditAgent:
        name = "EDIT"

        async def run(self, *args, **kwargs):
            return SimpleNamespace(output="diff_0", usage=lambda: RunUsage())

    async def start_action(*args, **kwargs):
        return checkpoint

    async def fail_finalization(
        actual_checkpoint,
        actual_ctx,
        action_args,
        *,
        result=None,
        error=None,
    ):
        finish_calls.append(
            {
                "checkpoint": actual_checkpoint,
                "ctx": actual_ctx,
                "action_args": action_args,
                "result": result,
                "error": error,
            }
        )
        raise RuntimeError("successful finalization failed")

    monkeypatch.setattr(meta, "init_edit_code_agent", lambda: SuccessfulEditAgent())
    monkeypatch.setattr(meta, "_start_top_level_action", start_action)
    monkeypatch.setattr(meta, "_finish_top_level_action", fail_finalization)

    with pytest.raises(RuntimeError, match="successful finalization failed"):
        await meta.edit_code(ctx, "make a successful edit")

    assert finish_calls == [
        {
            "checkpoint": checkpoint,
            "ctx": ctx,
            "action_args": {"instruction": "make a successful edit"},
            "result": "diff_0",
            "error": None,
        }
    ]


@pytest.mark.asyncio
async def test_ordered_hook_filters_actions_and_receives_immutable_workspace(tmp_path):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(ordered_config())
    seen: list[tuple[str, str]] = []

    async def hook(event, token):
        assert event.analysis_workspace is not None
        seen.append(
            (
                event.action_name,
                (event.analysis_workspace / "tracked.txt").read_text(),
            )
        )
        return HookDecision.noop()

    manager.register(
        hook,
        options=HookOptions(actions=frozenset({"edit_code"})),
    )
    await manager.start_session()
    try:
        set_tool_call(ctx, "call-search")
        search_checkpoint = await manager.begin_action("search_code", ctx)
        assert search_checkpoint is not None
        await manager.finish_action(
            checkpoint=search_checkpoint,
            action_args={},
            result=[],
            current_task_state=ctx.deps,
        )
        await manager.protocol_finalized("call-search", [])

        (tmp_path / "tracked.txt").write_text("edit-one")
        set_tool_call(ctx, "call-edit")
        edit_checkpoint = await manager.begin_action("edit_code", ctx)
        assert edit_checkpoint is not None
        await manager.finish_action(
            checkpoint=edit_checkpoint,
            action_args={"instruction": "edit"},
            result="diff_0",
            current_task_state=ctx.deps,
        )
        await manager.protocol_finalized("call-edit", [])
        await manager.final_drain()

        assert seen == [("edit_code", "edit-one")]
    finally:
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_same_action_hooks_receive_isolated_workspaces(tmp_path):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(ordered_config(max_concurrent_runs=2))
    (tmp_path / "tracked.txt").write_text("captured")
    first_wrote = asyncio.Event()
    second_checked = asyncio.Event()
    workspaces = []

    async def first_hook(event, token):
        assert event.analysis_workspace is not None
        workspaces.append(event.analysis_workspace)
        assert (event.analysis_workspace / "tracked.txt").read_text() == "captured"
        (event.analysis_workspace / "first-hook.txt").write_text("private")
        first_wrote.set()
        await second_checked.wait()
        assert not (event.analysis_workspace / "second-hook.txt").exists()
        return HookDecision.noop()

    async def second_hook(event, token):
        assert event.analysis_workspace is not None
        workspaces.append(event.analysis_workspace)
        await first_wrote.wait()
        assert not (event.analysis_workspace / "first-hook.txt").exists()
        assert (event.analysis_workspace / "tracked.txt").read_text() == "captured"
        (event.analysis_workspace / "second-hook.txt").write_text("private")
        second_checked.set()
        return HookDecision.noop()

    manager.register(first_hook, options=HookOptions(id="first"))
    manager.register(second_hook, options=HookOptions(id="second"))
    await manager.start_session()
    session = manager._ordered_session
    assert session is not None
    try:
        set_tool_call(ctx, "call-isolated-hooks")
        checkpoint = await manager.begin_action("edit_code", ctx)
        assert checkpoint is not None
        await asyncio.wait_for(
            manager.finish_action(
                checkpoint=checkpoint,
                action_args={},
                result="diff_0",
                current_task_state=ctx.deps,
            ),
            1,
        )
        await manager.protocol_finalized("call-isolated-hooks", [])
        await manager.final_drain()

        assert len(workspaces) == 2
        assert workspaces[0] != workspaces[1]
        assert workspaces[0].parent != workspaces[1].parent
        snapshot_event = next(
            event
            for event in manager.diagnostics()
            if event["event"] == "action_snapshot_created"
        )
        assert snapshot_event["analysis_workspace_count"] == 2
        assert snapshot_event["snapshot_duration_seconds"] >= 0
        assert snapshot_event["restore_snapshot_create_seconds"] >= 0
        assert snapshot_event["restore_snapshot_size_seconds"] >= 0
        assert snapshot_event["source_size_seconds"] >= 0
        assert snapshot_event["analysis_copy_seconds"] >= 0
        assert snapshot_event["analysis_size_seconds"] >= 0
        measured_phases = sum(
            snapshot_event[field]
            for field in (
                "restore_snapshot_create_seconds",
                "restore_snapshot_size_seconds",
                "source_size_seconds",
                "analysis_copy_seconds",
                "analysis_size_seconds",
            )
        )
        assert snapshot_event["snapshot_duration_seconds"] + 0.00001 >= measured_phases
        assert not (tmp_path / "first-hook.txt").exists()
        assert not (tmp_path / "second-hook.txt").exists()
    finally:
        second_checked.set()
        await manager.cancel_and_close()

    assert all(not workspace.parent.exists() for workspace in workspaces)
    assert session._snapshot_bytes == 0


@pytest.mark.asyncio
async def test_ordered_hook_cannot_access_authoritative_rollback_snapshot(tmp_path):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_ordered_scheduler(ordered_config())
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("post-action")
    callback_snapshots = []

    async def hook(event, token):
        callback_snapshots.append(event.current_filesystem_snapshot)
        assert event.analysis_workspace is not None
        (event.analysis_workspace / "tracked.txt").write_text("hook-private")
        return HookDecision.intervene(
            "restore the post-action revision",
            restore_to_checkpoint=True,
        )

    manager.register(hook)
    await manager.start_session()
    try:
        set_tool_call(ctx, "call-private-rollback")
        checkpoint = await manager.begin_action("edit_code", ctx)
        assert checkpoint is not None
        await manager.finish_action(
            checkpoint=checkpoint,
            action_args={},
            result="diff_0",
            current_task_state=ctx.deps,
        )
        await manager.protocol_finalized("call-private-rollback", [])
        request = await wait_for_intervention(manager)

        assert callback_snapshots == [None]
        assert request.restore_filesystem_snapshot is not None
        tracked.write_text("later-speculative-change")
        await manager.prepare_intervention(request)
        manager.reset_runtime(preserve_snapshot_id=request.checkpoint.id)
        restore_filesystem_snapshot(request.restore_filesystem_snapshot)
        assert tracked.read_text() == "post-action"
        manager.cleanup_filesystem_snapshot(request.checkpoint.id)
    finally:
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_duplicate_finish_is_rejected_before_second_snapshot(tmp_path):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_ordered_scheduler(ordered_config())
    started = asyncio.Event()
    release = asyncio.Event()

    async def hook(event, token):
        started.set()
        await release.wait()
        return HookDecision.noop()

    manager.register(hook)
    await manager.start_session()
    try:
        set_tool_call(ctx, "call-finish-once")
        checkpoint = await manager.begin_action("edit_code", ctx)
        assert checkpoint is not None
        first_finish = asyncio.create_task(
            manager.finish_action(
                checkpoint=checkpoint,
                action_args={},
                result="diff_0",
                current_task_state=ctx.deps,
            )
        )
        await asyncio.wait_for(started.wait(), 1)

        with pytest.raises(
            ActionHookSchedulerError,
            match="completion was already started",
        ):
            await manager.finish_action(
                checkpoint=checkpoint,
                action_args={},
                result="diff_0",
                current_task_state=ctx.deps,
            )

        release.set()
        await asyncio.wait_for(first_finish, 1)
        await manager.protocol_finalized("call-finish-once", [])
        await manager.final_drain()
        snapshots = [
            event
            for event in manager.diagnostics()
            if event["event"] == "action_snapshot_created"
        ]
        assert len(snapshots) == 1
    finally:
        release.set()
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_release_failure_retains_analysis_revision(tmp_path):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(ordered_config())
    analysis_workspace = None

    async def hook(event, token):
        nonlocal analysis_workspace
        assert event.analysis_workspace is not None
        analysis_workspace = event.analysis_workspace
        raise ActionHookResourceReleaseError("provider close was not acknowledged")

    manager.register(hook, options=HookOptions(id="leased-backend"))
    await manager.start_session()
    session = manager._ordered_session
    assert session is not None
    try:
        set_tool_call(ctx, "call-release-failure")
        checkpoint = await manager.begin_action("edit_code", ctx)
        assert checkpoint is not None
        await manager.finish_action(
            checkpoint=checkpoint,
            action_args={},
            result="diff_0",
            current_task_state=ctx.deps,
        )
        await manager.protocol_finalized("call-release-failure", [])
        with pytest.raises(
            ActionHookSchedulerError,
            match="did not confirm release",
        ):
            await manager.final_drain()

        async def wait_for_retention():
            while not any(
                event["event"] == "action_hook_revision_retained"
                for event in manager.diagnostics()
            ):
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_retention(), 1)
        assert analysis_workspace is not None
        assert analysis_workspace.exists()
        assert session._retained_revisions
        assert session._snapshot_bytes > 0
    finally:
        await manager.cancel_and_close()

    assert analysis_workspace is not None
    assert analysis_workspace.exists()
    scheduler_module.shutil.rmtree(analysis_workspace.parent)


@pytest.mark.asyncio
async def test_ordered_timeout_release_failure_is_fatal_and_retains_revision(
    tmp_path,
):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(
        ordered_config(run_timeout_seconds=0.01, cleanup_seconds=0.5)
    )
    analysis_workspace = None

    async def hook(event, token):
        nonlocal analysis_workspace
        assert event.analysis_workspace is not None
        analysis_workspace = event.analysis_workspace
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError as cancellation:
            raise ActionHookResourceReleaseError(
                "provider close failed during timeout cancellation"
            ) from cancellation

    manager.register(hook, options=HookOptions(id="timeout-leased-backend"))
    await manager.start_session()
    try:
        set_tool_call(ctx, "call-timeout-release-failure")
        checkpoint = await manager.begin_action("edit_code", ctx)
        assert checkpoint is not None
        await manager.finish_action(
            checkpoint=checkpoint,
            action_args={},
            result="diff_0",
            current_task_state=ctx.deps,
        )
        await manager.protocol_finalized("call-timeout-release-failure", [])
        with pytest.raises(
            ActionHookSchedulerError,
            match="did not confirm release",
        ):
            await manager.final_drain()
    finally:
        await manager.cancel_and_close()

    assert analysis_workspace is not None
    assert analysis_workspace.exists()
    assert any(
        event["event"] == "action_hook_revision_retained"
        for event in manager.diagnostics()
    )
    scheduler_module.shutil.rmtree(analysis_workspace.parent)


@pytest.mark.asyncio
async def test_ordered_multi_hook_copies_are_charged_before_materialization(
    monkeypatch,
    tmp_path,
):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    budget_bytes = 1536
    manager.configure_ordered_scheduler(
        ordered_config(snapshot_budget_mib=budget_bytes / (1024 * 1024))
    )
    snapshot_parent = tmp_path.parent / f"{tmp_path.name}-analysis-copies"
    snapshot_parent.mkdir()
    real_mkdtemp = scheduler_module.tempfile.mkdtemp
    monkeypatch.setattr(
        scheduler_module.tempfile,
        "mkdtemp",
        lambda *, prefix: real_mkdtemp(prefix=prefix, dir=snapshot_parent),
    )
    monkeypatch.setattr(scheduler_module, "_tree_size", lambda _root: 1024)
    hook_called = False

    async def hook(event, token):
        nonlocal hook_called
        hook_called = True
        return HookDecision.noop()

    manager.register(hook, options=HookOptions(id="copy-one"))
    manager.register(hook, options=HookOptions(id="copy-two"))
    await manager.start_session()
    session = manager._ordered_session
    assert session is not None
    try:
        set_tool_call(ctx, "call-copy-budget")
        checkpoint = await manager.begin_action("edit_code", ctx)
        assert checkpoint is not None
        with pytest.raises(ActionHookSchedulerError, match="budget exceeded"):
            await manager.finish_action(
                checkpoint=checkpoint,
                action_args={},
                result="diff_0",
                current_task_state=ctx.deps,
            )
        assert hook_called is False
        assert session._snapshot_bytes == 0
        assert list(snapshot_parent.iterdir()) == []
    finally:
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_worker_limit_queues_hooks_without_blocking_action_completion(
    tmp_path,
):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(
        ordered_config(max_concurrent_runs=1, max_unretired_actions=2)
    )
    releases = {1: asyncio.Event(), 2: asyncio.Event()}
    starts = {1: asyncio.Event(), 2: asyncio.Event()}
    running = 0
    peak_running = 0

    async def hook(event, token):
        nonlocal running, peak_running
        assert event.action_seq is not None
        running += 1
        peak_running = max(peak_running, running)
        starts[event.action_seq].set()
        try:
            await releases[event.action_seq].wait()
        finally:
            running -= 1
        return HookDecision.noop()

    manager.register(hook, options=HookOptions(actions=frozenset({"edit_code"})))
    await manager.start_session()
    try:
        set_tool_call(ctx, "call-1")
        checkpoint_1 = await manager.begin_action("edit_code", ctx)
        assert checkpoint_1 is not None
        await manager.finish_action(
            checkpoint=checkpoint_1,
            action_args={"index": 1},
            result="diff_1",
            current_task_state=ctx.deps,
        )
        await starts[1].wait()
        await manager.protocol_finalized("call-1", [])

        set_tool_call(ctx, "call-2")
        checkpoint_2 = await manager.begin_action("edit_code", ctx)
        assert checkpoint_2 is not None
        await asyncio.wait_for(
            manager.finish_action(
                checkpoint=checkpoint_2,
                action_args={"index": 2},
                result="diff_2",
                current_task_state=ctx.deps,
            ),
            1,
        )
        assert not starts[2].is_set()
        await manager.protocol_finalized("call-2", [])

        releases[1].set()
        await asyncio.wait_for(starts[2].wait(), 1)
        assert peak_running == 1
        releases[2].set()
        await manager.final_drain()
    finally:
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_post_action_patience_still_waits_for_running_hook(tmp_path):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(
        ordered_config(
            max_concurrent_runs=1,
            post_action_patience_seconds=1.0,
        )
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def hook(event, token):
        started.set()
        await release.wait()
        return HookDecision.noop()

    manager.register(hook, options=HookOptions(actions=frozenset({"edit_code"})))
    await manager.start_session()
    try:
        set_tool_call(ctx, "call-patience")
        checkpoint = await manager.begin_action("edit_code", ctx)
        assert checkpoint is not None
        finish = asyncio.create_task(
            manager.finish_action(
                checkpoint=checkpoint,
                action_args={},
                result="diff_0",
                current_task_state=ctx.deps,
            )
        )
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.sleep(0)
        assert not finish.done()
        release.set()
        await asyncio.wait_for(finish, 1)
        await manager.protocol_finalized("call-patience", [])
        await manager.final_drain()
    finally:
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_intervention_waits_for_protocol_and_action_order(tmp_path):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(ordered_config())
    release_first = asyncio.Event()
    second_done = asyncio.Event()

    async def hook(event, token):
        assert event.action_seq is not None
        if event.action_seq == 1:
            await release_first.wait()
            return HookDecision.intervene(
                "first action feedback", reason="first feedback reason"
            )
        second_done.set()
        return HookDecision.intervene(
            "second action feedback", reason="second feedback reason"
        )

    manager.register(hook, options=HookOptions(actions=frozenset({"edit_code"})))
    await manager.start_session()
    try:
        set_tool_call(ctx, "call-1")
        checkpoint_1 = await manager.begin_action("edit_code", ctx)
        assert checkpoint_1 is not None
        await manager.finish_action(
            checkpoint=checkpoint_1,
            action_args={},
            result="diff_1",
            current_task_state=ctx.deps,
        )
        await manager.protocol_finalized("call-1", [])

        set_tool_call(ctx, "call-2")
        checkpoint_2 = await manager.begin_action("edit_code", ctx)
        assert checkpoint_2 is not None
        await manager.finish_action(
            checkpoint=checkpoint_2,
            action_args={},
            result="diff_2",
            current_task_state=ctx.deps,
        )
        await second_done.wait()
        assert manager.pop_intervention() is None

        await manager.protocol_finalized("call-2", [])
        assert manager.pop_intervention() is None
        release_first.set()
        request = await wait_for_intervention(manager)
        assert request.checkpoint.tool_call_id == "call-1"
        assert request.decision.instruction == "first action feedback"
        snapshots = [
            event
            for event in manager.diagnostics()
            if event["event"] == "action_snapshot_created"
        ]
        assert [event["action_seq"] for event in snapshots] == [1, 2]
        assert all(event["action_name"] == "edit_code" for event in snapshots)
        assert [event["checkpoint_id"] for event in snapshots] == [
            checkpoint_1.id,
            checkpoint_2.id,
        ]
        selected = next(
            event
            for event in manager.diagnostics()
            if event["event"] == "intervention_selected"
        )
        assert selected["action_name"] == "edit_code"
        assert selected["reason"] == "first feedback reason"
        assert selected["restore_to_checkpoint"] is False
    finally:
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_fast_intervention_is_buffered_until_protocol_finalized(tmp_path):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(ordered_config())
    completed = asyncio.Event()

    async def hook(event, token):
        completed.set()
        return HookDecision.intervene("safe-point feedback")

    manager.register(hook, options=HookOptions(actions=frozenset({"edit_code"})))
    await manager.start_session()
    try:
        set_tool_call(ctx, "call-safe")
        checkpoint = await manager.begin_action("edit_code", ctx)
        assert checkpoint is not None
        await manager.finish_action(
            checkpoint=checkpoint,
            action_args={},
            result="diff_0",
            current_task_state=ctx.deps,
        )
        await completed.wait()
        await asyncio.sleep(0)
        assert manager.pop_intervention() is None

        await manager.protocol_finalized("call-safe", [])
        request = await wait_for_intervention(manager)
        assert request.checkpoint.tool_call_id == "call-safe"
    finally:
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_same_action_aggregation_uses_priority_not_completion(tmp_path):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(ordered_config())
    release_high = asyncio.Event()
    low_done = asyncio.Event()

    async def high_priority(event, token):
        await release_high.wait()
        return HookDecision.intervene(
            "high priority feedback",
            additional_knowledge={"winner": "high"},
            restore_to_checkpoint=False,
        )

    async def low_priority(event, token):
        low_done.set()
        return HookDecision.intervene(
            "low priority feedback",
            additional_knowledge={"winner": "low", "extra": "kept"},
            restore_to_checkpoint=True,
        )

    manager.register(high_priority, options=HookOptions(id="high", priority=10))
    manager.register(low_priority, options=HookOptions(id="low", priority=0))
    await manager.start_session()
    try:
        set_tool_call(ctx, "call-priority")
        checkpoint = await manager.begin_action("edit_code", ctx)
        assert checkpoint is not None
        await asyncio.wait_for(
            manager.finish_action(
                checkpoint=checkpoint,
                action_args={},
                result="diff_0",
                current_task_state=ctx.deps,
            ),
            1,
        )
        await manager.protocol_finalized("call-priority", [])
        await asyncio.wait_for(low_done.wait(), 1)
        assert manager.pop_intervention() is None

        release_high.set()
        request = await wait_for_intervention(manager)
        assert request.decision.instruction is not None
        assert request.decision.instruction.startswith("high priority feedback")
        assert "low priority feedback" in request.decision.instruction
        assert request.decision.additional_knowledge == {
            "winner": "high",
            "extra": "kept",
        }
        assert request.decision.restore_to_checkpoint is False
    finally:
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_run_timeout_becomes_policy_intervention(tmp_path):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(
        ordered_config(run_timeout_seconds=0.02, cleanup_seconds=0.5)
    )
    cancelled = asyncio.Event()

    async def hook(event, token):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    manager.register(
        hook,
        options=HookOptions(failure_policy="intervene"),
    )
    await manager.start_session()
    try:
        set_tool_call(ctx, "call-timeout")
        checkpoint = await manager.begin_action("edit_code", ctx)
        assert checkpoint is not None
        await manager.finish_action(
            checkpoint=checkpoint,
            action_args={},
            result="diff_0",
            current_task_state=ctx.deps,
        )
        await manager.protocol_finalized("call-timeout", [])
        await manager.final_drain()

        await asyncio.wait_for(cancelled.wait(), 1)
        request = manager.pop_intervention()
        assert request is not None
        assert request.decision.restore_to_checkpoint is False
        assert "did not complete successfully" in (request.decision.instruction or "")
        assert any(
            item["event"] == "hook_job_timed_out" for item in manager.diagnostics()
        )
    finally:
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_protocol_finalization_rejects_action_before_finish(tmp_path):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(ordered_config(max_unretired_actions=2))
    await manager.start_session()
    try:
        set_tool_call(ctx, "call-running")
        checkpoint_1 = await manager.begin_action("edit_code", ctx)
        assert checkpoint_1 is not None

        with pytest.raises(ActionHookSchedulerError, match="state 'running'"):
            await manager.protocol_finalized("call-running", [])

        set_tool_call(ctx, "call-waiting")
        begin_2 = asyncio.create_task(manager.begin_action("edit_code", ctx))
        await asyncio.sleep(0)
        assert not begin_2.done()

        await manager.invalidate_action(checkpoint_1)
        checkpoint_2 = await asyncio.wait_for(begin_2, 1)
        assert checkpoint_2 is not None
        await manager.invalidate_action(checkpoint_2)
    finally:
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_duplicate_finalization_cannot_release_later_action_lock(
    tmp_path,
):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(
        ordered_config(max_concurrent_runs=1, max_unretired_actions=3)
    )
    release_first = asyncio.Event()

    async def hook(event, token):
        await release_first.wait()
        return HookDecision.noop()

    manager.register(hook)
    await manager.start_session()
    try:
        set_tool_call(ctx, "call-finalized")
        checkpoint_1 = await manager.begin_action("edit_code", ctx)
        assert checkpoint_1 is not None
        await manager.finish_action(
            checkpoint=checkpoint_1,
            action_args={},
            result="diff_1",
            current_task_state=ctx.deps,
        )
        await manager.protocol_finalized("call-finalized", [])

        set_tool_call(ctx, "call-owner")
        checkpoint_2 = await manager.begin_action("edit_code", ctx)
        assert checkpoint_2 is not None

        with pytest.raises(
            ActionHookSchedulerError,
            match="state 'protocol_finalized'",
        ):
            await manager.protocol_finalized("call-finalized", [])

        set_tool_call(ctx, "call-waiting")
        begin_3 = asyncio.create_task(manager.begin_action("edit_code", ctx))
        await asyncio.sleep(0)
        assert not begin_3.done()

        await manager.invalidate_action(checkpoint_2)
        checkpoint_3 = await asyncio.wait_for(begin_3, 1)
        assert checkpoint_3 is not None
        await manager.invalidate_action(checkpoint_3)

        release_first.set()
        await manager.final_drain()
    finally:
        release_first.set()
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_unretired_limit_blocks_next_action_before_admission(tmp_path):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(
        ordered_config(max_concurrent_runs=1, max_unretired_actions=1)
    )
    release = asyncio.Event()

    async def hook(event, token):
        await release.wait()
        return HookDecision.noop()

    manager.register(hook)
    await manager.start_session()
    try:
        set_tool_call(ctx, "call-window-1")
        checkpoint_1 = await manager.begin_action("edit_code", ctx)
        assert checkpoint_1 is not None
        await manager.finish_action(
            checkpoint=checkpoint_1,
            action_args={},
            result="diff_1",
            current_task_state=ctx.deps,
        )
        await manager.protocol_finalized("call-window-1", [])

        set_tool_call(ctx, "call-window-2")
        begin_2 = asyncio.create_task(manager.begin_action("edit_code", ctx))
        await asyncio.sleep(0)
        assert not begin_2.done()

        release.set()
        checkpoint_2 = await asyncio.wait_for(begin_2, 1)
        assert checkpoint_2 is not None
        await manager.finish_action(
            checkpoint=checkpoint_2,
            action_args={},
            result="diff_2",
            current_task_state=ctx.deps,
        )
        await manager.protocol_finalized("call-window-2", [])
        await manager.final_drain()
    finally:
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_invalidated_action_releases_lock_and_window_without_hooks(
    tmp_path,
):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(ordered_config(max_unretired_actions=1))
    calls = 0

    async def hook(event, token):
        nonlocal calls
        calls += 1
        return HookDecision.noop()

    manager.register(hook)
    await manager.start_session()
    try:
        set_tool_call(ctx, "call-invalidated")
        invalidated = await manager.begin_action("edit_code", ctx)
        assert invalidated is not None
        await manager.invalidate_action(invalidated)

        set_tool_call(ctx, "call-after-invalidation")
        replacement = await asyncio.wait_for(
            manager.begin_action("edit_code", ctx),
            1,
        )
        assert replacement is not None
        assert replacement.action_seq == 2
        assert calls == 0
        await asyncio.wait_for(
            manager.finish_action(
                checkpoint=replacement,
                action_args={},
                result="diff_2",
                current_task_state=ctx.deps,
            ),
            1,
        )
        await asyncio.wait_for(
            manager.protocol_finalized("call-after-invalidation", []),
            1,
        )
        await asyncio.wait_for(manager.final_drain(), 1)
        assert calls == 1
    finally:
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_builtin_barrier_waits_for_prior_gate(tmp_path):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(ordered_config())
    release = asyncio.Event()

    async def hook(event, token):
        await release.wait()
        return HookDecision.noop()

    manager.register(hook, options=HookOptions(actions=frozenset({"edit_code"})))
    await manager.start_session()
    try:
        set_tool_call(ctx, "call-before-barrier")
        checkpoint = await manager.begin_action("edit_code", ctx)
        assert checkpoint is not None
        await manager.finish_action(
            checkpoint=checkpoint,
            action_args={},
            result="diff_0",
            current_task_state=ctx.deps,
        )
        await manager.protocol_finalized("call-before-barrier", [])

        barrier = asyncio.create_task(manager.before_tool_approval("search_code"))
        await asyncio.sleep(0)
        assert not barrier.done()
        release.set()
        await asyncio.wait_for(barrier, 1)
    finally:
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_prepare_intervention_awaits_later_hook_quiescence(tmp_path):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(ordered_config())
    release_first = asyncio.Event()
    later_started = asyncio.Event()
    later_cancelled = asyncio.Event()

    async def hook(event, token):
        if event.action_seq == 1:
            await release_first.wait()
            return HookDecision.intervene("restore before later work")
        later_started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            later_cancelled.set()
            raise

    manager.register(hook)
    await manager.start_session()
    try:
        set_tool_call(ctx, "call-quiesce-1")
        first = await manager.begin_action("edit_code", ctx)
        assert first is not None
        await manager.finish_action(
            checkpoint=first,
            action_args={},
            result="diff_1",
            current_task_state=ctx.deps,
        )
        await manager.protocol_finalized("call-quiesce-1", [])

        set_tool_call(ctx, "call-quiesce-2")
        second = await manager.begin_action("edit_code", ctx)
        assert second is not None
        await manager.finish_action(
            checkpoint=second,
            action_args={},
            result="diff_2",
            current_task_state=ctx.deps,
        )
        await manager.protocol_finalized("call-quiesce-2", [])
        await later_started.wait()
        release_first.set()
        request = await wait_for_intervention(manager)

        await manager.prepare_intervention(request)
        assert later_cancelled.is_set()
        manager.reset_runtime(preserve_snapshot_id=request.checkpoint.id)
    finally:
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_selected_intervention_cancels_queued_later_hook(tmp_path):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(
        ordered_config(max_concurrent_runs=1, max_unretired_actions=2)
    )
    release_first = asyncio.Event()
    later_started = asyncio.Event()

    async def hook(event, token):
        if event.action_seq == 1:
            await release_first.wait()
            return HookDecision.intervene("stop queued later hook")
        later_started.set()
        return HookDecision.noop()

    manager.register(hook)
    await manager.start_session()
    try:
        set_tool_call(ctx, "call-queued-1")
        first = await manager.begin_action("edit_code", ctx)
        assert first is not None
        await manager.finish_action(
            checkpoint=first,
            action_args={},
            result="diff_1",
            current_task_state=ctx.deps,
        )
        await manager.protocol_finalized("call-queued-1", [])

        set_tool_call(ctx, "call-queued-2")
        second = await manager.begin_action("edit_code", ctx)
        assert second is not None
        finish_second = asyncio.create_task(
            manager.finish_action(
                checkpoint=second,
                action_args={},
                result="diff_2",
                current_task_state=ctx.deps,
            )
        )
        await asyncio.sleep(0)
        assert not finish_second.done()

        release_first.set()
        request = await wait_for_intervention(manager)
        await asyncio.wait_for(finish_second, 1)
        assert not later_started.is_set()
        await manager.invalidate_action(second)
        await manager.prepare_intervention(request)
    finally:
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_clean_final_drain_reopens_admission(tmp_path):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(ordered_config(finalize_seconds=0.0))

    async def hook(event, token):
        return HookDecision.noop()

    manager.register(hook)
    await manager.start_session()
    try:
        for index in (1, 2):
            tool_call_id = f"call-drain-{index}"
            set_tool_call(ctx, tool_call_id)
            checkpoint = await manager.begin_action("edit_code", ctx)
            assert checkpoint is not None
            await manager.finish_action(
                checkpoint=checkpoint,
                action_args={},
                result=f"diff_{index}",
                current_task_state=ctx.deps,
            )
            await manager.protocol_finalized(tool_call_id, [])
            await asyncio.sleep(0)
            await manager.final_drain()
    finally:
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_close_awaits_hook_cancellation(tmp_path):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(ordered_config())
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def hook(event, token):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    manager.register(hook)
    await manager.start_session()
    set_tool_call(ctx, "call-close")
    checkpoint = await manager.begin_action("edit_code", ctx)
    assert checkpoint is not None
    await manager.finish_action(
        checkpoint=checkpoint,
        action_args={},
        result="diff_0",
        current_task_state=ctx.deps,
    )
    await started.wait()

    await manager.cancel_and_close()
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_ordered_close_reports_release_failure_discovered_during_cleanup(
    tmp_path,
):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(ordered_config())
    started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    finish_cancellation = asyncio.Event()
    analysis_workspace = None

    async def hook(event, token):
        nonlocal analysis_workspace
        analysis_workspace = event.analysis_workspace
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError as cancellation:
            cancellation_seen.set()
            await finish_cancellation.wait()
            raise ActionHookResourceReleaseError(
                "provider close failed during session cancellation"
            ) from cancellation

    manager.register(hook, options=HookOptions(id="close-release-race"))
    await manager.start_session()
    set_tool_call(ctx, "call-close-release-race")
    checkpoint = await manager.begin_action("edit_code", ctx)
    assert checkpoint is not None
    await manager.finish_action(
        checkpoint=checkpoint,
        action_args={},
        result="diff",
        current_task_state=ctx.deps,
    )
    await asyncio.wait_for(started.wait(), 1)
    session = manager._ordered_session
    assert session is not None

    # Force the worker to quiesce before the callback finishes. The callback's
    # resource-release failure is then discoverable only by record cleanup.
    session.request_cancel()
    await asyncio.wait_for(cancellation_seen.wait(), 1)
    for worker in session._workers:
        worker.cancel()
    await asyncio.gather(*session._workers, return_exceptions=True)

    close_task = asyncio.create_task(manager.cancel_and_close())
    await asyncio.sleep(0)
    finish_cancellation.set()
    with pytest.raises(ActionHookSchedulerError, match="did not confirm release"):
        await close_task

    assert session.closed is True
    assert manager._ordered_session is None
    assert analysis_workspace is not None
    assert analysis_workspace.exists()
    assert session._retained_revisions
    assert manager.diagnostics()[-1]["event"] == "session_finalization_completed"
    assert manager.diagnostics()[-1]["success"] is False
    scheduler_module.shutil.rmtree(analysis_workspace.parent)


@pytest.mark.asyncio
async def test_ordered_close_fails_if_cancelled_hook_does_not_terminate(tmp_path):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_ordered_scheduler(ordered_config(cleanup_seconds=0.02))
    started = asyncio.Event()
    release = asyncio.Event()
    captured_event = None

    async def hook(event, token):
        nonlocal captured_event
        captured_event = event
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        return HookDecision.noop()

    manager.register(hook)
    await manager.start_session()
    set_tool_call(ctx, "call-stubborn-close")
    checkpoint = await manager.begin_action("edit_code", ctx)
    assert checkpoint is not None
    await manager.finish_action(
        checkpoint=checkpoint,
        action_args={},
        result="diff",
        current_task_state=ctx.deps,
    )
    await asyncio.wait_for(started.wait(), 1)
    session = manager._ordered_session
    assert session is not None
    job = next(iter(session._jobs.values()))
    assert job.callback_task is not None

    with pytest.raises(
        ActionHookSchedulerError,
        match="callbacks did not terminate",
    ):
        await manager.cancel_and_close()

    assert session.closed is True
    assert manager._ordered_session is None
    assert captured_event is not None
    assert captured_event.analysis_workspace.exists()
    assert any(
        event["event"] == "action_hook_callback_cleanup_timed_out"
        for event in manager.diagnostics()
    )
    assert manager.diagnostics()[-1]["event"] == "session_finalization_completed"
    assert manager.diagnostics()[-1]["success"] is False

    release.set()
    await asyncio.wait_for(job.callback_task, 1)
    if captured_event.current_filesystem_snapshot is not None:
        cleanup_filesystem_snapshot(captured_event.current_filesystem_snapshot)
    scheduler_module.shutil.rmtree(
        captured_event.analysis_workspace.parent,
        ignore_errors=True,
    )


@pytest.mark.asyncio
async def test_ordered_close_does_not_redeliver_observed_cleanup_error(tmp_path):
    manager = ActionHookManager()
    manager.configure_ordered_scheduler(ordered_config())
    await manager.start_session()
    session = manager._ordered_session
    assert session is not None
    cleanup_error = ActionHookSchedulerError("observed cleanup failure")
    session._cleanup_error = cleanup_error
    session._fatal_error = cleanup_error

    with pytest.raises(ActionHookSchedulerError, match="observed cleanup failure"):
        session._raise_if_fatal_locked()

    await manager.cancel_and_close()
    assert manager._ordered_session is None
    assert manager.diagnostics()[-1]["event"] == "session_finalization_completed"
    assert manager.diagnostics()[-1]["success"] is False


@pytest.mark.asyncio
async def test_ordered_rejects_synchronous_python_hook(tmp_path):
    manager = ActionHookManager()
    manager.configure_ordered_scheduler(ordered_config())
    manager.register(lambda event, token: HookDecision.noop())

    with pytest.raises(TypeError, match="requires asynchronous Python hooks"):
        await manager.start_session()


@pytest.mark.parametrize(
    "options",
    [
        {"id": ""},
        {"actions": frozenset({"unknown"})},
        {"mode": "unknown"},
        {"execution": "thread"},
        {"failure_policy": "unknown"},
        {"priority": True},
        {"timeout_seconds": float("inf")},
        {"can_restore": "yes"},
        {"requires_speculation_barrier": 1},
    ],
)
def test_hook_options_reject_invalid_scheduler_metadata(options):
    with pytest.raises(ValueError):
        HookOptions(**options)


@pytest.mark.asyncio
async def test_ordered_snapshot_failure_aborts_before_hook_execution(
    monkeypatch,
    tmp_path,
):
    import useagent.action_hook_scheduler as scheduler_module

    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(ordered_config())
    called = False

    async def hook(event, token):
        nonlocal called
        called = True
        return HookDecision.noop()

    def fail_copy(*args, **kwargs):
        raise OSError("snapshot copy failed")

    manager.register(hook)
    await manager.start_session()
    monkeypatch.setattr(scheduler_module.shutil, "copytree", fail_copy)
    try:
        set_tool_call(ctx, "call-snapshot-failure")
        checkpoint = await manager.begin_action("edit_code", ctx)
        assert checkpoint is not None
        with pytest.raises(ActionHookSchedulerError, match="snapshot creation failed"):
            await manager.finish_action(
                checkpoint=checkpoint,
                action_args={},
                result="diff_0",
                current_task_state=ctx.deps,
            )
        assert called is False
    finally:
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_snapshot_cancellation_awaits_worker_and_releases_action(
    monkeypatch,
    tmp_path,
):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_ordered_scheduler(ordered_config())
    started = threading.Event()
    release = threading.Event()
    cleaned = []

    def create_snapshot(task_state):
        started.set()
        release.wait(timeout=2)
        return scheduler_module.FilesystemSnapshot(
            root=tmp_path,
            snapshot_root=tmp_path / "owned-snapshot",
        )

    def cleanup_snapshot(snapshot):
        cleaned.append(snapshot)

    monkeypatch.setattr(
        scheduler_module,
        "create_filesystem_snapshot",
        create_snapshot,
    )
    monkeypatch.setattr(
        scheduler_module,
        "cleanup_filesystem_snapshot",
        cleanup_snapshot,
    )

    async def hook(event, token):
        return HookDecision.noop()

    manager.register(hook)
    await manager.start_session()
    try:
        set_tool_call(ctx, "call-cancelled-snapshot")
        checkpoint = await manager.begin_action("edit_code", ctx)
        assert checkpoint is not None
        finish = asyncio.create_task(
            manager.finish_action(
                checkpoint=checkpoint,
                action_args={},
                result="diff_0",
                current_task_state=ctx.deps,
            )
        )
        while not started.is_set():
            await asyncio.sleep(0)

        finish.cancel()
        await asyncio.sleep(0)
        assert not finish.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(finish, timeout=2)
        assert len(cleaned) == 1

        set_tool_call(ctx, "call-after-cancelled-snapshot")
        next_checkpoint = await asyncio.wait_for(
            manager.begin_action("edit_code", ctx),
            timeout=1,
        )
        assert next_checkpoint is not None
        await manager.invalidate_action(next_checkpoint)
    finally:
        release.set()
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_hook_events_isolate_mutable_result_and_checkpoint(tmp_path):
    ctx = make_context(tmp_path)
    ctx.deps.additional_knowledge["original"] = "kept"
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(ordered_config(max_concurrent_runs=1))
    second_seen = asyncio.Event()

    async def mutating_hook(event, token):
        event.result["items"].append("mutated")
        event.checkpoint.task_state.additional_knowledge["hook"] = "leaked"
        return HookDecision.noop()

    async def observing_hook(event, token):
        assert event.result == {"items": ["original"]}
        assert event.checkpoint.task_state.additional_knowledge == {"original": "kept"}
        second_seen.set()
        return HookDecision.noop()

    manager.register(
        mutating_hook,
        options=HookOptions(id="mutator", priority=1),
    )
    manager.register(
        observing_hook,
        options=HookOptions(id="observer", priority=0),
    )
    await manager.start_session()
    try:
        set_tool_call(ctx, "call-isolated-events")
        checkpoint = await manager.begin_action("edit_code", ctx)
        assert checkpoint is not None
        result = {"items": ["original"]}
        await manager.finish_action(
            checkpoint=checkpoint,
            action_args={},
            result=result,
            current_task_state=ctx.deps,
        )
        await manager.protocol_finalized("call-isolated-events", [])
        await manager.final_drain()

        assert second_seen.is_set()
        assert result == {"items": ["original"]}
        assert checkpoint.task_state.additional_knowledge == {"original": "kept"}
    finally:
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_terminal_jobs_are_evicted_after_each_retirement(tmp_path):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(ordered_config())

    async def hook(event, token):
        return HookDecision.noop()

    manager.register(hook)
    await manager.start_session()
    try:
        for index in range(8):
            tool_call_id = f"call-evict-{index}"
            set_tool_call(ctx, tool_call_id)
            checkpoint = await manager.begin_action("edit_code", ctx)
            assert checkpoint is not None
            await manager.finish_action(
                checkpoint=checkpoint,
                action_args={"index": index},
                result={"index": index},
                current_task_state=ctx.deps,
            )
            await manager.protocol_finalized(tool_call_id, [])
            await manager.final_drain()
            assert manager._ordered_session is not None
            assert manager._ordered_session._jobs == {}
    finally:
        await manager.cancel_and_close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["invalidate", "reset", "ignore"])
async def test_ordered_cancelled_hook_quiesces_before_snapshot_cleanup(
    operation,
    tmp_path,
):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(ordered_config(cleanup_seconds=1.0))
    started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release_cancellation = asyncio.Event()
    callback_terminated = asyncio.Event()
    analysis_workspaces = []

    async def hook(event, token):
        assert event.analysis_workspace is not None
        analysis_workspaces.append(event.analysis_workspace)
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release_cancellation.wait()
            assert event.analysis_workspace.exists()
            callback_terminated.set()
            raise

    manager.register(hook)
    await manager.start_session()
    try:
        set_tool_call(ctx, f"call-cancel-cleanup-{operation}")
        checkpoint = await manager.begin_action("edit_code", ctx)
        assert checkpoint is not None
        await manager.finish_action(
            checkpoint=checkpoint,
            action_args={},
            result="diff",
            current_task_state=ctx.deps,
        )
        await asyncio.wait_for(started.wait(), 1)
        analysis_workspace = analysis_workspaces[0]

        if operation == "invalidate":
            await manager.invalidate_action(checkpoint)
        elif operation == "reset":
            manager.reset_runtime()
        else:
            manager.ignore_future_interventions("test intervention limit")

        await asyncio.wait_for(cancellation_seen.wait(), 1)
        await asyncio.sleep(0)
        assert analysis_workspace.exists()

        release_cancellation.set()
        await asyncio.wait_for(callback_terminated.wait(), 1)

        async def wait_for_cleanup():
            while analysis_workspace.exists():
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_cleanup(), 1)
    finally:
        release_cancellation.set()
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_snapshot_budget_includes_restore_snapshot(
    monkeypatch,
    tmp_path,
):
    ctx = make_context(tmp_path)
    restore_parent = tmp_path.parent / f"{tmp_path.name}-restore-budget"
    restore_root = restore_parent / "tree"
    restore_root.mkdir(parents=True)
    (restore_root / "rollback.bin").write_bytes(b"r" * 4096)
    restore_snapshot = scheduler_module.FilesystemSnapshot(
        root=tmp_path,
        snapshot_root=restore_root,
    )
    root_size = scheduler_module._tree_size(tmp_path)
    restore_size = scheduler_module._tree_size(restore_root)
    aggregate_limit = root_size + restore_size - 1
    assert aggregate_limit > root_size

    monkeypatch.setattr(
        scheduler_module,
        "create_filesystem_snapshot",
        lambda _task_state: restore_snapshot,
    )

    manager = ActionHookManager()
    manager.configure_ordered_scheduler(
        ordered_config(snapshot_budget_mib=aggregate_limit / (1024 * 1024))
    )
    hook_called = False

    async def hook(event, token):
        nonlocal hook_called
        hook_called = True
        return HookDecision.noop()

    manager.register(hook)
    await manager.start_session()
    try:
        set_tool_call(ctx, "call-aggregate-budget")
        checkpoint = await manager.begin_action("edit_code", ctx)
        assert checkpoint is not None
        with pytest.raises(ActionHookSchedulerError, match="snapshot budget exceeded"):
            await manager.finish_action(
                checkpoint=checkpoint,
                action_args={},
                result="diff",
                current_task_state=ctx.deps,
            )
        assert hook_called is False
        assert not restore_parent.exists()
        assert manager._ordered_session is not None
        assert manager._ordered_session._snapshot_bytes == 0
    finally:
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_preserved_restore_snapshot_stays_budgeted_until_cleanup(
    tmp_path,
):
    ctx = make_context(tmp_path)
    (tmp_path / "rollback-payload.bin").write_bytes(b"rollback" * 1024)
    manager = ActionHookManager()
    manager.configure_ordered_scheduler(ordered_config())

    async def hook(event, token):
        return HookDecision.intervene("restore this action")

    manager.register(hook)
    await manager.start_session()
    try:
        set_tool_call(ctx, "call-preserve-budget")
        checkpoint = await manager.begin_action("edit_code", ctx)
        assert checkpoint is not None
        await manager.finish_action(
            checkpoint=checkpoint,
            action_args={},
            result="diff",
            current_task_state=ctx.deps,
        )
        await manager.protocol_finalized("call-preserve-budget", [])
        request = await wait_for_intervention(manager)
        session = manager._ordered_session
        assert session is not None

        async def wait_for_analysis_cleanup():
            while session._snapshot_bytes != sum(
                session._restore_snapshot_bytes.values()
            ):
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_analysis_cleanup(), 1)
        restore_bytes = sum(session._restore_snapshot_bytes.values())
        assert restore_bytes > 0
        assert session._snapshot_bytes == restore_bytes

        await manager.prepare_intervention(request)
        manager.reset_runtime(preserve_snapshot_id=checkpoint.id)
        assert checkpoint.id in session._preserved_snapshots
        assert session._snapshot_bytes == restore_bytes

        manager.cleanup_filesystem_snapshot(checkpoint.id)

        async def wait_for_restore_cleanup():
            while session._snapshot_bytes:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_restore_cleanup(), 1)
        assert session._restore_snapshot_bytes == {}
    finally:
        await manager.cancel_and_close()


@pytest.mark.asyncio
async def test_ordered_cleanup_failure_stays_budgeted_and_fails_close(
    monkeypatch,
    tmp_path,
):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(ordered_config())
    captured_event = None

    async def hook(event, token):
        nonlocal captured_event
        captured_event = event
        return HookDecision.noop()

    manager.register(hook)
    await manager.start_session()
    set_tool_call(ctx, "call-cleanup-failure")
    checkpoint = await manager.begin_action("edit_code", ctx)
    assert checkpoint is not None
    await manager.finish_action(
        checkpoint=checkpoint,
        action_args={},
        result="diff",
        current_task_state=ctx.deps,
    )
    session = manager._ordered_session
    assert session is not None
    while captured_event is None:
        await asyncio.sleep(0)
    assert captured_event is not None
    analysis_parent = captured_event.analysis_workspace.parent
    assert session._snapshot_bytes > 0

    def fail_cleanup(path):
        assert path == analysis_parent
        raise PermissionError("simulated revision cleanup failure")

    monkeypatch.setattr(scheduler_module, "_remove_tree_checked", fail_cleanup)
    await manager.protocol_finalized("call-cleanup-failure", [])
    with pytest.raises(ActionHookSchedulerError, match="resource cleanup failed"):
        await manager.cancel_and_close()

    assert session._snapshot_bytes > 0
    assert analysis_parent.exists()
    scheduler_module.shutil.rmtree(analysis_parent, ignore_errors=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["begin_action", "tool_approval"])
async def test_ordered_delivery_attaches_current_messages_to_no_restore_request(
    delivery,
    tmp_path,
):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.configure_ordered_scheduler(ordered_config())
    release_first = asyncio.Event()

    async def hook(event, token):
        if event.action_seq == 1:
            await release_first.wait()
            return HookDecision.intervene(
                "continue from the current trajectory",
                restore_to_checkpoint=False,
            )
        return HookDecision.noop()

    manager.register(hook)
    await manager.start_session()
    try:
        set_tool_call(ctx, "call-current-messages-1")
        first = await manager.begin_action("edit_code", ctx)
        assert first is not None
        await manager.finish_action(
            checkpoint=first,
            action_args={},
            result="diff-1",
            current_task_state=ctx.deps,
        )
        await manager.protocol_finalized("call-current-messages-1", [])

        set_tool_call(ctx, "call-current-messages-2")
        second = await manager.begin_action("edit_code", ctx)
        assert second is not None
        await manager.finish_action(
            checkpoint=second,
            action_args={},
            result="diff-2",
            current_task_state=ctx.deps,
        )
        await manager.protocol_finalized("call-current-messages-2", [])

        current_messages = [
            ModelRequest(parts=[]),
            ModelResponse(parts=[TextPart(content="Both edits completed.")]),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="edit_code",
                        args={"instruction": "third edit"},
                        tool_call_id="call-current-messages-3",
                    )
                ]
            ),
        ]
        ctx.messages[:] = current_messages
        release_first.set()

        async def wait_for_selection():
            while not any(
                item["event"] == "intervention_selected"
                for item in manager.diagnostics()
            ):
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_selection(), 1)
        with pytest.raises(action_hooks_module.ActionIntervention) as exc_info:
            if delivery == "begin_action":
                set_tool_call(ctx, "call-current-messages-3")
                await manager.begin_action("edit_code", ctx)
            else:
                await manager.before_tool_approval(
                    "search_code",
                    current_messages=ctx.messages,
                )

        assert exc_info.value.request.decision.restore_to_checkpoint is False
        assert exc_info.value.request.replay_messages == current_messages
        assert exc_info.value.request.replay_messages is not ctx.messages
    finally:
        release_first.set()
        await manager.cancel_and_close()
