import asyncio
import inspect
from types import SimpleNamespace

import pytest
from pydantic_ai import RunContext
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
    ActionIntervention,
    ActionInterventionRequest,
    HookCancellationToken,
    HookDecision,
    load_action_hook_spec,
    parse_action_hook_spec_list,
    restore_task_state_from_checkpoint,
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
