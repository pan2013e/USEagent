import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from loguru import logger
from pydantic_ai import (
    Agent,
    CallToolsNode,
    DeferredToolRequests,
    DeferredToolResults,
    ModelRequestNode,
    RunContext,
    RunUsage,
    ToolApproved,
    ToolDenied,
)
from pydantic_ai.messages import (
    BaseToolCallPart,
    BaseToolReturnPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
)
from pydantic_ai.run import AgentRunResult
from pydantic_ai.tools import Tool
from pydantic_ai.usage import UsageLimits
from pydantic_graph import End

import useagent.common.constants as constants
from useagent.action_hooks import (
    ACTION_HOOK_MANAGER,
    ActionHookSchedulerError,
    ActionIntervention,
    ActionInterventionRequest,
    restore_filesystem_snapshot,
    restore_task_state_from_checkpoint,
    restore_task_state_from_snapshot,
)
from useagent.common.context_window import fit_messages_into_context_window
from useagent.config import AppConfig, ConfigSingleton
from useagent.microagents.decorators import (
    alias_for_microagents,
    conditional_microagents_triggers,
)
from useagent.microagents.management import load_microagents_from_project_dir
from useagent.pydantic_models.artifacts.git.diff import DiffEntry
from useagent.pydantic_models.common.constrained_types import NonEmptyStr
from useagent.pydantic_models.output.action import Action
from useagent.pydantic_models.output.answer import Answer
from useagent.pydantic_models.output.code_change import CodeChange
from useagent.pydantic_models.provides_output_instructions import (
    ProvidesOutputInstructions,
)
from useagent.pydantic_models.task_state import TaskState
from useagent.pydantic_models.tools.cliresult import CLIResult
from useagent.pydantic_models.tools.errorinfo import ToolErrorInfo
from useagent.state.usage_tracker import UsageTracker, usage_tracker_name
from useagent.tasks.swebench_task import SWEbenchTask
from useagent.tools.bash import (
    get_bash_history,
    init_bash_tool,
    make_bash_tool_for_agent,
)
from useagent.tools.edit import init_edit_tools
from useagent.tools.meta import (  # Agent-State Tools; Agent-Agent Tools
    ORDERED_ACTION_ERROR_PREFIX,
    _gather_checklist,
    _set_usage_tracker,
    advising_on_doubts,
    edit_code,
    execute_tests,
    probe_environment,
    search_code,
    vcs,
    view_command_history,
)

SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.md").read_text()

_NO_DOUBT_PREFIXES = {"none", "no", "n/a", "no doubt", "no doubts"}

_STATEFUL_META_TOOL_NAMES = frozenset(
    {
        "bash_tool",
        "edit_code",
        "search_code",
        "probe_environment",
        "execute_tests",
        "vcs",
    }
)


class OrderedToolDispatchError(RuntimeError):
    """The pinned agent runtime violated the ordered-dispatch contract."""


def _has_real_doubts(doubts: str | None) -> bool:
    if not doubts:
        return False
    cleaned = doubts.strip().rstrip(".").lower()
    return cleaned not in _NO_DOUBT_PREFIXES


async def _apply_action_hook_intervention(
    request: ActionInterventionRequest,
    task_state: TaskState,
    intervention_count: int,
) -> tuple[int, str | None]:
    intervention_count += 1
    max_interventions = constants.MAX_ACTION_HOOK_INTERVENTIONS
    if intervention_count > max_interventions:
        reason = (
            "maximum top-level action hook interventions exceeded "
            f"({intervention_count}>{max_interventions})"
        )
        logger.warning(
            "[ActionHook] Ignoring intervention from checkpoint "
            f"{request.checkpoint.id} after {request.checkpoint.action_name}; "
            f"{reason}. Future interventions will be ignored for this agent loop."
        )
        ACTION_HOOK_MANAGER.ignore_future_interventions(reason)
        return intervention_count, None

    await ACTION_HOOK_MANAGER.prepare_intervention(request)
    logger.info(
        "[ActionHook] Applying intervention from checkpoint "
        f"{request.checkpoint.id} after {request.checkpoint.action_name}"
    )
    ACTION_HOOK_MANAGER.reset_runtime(preserve_snapshot_id=request.checkpoint.id)
    if request.decision.restore_to_checkpoint:
        if request.restore_task_state is None:
            restore_task_state_from_checkpoint(task_state, request.checkpoint)
        else:
            restore_task_state_from_snapshot(
                task_state,
                request.restore_task_state,
                bash_history_length=request.restore_bash_history_length,
            )
        if request.restore_filesystem_snapshot is not None:
            await _run_blocking_restore_owned(
                restore_filesystem_snapshot,
                request.restore_filesystem_snapshot,
            )
    ACTION_HOOK_MANAGER.cleanup_filesystem_snapshot(request.checkpoint.id)
    task_state.additional_knowledge.update(request.decision.additional_knowledge)
    ACTION_HOOK_MANAGER.record_diagnostic(
        "intervention_applied",
        action_name=request.checkpoint.action_name,
        checkpoint_id=request.checkpoint.id,
        restore_to_checkpoint=request.decision.restore_to_checkpoint,
        restored_filesystem=request.restore_filesystem_snapshot is not None
        and request.decision.restore_to_checkpoint,
    )
    return intervention_count, _format_action_hook_intervention_instruction(request)


