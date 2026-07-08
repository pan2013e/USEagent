from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import importlib.util
import inspect
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, cast
from uuid import uuid4

from loguru import logger
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage

from useagent.pydantic_models.task_state import TaskState
from useagent.tools.bash import get_bash_history, truncate_bash_history

TopLevelActionName = Literal[
    "probe_environment",
    "search_code",
    "execute_tests",
    "edit_code",
    "vcs",
]


@dataclass(frozen=True)
class ActionCheckpoint:
    id: str
    action_name: TopLevelActionName
    task_state: TaskState
    messages: list[ModelMessage]
    bash_history_length: int
    generation: int
    created_at: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class HookCancellationToken:
    _cancelled: bool = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        object.__setattr__(self, "_cancelled", True)


@dataclass(frozen=True)
class ActionHookEvent:
    action_name: TopLevelActionName
    action_args: dict[str, Any]
    result: Any
    error: BaseException | None
    checkpoint: ActionCheckpoint
    current_task_state: TaskState


@dataclass(frozen=True)
class HookDecision:
    kind: Literal["noop", "intervene"] = "noop"
    reason: str | None = None
    instruction: str | None = None
    additional_knowledge: dict[str, str] = field(default_factory=dict)
    restore_to_checkpoint: bool = True

    @classmethod
    def noop(cls, reason: str | None = None) -> "HookDecision":
        return cls(kind="noop", reason=reason)

    @classmethod
    def intervene(
        cls,
        instruction: str,
        *,
        reason: str | None = None,
        additional_knowledge: dict[str, str] | None = None,
        restore_to_checkpoint: bool = True,
    ) -> "HookDecision":
        return cls(
            kind="intervene",
            reason=reason,
            instruction=instruction,
            additional_knowledge=additional_knowledge or {},
            restore_to_checkpoint=restore_to_checkpoint,
        )


ActionHook = Callable[
    [ActionHookEvent, HookCancellationToken],
    HookDecision | None | Awaitable[HookDecision | None],
]


@dataclass(frozen=True)
class ActionInterventionRequest:
    checkpoint: ActionCheckpoint
    decision: HookDecision


class ActionIntervention(Exception):
    def __init__(self, request: ActionInterventionRequest):
        self.request = request
        decision = request.decision
        reason = f": {decision.reason}" if decision.reason else ""
        super().__init__(
            f"Action hook requested intervention after "
            f"{request.checkpoint.action_name}{reason}"
        )


