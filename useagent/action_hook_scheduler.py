from __future__ import annotations

import asyncio
import copy
import inspect
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, TypeVar
from uuid import uuid4

from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage

from useagent.action_hooks import (
    ActionCheckpoint,
    ActionHookEvent,
    ActionHookPolicy,
    ActionHookResourceReleaseError,
    ActionHookSchedulerError,
    ActionInterventionRequest,
    FilesystemSnapshot,
    HookCancellationToken,
    HookDecision,
    HookRegistration,
    OrderedHookSchedulerConfig,
    TopLevelActionName,
    cleanup_filesystem_snapshot,
    create_filesystem_snapshot,
)
from useagent.pydantic_models.task_state import TaskState
from useagent.tools.bash import get_bash_history

HookJobState = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "timed_out_before_start",
    "timed_out",
    "cancelled",
    "stale",
]
HookJobOutcome = Literal[
    "noop",
    "intervene",
    "failed",
    "timed_out_before_start",
    "timed_out",
    "cancelled",
    "stale",
]


class _OwnedBlockingOperationCancelled(asyncio.CancelledError):
    """Cancellation observed after an owned thread operation reached terminal state."""

    def __init__(self, result: Any) -> None:
        super().__init__()
        self.result = result


@dataclass(frozen=True)
class WorkspaceRevision:
    id: str
    root: Path
    size_bytes: int
    analysis_roots: tuple[Path, ...]


@dataclass(frozen=True)
class HookJobResult:
    session_id: str
    epoch: int
    action_seq: int
    job_id: str
    hook_id: str
    workspace_revision_id: str
    outcome: HookJobOutcome
    decision: HookDecision | None = None
    error: BaseException | None = None
    duration_seconds: float = 0.0


@dataclass
class _HookJob:
    id: str
    registration: HookRegistration
    event: ActionHookEvent
    state: HookJobState = "queued"
    result: HookJobResult | None = None
    token: HookCancellationToken = field(default_factory=HookCancellationToken)
    callback_task: asyncio.Task[Any] | None = None
    timing_out: bool = False
    resource_release_error: ActionHookResourceReleaseError | None = None


@dataclass
class _ActionRecord:
    checkpoint: ActionCheckpoint
    tool_call_id: str
    state: Literal[
        "reserved",
        "running",
        "finishing",
        "snapshotted",
        "protocol_finalized",
        "retired_noop",
        "intervened",
        "aborted",
        "invalidated",
    ] = "reserved"
    result: Any = None
    error: BaseException | None = None
    restore_task_state: TaskState | None = None
    restore_bash_history_length: int | None = None
    restore_filesystem_snapshot: FilesystemSnapshot | None = None
    workspace_revision: WorkspaceRevision | None = None
    jobs: list[_HookJob] = field(default_factory=list)
    protocol_messages: list[ModelMessage] | None = None

    @property
    def action_seq(self) -> int:
        assert self.checkpoint.action_seq is not None
        return self.checkpoint.action_seq

    @property
    def mandatory_terminal(self) -> bool:
        return all(
            job.state
            in {
                "completed",
                "failed",
                "timed_out_before_start",
                "timed_out",
                "cancelled",
                "stale",
            }
            for job in self.jobs
        )


_BARRIER_TOOLS = {
    "bash_tool",
    "probe_environment",
    "search_code",
    "execute_tests",
    "vcs",
}