async def _run_blocking_restore_owned(function: Any, /, *args: Any) -> Any:
    """Do not orphan a destructive restore if the agent task is cancelled."""

    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except BaseException:
            pass
        raise


def _message_history_for_action_hook_intervention(
    request: ActionInterventionRequest,
    completed_messages: list[ModelMessage] | None = None,
) -> list[ModelMessage]:
    if not request.decision.restore_to_checkpoint:
        if completed_messages is not None:
            return completed_messages
        if request.replay_messages is not None:
            return _drop_trailing_tool_call_messages(request.replay_messages)
    if request.replay_messages is not None:
        return _message_history_through_action(
            request.replay_messages,
            request.checkpoint.action_name,
            start_index=len(request.checkpoint.messages),
            tool_call_id=request.checkpoint.tool_call_id,
        )
    if completed_messages is not None:
        return _message_history_through_action(
            completed_messages,
            request.checkpoint.action_name,
            start_index=len(request.checkpoint.messages),
            tool_call_id=request.checkpoint.tool_call_id,
        )
    return _drop_trailing_tool_call_messages(request.checkpoint.messages)


def _message_history_for_suppressed_action_hook_intervention(
    request: ActionInterventionRequest,
) -> list[ModelMessage]:
    """Resume the current trajectory when an intervention is policy-suppressed."""

    messages = request.replay_messages
    if messages is None:
        messages = request.checkpoint.messages
    return _drop_trailing_tool_call_messages(messages)


def _message_history_through_action(
    messages: list[ModelMessage],
    action_name: str,
    *,
    start_index: int = 0,
    tool_call_id: str | None = None,
) -> list[ModelMessage]:
    action_call_ids: set[str] = {tool_call_id} if tool_call_id else set()
    action_return_index: int | None = None
    scan_start = 0 if tool_call_id else start_index

    for index, message in enumerate(messages[scan_start:], start=scan_start):
        if isinstance(message, ModelResponse):
            for part in message.parts or []:
                if isinstance(part, BaseToolCallPart) and (
                    part.tool_call_id == tool_call_id
                    if tool_call_id
                    else part.tool_name == action_name
                ):
                    action_call_ids.add(part.tool_call_id)
        elif isinstance(message, ModelRequest) and action_call_ids:
            if any(
                isinstance(part, (BaseToolReturnPart, RetryPromptPart))
                and part.tool_call_id in action_call_ids
                for part in message.parts or []
            ):
                action_return_index = index
                break

    if action_return_index is None:
        return _drop_trailing_tool_call_messages(messages)
    return messages[: action_return_index + 1]


def _drop_trailing_tool_call_messages(
    messages: list[ModelMessage],
) -> list[ModelMessage]:
    cleaned = list(messages)
    while cleaned:
        message = cleaned[-1]
        if not isinstance(message, ModelResponse):
            break
        if not any(
            isinstance(part, BaseToolCallPart) for part in (message.parts or [])
        ):
            break
        cleaned.pop()
    return cleaned