class ActionHookManager:
    def __init__(self) -> None:
        self._hooks: list[ActionHook] = []
        self._pending: dict[asyncio.Task[None], HookCancellationToken] = {}
        self._intervention: ActionInterventionRequest | None = None
        self._interventions_ignored_reason: str | None = None
        self._generation = 0

    @property
    def has_hooks(self) -> bool:
        return bool(self._hooks)

    def register(self, hook: ActionHook) -> Callable[[], None]:
        self._hooks.append(hook)

        def unregister() -> None:
            self.unregister(hook)

        return unregister

    def unregister(self, hook: ActionHook) -> None:
        self._hooks = [registered for registered in self._hooks if registered != hook]

    def clear_hooks(self) -> None:
        self.cancel_pending()
        self._hooks.clear()
        self._intervention = None
        self._interventions_ignored_reason = None
        self._generation += 1

    def reset_runtime(self) -> None:
        self.cancel_pending()
        self._intervention = None
        self._interventions_ignored_reason = None
        self._generation += 1

    def ignore_future_interventions(self, reason: str) -> None:
        self.cancel_pending()
        self._intervention = None
        self._interventions_ignored_reason = reason
        self._generation += 1

    def create_checkpoint(
        self,
        action_name: TopLevelActionName,
        ctx: RunContext[TaskState],
    ) -> ActionCheckpoint | None:
        self.raise_if_intervention()
        if not self.has_hooks:
            return None
        return ActionCheckpoint(
            id=str(uuid4()),
            action_name=action_name,
            task_state=copy.deepcopy(ctx.deps),
            messages=copy.deepcopy(ctx.messages),
            bash_history_length=len(get_bash_history()),
            generation=self._generation,
        )

    def schedule(
        self,
        *,
        checkpoint: ActionCheckpoint | None,
        action_args: dict[str, Any],
        result: Any = None,
        error: BaseException | None = None,
        current_task_state: TaskState,
    ) -> None:
        if checkpoint is None or not self.has_hooks:
            return

        event = ActionHookEvent(
            action_name=checkpoint.action_name,
            action_args=action_args,
            result=result,
            error=error,
            checkpoint=checkpoint,
            current_task_state=current_task_state,
        )
        for hook in list(self._hooks):
            token = HookCancellationToken()
            task = asyncio.create_task(
                self._run_hook(hook, event, token),
                name=f"action-hook:{checkpoint.action_name}:{checkpoint.id}",
            )
            self._pending[task] = token
            task.add_done_callback(lambda done: self._pending.pop(done, None))

    def pop_intervention(self) -> ActionInterventionRequest | None:
        request = self._intervention
        self._intervention = None
        return request

    def raise_if_intervention(self) -> None:
        request = self.pop_intervention()
        if request is not None:
            raise ActionIntervention(request)

    def cancel_pending(self) -> None:
        pending = list(self._pending.items())
        self._pending.clear()
        for task, token in pending:
            token.cancel()
            if not task.done():
                task.cancel()

    async def _run_hook(
        self,
        hook: ActionHook,
        event: ActionHookEvent,
        token: HookCancellationToken,
    ) -> None:
        try:
            decision_or_awaitable = hook(event, token)
            if inspect.isawaitable(decision_or_awaitable):
                decision = await decision_or_awaitable
            else:
                decision = decision_or_awaitable
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                f"[ActionHook] Hook failed after {event.action_name}: {exc}"
            )
            return

        if token.cancelled or decision is None or decision.kind == "noop":
            return
        if decision.kind != "intervene":
            logger.warning(f"[ActionHook] Ignoring unknown decision: {decision}")
            return
        if event.checkpoint.generation != self._generation:
            logger.info(
                "[ActionHook] Ignoring stale intervention from generation "
                f"{event.checkpoint.generation}; current generation is "
                f"{self._generation}"
            )
            return
        if not decision.instruction:
            logger.warning("[ActionHook] Ignoring intervention without instruction")
            return

        if self._interventions_ignored_reason is not None:
            logger.warning(
                "[ActionHook] Ignoring intervention after "
                f"{event.action_name}: {self._interventions_ignored_reason}"
            )
            return

        if self._intervention is None:
            self._intervention = ActionInterventionRequest(
                checkpoint=event.checkpoint,
                decision=decision,
            )
            logger.info(
                "[ActionHook] Queued intervention after "
                f"{event.action_name}: {decision.reason or decision.instruction}"
            )


def restore_task_state_from_checkpoint(
    target: TaskState, checkpoint: ActionCheckpoint
) -> None:
    source = copy.deepcopy(checkpoint.task_state)
    target.code_locations = source.code_locations
    target.test_locations = source.test_locations
    target.diff_store = source.diff_store
    target.active_environment = source.active_environment
    target.known_environments = source.known_environments
    target.additional_knowledge = source.additional_knowledge
    truncate_bash_history(checkpoint.bash_history_length)


ACTION_HOOK_MANAGER = ActionHookManager()


def load_action_hook_spec(spec: str) -> ActionHook:
    module_ref, separator, attr_path = spec.strip().partition(":")
    if not separator or not module_ref or not attr_path:
        raise ValueError(
            "Action hook specs must use 'module:function' or "
            "'/path/to/file.py:function'"
        )

    if module_ref.endswith(".py") or "/" in module_ref:
        module = _load_module_from_file(module_ref)
    else:
        module = importlib.import_module(module_ref)

    obj: object = module
    for attr in attr_path.split("."):
        obj = getattr(obj, attr)

    if not callable(obj):
        raise TypeError(f"Action hook target is not callable: {spec}")
    return cast(ActionHook, obj)


def register_action_hook_specs(specs: list[str]) -> int:
    registered = 0
    for spec in specs:
        ACTION_HOOK_MANAGER.register(load_action_hook_spec(spec))
        registered += 1
    return registered


def parse_action_hook_spec_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _load_module_from_file(module_ref: str):
    path = Path(module_ref).expanduser().resolve()
    module_name = (
        "useagent_external_hook_" + hashlib.sha1(str(path).encode("utf-8")).hexdigest()
    )
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load action hook module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
