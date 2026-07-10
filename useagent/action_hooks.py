from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import importlib.util
import json
import math
import os
import signal
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, cast

from loguru import logger
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage
from pydantic_core import to_jsonable_python

import useagent.common.constants as constants
from useagent.pydantic_models.task_state import TaskState
from useagent.tools.bash import truncate_bash_history

TopLevelActionName = Literal[
    "probe_environment",
    "search_code",
    "execute_tests",
    "edit_code",
    "vcs",
]
_TOP_LEVEL_ACTION_NAMES = frozenset(
    {"probe_environment", "search_code", "execute_tests", "edit_code", "vcs"}
)

HookMode = Literal["gate", "observer"]
HookExecution = Literal["async", "process"]
HookFailurePolicy = Literal["continue", "intervene", "abort"]

if TYPE_CHECKING:
    from useagent.action_hook_scheduler import ActionHookSession


class ActionHookSchedulerError(RuntimeError):
    """Ordered scheduling cannot safely continue."""


class ActionHookResourceReleaseError(RuntimeError):
    """A hook backend could not confirm release of its analysis workspace."""


@dataclass(frozen=True)
class OrderedHookSchedulerConfig:
    max_concurrent_runs: int = 2
    max_unretired_actions: int = 2
    run_timeout_seconds: float = 300.0
    post_action_patience_seconds: float = 0.0
    intervention_quiesce_seconds: float = 30.0
    cleanup_seconds: float = 30.0
    finalize_seconds: float = 60.0
    snapshot_budget_mib: float = 2048.0

    def __post_init__(self) -> None:
        for name in ("max_concurrent_runs", "max_unretired_actions"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in (
            "run_timeout_seconds",
            "intervention_quiesce_seconds",
            "cleanup_seconds",
            "snapshot_budget_mib",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive finite number")
        for name in ("post_action_patience_seconds", "finalize_seconds"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative finite number")

    @classmethod
    def from_settings(cls, settings: object) -> "OrderedHookSchedulerConfig":
        return cls(
            max_concurrent_runs=int(getattr(settings, "max_concurrent_runs")),
            max_unretired_actions=int(getattr(settings, "max_unretired_actions")),
            run_timeout_seconds=float(getattr(settings, "run_timeout_seconds")),
            post_action_patience_seconds=float(
                getattr(settings, "post_action_patience_seconds")
            ),
            intervention_quiesce_seconds=float(
                getattr(settings, "intervention_quiesce_seconds")
            ),
            cleanup_seconds=float(getattr(settings, "cleanup_seconds")),
            finalize_seconds=float(getattr(settings, "finalize_seconds")),
            snapshot_budget_mib=float(getattr(settings, "snapshot_budget_mib")),
        )


@dataclass(frozen=True)
class ActionCheckpoint:
    id: str
    action_name: TopLevelActionName
    task_state: TaskState
    messages: list[ModelMessage]
    bash_history_length: int
    generation: int
    created_at: datetime = field(default_factory=datetime.now)
    session_id: str | None = None
    epoch: int | None = None
    action_seq: int | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True)
class FilesystemSnapshot:
    root: Path
    snapshot_root: Path
    strategy: Literal["git", "full"] = "full"
    git_snapshot: GitFilesystemSnapshot | None = None
    created_at: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class GitFilesystemSnapshot:
    head_sha: str
    symbolic_head: str | None
    refs: dict[str, str]
    staged_patch: Path
    unstaged_patch: Path
    untracked_root: Path


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
    current_bash_history_length: int
    current_filesystem_snapshot: FilesystemSnapshot | None = None
    session_id: str | None = None
    epoch: int | None = None
    action_seq: int | None = None
    hook_job_id: str | None = None
    workspace_revision_id: str | None = None
    analysis_workspace: Path | None = None


@dataclass(frozen=True)
class HookDecision:
    kind: Literal["noop", "intervene"] = "noop"
    reason: str | None = None
    instruction: str | None = None
    additional_knowledge: dict[str, str] = field(default_factory=dict)
    restore_to_checkpoint: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in {"noop", "intervene"}:
            raise ValueError("HookDecision.kind must be noop or intervene")
        if self.reason is not None and not isinstance(self.reason, str):
            raise ValueError("HookDecision.reason must be a string or None")
        if self.instruction is not None and not isinstance(self.instruction, str):
            raise ValueError("HookDecision.instruction must be a string or None")
        if self.kind == "intervene" and (
            self.instruction is None or not self.instruction.strip()
        ):
            raise ValueError(
                "HookDecision.instruction must be a non-empty string for intervention"
            )
        if self.kind == "noop" and self.instruction is not None:
            raise ValueError("HookDecision.instruction must be None for a no-op")
        if not isinstance(self.additional_knowledge, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.additional_knowledge.items()
        ):
            raise ValueError(
                "HookDecision.additional_knowledge must be a dict of strings"
            )
        if not isinstance(self.restore_to_checkpoint, bool):
            raise ValueError("HookDecision.restore_to_checkpoint must be a boolean")

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
            additional_knowledge=(
                {} if additional_knowledge is None else additional_knowledge
            ),
            restore_to_checkpoint=restore_to_checkpoint,
        )


ActionHook = Callable[
    [ActionHookEvent, HookCancellationToken],
    HookDecision | None | Awaitable[HookDecision | None],
]


@dataclass(frozen=True)
class HookOptions:
    """Scheduler metadata for one action-hook registration.

    Metadata-free registrations use deterministic defaults. The runtime
    currently supports gate hooks; observer hooks are reserved for a later
    rollout and are rejected when a session starts.
    """

    id: str | None = None
    actions: frozenset[TopLevelActionName] | None = None
    mode: HookMode = "gate"
    execution: HookExecution = "async"
    priority: int = 0
    timeout_seconds: float | None = None
    failure_policy: HookFailurePolicy = "continue"
    can_restore: bool = True
    requires_speculation_barrier: bool = False

    def __post_init__(self) -> None:
        if self.id is not None and (
            not isinstance(self.id, str) or not self.id.strip()
        ):
            raise ValueError("HookOptions.id must be a non-empty string")
        if self.actions is not None:
            if not isinstance(self.actions, frozenset):
                raise ValueError("HookOptions.actions must be a frozenset")
            if any(not isinstance(action, str) for action in self.actions):
                raise ValueError("HookOptions.actions must contain strings")
            unknown_actions = set(self.actions) - _TOP_LEVEL_ACTION_NAMES
            if unknown_actions:
                raise ValueError(
                    "HookOptions.actions contains unknown top-level actions: "
                    + ", ".join(sorted(unknown_actions))
                )
        if self.mode not in {"gate", "observer"}:
            raise ValueError("HookOptions.mode must be gate or observer")
        if self.execution not in {"async", "process"}:
            raise ValueError("HookOptions.execution must be async or process")
        if self.failure_policy not in {"continue", "intervene", "abort"}:
            raise ValueError(
                "HookOptions.failure_policy must be continue, intervene, or abort"
            )
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise ValueError("HookOptions.priority must be an integer")
        if self.timeout_seconds is not None:
            if (
                isinstance(self.timeout_seconds, bool)
                or not isinstance(self.timeout_seconds, (int, float))
                or not math.isfinite(self.timeout_seconds)
                or self.timeout_seconds <= 0
            ):
                raise ValueError(
                    "HookOptions.timeout_seconds must be a positive finite number"
                )
        if not isinstance(self.can_restore, bool):
            raise ValueError("HookOptions.can_restore must be a boolean")
        if not isinstance(self.requires_speculation_barrier, bool):
            raise ValueError(
                "HookOptions.requires_speculation_barrier must be a boolean"
            )

    def matches(self, action_name: TopLevelActionName) -> bool:
        return self.actions is None or action_name in self.actions


@dataclass(frozen=True)
class HookRegistration:
    id: str
    hook: ActionHook
    options: HookOptions
    order: int


@dataclass(frozen=True)
class ActionInterventionRequest:
    checkpoint: ActionCheckpoint
    decision: HookDecision
    replay_messages: list[ModelMessage] | None = None
    restore_task_state: TaskState | None = None
    restore_bash_history_length: int | None = None
    restore_filesystem_snapshot: FilesystemSnapshot | None = None


@dataclass(frozen=True)
class ActionHookPolicy:
    allow_restore: bool = True
    restore_actions: frozenset[TopLevelActionName] | None = None

    def allows_restore(self, action_name: TopLevelActionName) -> bool:
        if not self.allow_restore:
            return False
        if self.restore_actions is not None and action_name not in self.restore_actions:
            return False
        return True


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
        self._registrations: list[HookRegistration] = []
        self._next_registration_order = 0
        self._policy = ActionHookPolicy()
        self._diagnostics: list[dict[str, Any]] = []
        self._ordered_session: ActionHookSession | None = None
        self._ordered_config: OrderedHookSchedulerConfig | None = None

    @property
    def has_hooks(self) -> bool:
        return bool(self._registrations)

    def register(
        self,
        hook: ActionHook,
        *,
        options: HookOptions | None = None,
    ) -> Callable[[], None]:
        if self._ordered_session is not None and not self._ordered_session.closed:
            raise RuntimeError("Cannot register hooks after an ordered session starts")
        options = options or HookOptions()
        registration_order = self._next_registration_order
        self._next_registration_order += 1
        registration_id = options.id or f"{_hook_name(hook)}:{registration_order}"
        if any(item.id == registration_id for item in self._registrations):
            raise ValueError(
                f"Duplicate action hook registration id: {registration_id}"
            )
        self._registrations.append(
            HookRegistration(
                id=registration_id,
                hook=hook,
                options=options,
                order=registration_order,
            )
        )
        self.record_diagnostic(
            "hook_registered",
            hook=_hook_name(hook),
            hook_id=registration_id,
            actions=sorted(options.actions or []),
            mode=options.mode,
            priority=options.priority,
            failure_policy=options.failure_policy,
        )

        def unregister() -> None:
            self.unregister(hook)

        return unregister

    def unregister(self, hook: ActionHook) -> None:
        self._registrations = [
            registration
            for registration in self._registrations
            if registration.hook != hook
        ]

    def clear_hooks(self) -> None:
        if self._ordered_session is not None and not self._ordered_session.closed:
            self._ordered_session.request_cancel()
        self._registrations.clear()

    def reset_runtime(self, preserve_snapshot_id: str | None = None) -> None:
        if self._ordered_session is not None:
            self._ordered_session.reset_runtime(preserve_snapshot_id)

    def ignore_future_interventions(self, reason: str) -> None:
        if self._ordered_session is not None:
            self._ordered_session.ignore_future_interventions(reason)
        self.record_diagnostic("interventions_ignored", reason=reason)

    def configure_policy(self, policy: ActionHookPolicy) -> None:
        self._policy = policy
        self.record_diagnostic(
            "policy_configured",
            allow_restore=policy.allow_restore,
            restore_actions=sorted(policy.restore_actions or []),
        )

    def configure_ordered_scheduler(
        self,
        config: OrderedHookSchedulerConfig,
    ) -> None:
        if self._ordered_session is not None and not self._ordered_session.closed:
            raise RuntimeError("Cannot reconfigure an active action-hook session")
        self._ordered_config = config

    async def start_session(self) -> None:
        """Start ordered session ownership."""

        if self._ordered_session is not None and not self._ordered_session.closed:
            return
        from useagent.action_hook_scheduler import ActionHookSession

        config = self._ordered_config
        if config is None:
            from useagent.action_hook_settings import get_action_hook_settings

            config = OrderedHookSchedulerConfig.from_settings(
                get_action_hook_settings()
            )
        session = ActionHookSession(
            registrations=tuple(self._registrations),
            policy=self._policy,
            config=config,
            diagnostic=self.record_diagnostic,
        )
        await session.start()
        self._ordered_session = session

    async def begin_action(
        self,
        action_name: TopLevelActionName,
        ctx: RunContext[TaskState],
    ) -> ActionCheckpoint | None:
        await self.start_session()
        assert self._ordered_session is not None
        try:
            return await self._ordered_session.begin_action(action_name, ctx)
        except ActionHookSchedulerError:
            self.raise_if_intervention(current_messages=ctx.messages)
            raise

    async def finish_action(
        self,
        *,
        checkpoint: ActionCheckpoint | None,
        action_args: dict[str, Any],
        result: Any = None,
        error: BaseException | None = None,
        current_task_state: TaskState,
        current_messages: list[ModelMessage] | None = None,
    ) -> None:
        if checkpoint is None:
            return
        await self.start_session()
        assert self._ordered_session is not None
        await self._ordered_session.finish_action(
            checkpoint=checkpoint,
            action_args=action_args,
            result=result,
            error=error,
            current_task_state=current_task_state,
        )

    async def before_tool_approval(
        self,
        tool_name: str,
        current_messages: list[ModelMessage] | None = None,
    ) -> None:
        await self.start_session()
        assert self._ordered_session is not None
        await self._ordered_session.before_tool_approval(tool_name)
        self.raise_if_intervention(current_messages=current_messages)

    async def protocol_finalized(
        self,
        tool_call_id: str,
        messages: list[ModelMessage],
    ) -> None:
        await self.start_session()
        assert self._ordered_session is not None
        await self._ordered_session.protocol_finalized(tool_call_id, messages)

    async def invalidate_action(
        self,
        checkpoint: ActionCheckpoint | None = None,
        *,
        tool_call_id: str | None = None,
    ) -> None:
        if self._ordered_session is None:
            return
        await self._ordered_session.invalidate_action(
            checkpoint=checkpoint,
            tool_call_id=tool_call_id,
        )

    async def final_drain(self) -> None:
        await self.start_session()
        assert self._ordered_session is not None
        await self._ordered_session.final_drain()

    async def prepare_intervention(
        self,
        request: ActionInterventionRequest,
    ) -> None:
        if self._ordered_session is None:
            return
        await self._ordered_session.prepare_intervention(request.checkpoint.id)

    async def cancel_and_close(self, *, clean_snapshots: bool = True) -> None:
        if self._ordered_session is not None:
            session = self._ordered_session
            try:
                await session.close(clean_snapshots=clean_snapshots)
            finally:
                self._ordered_session = None

    def pop_action_error(self, tool_call_id: str) -> BaseException | None:
        if self._ordered_session is None:
            return None
        return self._ordered_session.pop_action_error(tool_call_id)

    def record_diagnostic(self, event: str, **fields: Any) -> None:
        self._diagnostics.append(
            {
                "timestamp": datetime.now().isoformat(),
                "event": event,
                **_safe_jsonable(fields),
            }
        )

    def diagnostics(self) -> list[dict[str, Any]]:
        return list(self._diagnostics)

    def clear_diagnostics(self) -> None:
        self._diagnostics.clear()

    def write_diagnostics(self, path: Path) -> None:
        with open(path, "w", encoding="utf-8") as file:
            for item in self._diagnostics:
                file.write(json.dumps(item, sort_keys=True) + "\n")

    def pop_intervention(self) -> ActionInterventionRequest | None:
        if self._ordered_session is None:
            return None
        return self._ordered_session.pop_intervention()

    def raise_if_intervention(
        self, current_messages: list[ModelMessage] | None = None
    ) -> None:
        request = self.pop_intervention()
        if request is not None:
            if current_messages is not None:
                request = ActionInterventionRequest(
                    checkpoint=request.checkpoint,
                    decision=request.decision,
                    replay_messages=copy.deepcopy(current_messages),
                    restore_task_state=request.restore_task_state,
                    restore_bash_history_length=request.restore_bash_history_length,
                    restore_filesystem_snapshot=request.restore_filesystem_snapshot,
                )
            raise ActionIntervention(request)

    def cleanup_filesystem_snapshot(self, checkpoint_id: str) -> None:
        if self._ordered_session is not None:
            self._ordered_session.cleanup_checkpoint_snapshot(checkpoint_id)


def restore_task_state_from_checkpoint(
    target: TaskState, checkpoint: ActionCheckpoint
) -> None:
    _restore_task_state(
        target,
        checkpoint.task_state,
        bash_history_length=checkpoint.bash_history_length,
    )


def restore_task_state_from_snapshot(
    target: TaskState,
    source: TaskState,
    *,
    bash_history_length: int | None,
) -> None:
    _restore_task_state(
        target,
        source,
        bash_history_length=bash_history_length,
    )


def _restore_task_state(
    target: TaskState,
    source: TaskState,
    *,
    bash_history_length: int | None,
) -> None:
    source = copy.deepcopy(source)
    target.code_locations = source.code_locations
    target.test_locations = source.test_locations
    target.diff_store = source.diff_store
    target.active_environment = source.active_environment
    target.known_environments = source.known_environments
    target.additional_knowledge = source.additional_knowledge
    if bash_history_length is not None:
        truncate_bash_history(bash_history_length)


def create_filesystem_snapshot(task_state: TaskState) -> FilesystemSnapshot | None:
    try:
        root = Path(task_state._task.get_working_directory()).resolve()
    except Exception as exc:
        logger.warning(f"[ActionHook] Could not resolve task working directory: {exc}")
        return None
    if not root.exists() or not root.is_dir():
        logger.warning(f"[ActionHook] Working directory is not a directory: {root}")
        return None

    git_snapshot = create_git_filesystem_snapshot(root)
    if git_snapshot is not None:
        return git_snapshot

    snapshot_parent = Path(tempfile.mkdtemp(prefix="useagent-action-hook-fs-"))
    snapshot_root = snapshot_parent / "tree"
    try:
        _copy_tree_contents(root, snapshot_root)
    except Exception as exc:
        logger.warning(f"[ActionHook] Could not create filesystem snapshot: {exc}")
        shutil.rmtree(snapshot_parent, ignore_errors=True)
        return None
    return FilesystemSnapshot(root=root, snapshot_root=snapshot_root)


def restore_filesystem_snapshot(snapshot: FilesystemSnapshot) -> None:
    if snapshot.strategy == "git":
        if snapshot.git_snapshot is None:
            raise RuntimeError("Git filesystem snapshot metadata is missing")
        restore_git_filesystem_snapshot(snapshot)
        return

    root = snapshot.root
    if not root.exists():
        root.mkdir(parents=True)

    for child in root.iterdir():
        _remove_path(child)

    if snapshot.snapshot_root.exists():
        _copy_tree_contents(snapshot.snapshot_root, root)


def cleanup_filesystem_snapshot(snapshot: FilesystemSnapshot) -> None:
    snapshot_parent = snapshot.snapshot_root.parent
    if snapshot_parent.exists():
        shutil.rmtree(snapshot_parent)
    if snapshot_parent.exists():
        raise RuntimeError(
            f"Filesystem snapshot cleanup left data behind: {snapshot_parent}"
        )


def create_git_filesystem_snapshot(root: Path) -> FilesystemSnapshot | None:
    if not (root / ".git").is_dir():
        return None

    toplevel = _git_output(root, "rev-parse", "--show-toplevel", check=False)
    if toplevel is None or Path(toplevel).resolve() != root:
        return None

    head_sha = _git_output(root, "rev-parse", "--verify", "HEAD", check=False)
    if head_sha is None:
        return None

    snapshot_parent = Path(tempfile.mkdtemp(prefix="useagent-action-hook-git-"))
    snapshot_root = snapshot_parent / "git"
    snapshot_root.mkdir(parents=True)
    try:
        staged_patch = snapshot_root / "staged.patch"
        unstaged_patch = snapshot_root / "unstaged.patch"
        staged_patch.write_bytes(
            _git_bytes(root, "diff", "--binary", "--full-index", "--cached")
        )
        unstaged_patch.write_bytes(_git_bytes(root, "diff", "--binary", "--full-index"))
        untracked_root = snapshot_root / "untracked"
        _copy_untracked_files(root, untracked_root)
        symbolic_head = _git_output(root, "symbolic-ref", "-q", "HEAD", check=False)
        git_snapshot = GitFilesystemSnapshot(
            head_sha=head_sha,
            symbolic_head=symbolic_head,
            refs=_read_git_refs(root),
            staged_patch=staged_patch,
            unstaged_patch=unstaged_patch,
            untracked_root=untracked_root,
        )
        return FilesystemSnapshot(
            root=root,
            snapshot_root=snapshot_root,
            strategy="git",
            git_snapshot=git_snapshot,
        )
    except Exception as exc:
        logger.warning(f"[ActionHook] Could not create Git filesystem snapshot: {exc}")
        shutil.rmtree(snapshot_parent, ignore_errors=True)
        return None


def restore_git_filesystem_snapshot(snapshot: FilesystemSnapshot) -> None:
    git_snapshot = snapshot.git_snapshot
    if git_snapshot is None:
        raise RuntimeError("Git filesystem snapshot metadata is missing")

    root = snapshot.root
    if not root.exists():
        root.mkdir(parents=True)

    _git_run(root, "reset", "--hard")
    _git_run(root, "clean", "-ffd")
    _git_run(root, "checkout", "--detach", "--quiet", git_snapshot.head_sha)
    _restore_git_refs(root, git_snapshot.refs)
    if git_snapshot.symbolic_head is None:
        _git_run(root, "checkout", "--detach", "--quiet", git_snapshot.head_sha)
    else:
        _git_run(root, "symbolic-ref", "HEAD", git_snapshot.symbolic_head)
    _git_run(root, "reset", "--hard", git_snapshot.head_sha)
    _git_run(root, "clean", "-ffd")

    if git_snapshot.untracked_root.exists():
        _copy_tree_contents(git_snapshot.untracked_root, root)
    if git_snapshot.staged_patch.stat().st_size:
        _git_apply(root, git_snapshot.staged_patch, "--index")
    if git_snapshot.unstaged_patch.stat().st_size:
        _git_apply(root, git_snapshot.unstaged_patch)


def _copy_tree_contents(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        target = destination / child.name
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, target, symlinks=True)
        else:
            shutil.copy2(child, target, follow_symlinks=False)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _copy_untracked_files(root: Path, destination: Path) -> None:
    paths = _git_bytes(root, "ls-files", "--others", "--exclude-standard", "-z")
    for raw_path in paths.split(b"\0"):
        if not raw_path:
            continue
        relative = Path(os.fsdecode(raw_path))
        source = root / relative
        if not source.exists() and not source.is_symlink():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target, follow_symlinks=False)