@conditional_microagents_triggers(load_microagents_from_project_dir())
@alias_for_microagents("META")
def init_agent(
    config: AppConfig | None = None,
    output_type: Literal[CodeChange, Answer, Action] = CodeChange,
) -> Agent[TaskState, CodeChange | Answer]:
    if config is None:
        config = ConfigSingleton.config
    if config is None:
        raise RuntimeError(
            "AppConfig must not be None when initializing the meta agent."
        )

    agent_output_type = [output_type, DeferredToolRequests]
    meta_agent = Agent(
        config.model,
        instructions=SYSTEM_PROMPT,
        deps_type=TaskState,
        model_settings={"parallel_tool_calls": False},
        retries=constants.META_AGENT_RETRIES,
        output_retries=constants.META_AGENT_OUTPUT_RETRIES,
        tools=[
            # Non-Agentic Tools
            Tool(view_command_history, max_retries=2),
            Tool(
                make_bash_tool_for_agent(
                    "META",
                    bash_call_delay_in_seconds=constants.META_AGENT_BASH_TOOL_DELAY,
                ),
                max_retries=4,
                requires_approval=True,
            ),
            # Agent-Agent Tools
            Tool(
                edit_code,
                takes_ctx=True,
                max_retries=constants.EDIT_CODE_RETRIES,
                requires_approval=True,
            ),
            Tool(
                search_code,
                takes_ctx=True,
                max_retries=constants.SEARCH_AGENT_RETRIES,
                requires_approval=True,
            ),
            Tool(
                probe_environment,
                takes_ctx=True,
                max_retries=constants.PROBE_ENVIRONMENT_RETRIES,
                requires_approval=True,
            ),
            Tool(
                execute_tests,
                takes_ctx=True,
                max_retries=constants.EXECUTE_TESTS_RETRIES,
                requires_approval=True,
            ),
            Tool(
                vcs,
                takes_ctx=True,
                max_retries=constants.VCS_AGENT_RETRIES,
                requires_approval=True,
            ),
        ],
        output_type=agent_output_type,
        history_processors=[fit_messages_into_context_window],
    )

    ## This adds the task description to instructions (SYSTEM prompt).
    @meta_agent.instructions
    def add_task_description(ctx: RunContext[TaskState]) -> str:
        """Add a task description to the TaskState.

        Args:
            task_description (str): The description of the task to be added.
        """
        return ctx.deps._task.get_issue_statement()

    ## Depending on the output type (if possible), describes the expected output format.
    @meta_agent.instructions
    def add_output_description() -> str:
        if isinstance(output_type, ProvidesOutputInstructions):
            logger.trace(
                f"[Setup] MetaAgent is expected to output a `{str(output_type)}`, adding output instructions."
            )
            return f"""
            ---------------------------------------------------
            Output:

            You are expected to return a `{str(output_type)}`.
            """ + output_type.get_output_instructions()
        else:
            logger.warning(
                "[Setup] MetaAgent received a output type that did not implement the `get_output_instructions` method and will have less info."
            )
            return f"Output: You are expected to return a `{output_type}`"

    ### Define actions as tools to meta_agent. Each action interfaces to another agent in Pydantic AI.

    ### Action definitions END

    return meta_agent


def _ordered_approval_calls(
    requests: DeferredToolRequests,
    messages: list[ModelMessage],
) -> list[ToolCallPart]:
    if requests.calls:
        raise OrderedToolDispatchError(
            "Ordered Meta dispatch does not support externally executed tools"
        )
    if not requests.approvals:
        raise OrderedToolDispatchError(
            "Pydantic AI returned empty DeferredToolRequests"
        )

    approval_ids = [call.tool_call_id for call in requests.approvals]
    if len(approval_ids) != len(set(approval_ids)):
        raise OrderedToolDispatchError("Deferred tool-call identifiers are not unique")

    response: ModelResponse | None = None
    part_indexes: dict[str, int] = {}
    for message in reversed(messages):
        if not isinstance(message, ModelResponse):
            continue
        candidate_indexes = {
            part.tool_call_id: index
            for index, part in enumerate(message.parts)
            if isinstance(part, ToolCallPart)
        }
        if set(approval_ids).issubset(candidate_indexes):
            response = message
            part_indexes = candidate_indexes
            break

    if response is None:
        raise OrderedToolDispatchError(
            "Deferred tool calls do not belong to a retained ModelResponse"
        )

    calls = sorted(requests.approvals, key=lambda call: part_indexes[call.tool_call_id])
    unknown = [
        call.tool_name
        for call in calls
        if call.tool_name not in _STATEFUL_META_TOOL_NAMES
    ]
    if unknown:
        raise OrderedToolDispatchError(
            "Unexpected approval-required Meta tools: " + ", ".join(unknown)
        )
    return calls


