import asyncio
import inspect
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic_ai import RunContext
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
import useagent.common.constants as constants
from useagent.agents.meta import agent as meta_agent_module
from useagent.action_hooks import (
    ACTION_HOOK_MANAGER,
    ActionCheckpoint,
    ActionHookEvent,
    ActionHookManager,
    ActionHookPolicy,
    ActionIntervention,
    ActionInterventionRequest,
    HookCancellationToken,
    HookDecision,
    cleanup_filesystem_snapshot,
    create_filesystem_snapshot,
    load_action_hook_spec,
    parse_action_hook_spec_list,
    restore_filesystem_snapshot,
    restore_task_state_from_checkpoint,
    restore_task_state_from_snapshot,
)
from useagent.pydantic_models.task_state import TaskState
from useagent.state.git_repo import GitRepository
from useagent.state.usage_tracker import UsageTracker
from useagent.tasks.test_task import TestTask
from useagent.tools import meta


@pytest.fixture(autouse=True)
def clear_global_hooks():
    ACTION_HOOK_MANAGER.clear_hooks()
    yield
    ACTION_HOOK_MANAGER.clear_hooks()


def make_context(tmp_path) -> RunContext[TaskState]:
    task = TestTask(root=tmp_path)
    state = TaskState(task=task, git_repo=GitRepository(local_path=tmp_path))
    return RunContext(
        deps=state,
        model=TestModel(),
        usage=RunUsage(),
        messages=[],
    )


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


def test_parse_action_hook_spec_list():
    assert parse_action_hook_spec_list("pkg.mod:hook, /tmp/hooks.py:other ,,") == [
        "pkg.mod:hook",
        "/tmp/hooks.py:other",
    ]


@pytest.mark.asyncio
async def test_top_level_action_hook_does_not_block_search_code(monkeypatch, tmp_path):
    ctx = make_context(tmp_path)
    meta.USAGE_TRACKER = UsageTracker()
    started = asyncio.Event()
    release = asyncio.Event()
    seen_actions: list[str] = []

    class FakeSearchAgent:
        name = "SEARCH"

        async def run(self, *args, **kwargs):
            return SimpleNamespace(output=[], usage=lambda: RunUsage())

    async def hook(event, token):
        seen_actions.append(event.action_name)
        started.set()
        await release.wait()
        return None

    monkeypatch.setattr(meta, "init_search_code_agent", lambda: FakeSearchAgent())
    ACTION_HOOK_MANAGER.register(hook)

    result = await asyncio.wait_for(meta.search_code(ctx, "find relevant code"), 1)

    assert result == []
    await asyncio.wait_for(started.wait(), 1)
    assert seen_actions == ["search_code"]
    release.set()


@pytest.mark.asyncio
async def test_hook_intervention_is_queued_after_action(monkeypatch, tmp_path):
    ctx = make_context(tmp_path)
    meta.USAGE_TRACKER = UsageTracker()

    class FakeSearchAgent:
        name = "SEARCH"

        async def run(self, *args, **kwargs):
            return SimpleNamespace(output=[], usage=lambda: RunUsage())

    async def hook(event, token):
        return HookDecision.intervene(
            "Reconsider the previous search with narrower criteria.",
            reason="search was too broad",
            additional_knowledge={"hook.search": "narrower search requested"},
        )

    monkeypatch.setattr(meta, "init_search_code_agent", lambda: FakeSearchAgent())
    ACTION_HOOK_MANAGER.register(hook)

    await meta.search_code(ctx, "find relevant code")
    await asyncio.sleep(0)

    with pytest.raises(ActionIntervention) as exc_info:
        ACTION_HOOK_MANAGER.raise_if_intervention()

    request = exc_info.value.request
    assert request.checkpoint.action_name == "search_code"
    assert request.decision.instruction is not None
    assert request.decision.instruction.startswith("Reconsider")
    assert request.decision.additional_knowledge == {
        "hook.search": "narrower search requested"
    }