def _read_git_refs(root: Path) -> dict[str, str]:
    output = _git_output(root, "for-each-ref", "--format=%(refname) %(objectname)")
    refs: dict[str, str] = {}
    for line in output.splitlines():
        ref, sha = line.split(" ", 1)
        refs[ref] = sha
    return refs


def _restore_git_refs(root: Path, saved_refs: dict[str, str]) -> None:
    current_refs = _read_git_refs(root)
    for ref in sorted(set(current_refs) - set(saved_refs)):
        _git_run(root, "update-ref", "-d", ref)
    for ref, sha in sorted(saved_refs.items()):
        _git_run(root, "update-ref", ref, sha)


def _git_apply(root: Path, patch: Path, *args: str) -> None:
    _git_run(root, "apply", "--binary", *args, str(patch))


def _git_output(root: Path, *args: str, check: bool = True) -> str | None:
    result = _git_run(root, *args, check=check, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_bytes(root: Path, *args: str) -> bytes:
    return _git_run(root, *args).stdout


def _git_run(
    root: Path,
    *args: str,
    check: bool = True,
    text: bool = False,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
    )
    if check and result.returncode != 0:
        stderr = result.stderr if text else result.stderr.decode("utf-8", "replace")
        raise RuntimeError(f"git {' '.join(args)} failed: {stderr.strip()}")
    return result


def configure_action_hook_policy_from_environment(
    *,
    allow_restore: bool | None = None,
    restore_actions_value: str | None = None,
) -> None:
    if allow_restore is None:
        allow_restore = _env_flag("USEAGENT_ACTION_HOOK_ALLOW_RESTORE", default=True)
    if restore_actions_value is None:
        restore_actions_value = os.environ.get("USEAGENT_ACTION_HOOK_RESTORE_ACTIONS")
    restore_actions: frozenset[TopLevelActionName] | None = None
    if restore_actions_value:
        values: set[TopLevelActionName] = set()
        allowed_actions = set(cast(tuple[str, ...], TopLevelActionName.__args__))
        for raw in restore_actions_value.split(","):
            action = raw.strip()
            if not action:
                continue
            if action not in allowed_actions:
                raise ValueError(
                    "USEAGENT_ACTION_HOOK_RESTORE_ACTIONS contains unknown "
                    f"top-level action: {action}"
                )
            values.add(cast(TopLevelActionName, action))
        restore_actions = frozenset(values)
    ACTION_HOOK_MANAGER.configure_policy(
        ActionHookPolicy(
            allow_restore=allow_restore,
            restore_actions=restore_actions,
        )
    )


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


ACTION_HOOK_MANAGER = ActionHookManager()


def load_action_hook_spec(spec: str) -> ActionHook:
    if spec.strip().startswith("command:"):
        return _load_command_hook_spec(spec.strip()[len("command:") :].strip())

    module_ref, separator, attr_path = spec.strip().partition(":")
    if not separator or not module_ref or not attr_path:
        raise ValueError(
            "Action hook specs must use 'module:function', "
            "'/path/to/file.py:function', or 'command:<shell command>'"
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
        hook = load_action_hook_spec(spec)
        options = getattr(hook, "__useagent_hook_options__", None)
        if options is not None and not isinstance(options, HookOptions):
            raise TypeError(
                "__useagent_hook_options__ must be a useagent.action_hooks.HookOptions"
            )
        ACTION_HOOK_MANAGER.register(hook, options=options)
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


def _load_command_hook_spec(command: str) -> ActionHook:
    if not command:
        raise ValueError("Command action hook specs must include a command")

    async def command_hook(
        event: ActionHookEvent,
        token: HookCancellationToken,
    ) -> HookDecision | None:
        if token.cancelled:
            return None

        payload = _safe_jsonable(
            {
                "schema_version": 1,
                "event": _event_payload(event),
            }
        )
        logger.debug(f"[ActionHook] Running command hook: {command}")
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(json.dumps(payload).encode("utf-8")),
                timeout=constants.ACTION_HOOK_COMMAND_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            cleanup_task = asyncio.create_task(
                _terminate_command_hook_process(proc),
                name=f"action-hook-command-cleanup:{proc.pid}",
            )
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
            raise
        except TimeoutError:
            await _terminate_command_hook_process(proc)
            logger.warning(
                "[ActionHook] Command hook timed out after "
                f"{constants.ACTION_HOOK_COMMAND_TIMEOUT_SECONDS}s: {command}"
            )
            raise RuntimeError("command hook timed out")

        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            logger.warning(
                "[ActionHook] Command hook failed with exit code "
                f"{proc.returncode}: {command}\n{stderr_text}"
            )
            raise RuntimeError(f"command hook failed with exit code {proc.returncode}")

        output = stdout.decode("utf-8", errors="replace").strip()
        if not output or token.cancelled:
            return None
        try:
            data = json.loads(output)
        except json.JSONDecodeError as exc:
            logger.warning(f"[ActionHook] Command hook returned invalid JSON: {exc}")
            raise RuntimeError("command hook returned invalid JSON") from exc
        return _decision_from_payload(data)

    command_hook.__name__ = f"command_hook:{shlex.split(command)[0] if command else ''}"
    setattr(
        command_hook,
        "__useagent_hook_options__",
        HookOptions(execution="process"),
    )
    return command_hook


async def _terminate_command_hook_process(
    proc: asyncio.subprocess.Process,
) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:  # pragma: no cover - exercised on non-POSIX runners
            proc.terminate()
    except ProcessLookupError:
        pass

    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=1.0)
    except TimeoutError:
        pass

    try:
        if os.name == "posix":
            # The shell may exit on SIGTERM while a descendant ignores it.
            # Re-signal the original process group even when the leader has
            # already been reaped.
            os.killpg(proc.pid, signal.SIGKILL)
        elif proc.returncode is None:  # pragma: no cover - non-POSIX runners
            proc.kill()
    except ProcessLookupError:
        pass
    if proc.returncode is None:
        await proc.wait()


def _event_payload(event: ActionHookEvent) -> dict[str, Any]:
    return {
        "session_id": event.session_id,
        "epoch": event.epoch,
        "action_seq": event.action_seq,
        "hook_job_id": event.hook_job_id,
        "workspace_revision_id": event.workspace_revision_id,
        "analysis_workspace": (
            str(event.analysis_workspace)
            if event.analysis_workspace is not None
            else None
        ),
        "action_name": event.action_name,
        "action_args": event.action_args,
        "result": event.result,
        "error": (
            None
            if event.error is None
            else {
                "type": type(event.error).__name__,
                "message": str(event.error),
            }
        ),
        "checkpoint": {
            "id": event.checkpoint.id,
            "action_name": event.checkpoint.action_name,
            "generation": event.checkpoint.generation,
            "created_at": event.checkpoint.created_at.isoformat(),
            "session_id": event.checkpoint.session_id,
            "epoch": event.checkpoint.epoch,
            "action_seq": event.checkpoint.action_seq,
            "tool_call_id": event.checkpoint.tool_call_id,
        },
        "task_state": event.current_task_state,
        "task_state_summary": event.current_task_state.to_model_repr(),
        "bash_history_length": event.current_bash_history_length,
        "has_filesystem_snapshot": event.current_filesystem_snapshot is not None,
        "filesystem_snapshot_strategy": (
            event.current_filesystem_snapshot.strategy
            if event.current_filesystem_snapshot is not None
            else None
        ),
    }


def _decision_from_payload(data: Any) -> HookDecision | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError("Command hook JSON response must be an object")
    kind = data.get("kind", "noop")
    if kind == "noop":
        return HookDecision.noop(data.get("reason"))
    if kind == "intervene":
        instruction = data.get("instruction")
        return HookDecision.intervene(
            instruction,
            reason=data.get("reason"),
            additional_knowledge=data.get("additional_knowledge"),
            restore_to_checkpoint=data.get("restore_to_checkpoint", True),
        )
    raise ValueError(f"Unknown command hook decision kind: {kind}")


def _safe_jsonable(value: Any) -> Any:
    try:
        return to_jsonable_python(value)
    except Exception:
        return json.loads(json.dumps(value, default=str))


def _hook_name(hook: ActionHook) -> str:
    module = getattr(hook, "__module__", "")
    name = getattr(hook, "__qualname__", getattr(hook, "__name__", repr(hook)))
    return f"{module}.{name}" if module else name