async def _run_approved_tool_to_safe_point(
    meta_agent: Agent[TaskState, Any],
    task_state: TaskState,
    messages: list[ModelMessage],
    deferred_results: DeferredToolResults,
    usage: RunUsage,
) -> list[ModelMessage]:
    async with meta_agent.iter(
        None,
        deps=task_state,
        message_history=messages,
        deferred_tool_results=deferred_results,
        usage=usage,
        usage_limits=UsageLimits(request_limit=constants.META_AGENT_REQUEST_LIMIT),
    ) as agent_run:
        node = agent_run.next_node
        while not isinstance(node, End):
            current = node
            node = await agent_run.next(current)
            if isinstance(current, CallToolsNode):
                if not isinstance(node, ModelRequestNode):
                    raise OrderedToolDispatchError(
                        "Approved stateful tool did not produce a ModelRequestNode"
                    )
                return [
                    *deepcopy(agent_run.ctx.state.message_history),
                    deepcopy(node.request),
                ]

    raise OrderedToolDispatchError(
        "Approved stateful tool reached the end of the agent graph without a safe point"
    )


async def _run_meta_agent_turn(
    meta_agent: Agent[TaskState, Any],
    prompt: str | None,
    task_state: TaskState,
    message_history: list[ModelMessage] | None,
) -> AgentRunResult[Any]:
    usage = RunUsage()
    current_prompt: str | None = prompt
    current_messages = message_history
    while True:
        result = await meta_agent.run(
            current_prompt,
            deps=task_state,
            usage=usage,
            usage_limits=UsageLimits(request_limit=constants.META_AGENT_REQUEST_LIMIT),
            message_history=current_messages,
        )
        if not isinstance(result.output, DeferredToolRequests):
            await ACTION_HOOK_MANAGER.final_drain()
            ACTION_HOOK_MANAGER.raise_if_intervention(
                current_messages=result.all_messages()
            )
            return result

        deferred_messages = result.all_messages()
        calls = _ordered_approval_calls(result.output, deferred_messages)
        selected = calls[0]

        await ACTION_HOOK_MANAGER.before_tool_approval(
            selected.tool_name,
            current_messages=deferred_messages,
        )

        deferred_results = DeferredToolResults(
            approvals={
                call.tool_call_id: (
                    ToolApproved()
                    if call.tool_call_id == selected.tool_call_id
                    else ToolDenied(
                        "Deferred by USEagent ordered dispatch. Resubmit this "
                        "tool call after the current action boundary."
                    )
                )
                for call in calls
            }
        )
        try:
            protocol_messages = await _run_approved_tool_to_safe_point(
                meta_agent,
                task_state,
                deferred_messages,
                deferred_results,
                usage,
            )
        except BaseException:
            await ACTION_HOOK_MANAGER.invalidate_action(
                tool_call_id=selected.tool_call_id
            )
            raise
        await ACTION_HOOK_MANAGER.protocol_finalized(
            selected.tool_call_id,
            protocol_messages,
        )
        selected_action_error = any(
            isinstance(message, ModelRequest)
            and any(
                isinstance(part, RetryPromptPart)
                and part.tool_call_id == selected.tool_call_id
                and isinstance(part.content, str)
                and part.content.startswith(ORDERED_ACTION_ERROR_PREFIX)
                for part in message.parts
            )
            for message in protocol_messages
        )
        if selected_action_error:
            # Ordered wrappers translate an uncaught action exception into a
            # protocol-valid retry part while the scheduler retains the real
            # exception. Error actions are speculation barriers, so finish
            # their gate analysis before deciding whether feedback supersedes
            # the original exception.
            await ACTION_HOOK_MANAGER.final_drain()
        ACTION_HOOK_MANAGER.raise_if_intervention(current_messages=protocol_messages)
        action_error = ACTION_HOOK_MANAGER.pop_action_error(selected.tool_call_id)
        if action_error is not None:
            raise action_error

        current_prompt = None
        current_messages = protocol_messages