@pytest.mark.asyncio
async def test_queued_intervention_stops_next_top_level_action(monkeypatch, tmp_path):
    ctx = make_context(tmp_path)
    meta.USAGE_TRACKER = UsageTracker()

    class FakeSearchAgent:
        name = "SEARCH"

        async def run(self, *args, **kwargs):
            return SimpleNamespace(output=[], usage=lambda: RunUsage())

    async def hook(event, token):
        return HookDecision.intervene("Stop before running another top-level action.")

    monkeypatch.setattr(meta, "init_search_code_agent", lambda: FakeSearchAgent())
    ACTION_HOOK_MANAGER.register(hook)

    await meta.search_code(ctx, "first search")
    await asyncio.sleep(0)

    with pytest.raises(ActionIntervention):
        await meta.search_code(ctx, "second search")


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

    intervention_count, intervention_prompt = (
        meta_agent_module._apply_action_hook_intervention(
            request,
            ctx.deps,
            intervention_count=0,
        )
    )

    assert intervention_count == 1
    assert intervention_prompt is None
    assert any(
        "Future interventions will be ignored" in warning for warning in warnings
    )

    ACTION_HOOK_MANAGER.register(
        lambda event, token: HookDecision.intervene("Apply a future intervention.")
    )
    checkpoint = ACTION_HOOK_MANAGER.create_checkpoint("search_code", ctx)
    assert checkpoint is not None
    ACTION_HOOK_MANAGER.schedule(
        checkpoint=checkpoint,
        action_args={"instruction": "find code"},
        result=[],
        current_task_state=ctx.deps,
    )

    await asyncio.sleep(0)

    assert ACTION_HOOK_MANAGER.pop_intervention() is None
    assert any(
        "Ignoring intervention after search_code" in warning for warning in warnings
    )


def test_restore_task_state_from_checkpoint_restores_in_memory_state(tmp_path):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.register(lambda event, token: None)
    checkpoint = manager.create_checkpoint("search_code", ctx)
    assert checkpoint is not None

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


def test_pending_intervention_captures_interrupted_history(tmp_path):
    ctx = make_context(tmp_path)
    current_message = ModelRequest(parts=[])
    manager = ActionHookManager()
    manager._intervention = ActionInterventionRequest(
        checkpoint=ActionCheckpoint(
            id="checkpoint",
            action_name="probe_environment",
            task_state=ctx.deps,
            messages=[],
            bash_history_length=0,
            generation=0,
        ),
        decision=HookDecision.intervene(
            "Continue from current state.",
            restore_to_checkpoint=False,
        ),
    )

    with pytest.raises(ActionIntervention) as exc_info:
        manager.raise_if_intervention(current_messages=[current_message])

    assert exc_info.value.request.replay_messages == [current_message]


@pytest.mark.asyncio
async def test_restore_policy_downgrades_restore_intervention(tmp_path):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    manager.configure_policy(ActionHookPolicy(allow_restore=False))
    manager.register(
        lambda event, token: HookDecision.intervene("Continue without restore.")
    )
    checkpoint = manager.create_checkpoint("search_code", ctx)
    assert checkpoint is not None

    manager.schedule(
        checkpoint=checkpoint,
        action_args={"instruction": "find code"},
        result=[],
        current_task_state=ctx.deps,
    )
    await asyncio.sleep(0)

    request = manager.pop_intervention()
    assert request is not None
    assert request.decision.restore_to_checkpoint is False
    assert any(
        item["event"] == "restore_downgraded" for item in manager.diagnostics()
    )
    manager.cleanup_filesystem_snapshots()


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
            action_args={"instruction": "find code"},
            result=[],
            error=None,
            checkpoint=checkpoint,
            current_task_state=ctx.deps,
            current_bash_history_length=0,
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
async def test_mid_action_intervention_cancels_running_action(monkeypatch, tmp_path):
    monkeypatch.setattr(constants, "ACTION_HOOK_CANCELLATION_POLL_SECONDS", 0.01)
    ctx = make_context(tmp_path)
    cancelled = asyncio.Event()
    manager = ACTION_HOOK_MANAGER
    manager._intervention = ActionInterventionRequest(
        checkpoint=ActionCheckpoint(
            id="checkpoint",
            action_name="probe_environment",
            task_state=ctx.deps,
            messages=[],
            bash_history_length=0,
            generation=0,
        ),
        decision=HookDecision.intervene("Stop the current action."),
    )

    async def slow_action():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(ActionIntervention):
        await meta._await_with_action_hook_cancellation(ctx, slow_action())

    await asyncio.wait_for(cancelled.wait(), 1)


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

    with pytest.raises(RuntimeError, match="search failed"):
        await meta.search_code(ctx, "find relevant code")

    await asyncio.wait_for(seen.wait(), 1)
    assert isinstance(seen_error[0], RuntimeError)


@pytest.mark.asyncio
async def test_cancel_pending_hook_tasks(tmp_path):
    ctx = make_context(tmp_path)
    manager = ActionHookManager()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def hook(event, token):
        started.set()
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    manager.register(hook)
    checkpoint = manager.create_checkpoint("search_code", ctx)
    assert checkpoint is not None
    manager.schedule(
        checkpoint=checkpoint,
        action_args={"instruction": "find code"},
        result=[],
        current_task_state=ctx.deps,
    )

    await asyncio.wait_for(started.wait(), 1)
    manager.cancel_pending()

    await asyncio.wait_for(cancelled.wait(), 1)