class ActionHookSession:
    """Per-agent-session owner for ordered action-hook execution."""

    def __init__(
        self,
        *,
        registrations: tuple[HookRegistration, ...],
        policy: ActionHookPolicy,
        config: OrderedHookSchedulerConfig,
        diagnostic: Callable[..., None],
    ) -> None:
        self.session_id = str(uuid4())
        self.epoch = 0
        self._registrations = registrations
        self._policy = policy
        self._config = config
        self._diagnostic = diagnostic
        self._condition = asyncio.Condition()
        self._action_lock = asyncio.Lock()
        self._action_lock_owner: object | None = None
        self._queue: asyncio.PriorityQueue[tuple[int, int, int, str]] = (
            asyncio.PriorityQueue()
        )
        self._workers: list[asyncio.Task[None]] = []
        self._jobs: dict[str, _HookJob] = {}
        self._records: dict[int, _ActionRecord] = {}
        self._records_by_tool_call: dict[str, _ActionRecord] = {}
        self._next_action_seq = 1
        self._retirement_cursor = 1
        self._accepting_actions = True
        self._closing = False
        self._closed = False
        self._cancel_requested = False
        self._selected_intervention: ActionInterventionRequest | None = None
        self._intervention_delivered = False
        self._fatal_error: ActionHookSchedulerError | None = None
        self._cleanup_error: ActionHookSchedulerError | None = None
        self._cleanup_error_delivered = False
        self._retired_errors: dict[str, BaseException] = {}
        self._ignored_interventions_reason: str | None = None
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._preserved_snapshots: dict[str, FilesystemSnapshot] = {}
        self._retained_revisions: dict[str, WorkspaceRevision] = {}
        self._restore_snapshot_bytes: dict[Path, int] = {}
        self._snapshot_bytes = 0

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        for registration in self._registrations:
            if registration.options.mode != "gate":
                raise ValueError(
                    "Ordered action-hook mode does not yet support observer hooks"
                )
            if not inspect.iscoroutinefunction(registration.hook):
                raise TypeError(
                    "Ordered action-hook mode requires asynchronous Python hooks "
                    f"or asynchronous command wrappers: {registration.id}"
                )
        self._workers = [
            asyncio.create_task(
                self._worker(index),
                name=f"action-hook-worker:{self.session_id}:{index}",
            )
            for index in range(self._config.max_concurrent_runs)
        ]
        self._diagnostic(
            "action_hook_session_started",
            session_id=self.session_id,
            max_concurrent_runs=self._config.max_concurrent_runs,
            max_unretired_actions=self._config.max_unretired_actions,
        )

    async def begin_action(
        self,
        action_name: TopLevelActionName,
        ctx: RunContext[TaskState],
    ) -> ActionCheckpoint | None:
        tool_call_id = ctx.tool_call_id
        if not tool_call_id:
            raise ActionHookSchedulerError(
                "Ordered action hooks require an exact RunContext.tool_call_id"
            )

        action_lock_claim = object()
        await self._action_lock.acquire()
        self._action_lock_owner = action_lock_claim
        action_lock_owner: object = action_lock_claim
        try:
            async with self._condition:
                while self._unretired_count() >= self._config.max_unretired_actions:
                    self._attach_current_messages_locked(ctx.messages)
                    self._raise_if_unavailable_locked()
                    self._diagnostic(
                        "action_admission_wait_started",
                        session_id=self.session_id,
                        action_name=action_name,
                        tool_call_id=tool_call_id,
                    )
                    await self._condition.wait()
                self._attach_current_messages_locked(ctx.messages)
                self._raise_if_unavailable_locked()
                if tool_call_id in self._records_by_tool_call:
                    raise ActionHookSchedulerError(
                        f"Duplicate ordered tool call id: {tool_call_id}"
                    )
                action_seq = self._next_action_seq
                self._next_action_seq += 1
                checkpoint = ActionCheckpoint(
                    id=str(uuid4()),
                    action_name=action_name,
                    task_state=copy.deepcopy(ctx.deps),
                    messages=copy.deepcopy(ctx.messages),
                    bash_history_length=len(get_bash_history()),
                    generation=self.epoch,
                    session_id=self.session_id,
                    epoch=self.epoch,
                    action_seq=action_seq,
                    tool_call_id=tool_call_id,
                )
                record = _ActionRecord(
                    checkpoint=checkpoint,
                    tool_call_id=tool_call_id,
                    state="running",
                )
                self._records[action_seq] = record
                self._records_by_tool_call[tool_call_id] = record
                self._action_lock_owner = record
                action_lock_owner = record
                self._diagnostic(
                    "action_admitted",
                    session_id=self.session_id,
                    epoch=self.epoch,
                    action_seq=action_seq,
                    action_name=action_name,
                    tool_call_id=tool_call_id,
                )
                return checkpoint
        except BaseException:
            self._release_action_lock_if_owned(action_lock_owner)
            raise

    async def finish_action(
        self,
        *,
        checkpoint: ActionCheckpoint,
        action_args: dict[str, Any],
        result: Any,
        error: BaseException | None,
        current_task_state: TaskState,
    ) -> None:
        record = self._record_for_checkpoint(checkpoint)
        async with self._condition:
            if record.state != "running":
                raise ActionHookSchedulerError(
                    "Ordered action completion was already started for "
                    f"checkpoint {checkpoint.id}"
                )
            record.state = "finishing"
        registrations = [
            registration
            for registration in self._registrations
            if registration.options.matches(checkpoint.action_name)
        ]
        record.result = result
        record.error = error
        record.restore_task_state = copy.deepcopy(current_task_state)
        record.restore_bash_history_length = len(get_bash_history())

        async with self._condition:
            if self._earlier_intervention_selected_locked(record):
                record.state = "snapshotted"
                self._diagnostic(
                    "action_hook_jobs_skipped",
                    session_id=self.session_id,
                    epoch=self.epoch,
                    action_seq=record.action_seq,
                    reason="earlier intervention selected",
                )
                self._condition.notify_all()
                return

        if registrations:
            try:
                restore_snapshot, revision = await self._materialize_revision(
                    current_task_state,
                    checkpoint=checkpoint,
                    require_restore=self._policy.allows_restore(checkpoint.action_name),
                    analysis_copy_count=len(registrations),
                )
            except asyncio.CancelledError:
                await self.invalidate_action(checkpoint=checkpoint)
                raise
            except BaseException as exc:
                await self._abort_snapshot_failure(record, exc)
                assert self._fatal_error is not None
                raise self._fatal_error from exc
            record.restore_filesystem_snapshot = restore_snapshot
            record.workspace_revision = revision

        async with self._condition:
            if record.state == "invalidated":
                self._schedule_record_cleanup(record)
                self._condition.notify_all()
                return
            if self._earlier_intervention_selected_locked(record):
                record.state = "snapshotted"
                self._schedule_record_cleanup(record)
                self._diagnostic(
                    "action_hook_jobs_skipped",
                    session_id=self.session_id,
                    epoch=self.epoch,
                    action_seq=record.action_seq,
                    reason="earlier intervention selected during snapshot",
                )
                self._condition.notify_all()
                return
            self._raise_if_stale_locked(record)
            record.state = "snapshotted"
            ordered_registrations = sorted(
                registrations,
                key=lambda item: (-item.options.priority, item.order),
            )
            if ordered_registrations:
                assert record.workspace_revision is not None
                assert len(record.workspace_revision.analysis_roots) == len(
                    ordered_registrations
                )
            for registration, analysis_root in zip(
                ordered_registrations,
                (
                    record.workspace_revision.analysis_roots
                    if record.workspace_revision is not None
                    else ()
                ),
                strict=True,
            ):
                job_id = str(uuid4())
                event = ActionHookEvent(
                    action_name=checkpoint.action_name,
                    action_args=copy.deepcopy(action_args),
                    result=copy.deepcopy(result),
                    error=error,
                    checkpoint=copy.deepcopy(checkpoint),
                    current_task_state=copy.deepcopy(current_task_state),
                    current_bash_history_length=(
                        record.restore_bash_history_length or 0
                    ),
                    # The rollback snapshot is scheduler-owned authority. Exposing
                    # its path would let a callback corrupt later restoration.
                    current_filesystem_snapshot=None,
                    session_id=self.session_id,
                    epoch=self.epoch,
                    action_seq=record.action_seq,
                    hook_job_id=job_id,
                    workspace_revision_id=record.workspace_revision.id,
                    analysis_workspace=analysis_root,
                )
                job = _HookJob(
                    id=job_id,
                    registration=registration,
                    event=event,
                )
                record.jobs.append(job)
                self._jobs[job_id] = job
                await self._queue.put(
                    (
                        record.action_seq,
                        -registration.options.priority,
                        registration.order,
                        job_id,
                    )
                )
                self._diagnostic(
                    "hook_job_queued",
                    session_id=self.session_id,
                    epoch=self.epoch,
                    action_seq=record.action_seq,
                    job_id=job_id,
                    hook_id=registration.id,
                    workspace_revision_id=record.workspace_revision.id,
                )
            self._condition.notify_all()

        patience = self._config.post_action_patience_seconds
        if patience > 0 and not record.mandatory_terminal:
            try:
                async with asyncio.timeout(patience):
                    async with self._condition:
                        await self._condition.wait_for(
                            lambda: record.mandatory_terminal
                            or self._fatal_error is not None
                        )
            except TimeoutError:
                self._diagnostic(
                    "hook_post_action_patience_expired",
                    session_id=self.session_id,
                    epoch=self.epoch,
                    action_seq=record.action_seq,
                    timeout_seconds=patience,
                )

    async def protocol_finalized(
        self,
        tool_call_id: str,
        messages: list[ModelMessage],
    ) -> None:
        async with self._condition:
            record = self._records_by_tool_call.get(tool_call_id)
            if record is None:
                return
            self._raise_if_stale_locked(record)
            if record.state != "snapshotted":
                raise ActionHookSchedulerError(
                    "Ordered action protocol cannot be finalized from state "
                    f"{record.state!r} for tool call {tool_call_id!r}; expected "
                    "'snapshotted'"
                )
            if self._action_lock_owner is not record or not self._action_lock.locked():
                raise ActionHookSchedulerError(
                    "Ordered action protocol finalization does not own the mutable "
                    f"action lock for tool call {tool_call_id!r}"
                )
            record.protocol_messages = copy.deepcopy(messages)
            record.state = "protocol_finalized"
            self._diagnostic(
                "action_protocol_finalized",
                session_id=self.session_id,
                epoch=self.epoch,
                action_seq=record.action_seq,
                tool_call_id=tool_call_id,
            )
            released = self._release_action_lock_if_owned(record)
            assert released
            self._retire_ready_locked()
            self._condition.notify_all()
            self._raise_if_fatal_locked()

    async def invalidate_action(
        self,
        *,
        checkpoint: ActionCheckpoint | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        if checkpoint is not None:
            tool_call_id = checkpoint.tool_call_id
        if not tool_call_id:
            return
        async with self._condition:
            record = self._records_by_tool_call.get(tool_call_id)
            if record is None:
                return
            for job in record.jobs:
                job.token.cancel()
                if job.state == "queued":
                    job.state = "cancelled"
                    job.result = self._job_result(
                        job,
                        "cancelled",
                        duration_seconds=0.0,
                    )
                    self._jobs.pop(job.id, None)
                elif job.callback_task is not None and not job.callback_task.done():
                    job.callback_task.cancel()
            record.state = "invalidated"
            self._records_by_tool_call.pop(tool_call_id, None)
            self._diagnostic(
                "action_invalidated",
                session_id=self.session_id,
                epoch=self.epoch,
                action_seq=record.action_seq,
                tool_call_id=tool_call_id,
                hook_jobs_created=len(record.jobs),
            )
            self._release_action_lock_if_owned(record)
            self._retire_ready_locked()
            self._condition.notify_all()

    async def before_tool_approval(self, tool_name: str) -> None:
        if not self._is_barrier(tool_name):
            return
        async with self._condition:
            self._diagnostic(
                "action_barrier_wait_started",
                session_id=self.session_id,
                tool_name=tool_name,
            )
            await self._condition.wait_for(
                lambda: self._unretired_count() == 0
                or self._selected_intervention is not None
                or self._fatal_error is not None
                or self._cancel_requested
            )
            self._raise_if_fatal_locked()

    def _is_barrier(self, tool_name: str) -> bool:
        normalized = "bash_tool" if "bash" in tool_name.lower() else tool_name
        if normalized in _BARRIER_TOOLS:
            return True
        try:
            action_name = TopLevelActionName.__args__  # type: ignore[attr-defined]
        except AttributeError:  # pragma: no cover - typing implementation detail
            action_name = ()
        if tool_name not in action_name:
            return False
        return any(
            registration.options.matches(tool_name)  # type: ignore[arg-type]
            and registration.options.requires_speculation_barrier
            for registration in self._registrations
        )

    async def _worker(self, worker_index: int) -> None:
        while True:
            try:
                _seq, _priority, _order, job_id = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                job = self._jobs.get(job_id)
                if job is None or job.state != "queued":
                    continue
                async with self._condition:
                    if self._cancel_requested:
                        job.state = "cancelled"
                        self._condition.notify_all()
                        continue
                    job.state = "running"
                    self._diagnostic(
                        "hook_job_started",
                        session_id=self.session_id,
                        epoch=job.event.epoch,
                        action_seq=job.event.action_seq,
                        job_id=job.id,
                        hook_id=job.registration.id,
                        worker_index=worker_index,
                    )
                    self._condition.notify_all()

                result = await self._execute_job(job)
                async with self._condition:
                    job.result = result
                    job.state = self._state_for_outcome(result.outcome)
                    self._jobs.pop(job.id, None)
                    self._diagnostic(
                        f"hook_job_{result.outcome}",
                        session_id=self.session_id,
                        epoch=result.epoch,
                        action_seq=result.action_seq,
                        job_id=result.job_id,
                        hook_id=result.hook_id,
                        duration_seconds=result.duration_seconds,
                    )
                    self._retire_ready_locked()
                    self._condition.notify_all()
                if self._cancel_requested:
                    return
            except asyncio.CancelledError:
                return
            except BaseException as exc:  # pragma: no cover - integrity fallback
                async with self._condition:
                    self._fatal_error = ActionHookSchedulerError(
                        f"Action-hook worker failed: {exc}"
                    )
                    self._accepting_actions = False
                    self._condition.notify_all()
            finally:
                self._queue.task_done()

    async def _execute_job(self, job: _HookJob) -> HookJobResult:
        start = time.monotonic()
        registration = job.registration
        if job.token.cancelled:
            return self._job_result(
                job,
                "timed_out" if job.timing_out else "cancelled",
                duration_seconds=0.0,
            )
        timeout_seconds = (
            registration.options.timeout_seconds or self._config.run_timeout_seconds
        )
        try:
            result_or_awaitable = registration.hook(job.event, job.token)
            if not inspect.isawaitable(result_or_awaitable):
                raise TypeError(
                    "Ordered hook returned synchronously despite async registration"
                )
            job.callback_task = asyncio.create_task(
                result_or_awaitable,
                name=f"action-hook-job:{job.id}",
            )
            done, _pending = await asyncio.wait(
                {job.callback_task},
                timeout=timeout_seconds,
            )
            if not done:
                job.timing_out = True
                job.token.cancel()
                job.callback_task.cancel()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(job.callback_task),
                        timeout=self._config.cleanup_seconds,
                    )
                except asyncio.CancelledError:
                    pass
                except TimeoutError as exc:
                    raise ActionHookSchedulerError(
                        f"Hook {registration.id} did not terminate after timeout"
                    ) from exc
                except ActionHookResourceReleaseError:
                    raise
                except BaseException:
                    pass
                return self._job_result(
                    job,
                    "timed_out",
                    duration_seconds=time.monotonic() - start,
                )
            decision = job.callback_task.result()
        except asyncio.CancelledError:
            outcome: HookJobOutcome = "timed_out" if job.timing_out else "cancelled"
            return self._job_result(
                job,
                outcome,
                duration_seconds=time.monotonic() - start,
            )
        except ActionHookResourceReleaseError as exc:
            job.resource_release_error = exc
            error = self._record_resource_release_failure(job, exc)
            return self._job_result(
                job,
                "failed",
                error=error,
                duration_seconds=time.monotonic() - start,
            )
        except ActionHookSchedulerError as exc:
            async with self._condition:
                self._fatal_error = exc
                self._accepting_actions = False
            return self._job_result(
                job,
                "failed",
                error=exc,
                duration_seconds=time.monotonic() - start,
            )
        except BaseException as exc:
            return self._job_result(
                job,
                "failed",
                error=exc,
                duration_seconds=time.monotonic() - start,
            )

        record = self._records.get(job.event.action_seq or -1)
        if (
            record is None
            or record.workspace_revision is None
            or job.event.session_id != self.session_id
            or job.event.epoch != self.epoch
            or job.event.workspace_revision_id != record.workspace_revision.id
        ):
            return self._job_result(
                job,
                "stale",
                duration_seconds=time.monotonic() - start,
            )
        if decision is None:
            decision = HookDecision.noop()
        if not isinstance(decision, HookDecision):
            return self._job_result(
                job,
                "failed",
                error=TypeError("Hook returned an unsupported decision"),
                duration_seconds=time.monotonic() - start,
            )
        if decision.kind == "intervene":
            if not decision.instruction:
                return self._job_result(
                    job,
                    "failed",
                    error=ValueError("Hook intervention is missing an instruction"),
                    duration_seconds=time.monotonic() - start,
                )
            if decision.restore_to_checkpoint and not registration.options.can_restore:
                return self._job_result(
                    job,
                    "failed",
                    error=ValueError(
                        "Hook requested restoration but can_restore is false"
                    ),
                    duration_seconds=time.monotonic() - start,
                )
            return self._job_result(
                job,
                "intervene",
                decision=decision,
                duration_seconds=time.monotonic() - start,
            )
        return self._job_result(
            job,
            "noop",
            decision=decision,
            duration_seconds=time.monotonic() - start,
        )

    def _job_result(
        self,
        job: _HookJob,
        outcome: HookJobOutcome,
        *,
        decision: HookDecision | None = None,
        error: BaseException | None = None,
        duration_seconds: float,
    ) -> HookJobResult:
        assert job.event.action_seq is not None
        assert job.event.workspace_revision_id is not None
        assert job.event.epoch is not None
        return HookJobResult(
            session_id=self.session_id,
            epoch=job.event.epoch,
            action_seq=job.event.action_seq,
            job_id=job.id,
            hook_id=job.registration.id,
            workspace_revision_id=job.event.workspace_revision_id,
            outcome=outcome,
            decision=decision,
            error=error,
            duration_seconds=round(duration_seconds, 6),
        )

    @staticmethod
    def _state_for_outcome(outcome: HookJobOutcome) -> HookJobState:
        if outcome in {"noop", "intervene"}:
            return "completed"
        return outcome

    async def _materialize_revision(
        self,
        task_state: TaskState,
        *,
        checkpoint: ActionCheckpoint,
        require_restore: bool,
        analysis_copy_count: int,
    ) -> tuple[FilesystemSnapshot | None, WorkspaceRevision]:
        if analysis_copy_count <= 0:
            raise ValueError("analysis_copy_count must be positive")
        materialize_started = time.monotonic()
        try:
            root = Path(task_state._task.get_working_directory()).resolve()
        except BaseException as exc:
            raise ActionHookSchedulerError(
                f"Could not resolve ordered hook workspace: {exc}"
            ) from exc
        if not root.is_dir():
            raise ActionHookSchedulerError(
                f"Ordered hook workspace is not a directory: {root}"
            )

        parents: list[Path] = []
        analysis_roots: tuple[Path, ...] = ()
        restore_snapshot: FilesystemSnapshot | None = None
        restore_snapshot_size = 0
        restore_snapshot_create_seconds = 0.0
        restore_snapshot_size_seconds = 0.0
        source_size_seconds = 0.0
        analysis_copy_seconds = 0.0
        analysis_size_seconds = 0.0
        try:
            for _ in range(analysis_copy_count):
                parents.append(
                    Path(tempfile.mkdtemp(prefix="useagent-action-hook-revision-"))
                )
            analysis_roots = tuple(parent / "tree" for parent in parents)
            if require_restore:
                phase_started = time.monotonic()
                restore_snapshot = await _run_blocking_owned(
                    create_filesystem_snapshot,
                    task_state,
                )
                restore_snapshot_create_seconds = time.monotonic() - phase_started
                if restore_snapshot is None:
                    raise ActionHookSchedulerError(
                        "Could not create the required ordered restore point"
                    )
                phase_started = time.monotonic()
                restore_snapshot_size = await _run_blocking_owned(
                    _tree_size,
                    restore_snapshot.snapshot_root,
                )
                restore_snapshot_size_seconds = time.monotonic() - phase_started
            budget_bytes = int(self._config.snapshot_budget_mib * 1024 * 1024)
            phase_started = time.monotonic()
            source_size = await _run_blocking_owned(_tree_size, root)
            source_size_seconds = time.monotonic() - phase_started
            projected_size = (
                self._snapshot_bytes
                + restore_snapshot_size
                + source_size * analysis_copy_count
            )
            if projected_size > budget_bytes:
                raise ActionHookSchedulerError(
                    "Ordered hook snapshot budget exceeded before copy "
                    f"({projected_size}>{budget_bytes} bytes)"
                )
            phase_started = time.monotonic()
            await _run_blocking_owned(
                shutil.copytree,
                root,
                analysis_roots[0],
                symlinks=True,
            )
            for analysis_root in analysis_roots[1:]:
                await _run_blocking_owned(
                    shutil.copytree,
                    analysis_roots[0],
                    analysis_root,
                    symlinks=True,
                )
            analysis_copy_seconds = time.monotonic() - phase_started
            phase_started = time.monotonic()
            sizes = [
                await _run_blocking_owned(_tree_size, analysis_root)
                for analysis_root in analysis_roots
            ]
            analysis_size_seconds = time.monotonic() - phase_started
            size_bytes = sum(sizes)
            projected_size = self._snapshot_bytes + restore_snapshot_size + size_bytes
            if projected_size > budget_bytes:
                raise ActionHookSchedulerError(
                    "Ordered hook snapshot budget exceeded "
                    f"({projected_size}>{budget_bytes} bytes)"
                )
        except BaseException as exc:
            if (
                restore_snapshot is None
                and isinstance(exc, _OwnedBlockingOperationCancelled)
                and isinstance(exc.result, FilesystemSnapshot)
            ):
                restore_snapshot = exc.result
            await _run_blocking_owned(_remove_trees_checked, tuple(parents))
            if restore_snapshot is not None:
                await _run_blocking_owned(
                    cleanup_filesystem_snapshot,
                    restore_snapshot,
                )
            raise

        retained_size_bytes = size_bytes + restore_snapshot_size
        self._snapshot_bytes += retained_size_bytes
        if restore_snapshot is not None:
            self._restore_snapshot_bytes[restore_snapshot.snapshot_root] = (
                restore_snapshot_size
            )
        revision = WorkspaceRevision(
            id=str(uuid4()),
            root=analysis_roots[0],
            size_bytes=size_bytes,
            analysis_roots=analysis_roots,
        )
        self._diagnostic(
            "action_snapshot_created",
            session_id=self.session_id,
            epoch=self.epoch,
            action_seq=checkpoint.action_seq,
            action_name=checkpoint.action_name,
            checkpoint_id=checkpoint.id,
            tool_call_id=checkpoint.tool_call_id,
            workspace_revision_id=revision.id,
            analysis_workspace=str(revision.root),
            analysis_workspace_count=len(revision.analysis_roots),
            snapshot_size_bytes=size_bytes,
            restore_snapshot_size_bytes=restore_snapshot_size,
            retained_snapshot_size_bytes=retained_size_bytes,
            retained_snapshot_bytes=self._snapshot_bytes,
            snapshot_budget_bytes=budget_bytes,
            has_restore_snapshot=restore_snapshot is not None,
            snapshot_duration_seconds=round(
                time.monotonic() - materialize_started,
                6,
            ),
            restore_snapshot_create_seconds=round(
                restore_snapshot_create_seconds,
                6,
            ),
            restore_snapshot_size_seconds=round(
                restore_snapshot_size_seconds,
                6,
            ),
            source_size_seconds=round(source_size_seconds, 6),
            analysis_copy_seconds=round(analysis_copy_seconds, 6),
            analysis_size_seconds=round(analysis_size_seconds, 6),
        )
        return restore_snapshot, revision

    async def _abort_snapshot_failure(
        self,
        record: _ActionRecord,
        exc: BaseException,
    ) -> None:
        error = (
            exc
            if isinstance(exc, ActionHookSchedulerError)
            else ActionHookSchedulerError(f"Ordered snapshot creation failed: {exc}")
        )
        async with self._condition:
            record.state = "aborted"
            self._fatal_error = error
            self._accepting_actions = False
            self._release_action_lock_if_owned(record)
            self._condition.notify_all()

    def _retire_ready_locked(self) -> None:
        while True:
            record = self._records.get(self._retirement_cursor)
            if record is None:
                return
            if record.state == "invalidated":
                self._records.pop(record.action_seq, None)
                self._retirement_cursor += 1
                self._schedule_record_cleanup(record)
                continue
            if record.state != "protocol_finalized" or not record.mandatory_terminal:
                return
            outcome, decision, error = self._aggregate_record_locked(record)
            if outcome == "abort":
                record.state = "aborted"
                self._fatal_error = ActionHookSchedulerError(
                    f"Mandatory action hook aborted after "
                    f"{record.checkpoint.action_name}: {error}"
                )
                self._accepting_actions = False
                return
            if outcome == "intervene":
                assert decision is not None
                record.state = "intervened"
                self._accepting_actions = False
                self._selected_intervention = ActionInterventionRequest(
                    checkpoint=record.checkpoint,
                    decision=decision,
                    replay_messages=copy.deepcopy(record.protocol_messages),
                    restore_task_state=record.restore_task_state,
                    restore_bash_history_length=record.restore_bash_history_length,
                    restore_filesystem_snapshot=record.restore_filesystem_snapshot,
                )
                self._intervention_delivered = False
                self._evict_terminal_jobs_locked(record)
                self._cancel_queued_jobs_after_locked(record.action_seq)
                self._diagnostic(
                    "intervention_selected",
                    session_id=self.session_id,
                    epoch=self.epoch,
                    action_seq=record.action_seq,
                    action_name=record.checkpoint.action_name,
                    checkpoint_id=record.checkpoint.id,
                    reason=decision.reason,
                    restore_to_checkpoint=decision.restore_to_checkpoint,
                )
                self._schedule_revision_cleanup(record)
                return

            record.state = "retired_noop"
            if record.error is not None:
                self._retired_errors[record.tool_call_id] = record.error
            self._diagnostic(
                "action_retired",
                session_id=self.session_id,
                epoch=self.epoch,
                action_seq=record.action_seq,
                checkpoint_id=record.checkpoint.id,
                outcome="noop",
            )
            self._records.pop(record.action_seq, None)
            self._records_by_tool_call.pop(record.tool_call_id, None)
            self._retirement_cursor += 1
            self._evict_terminal_jobs_locked(record)
            self._schedule_record_cleanup(record)

    def _aggregate_record_locked(
        self,
        record: _ActionRecord,
    ) -> tuple[
        Literal["noop", "intervene", "abort"],
        HookDecision | None,
        BaseException | None,
    ]:
        interventions: list[tuple[HookRegistration, HookDecision]] = []
        for job in sorted(
            record.jobs,
            key=lambda item: (
                -item.registration.options.priority,
                item.registration.order,
            ),
        ):
            result = job.result
            if result is None:
                return (
                    "abort",
                    None,
                    ActionHookSchedulerError(
                        f"Missing terminal result for hook job {job.id}"
                    ),
                )
            if not self._result_matches(record, job, result):
                return (
                    "abort",
                    None,
                    ActionHookSchedulerError(
                        f"Hook result identity mismatch for job {job.id}"
                    ),
                )
            if result.outcome == "intervene":
                assert result.decision is not None
                interventions.append((job.registration, result.decision))
                continue
            if result.outcome == "noop":
                continue
            policy = job.registration.options.failure_policy
            if policy == "abort":
                return "abort", None, result.error
            if policy == "intervene":
                reason = (
                    str(result.error)
                    if result.error is not None
                    else f"hook outcome: {result.outcome}"
                )
                interventions.append(
                    (
                        job.registration,
                        HookDecision.intervene(
                            f"The action hook {job.registration.id} did not "
                            f"complete successfully ({reason}). Reassess the "
                            "last action before continuing.",
                            reason=reason,
                            restore_to_checkpoint=False,
                        ),
                    )
                )

        if not interventions:
            return "noop", None, None
        if self._ignored_interventions_reason is not None:
            self._diagnostic(
                "intervention_suppressed_limit",
                session_id=self.session_id,
                epoch=self.epoch,
                action_seq=record.action_seq,
                reason=self._ignored_interventions_reason,
            )
            return "noop", None, None

        controlling_registration, controlling = interventions[0]
        knowledge = dict(controlling.additional_knowledge)
        supplemental: list[str] = []
        restore_requested = controlling.restore_to_checkpoint
        for registration, decision in interventions[1:]:
            restore_requested = restore_requested or decision.restore_to_checkpoint
            for key, value in decision.additional_knowledge.items():
                if key in knowledge and knowledge[key] != value:
                    self._diagnostic(
                        "hook_knowledge_collision",
                        session_id=self.session_id,
                        epoch=self.epoch,
                        action_seq=record.action_seq,
                        key=key,
                        controlling_hook_id=controlling_registration.id,
                        ignored_hook_id=registration.id,
                    )
                    continue
                knowledge[key] = value
            supplemental.append(
                decision.instruction or decision.reason or registration.id
            )

        instruction = controlling.instruction or "Reassess the last action."
        if supplemental:
            instruction += "\n\nAdditional hook findings:\n" + "\n".join(
                f"- {finding}" for finding in supplemental
            )
        restore_requested = restore_requested and self._policy.allows_restore(
            record.checkpoint.action_name
        )
        decision = HookDecision.intervene(
            instruction,
            reason=controlling.reason,
            additional_knowledge=knowledge,
            restore_to_checkpoint=restore_requested,
        )
        return "intervene", decision, None

    def _result_matches(
        self,
        record: _ActionRecord,
        job: _HookJob,
        result: HookJobResult,
    ) -> bool:
        revision = record.workspace_revision
        return (
            revision is not None
            and result.session_id == self.session_id
            and result.epoch == record.checkpoint.epoch
            and result.action_seq == record.action_seq
            and result.job_id == job.id
            and result.hook_id == job.registration.id
            and result.workspace_revision_id == revision.id
        )

    def _cancel_queued_jobs_after_locked(self, action_seq: int) -> None:
        for later in self._records.values():
            if later.action_seq <= action_seq:
                continue
            for job in later.jobs:
                if job.state != "queued":
                    continue
                job.token.cancel()
                job.state = "cancelled"
                job.result = self._job_result(
                    job,
                    "cancelled",
                    duration_seconds=0.0,
                )
                self._jobs.pop(job.id, None)

    def _evict_terminal_jobs_locked(self, record: _ActionRecord) -> None:
        for job in record.jobs:
            if job.state != "running" and job.state != "queued":
                self._jobs.pop(job.id, None)

    def _schedule_record_cleanup(self, record: _ActionRecord) -> None:
        snapshot = record.restore_filesystem_snapshot
        record.restore_filesystem_snapshot = None
        revision = record.workspace_revision
        record.workspace_revision = None
        self._schedule_cleanup(
            snapshot,
            revision,
            callback_tasks=self._live_callback_tasks(record),
            jobs=tuple(record.jobs),
        )

    def _schedule_revision_cleanup(self, record: _ActionRecord) -> None:
        revision = record.workspace_revision
        record.workspace_revision = None
        self._schedule_cleanup(None, revision, jobs=tuple(record.jobs))

    def _schedule_cleanup(
        self,
        snapshot: FilesystemSnapshot | None,
        revision: WorkspaceRevision | None,
        *,
        callback_tasks: tuple[asyncio.Task[Any], ...] = (),
        jobs: tuple[_HookJob, ...] = (),
    ) -> None:
        if snapshot is None and revision is None:
            return
        task = asyncio.create_task(
            self._cleanup_resources(snapshot, revision, callback_tasks, jobs),
            name=f"action-hook-cleanup:{self.session_id}",
        )
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_task_done)

    def _cleanup_task_done(self, task: asyncio.Task[None]) -> None:
        self._cleanup_tasks.discard(task)
        if task.cancelled():
            self._diagnostic(
                "action_hook_cleanup_cancelled",
                session_id=self.session_id,
            )
            return
        error = task.exception()
        if error is None:
            return
        cleanup_error = ActionHookSchedulerError(
            f"Action-hook resource cleanup failed: {error}"
        )
        self._cleanup_error = cleanup_error
        self._cleanup_error_delivered = False
        self._fatal_error = self._fatal_error or cleanup_error
        self._accepting_actions = False
        self._diagnostic(
            "action_hook_cleanup_failed",
            session_id=self.session_id,
            error_type=type(error).__name__,
            error=str(error),
        )
        self._schedule_notify()

    async def _cleanup_resources(
        self,
        snapshot: FilesystemSnapshot | None,
        revision: WorkspaceRevision | None,
        callback_tasks: tuple[asyncio.Task[Any], ...] = (),
        jobs: tuple[_HookJob, ...] = (),
    ) -> None:
        if callback_tasks:
            done, pending = await asyncio.wait(
                callback_tasks,
                timeout=self._config.cleanup_seconds,
            )
            if pending:
                raise ActionHookSchedulerError(
                    "Cancelled action-hook work did not terminate before resource "
                    "cleanup; snapshots were retained"
                )
            for task in done:
                try:
                    task.result()
                except BaseException:
                    pass
        release_failure = self._resource_release_failure(jobs)
        if release_failure is not None:
            job, error = release_failure
            self._record_resource_release_failure(job, error)
        if snapshot is not None:
            await _run_blocking_owned(cleanup_filesystem_snapshot, snapshot)
            snapshot_size = self._restore_snapshot_bytes.pop(
                snapshot.snapshot_root,
                0,
            )
            self._snapshot_bytes = max(0, self._snapshot_bytes - snapshot_size)
        if revision is not None and release_failure is not None:
            self._retained_revisions[revision.id] = revision
            self._diagnostic(
                "action_hook_revision_retained",
                session_id=self.session_id,
                workspace_revision_id=revision.id,
                analysis_workspaces=[str(path) for path in revision.analysis_roots],
                retained_snapshot_bytes=self._snapshot_bytes,
                reason="hook backend release was not confirmed",
            )
        elif revision is not None:
            await _run_blocking_owned(
                _remove_trees_checked,
                tuple(root.parent for root in revision.analysis_roots),
            )
            self._snapshot_bytes = max(0, self._snapshot_bytes - revision.size_bytes)

    @staticmethod
    def _resource_release_failure(
        jobs: tuple[_HookJob, ...],
    ) -> tuple[_HookJob, ActionHookResourceReleaseError] | None:
        for job in jobs:
            if job.resource_release_error is not None:
                return job, job.resource_release_error
            task = job.callback_task
            if task is None or not task.done() or task.cancelled():
                continue
            try:
                error = task.exception()
            except asyncio.CancelledError:
                continue
            if isinstance(error, ActionHookResourceReleaseError):
                job.resource_release_error = error
                return job, error
        return None

    def _record_resource_release_failure(
        self,
        job: _HookJob,
        error: ActionHookResourceReleaseError,
    ) -> ActionHookSchedulerError:
        existing = self._cleanup_error
        if existing is not None:
            return existing
        scheduler_error = ActionHookSchedulerError(
            "Action hook did not confirm release of analysis workspace "
            f"for job {job.id} ({job.registration.id}): {error}"
        )
        self._cleanup_error = scheduler_error
        self._cleanup_error_delivered = False
        self._fatal_error = scheduler_error
        self._accepting_actions = False
        self._diagnostic(
            "action_hook_resource_release_failed",
            session_id=self.session_id,
            epoch=job.event.epoch,
            action_seq=job.event.action_seq,
            job_id=job.id,
            hook_id=job.registration.id,
            workspace_revision_id=job.event.workspace_revision_id,
            analysis_workspace=str(job.event.analysis_workspace),
            error=str(error),
        )
        self._schedule_notify()
        return scheduler_error

    @staticmethod
    def _live_callback_tasks(
        record: _ActionRecord,
    ) -> tuple[asyncio.Task[Any], ...]:
        return tuple(
            job.callback_task
            for job in record.jobs
            if job.callback_task is not None and not job.callback_task.done()
        )

    async def final_drain(self) -> None:
        self._closing = True
        self._diagnostic(
            "session_finalization_started",
            session_id=self.session_id,
        )
        async with self._condition:
            self._retire_ready_locked()
            already_resolved = (
                self._unretired_count() == 0
                or self._selected_intervention is not None
                or self._fatal_error is not None
            )
        if not already_resolved and self._config.finalize_seconds > 0:
            try:
                async with asyncio.timeout(self._config.finalize_seconds):
                    async with self._condition:
                        await self._condition.wait_for(
                            lambda: self._unretired_count() == 0
                            or self._selected_intervention is not None
                            or self._fatal_error is not None
                        )
            except TimeoutError:
                await self._expire_unresolved_jobs()
        elif not already_resolved:
            await self._expire_unresolved_jobs()

        async with self._condition:
            self._retire_ready_locked()
            self._raise_if_fatal_locked()
            if self._selected_intervention is None:
                self._closing = False
                self._accepting_actions = not self._cancel_requested
                self._condition.notify_all()
        self._diagnostic(
            "session_finalization_drained",
            session_id=self.session_id,
            intervention_selected=self._selected_intervention is not None,
        )

    async def prepare_intervention(self, checkpoint_id: str) -> None:
        selected = next(
            (
                record
                for record in self._records.values()
                if record.checkpoint.id == checkpoint_id
            ),
            None,
        )
        if selected is None:
            return
        callbacks: list[asyncio.Task[Any]] = []
        async with self._condition:
            self._accepting_actions = False
            for record in self._records.values():
                if record.action_seq <= selected.action_seq:
                    continue
                record.state = "invalidated"
                self._records_by_tool_call.pop(record.tool_call_id, None)
                for job in record.jobs:
                    job.token.cancel()
                    if job.state == "queued":
                        job.state = "cancelled"
                        job.result = self._job_result(
                            job,
                            "cancelled",
                            duration_seconds=0.0,
                        )
                        self._jobs.pop(job.id, None)
                    elif job.callback_task is not None and not job.callback_task.done():
                        job.callback_task.cancel()
                        callbacks.append(job.callback_task)
            self._condition.notify_all()

        if callbacks:
            done, pending = await asyncio.wait(
                callbacks,
                timeout=self._config.intervention_quiesce_seconds,
            )
            if pending:
                raise ActionHookSchedulerError(
                    "Later action-hook work did not quiesce before intervention"
                )
            for task in done:
                try:
                    task.result()
                except BaseException:
                    pass
        if self._action_lock.locked():
            raise ActionHookSchedulerError(
                "Mutable action ownership remained live before intervention"
            )

    async def _expire_unresolved_jobs(self) -> None:
        callback_tasks: list[asyncio.Task[Any]] = []
        async with self._condition:
            for job in list(self._jobs.values()):
                if job.state == "queued":
                    job.state = "timed_out_before_start"
                    job.result = self._job_result(
                        job,
                        "timed_out_before_start",
                        duration_seconds=0.0,
                    )
                    self._jobs.pop(job.id, None)
                elif job.state == "running":
                    job.timing_out = True
                    job.token.cancel()
                    if job.callback_task is not None:
                        job.callback_task.cancel()
                        callback_tasks.append(job.callback_task)
            self._retire_ready_locked()
            self._condition.notify_all()

        if callback_tasks:
            done, pending = await asyncio.wait(
                callback_tasks,
                timeout=self._config.cleanup_seconds,
            )
            if pending:
                async with self._condition:
                    self._fatal_error = ActionHookSchedulerError(
                        "Mandatory action-hook resources did not terminate during "
                        "finalization"
                    )
                    self._accepting_actions = False
                    self._condition.notify_all()
                return
            for task in done:
                try:
                    task.result()
                except BaseException:
                    pass

        async with self._condition:
            await self._condition.wait_for(
                lambda: all(job.state != "running" for job in self._jobs.values())
                or self._fatal_error is not None
            )
            self._retire_ready_locked()

    def pop_intervention(self) -> ActionInterventionRequest | None:
        if self._intervention_delivered:
            return None
        request = self._selected_intervention
        if request is not None:
            self._intervention_delivered = True
        return request

    def pop_action_error(self, tool_call_id: str) -> BaseException | None:
        return self._retired_errors.pop(tool_call_id, None)

    def ignore_future_interventions(self, reason: str) -> None:
        self._ignored_interventions_reason = reason
        self.reset_runtime(None)
        self._ignored_interventions_reason = reason

    def reset_runtime(self, preserve_snapshot_id: str | None = None) -> None:
        self.epoch += 1
        for record in list(self._records.values()):
            for job in record.jobs:
                job.token.cancel()
                if job.callback_task is not None and not job.callback_task.done():
                    job.callback_task.cancel()
            snapshot = record.restore_filesystem_snapshot
            if snapshot is not None and record.checkpoint.id == preserve_snapshot_id:
                self._preserved_snapshots[record.checkpoint.id] = snapshot
                record.restore_filesystem_snapshot = None
            self._schedule_record_cleanup(record)
        self._records.clear()
        self._records_by_tool_call.clear()
        self._jobs.clear()
        self._retirement_cursor = self._next_action_seq
        self._selected_intervention = None
        self._intervention_delivered = False
        self._fatal_error = self._cleanup_error
        self._closing = False
        self._accepting_actions = not self._cancel_requested
        self._force_release_action_lock()
        self._schedule_notify()

    def cleanup_checkpoint_snapshot(self, checkpoint_id: str) -> None:
        snapshot = self._preserved_snapshots.pop(checkpoint_id, None)
        if snapshot is None:
            record = next(
                (
                    item
                    for item in self._records.values()
                    if item.checkpoint.id == checkpoint_id
                ),
                None,
            )
            if record is not None:
                snapshot = record.restore_filesystem_snapshot
                record.restore_filesystem_snapshot = None
        if snapshot is not None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                cleanup_filesystem_snapshot(snapshot)
                snapshot_size = self._restore_snapshot_bytes.pop(
                    snapshot.snapshot_root,
                    0,
                )
                self._snapshot_bytes = max(0, self._snapshot_bytes - snapshot_size)
            else:
                self._schedule_cleanup(snapshot, None)

    def request_cancel(self) -> None:
        self._cancel_requested = True
        self._accepting_actions = False
        for job in self._jobs.values():
            job.token.cancel()
            if job.callback_task is not None and not job.callback_task.done():
                job.callback_task.cancel()
        self._schedule_notify()

    async def close(self, *, clean_snapshots: bool = True) -> None:
        if self._closed:
            return
        self.request_cancel()
        callbacks = [
            job.callback_task
            for job in self._jobs.values()
            if job.callback_task is not None and not job.callback_task.done()
        ]
        close_error = None if self._cleanup_error_delivered else self._cleanup_error
        if callbacks:
            done, pending = await asyncio.wait(
                callbacks,
                timeout=self._config.cleanup_seconds,
            )
            if pending:
                close_error = close_error or ActionHookSchedulerError(
                    "Cancelled action-hook callbacks did not terminate before "
                    "the session cleanup deadline"
                )
                self._cleanup_error = close_error
                self._cleanup_error_delivered = False
                self._fatal_error = close_error
                self._diagnostic(
                    "action_hook_callback_cleanup_timed_out",
                    session_id=self.session_id,
                    pending_callback_count=len(pending),
                    timeout_seconds=self._config.cleanup_seconds,
                )
            for task in done:
                try:
                    task.result()
                except BaseException:
                    pass
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

        if clean_snapshots:
            for record in list(self._records.values()):
                self._schedule_record_cleanup(record)
            for snapshot in self._preserved_snapshots.values():
                self._schedule_cleanup(snapshot, None)
            self._preserved_snapshots.clear()
        self._records.clear()
        self._records_by_tool_call.clear()
        self._jobs.clear()
        if self._cleanup_tasks:
            cleanup_results = await asyncio.gather(
                *list(self._cleanup_tasks),
                return_exceptions=True,
            )
            cleanup_error = next(
                (
                    result
                    for result in cleanup_results
                    if isinstance(result, BaseException)
                    and not isinstance(result, asyncio.CancelledError)
                ),
                None,
            )
            if cleanup_error is not None:
                close_error = close_error or ActionHookSchedulerError(
                    f"Action-hook resource cleanup failed: {cleanup_error}"
                )
                self._cleanup_error = close_error
                self._cleanup_error_delivered = False
                self._fatal_error = close_error
        if (
            close_error is None
            and self._cleanup_error is not None
            and not self._cleanup_error_delivered
        ):
            # A cancelled callback can report a mandatory resource-release
            # failure only when record cleanup inspects the completed callback.
            # Refresh after cleanup has quiesced so close cannot silently miss
            # that late failure.
            close_error = self._cleanup_error
        self._force_release_action_lock()
        self._closed = True
        finalization_error = close_error or self._cleanup_error
        self._diagnostic(
            "session_finalization_completed",
            session_id=self.session_id,
            success=finalization_error is None,
            error=(str(finalization_error) if finalization_error is not None else None),
        )
        if close_error is not None:
            self._cleanup_error_delivered = True
            raise close_error

    def _schedule_notify(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def notify() -> None:
            async with self._condition:
                self._condition.notify_all()

        loop.create_task(notify())

    def _record_for_checkpoint(self, checkpoint: ActionCheckpoint) -> _ActionRecord:
        if checkpoint.session_id != self.session_id:
            raise ActionHookSchedulerError("Checkpoint belongs to another session")
        if checkpoint.epoch != self.epoch:
            raise ActionHookSchedulerError("Checkpoint belongs to a stale epoch")
        if checkpoint.action_seq is None:
            raise ActionHookSchedulerError("Ordered checkpoint has no action sequence")
        record = self._records.get(checkpoint.action_seq)
        if record is None or record.checkpoint.id != checkpoint.id:
            raise ActionHookSchedulerError("Unknown ordered action checkpoint")
        return record

    def _release_action_lock_if_owned(self, owner: object) -> bool:
        if self._action_lock_owner is not owner or not self._action_lock.locked():
            return False
        self._action_lock_owner = None
        self._action_lock.release()
        return True

    def _force_release_action_lock(self) -> None:
        self._action_lock_owner = None
        if self._action_lock.locked():
            self._action_lock.release()

    def _unretired_count(self) -> int:
        return sum(
            record.state not in {"retired_noop", "intervened", "aborted", "invalidated"}
            for record in self._records.values()
        )

    def _earlier_intervention_selected_locked(
        self,
        record: _ActionRecord,
    ) -> bool:
        request = self._selected_intervention
        selected_seq = None if request is None else request.checkpoint.action_seq
        return selected_seq is not None and selected_seq < record.action_seq

    def _raise_if_unavailable_locked(self) -> None:
        self._raise_if_fatal_locked()
        if self._selected_intervention is not None:
            raise ActionHookSchedulerError(
                "Action admission is frozen for a selected hook intervention"
            )
        if self._closing or self._cancel_requested or not self._accepting_actions:
            raise ActionHookSchedulerError(
                "Action-hook session is not accepting actions"
            )

    def _attach_current_messages_locked(
        self,
        messages: list[ModelMessage],
    ) -> None:
        request = self._selected_intervention
        if request is None or request.decision.restore_to_checkpoint:
            return
        self._selected_intervention = ActionInterventionRequest(
            checkpoint=request.checkpoint,
            decision=request.decision,
            replay_messages=copy.deepcopy(messages),
            restore_task_state=request.restore_task_state,
            restore_bash_history_length=request.restore_bash_history_length,
            restore_filesystem_snapshot=request.restore_filesystem_snapshot,
        )

    def _raise_if_stale_locked(self, record: _ActionRecord) -> None:
        if record.checkpoint.epoch != self.epoch:
            raise ActionHookSchedulerError("Action record belongs to a stale epoch")
        self._raise_if_fatal_locked()

    def _raise_if_fatal_locked(self) -> None:
        if self._fatal_error is not None:
            if self._fatal_error is self._cleanup_error:
                self._cleanup_error_delivered = True
            raise self._fatal_error


def _tree_size(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _remove_tree_checked(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    if path.exists():
        raise RuntimeError(f"Action-hook cleanup left data behind: {path}")


def _remove_trees_checked(paths: tuple[Path, ...]) -> None:
    for path in paths:
        _remove_tree_checked(path)


_BlockingResultT = TypeVar("_BlockingResultT")


async def _run_blocking_owned(
    function: Callable[..., _BlockingResultT],
    /,
    *args: Any,
    **kwargs: Any,
) -> _BlockingResultT:
    """Run blocking filesystem work without orphaning its worker on cancellation."""

    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        try:
            result = await task
        except BaseException:
            raise cancellation
        raise _OwnedBlockingOperationCancelled(result) from cancellation