async def agent_loop(
    task_state: TaskState,
    output_type: Literal[CodeChange, Answer, Action] = CodeChange,
    output_dir: Path | None = None,
):
    """
    Main agent loop.
    """
    # first initialize some of the tools based on the task.
    init_bash_tool(
        str(task_state._task.get_working_directory()),
        command_transformer=task_state._task.command_transformer,
    )
    init_edit_tools(str(task_state._task.get_working_directory()))

    USAGE_TRACKER = UsageTracker()
    _set_usage_tracker(USAGE_TRACKER)
    meta_agent = init_agent(output_type=output_type)

    await ACTION_HOOK_MANAGER.start_session()

    # actually running the agent
    prompt = "Invoke tools to complete the task."
    message_history = None
    action_hook_interventions = 0
    while True:
        try:
            result = await _run_meta_agent_turn(
                meta_agent,
                prompt,
                task_state,
                message_history,
            )
            USAGE_TRACKER.add(
                usage_tracker_name(meta_agent.name, "meta"),
                result.usage(),
            )
            request = ACTION_HOOK_MANAGER.pop_intervention()
            if request is not None:
                completed_messages = result.all_messages()
                (
                    action_hook_interventions,
                    intervention_prompt,
                ) = await _apply_action_hook_intervention(
                    request,
                    task_state,
                    action_hook_interventions,
                )
                if intervention_prompt is None:
                    break
                prompt = intervention_prompt
                message_history = _message_history_for_action_hook_intervention(
                    request,
                    completed_messages,
                )
                continue
            break
        except ActionIntervention as intervention_exc:
            request = intervention_exc.request
            (
                action_hook_interventions,
                intervention_prompt,
            ) = await _apply_action_hook_intervention(
                request,
                task_state,
                action_hook_interventions,
            )
            if intervention_prompt is not None:
                prompt = intervention_prompt
                message_history = _message_history_for_action_hook_intervention(request)
            else:
                # The cap suppresses the intervention itself, including its
                # requested rollback. Resume from the live safe boundary that
                # raised ActionIntervention, not the stale turn input.
                prompt = None
                message_history = (
                    _message_history_for_suppressed_action_hook_intervention(request)
                )

    last_iteration_messages = result.all_messages()

    if (
        ConfigSingleton.is_initialized()
        and ConfigSingleton.config.optimization_toggles["reiterate-on-doubts"]
    ):
        DOUBT_REITERATION = 0
        while (
            DOUBT_REITERATION < constants.MAX_DOUBT_REITERATIONS
            and result.output
            and result.output.doubts
            and _has_real_doubts(result.output.doubts)
        ):
            try:
                # TODO: store the result? To have something in case of timeout?
                logger.info(
                    f"[MetaAgent] Attempt at solving the task produced a result with doubts: {result.output.doubts}. Attempting to resolve doubts with changes (RE-ITERATION {DOUBT_REITERATION})"
                )
                logger.debug(f"[MetaAgent] Doubtful result was: {result.output}")
                current_bash_hist: list[
                    tuple[
                        NonEmptyStr, NonEmptyStr, CLIResult | ToolErrorInfo | Exception
                    ]
                ] = get_bash_history()[:10]
                bash_infos = [
                    "command\t" + t[0] + "outcome:\t" + str(t[2])
                    for t in current_bash_hist
                ]

                artifact = "UNK"
                match result.output:
                    case Action():
                        artifact = (
                            "SUCCESSFUL"
                            if result.output.success
                            else "UNSUCCESSFUL" + "---" + result.output.evidence
                        )
                    case Answer():
                        artifact = result.output.answer
                    case CodeChange():
                        artifact = (
                            f"Chosen ID: {result.output.diff_id} , which references this patch:"
                            + str(
                                task_state.diff_store.id_to_diff[result.output.diff_id]  # type: ignore
                            )
                            + "\nExplanation:"
                            + result.output.explanation
                        )
                    case _:
                        artifact = str(result.output)
                new_instruction: str | None = await advising_on_doubts(
                    artifact=artifact,
                    doubts=result.output.doubts,
                    task_desc=task_state._task.get_issue_statement(),
                    cmd_history=bash_infos,
                )

                checklist = await _gather_checklist(
                    task_instruction=new_instruction,
                    task_state=task_state,
                    cmd_history=bash_infos,
                    environment=task_state.active_environment,
                )
                new_instruction += f"\n Checklist:\n {str(checklist)}"

                message_history = last_iteration_messages
                while True:
                    try:
                        result = await _run_meta_agent_turn(
                            meta_agent,
                            new_instruction,
                            task_state,
                            message_history,
                        )
                        USAGE_TRACKER.add(
                            usage_tracker_name(meta_agent.name, "meta"),
                            result.usage(),
                        )
                        request = ACTION_HOOK_MANAGER.pop_intervention()
                        if request is not None:
                            completed_messages = result.all_messages()
                            (
                                action_hook_interventions,
                                intervention_prompt,
                            ) = await _apply_action_hook_intervention(
                                request,
                                task_state,
                                action_hook_interventions,
                            )
                            if intervention_prompt is None:
                                break
                            new_instruction = intervention_prompt
                            message_history = (
                                _message_history_for_action_hook_intervention(
                                    request,
                                    completed_messages,
                                )
                            )
                            continue
                        break
                    except ActionIntervention as intervention_exc:
                        request = intervention_exc.request
                        (
                            action_hook_interventions,
                            intervention_prompt,
                        ) = await _apply_action_hook_intervention(
                            request,
                            task_state,
                            action_hook_interventions,
                        )
                        if intervention_prompt is not None:
                            new_instruction = intervention_prompt
                            message_history = (
                                _message_history_for_action_hook_intervention(request)
                            )
                        else:
                            new_instruction = None
                            message_history = _message_history_for_suppressed_action_hook_intervention(
                                request
                            )
                last_iteration_messages = result.all_messages()
            except Exception as exc:
                if isinstance(
                    exc,
                    (ActionHookSchedulerError, OrderedToolDispatchError),
                ):
                    raise
                logger.error(
                    f"[MetaAgent] Error while re-iterating the result after doubts. Re-using previous, initial result (with doubts). Exception was: {exc}"
                )
            finally:
                DOUBT_REITERATION += 1
        else:
            logger.debug("[MetaAgent] Task was finished without any doubts.")

    if isinstance(result.output, CodeChange):
        diff_id = result.output.diff_id
        logger.info(f"[Post-Processing] Resolving {diff_id} in DiffStore:")
        try:
            diff_entry: DiffEntry | None = task_state.diff_store.id_to_diff[diff_id]  # type: ignore
            diff_content: str = (
                diff_entry.diff_content
                if diff_entry
                else f"FAILED to retrieve diff_content for diff_id {diff_id}"
            )
            if diff_content and output_dir:
                patch_file = output_dir / "patch.diff"
                patch_file.parent.mkdir(parents=True, exist_ok=True)
                patch_file.write_text(diff_content)
                logger.debug(f"[Post-Processing] Wrote chosen patch to {patch_file}")
            if output_dir and isinstance(task_state._task, SWEbenchTask):
                task_state._task.postprocess_swebench_task(diff_content, output_dir)
        except Exception as e:
            logger.error(
                f"[Post-Processing] Issue finding {diff_id} in DiffStore {task_state.diff_store}"
            )
            logger.error(e)

    return result.output, USAGE_TRACKER, result.all_messages()


def _format_action_hook_intervention_instruction(
    request: ActionInterventionRequest,
) -> str:
    decision = request.decision
    reason = decision.reason or "No reason was provided."
    if decision.restore_to_checkpoint:
        replay_description = f"""
    The agent trajectory has been restored to the state immediately after
    `{request.checkpoint.action_name}` completed. In-memory TaskState, message
    history, and recorded bash history preserve that action and exclude later
    actions. Project filesystem state is restored when a filesystem snapshot was
    available, within the documented rollback scope. Process, service, remote
    VCS, and other external side effects are not rolled back.
    """
    else:
        replay_description = f"""
    The agent trajectory has not been restored to the checkpoint captured after
    `{request.checkpoint.action_name}`. Continue from the current post-action
    state. If this intervention interrupted a newly requested action, only that
    pending action was removed from message history before replay.
    """
    return f"""
    An action hook scheduled after top-level USEagent actions has completed
    analysis of the `{request.checkpoint.action_name}` action and requested an
    intervention.

    {replay_description}

    Hook reason:
    {reason}

    Continue from this checkpoint and follow the hook instruction:
    {decision.instruction}
    """
