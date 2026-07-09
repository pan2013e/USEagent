from pathlib import Path
from typing import Literal

from loguru import logger
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import (
    BaseToolCallPart,
    BaseToolReturnPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
)
from pydantic_ai.tools import Tool
from pydantic_ai.usage import UsageLimits

import useagent.common.constants as constants
from useagent.action_hooks import (
    ACTION_HOOK_MANAGER,
    ActionIntervention,
    ActionInterventionRequest,
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
from useagent.state.usage_tracker import UsageTracker
from useagent.tasks.swebench_task import SWEbenchTask
from useagent.tools.bash import (
    get_bash_history,
    init_bash_tool,
    make_bash_tool_for_agent,
)
from useagent.tools.edit import init_edit_tools
from useagent.tools.meta import (  # Agent-State Tools; Agent-Agent Tools
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


def _has_real_doubts(doubts: str | None) -> bool:
    if not doubts:
        return False
    cleaned = doubts.strip().rstrip(".").lower()
    return cleaned not in _NO_DOUBT_PREFIXES


def _apply_action_hook_intervention(
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

    logger.info(
        "[ActionHook] Applying intervention from checkpoint "
        f"{request.checkpoint.id} after {request.checkpoint.action_name}"
    )
    ACTION_HOOK_MANAGER.reset_runtime()
    if request.decision.restore_to_checkpoint:
        if request.restore_task_state is None:
            restore_task_state_from_checkpoint(task_state, request.checkpoint)
        else:
            restore_task_state_from_snapshot(
                task_state,
                request.restore_task_state,
                bash_history_length=request.restore_bash_history_length,
            )
    task_state.additional_knowledge.update(request.decision.additional_knowledge)
    return intervention_count, _format_action_hook_intervention_instruction(request)


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
        )
    if completed_messages is not None:
        return _message_history_through_action(
            completed_messages,
            request.checkpoint.action_name,
        )
    return _drop_trailing_tool_call_messages(request.checkpoint.messages)


def _message_history_through_action(
    messages: list[ModelMessage],
    action_name: str,
) -> list[ModelMessage]:
    action_call_ids: set[str] = set()
    last_action_return_index: int | None = None

    for index, message in enumerate(messages):
        if isinstance(message, ModelResponse):
            for part in message.parts or []:
                if (
                    isinstance(part, BaseToolCallPart)
                    and part.tool_name == action_name
                ):
                    action_call_ids.add(part.tool_call_id)
        elif isinstance(message, ModelRequest) and action_call_ids:
            if any(
                isinstance(part, BaseToolReturnPart)
                and part.tool_call_id in action_call_ids
                for part in message.parts or []
            ):
                last_action_return_index = index

    if last_action_return_index is None:
        return _drop_trailing_tool_call_messages(messages)
    return messages[: last_action_return_index + 1]


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

    meta_agent = Agent(
        config.model,
        instructions=SYSTEM_PROMPT,
        deps_type=TaskState,
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
            ),
            # Agent-Agent Tools
            Tool(edit_code, takes_ctx=True, max_retries=constants.EDIT_CODE_RETRIES),
            Tool(
                search_code, takes_ctx=True, max_retries=constants.SEARCH_AGENT_RETRIES
            ),
            Tool(
                probe_environment,
                takes_ctx=True,
                max_retries=constants.PROBE_ENVIRONMENT_RETRIES,
            ),
            Tool(
                execute_tests,
                takes_ctx=True,
                max_retries=constants.EXECUTE_TESTS_RETRIES,
            ),
            Tool(vcs, takes_ctx=True, max_retries=constants.VCS_AGENT_RETRIES),
        ],
        output_type=output_type,
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


def agent_loop(
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

    ACTION_HOOK_MANAGER.reset_runtime()

    # actually running the agent
    prompt = "Invoke tools to complete the task."
    message_history = None
    action_hook_interventions = 0
    while True:
        try:
            result = meta_agent.run_sync(
                prompt,
                deps=task_state,
                usage_limits=UsageLimits(
                    request_limit=constants.META_AGENT_REQUEST_LIMIT
                ),
                message_history=message_history,
            )
            USAGE_TRACKER.add(meta_agent.name, result.usage())
            request = ACTION_HOOK_MANAGER.pop_intervention()
            if request is not None:
                completed_messages = result.all_messages()
                (
                    action_hook_interventions,
                    intervention_prompt,
                ) = _apply_action_hook_intervention(
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
            ) = _apply_action_hook_intervention(
                request,
                task_state,
                action_hook_interventions,
            )
            if intervention_prompt is not None:
                prompt = intervention_prompt
                message_history = _message_history_for_action_hook_intervention(request)

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
                new_instruction: str = advising_on_doubts(
                    artifact=artifact,
                    doubts=result.output.doubts,
                    task_desc=task_state._task.get_issue_statement(),
                    cmd_history=bash_infos,
                )

                checklist = _gather_checklist(
                    task_instruction=new_instruction,
                    task_state=task_state,
                    cmd_history=bash_infos,
                    environment=task_state.active_environment,
                )
                new_instruction += f"\n Checklist:\n {str(checklist)}"

                message_history = last_iteration_messages
                while True:
                    try:
                        result = meta_agent.run_sync(
                            new_instruction,
                            deps=task_state,
                            usage_limits=UsageLimits(
                                request_limit=constants.META_AGENT_REQUEST_LIMIT
                            ),
                            message_history=message_history,
                        )
                        USAGE_TRACKER.add(meta_agent.name, result.usage())
                        request = ACTION_HOOK_MANAGER.pop_intervention()
                        if request is not None:
                            completed_messages = result.all_messages()
                            (
                                action_hook_interventions,
                                intervention_prompt,
                            ) = _apply_action_hook_intervention(
                                request,
                                task_state,
                                action_hook_interventions,
                            )
                            if intervention_prompt is None:
                                break
                            new_instruction = intervention_prompt
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
                        ) = _apply_action_hook_intervention(
                            request,
                            task_state,
                            action_hook_interventions,
                        )
                        if intervention_prompt is not None:
                            new_instruction = intervention_prompt
                            message_history = _message_history_for_action_hook_intervention(
                                request
                            )
                last_iteration_messages = result.all_messages()
            except Exception as exc:
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

    ACTION_HOOK_MANAGER.cancel_pending()
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
    actions. External filesystem or process side effects are not rolled back.
    """
    else:
        replay_description = f"""
    The agent trajectory has not been restored to the checkpoint captured before
    `{request.checkpoint.action_name}`. Continue from the current post-action
    state. If this intervention interrupted a newly requested action, only that
    pending action was removed from message history before replay.
    """
    return f"""
    A non-blocking hook that runs after top-level USEagent actions has completed
    analysis of the `{request.checkpoint.action_name}` action and requested an
    intervention.

    {replay_description}

    Hook reason:
    {reason}

    Continue from this checkpoint and follow the hook instruction:
    {decision.instruction}
    """
