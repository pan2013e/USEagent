from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import importlib.util
import inspect
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, cast
from uuid import uuid4

from loguru import logger
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage
from pydantic_core import to_jsonable_python

import useagent.common.constants as constants
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
        self._hooks: list[ActionHook] = []
        self._pending: dict[asyncio.Task[None], HookCancellationToken] = {}
        self._pending_checkpoints: dict[asyncio.Task[None], str] = {}
        self._checkpoint_pending_counts: dict[str, int] = {}
        self._filesystem_snapshots: dict[str, FilesystemSnapshot] = {}
        self._intervention: ActionInterventionRequest | None = None
        self._interventions_ignored_reason: str | None = None
        self._policy = ActionHookPolicy()
        self._diagnostics: list[dict[str, Any]] = []
        self._generation = 0

    @property
    def has_hooks(self) -> bool:
        return bool(self._hooks)

    def register(self, hook: ActionHook) -> Callable[[], None]:
        self._hooks.append(hook)
        self.record_diagnostic("hook_registered", hook=_hook_name(hook))

        def unregister() -> None:
            self.unregister(hook)

        return unregister

    def unregister(self, hook: ActionHook) -> None:
        self._hooks = [registered for registered in self._hooks if registered != hook]

    def clear_hooks(self) -> None:
        self.cancel_pending(clean_snapshots=True)
        self._hooks.clear()
        self._intervention = None
        self._interventions_ignored_reason = None
        self._generation += 1

    def reset_runtime(self, preserve_snapshot_id: str | None = None) -> None:
        self.cancel_pending(
            clean_snapshots=True,
            preserve_snapshot_id=preserve_snapshot_id,
        )
        self._intervention = None
        self._interventions_ignored_reason = None
        self._generation += 1

    def ignore_future_interventions(self, reason: str) -> None:
        self.cancel_pending(clean_snapshots=True)
        self._intervention = None
        self._interventions_ignored_reason = reason
        self._generation += 1
        self.record_diagnostic("interventions_ignored", reason=reason)

    def configure_policy(self, policy: ActionHookPolicy) -> None:
        self._policy = policy
        self.record_diagnostic(
            "policy_configured",
            allow_restore=policy.allow_restore,
            restore_actions=sorted(policy.restore_actions or []),
        )

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

    def create_checkpoint(
        self,
        action_name: TopLevelActionName,
        ctx: RunContext[TaskState],
    ) -> ActionCheckpoint | None:
        self.raise_if_intervention(current_messages=ctx.messages)
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

        filesystem_snapshot = None
        if self._policy.allows_restore(checkpoint.action_name):
            filesystem_snapshot = create_filesystem_snapshot(current_task_state)
        if filesystem_snapshot is not None:
            self._filesystem_snapshots[checkpoint.id] = filesystem_snapshot
        self._checkpoint_pending_counts[checkpoint.id] = len(self._hooks)
        self.record_diagnostic(
            "action_scheduled",
            action_name=checkpoint.action_name,
            checkpoint_id=checkpoint.id,
            has_filesystem_snapshot=filesystem_snapshot is not None,
            filesystem_snapshot_strategy=(
                filesystem_snapshot.strategy if filesystem_snapshot is not None else None
            ),
            error_type=type(error).__name__ if error is not None else None,
        )
        event = ActionHookEvent(
            action_name=checkpoint.action_name,
            action_args=action_args,
            result=result,
            error=error,
            checkpoint=checkpoint,
            current_task_state=copy.deepcopy(current_task_state),
            current_bash_history_length=len(get_bash_history()),
            current_filesystem_snapshot=filesystem_snapshot,
        )
        for hook in list(self._hooks):
            token = HookCancellationToken()
            task = asyncio.create_task(
                self._run_hook(hook, event, token),
                name=f"action-hook:{checkpoint.action_name}:{checkpoint.id}",
            )
            self._pending[task] = token
            self._pending_checkpoints[task] = checkpoint.id
            task.add_done_callback(self._hook_task_done)

    def pop_intervention(self) -> ActionInterventionRequest | None:
        request = self._intervention
        self._intervention = None
        return request

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

    async def wait_for_checkpoint(self, checkpoint_id: str, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            return
        tasks = [
            task
            for task, pending_checkpoint_id in self._pending_checkpoints.items()
            if pending_checkpoint_id == checkpoint_id
        ]
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
        if pending:
            self.record_diagnostic(
                "hook_wait_timeout",
                checkpoint_id=checkpoint_id,
                completed_hooks=len(done),
                pending_hooks=len(pending),
                timeout_seconds=timeout_seconds,
            )
            for task in pending:
                token = self._pending.pop(task, None)
                if token is not None:
                    token.cancel()
                self._pending_checkpoints.pop(task, None)
                if not task.done():
                    task.cancel()
            self._checkpoint_pending_counts.pop(checkpoint_id, None)
            if self._intervention is None:
                self.cleanup_filesystem_snapshot(checkpoint_id)

    def cancel_pending(
        self,
        *,
        clean_snapshots: bool = False,
        preserve_snapshot_id: str | None = None,
    ) -> None:
        pending = list(self._pending.items())
        self._pending.clear()
        self._pending_checkpoints.clear()
        self._checkpoint_pending_counts.clear()
        for task, token in pending:
            token.cancel()
            if not task.done():
                task.cancel()
        if clean_snapshots:
            self.cleanup_filesystem_snapshots(preserve_snapshot_id=preserve_snapshot_id)

    def cleanup_filesystem_snapshots(
        self,
        *,
        preserve_snapshot_id: str | None = None,
    ) -> None:
        for checkpoint_id in list(self._filesystem_snapshots):
            if checkpoint_id == preserve_snapshot_id:
                continue
            snapshot = self._filesystem_snapshots.pop(checkpoint_id)
            cleanup_filesystem_snapshot(snapshot)

    def cleanup_filesystem_snapshot(self, checkpoint_id: str) -> None:
        snapshot = self._filesystem_snapshots.pop(checkpoint_id, None)
        if snapshot is not None:
            cleanup_filesystem_snapshot(snapshot)

    def _hook_task_done(self, task: asyncio.Task[None]) -> None:
        self._pending.pop(task, None)
        checkpoint_id = self._pending_checkpoints.pop(task, None)
        if checkpoint_id is None:
            return
        remaining = self._checkpoint_pending_counts.get(checkpoint_id, 0) - 1
        if remaining <= 0:
            self._checkpoint_pending_counts.pop(checkpoint_id, None)
            if (
                self._intervention is None
                or self._intervention.checkpoint.id != checkpoint_id
            ):
                self.cleanup_filesystem_snapshot(checkpoint_id)
        else:
            self._checkpoint_pending_counts[checkpoint_id] = remaining

    async def _run_hook(
        self,
        hook: ActionHook,
        event: ActionHookEvent,
        token: HookCancellationToken,
    ) -> None:
        hook_name = _hook_name(hook)
        start = time.monotonic()
        try:
            decision_or_awaitable = hook(event, token)
            if inspect.isawaitable(decision_or_awaitable):
                decision = await decision_or_awaitable
            else:
                decision = decision_or_awaitable
        except asyncio.CancelledError:
            self.record_diagnostic(
                "hook_cancelled",
                hook=hook_name,
                action_name=event.action_name,
                checkpoint_id=event.checkpoint.id,
            )
            raise
        except Exception as exc:
            logger.exception(
                f"[ActionHook] Hook failed after {event.action_name}: {exc}"
            )
            self.record_diagnostic(
                "hook_failed",
                hook=hook_name,
                action_name=event.action_name,
                checkpoint_id=event.checkpoint.id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return

        if token.cancelled or decision is None or decision.kind == "noop":
            self.record_diagnostic(
                "hook_completed",
                hook=hook_name,
                action_name=event.action_name,
                checkpoint_id=event.checkpoint.id,
                decision_kind="noop" if decision is None else decision.kind,
                reason=None if decision is None else decision.reason,
                duration_seconds=round(time.monotonic() - start, 6),
            )
            return
        if decision.kind != "intervene":
            logger.warning(f"[ActionHook] Ignoring unknown decision: {decision}")
            self.record_diagnostic(
                "hook_ignored",
                hook=hook_name,
                action_name=event.action_name,
                checkpoint_id=event.checkpoint.id,
                reason="unknown decision kind",
            )
            return
        if event.checkpoint.generation != self._generation:
            logger.info(
                "[ActionHook] Ignoring stale intervention from generation "
                f"{event.checkpoint.generation}; current generation is "
                f"{self._generation}"
            )
            self.record_diagnostic(
                "intervention_ignored",
                hook=hook_name,
                action_name=event.action_name,
                checkpoint_id=event.checkpoint.id,
                reason="stale generation",
            )
            return
        if not decision.instruction:
            logger.warning("[ActionHook] Ignoring intervention without instruction")
            self.record_diagnostic(
                "intervention_ignored",
                hook=hook_name,
                action_name=event.action_name,
                checkpoint_id=event.checkpoint.id,
                reason="missing instruction",
            )
            return

        if self._interventions_ignored_reason is not None:
            logger.warning(
                "[ActionHook] Ignoring intervention after "
                f"{event.action_name}: {self._interventions_ignored_reason}"
            )
            self.record_diagnostic(
                "intervention_ignored",
                hook=hook_name,
                action_name=event.action_name,
                checkpoint_id=event.checkpoint.id,
                reason=self._interventions_ignored_reason,
            )
            return

        if decision.restore_to_checkpoint and not self._policy.allows_restore(
            event.action_name
        ):
            decision = HookDecision.intervene(
                decision.instruction,
                reason=decision.reason,
                additional_knowledge=decision.additional_knowledge,
                restore_to_checkpoint=False,
            )
            self.record_diagnostic(
                "restore_downgraded",
                hook=hook_name,
                action_name=event.action_name,
                checkpoint_id=event.checkpoint.id,
                reason="restore disallowed by action hook policy",
            )

        if self._intervention is None:
            self._intervention = ActionInterventionRequest(
                checkpoint=event.checkpoint,
                decision=decision,
                restore_task_state=event.current_task_state,
                restore_bash_history_length=event.current_bash_history_length,
                restore_filesystem_snapshot=event.current_filesystem_snapshot,
            )
            logger.info(
                "[ActionHook] Queued intervention after "
                f"{event.action_name}: {decision.reason or decision.instruction}"
            )
            self.record_diagnostic(
                "intervention_queued",
                hook=hook_name,
                action_name=event.action_name,
                checkpoint_id=event.checkpoint.id,
                reason=decision.reason,
                restore_to_checkpoint=decision.restore_to_checkpoint,
                has_filesystem_snapshot=event.current_filesystem_snapshot is not None,
                duration_seconds=round(time.monotonic() - start, 6),
            )
        else:
            self.record_diagnostic(
                "intervention_ignored",
                hook=hook_name,
                action_name=event.action_name,
                checkpoint_id=event.checkpoint.id,
                reason="another intervention is already queued",
            )


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
    shutil.rmtree(snapshot.snapshot_root.parent, ignore_errors=True)


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
        unstaged_patch.write_bytes(
            _git_bytes(root, "diff", "--binary", "--full-index")
        )
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
        logger.warning(
            f"[ActionHook] Could not create Git filesystem snapshot: {exc}"
        )
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


def action_hook_wait_seconds() -> float:
    return _env_float("USEAGENT_ACTION_HOOK_WAIT_SECONDS", default=0.0, minimum=0.0)


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_float(name: str, *, default: float, minimum: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return max(minimum, parsed)


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
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(json.dumps(payload).encode("utf-8")),
                timeout=constants.ACTION_HOOK_COMMAND_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning(
                "[ActionHook] Command hook timed out after "
                f"{constants.ACTION_HOOK_COMMAND_TIMEOUT_SECONDS}s: {command}"
            )
            return HookDecision.noop("command hook timed out")

        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            logger.warning(
                "[ActionHook] Command hook failed with exit code "
                f"{proc.returncode}: {command}\n{stderr_text}"
            )
            return HookDecision.noop(
                f"command hook failed with exit code {proc.returncode}"
            )

        output = stdout.decode("utf-8", errors="replace").strip()
        if not output or token.cancelled:
            return None
        try:
            data = json.loads(output)
        except json.JSONDecodeError as exc:
            logger.warning(f"[ActionHook] Command hook returned invalid JSON: {exc}")
            return HookDecision.noop("command hook returned invalid JSON")
        return _decision_from_payload(data)

    command_hook.__name__ = f"command_hook:{shlex.split(command)[0] if command else ''}"
    return command_hook


def _event_payload(event: ActionHookEvent) -> dict[str, Any]:
    return {
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
        return HookDecision.noop(_optional_str(data.get("reason")))
    if kind == "intervene":
        instruction = data.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("Command hook intervention needs a string instruction")
        additional_knowledge = data.get("additional_knowledge") or {}
        if not isinstance(additional_knowledge, dict):
            raise ValueError("additional_knowledge must be an object")
        return HookDecision.intervene(
            instruction,
            reason=_optional_str(data.get("reason")),
            additional_knowledge={
                str(key): str(value) for key, value in additional_knowledge.items()
            },
            restore_to_checkpoint=bool(data.get("restore_to_checkpoint", True)),
        )
    raise ValueError(f"Unknown command hook decision kind: {kind}")


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _safe_jsonable(value: Any) -> Any:
    try:
        return to_jsonable_python(value)
    except Exception:
        return json.loads(json.dumps(value, default=str))


def _hook_name(hook: ActionHook) -> str:
    module = getattr(hook, "__module__", "")
    name = getattr(hook, "__qualname__", getattr(hook, "__name__", repr(hook)))
    return f"{module}.{name}" if module else name
