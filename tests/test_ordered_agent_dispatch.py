from types import SimpleNamespace

import pytest
from pydantic_ai import Agent, DeferredToolRequests, Tool
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.test import TestModel

from useagent.action_hooks import (
    ActionCheckpoint,
    ActionInterventionRequest,
    HookDecision,
)
from useagent.agents.meta import agent as meta_agent_module
from useagent.pydantic_models.task_state import TaskState
from useagent.state.git_repo import GitRepository
from useagent.tasks.test_task import TestTask
from useagent import task_runner


def make_task_state(tmp_path) -> TaskState:
    task = TestTask(root=tmp_path)
    return TaskState(task=task, git_repo=GitRepository(local_path=tmp_path))


class FakeOrderedManager:
    def __init__(self) -> None:
        self.before_approvals: list[str] = []
        self.protocol_calls: list[tuple[str, list[object]]] = []
        self.final_drains = 0

    async def before_tool_approval(
        self,
        tool_name: str,
        current_messages=None,
    ) -> None:
        self.before_approvals.append(tool_name)

    async def protocol_finalized(
        self, tool_call_id: str, messages: list[object]
    ) -> None:
        self.protocol_calls.append((tool_call_id, messages))

    async def final_drain(self) -> None:
        self.final_drains += 1

    def raise_if_intervention(self, current_messages=None) -> None:
        return None

    def pop_action_error(self, tool_call_id: str):
        return None


@pytest.mark.asyncio
async def test_ordered_driver_executes_one_stateful_sibling_and_finalizes_protocol(
    monkeypatch,
    tmp_path,
):
    calls: list[str] = []

    async def view_command_history() -> str:
        calls.append("view_command_history")
        return "history"

    async def edit_code() -> str:
        calls.append("edit_code")
        return "edited"

    async def search_code() -> str:
        calls.append("search_code")
        return "searched"

    manager = FakeOrderedManager()
    monkeypatch.setattr(meta_agent_module, "ACTION_HOOK_MANAGER", manager)
    agent = Agent(
        TestModel(call_tools=["view_command_history", "edit_code", "search_code"]),
        deps_type=TaskState,
        tools=[
            Tool(view_command_history),
            Tool(edit_code, requires_approval=True),
            Tool(search_code, requires_approval=True),
        ],
        output_type=[str, DeferredToolRequests],
        model_settings={"parallel_tool_calls": False},
    )

    result = await meta_agent_module._run_meta_agent_turn(
        agent,
        "Run the tools.",
        make_task_state(tmp_path),
        None,
    )

    assert isinstance(result.output, str)
    assert calls == ["view_command_history", "edit_code"]
    assert manager.before_approvals == ["edit_code"]
    assert manager.final_drains == 1
    assert len(manager.protocol_calls) == 1

    selected_id, protocol_messages = manager.protocol_calls[0]
    assert selected_id.endswith("__edit_code")
    assert isinstance(protocol_messages[-1], ModelRequest)
    returns = [
        part for part in protocol_messages[-1].parts if isinstance(part, ToolReturnPart)
    ]
    assert [part.tool_name for part in returns] == [
        "view_command_history",
        "edit_code",
        "search_code",
    ]
    assert returns[0].content == "history"
    assert returns[1].content == "edited"
    assert "Resubmit this tool call" in returns[2].content


def test_ordered_approval_selection_uses_model_response_part_order():
    first = ToolCallPart(
        tool_name="edit_code",
        args={},
        tool_call_id="call-edit",
    )
    second = ToolCallPart(
        tool_name="search_code",
        args={},
        tool_call_id="call-search",
    )
    requests = DeferredToolRequests(approvals=[second, first])

    calls = meta_agent_module._ordered_approval_calls(
        requests,
        [ModelResponse(parts=[first, second])],
    )

    assert [call.tool_call_id for call in calls] == ["call-edit", "call-search"]


def test_intervention_replay_matches_exact_live_tool_call_id(tmp_path):
    initial = ModelRequest(parts=[])
    calls = ModelResponse(
        parts=[
            ToolCallPart(
                tool_name="search_code",
                args={"instruction": "first"},
                tool_call_id="call-search-1",
            ),
            ToolCallPart(
                tool_name="search_code",
                args={"instruction": "second"},
                tool_call_id="call-search-2",
            ),
        ]
    )
    returns = ModelRequest(
        parts=[
            ToolReturnPart(
                tool_name="search_code",
                content="first result",
                tool_call_id="call-search-1",
            ),
            ToolReturnPart(
                tool_name="search_code",
                content="second result",
                tool_call_id="call-search-2",
            ),
        ]
    )
    later = ModelResponse(parts=[TextPart(content="later trajectory")])
    state = make_task_state(tmp_path)
    request = ActionInterventionRequest(
        checkpoint=ActionCheckpoint(
            id="checkpoint",
            action_name="search_code",
            task_state=state,
            # A real Pydantic AI RunContext already contains the triggering
            # ModelResponse when the tool wrapper starts.
            messages=[initial, calls],
            bash_history_length=0,
            generation=0,
            tool_call_id="call-search-2",
        ),
        decision=HookDecision.intervene("Use the second result."),
        replay_messages=[initial, calls, returns, later],
    )

    replay = meta_agent_module._message_history_for_action_hook_intervention(request)

    assert replay == [initial, calls, returns]


def test_task_runner_reraises_ordered_scheduler_failures(monkeypatch, tmp_path):
    async def fail_run(*args, **kwargs):
        raise RuntimeError("ordered scheduler integrity failure")

    manager = SimpleNamespace(
        write_diagnostics=lambda path: path.write_text(""),
        clear_diagnostics=lambda: None,
    )
    monkeypatch.setattr(task_runner, "_run", fail_run)
    monkeypatch.setattr(task_runner, "ACTION_HOOK_MANAGER", manager)
    monkeypatch.setattr(task_runner, "get_bash_history", lambda: [])
    task = SimpleNamespace(uid="ordered-failure")

    with pytest.raises(RuntimeError, match="ordered scheduler integrity failure"):
        task_runner.run(task, str(tmp_path))
