# Action Hooks

USEagent action hooks are advisory callbacks that run after high-level
Meta-Agent actions. They are useful for adding project-specific checks,
policy feedback, trace inspection, or guardrail feedback without modifying the
agent loop itself.

`ordered` is the only supported scheduler. Hook analysis can overlap across
edits, but decisions retire in action order. A fixed worker limit bounds
running hooks; excess jobs remain queued. A separate speculation limit bounds
unresolved action restore points and blocks a new action before mutation when
that limit is reached. A queued job does not by itself delay protocol
finalization for its completed action. `legacy` is rejected by both CLI and
environment configuration.

Ordered mode gives each matching hook job a private filesystem copy of its
action's captured content and retains the action's post-action restore point.
If an earlier decision requests
restoration, USEagent invalidates later speculative records, awaits their hook
quiescence, restores the triggering action's completed state, and resumes with
the hook instruction. Final output is provisional until mandatory gate jobs
retire or reach their configured failure policy.

## Hooked Actions

Hooks run only after USEagent top-level actions:

- `probe_environment`
- `search_code`
- `execute_tests`
- `edit_code`
- `vcs`

Hooks do not run after low-level bash calls, file edits, or helper tool calls
inside sub-agents.

## Enabling Hooks

Register a Python hook with `--action-hook`:

```bash
useagent local \
  --project-directory /path/to/project \
  --task-description "Fix the failing test" \
  --action-hook /path/to/hooks.py:review_after_action
```

The same hook can be supplied through `USEAGENT_ACTION_HOOKS`:

```bash
USEAGENT_ACTION_HOOKS=/path/to/hooks.py:review_after_action \
  useagent local --project-directory /path/to/project --task-description "..."
```

Multiple hooks may be registered by repeating `--action-hook` or by separating
environment variable entries with commas:

```bash
USEAGENT_ACTION_HOOKS=/tmp/a.py:hook_a,package.module:hook_b useagent local ...
```

Hook specs have one of these forms:

- `package.module:function_name`
- `/absolute/path/to/file.py:function_name`
- `command:<shell command>`

Ordered scheduling is enabled by default. The explicit scheduler option is
accepted when a fully self-describing command is useful:

```bash
useagent local \
  --project-directory /path/to/project \
  --task-description "Fix the failing test" \
  --action-hook /path/to/hooks.py:review_after_action \
  --action-hook-scheduler ordered \
  --action-hook-max-concurrent-runs 2 \
  --action-hook-max-unretired-actions 2
```

The scheduler and limits can also be set with
`USEAGENT_ACTION_HOOK_SCHEDULER`,
`USEAGENT_ACTION_HOOK_MAX_CONCURRENT_RUNS`, and
`USEAGENT_ACTION_HOOK_MAX_UNRETIRED_ACTIONS`.

## Python Hook API

A Python hook receives an `ActionHookEvent` and a `HookCancellationToken`.
It must be asynchronous so cancellation and session cleanup remain bounded.

```python
from useagent.action_hooks import ActionHookEvent, HookCancellationToken, HookDecision


async def review_after_action(
    event: ActionHookEvent,
    token: HookCancellationToken,
):
    if token.cancelled:
        return None

    if event.action_name != "execute_tests" or event.error is not None:
        return HookDecision.noop("waiting for a successful test run")

    test_output = str(event.result)
    if "FAILED" not in test_output:
        return HookDecision.noop("tests did not report failure")

    return HookDecision.intervene(
        "The previous test run still failed. Inspect the failing test output, "
        "make the smallest targeted fix, then rerun that specific test.",
        reason="test failure observed by action hook",
        additional_knowledge={
            "hook.test_feedback": "A hook saw a failing execute_tests result."
        },
        restore_to_checkpoint=True,
    )
```

Return values:

- `None`: no decision.
- `HookDecision.noop(reason=None)`: record a no-op decision.
- `HookDecision.intervene(...)`: ask the Meta-Agent to resume with new
  instructions.

### Registration Metadata

Python hooks may attach `HookOptions` to the callable. Metadata-free hooks use
deterministic defaults (`gate`, all actions, priority `0`, global
timeout, `continue` failure policy). The bundled CodeQL hook uses this path to
declare an asynchronous `edit_code` gate with an `intervene` failure policy.

```python
from useagent.action_hooks import HookOptions

review_after_action.__useagent_hook_options__ = HookOptions(
    id="project-review",
    actions=frozenset({"edit_code"}),
    mode="gate",
    execution="async",
    priority=10,
    timeout_seconds=120,
    failure_policy="intervene",
    can_restore=True,
    requires_speculation_barrier=False,
)
```

The runtime accepts asynchronous Python gate hooks and command-hook wrappers.
Synchronous Python callbacks and observer registrations are rejected
at session startup. `failure_policy` is one of `continue`, `intervene`, or
`abort`; it controls how failures, cancellation, and timeout outcomes retire.

`HookDecision.intervene` fields:

- `instruction`: required text given to the Meta-Agent.
- `reason`: optional diagnostic string.
- `additional_knowledge`: optional `dict[str, str]` notes merged into
  `TaskState`.
- `restore_to_checkpoint`: boolean, defaulting to `True`. If true and restore is
  allowed, USEagent restores the state immediately after the triggering action
  completed.

Decision construction validates these types at runtime. An intervention with a
missing or blank instruction is rejected as a hook contract failure.

## Event Data

`ActionHookEvent` exposes:

- `action_name`: one of the top-level action names.
- `action_args`: arguments passed to the action.
- `result`: action result, or `None` if the action failed before returning.
- `error`: exception raised by the action, or `None`.
- `checkpoint`: pre-action checkpoint metadata.
- `current_task_state`: deep copy of `TaskState` immediately after the action.
- `current_bash_history_length`: recorded bash history length after the action.
- `current_filesystem_snapshot`: reserved compatibility field. Ordered
  callbacks receive `None`; the authoritative rollback snapshot and its path
  remain private to the scheduler so hook code cannot corrupt restoration.
- `session_id`, `epoch`, and `action_seq`: ordered trajectory identity.
- `hook_job_id`: unique ordered job identity.
- `workspace_revision_id`: identity of the captured analysis tree.
- `analysis_workspace`: callback-private post-action tree that an ordered
  integration should analyze instead of the mutable live task directory. Hooks
  for the same action start from identical content but receive distinct paths,
  so one hook's writes are not visible to another hook.

Treat event objects as read-only. A hook may use its `analysis_workspace` as
private scratch, but those writes are discarded and never applied to the live
workspace. Hook callbacks run in-process and are not a security sandbox: they
must not follow external symlinks or derive and mutate the live task path from
`current_task_state`. A hook should return a decision instead of mutating
USEagent state directly. An external backend
must echo the session, epoch, action sequence, hook job, and workspace revision
on its result; a mismatched or merely "latest" result is not valid for an
ordered gate.

If a hook delegates analysis to a backend that leases the analysis path, it
must not return until release is acknowledged. Raise
`ActionHookResourceReleaseError` when release cannot be confirmed. This is an
integrity failure rather than an ordinary hook failure: the session stops and
retains the affected revision tree instead of deleting a path the backend may
still be using.

## Intervention And Rollback

When a hook asks for intervention with `restore_to_checkpoint=True`, USEagent
restores to the state immediately after the triggering action completed. The
triggering action is preserved; later actions are removed from replay. This also
works when a later action has the same top-level action name.

Completion timing cannot choose the restore point. All hooks
for the retirement-cursor action reach a terminal state, same-action decisions
are aggregated by registration priority and order, and later completed results
remain buffered. Before restoration, later queued jobs are invalidated and
later running jobs are cancelled and awaited up to the intervention quiescence
deadline.

Rollback includes:

- in-memory `TaskState`
- saved message history
- recorded bash history length
- task working-tree files, when a filesystem snapshot exists
- local Git refs, HEAD, staged changes, unstaged changes, and untracked
  non-ignored files in normal in-tree Git repositories

Rollback does not include:

- running processes
- package installations or external services
- files outside the task working directory
- ignored files such as dependency directories and build caches in Git-aware
  rollback
- remote Git effects such as pushes or remote branch changes
- linked-worktree or submodule Git directories outside the task directory

If a later action deletes or corrupts `.git`, Git-aware rollback can fail
because the optimized snapshot does not copy the full Git object database.
Non-Git and unusual Git layouts fall back to a full task-directory copy.

## Restore Policy

Restore can be disabled while still allowing hooks to intervene:

```bash
useagent local ... --action-hook-disable-restore
```

Restore can also be limited to selected top-level actions:

```bash
useagent local ... --action-hook-restore-actions execute_tests,edit_code
```

Environment variable equivalents:

- `USEAGENT_ACTION_HOOK_ALLOW_RESTORE=0`
- `USEAGENT_ACTION_HOOK_RESTORE_ACTIONS=execute_tests,edit_code`

When restore is disallowed, an intervention continues from the current state
instead of rolling back.

## Scheduler And Timing Controls

`USEAGENT_ACTION_HOOK_WAIT_SECONDS` is retired. A zero value is tolerated to
ease environment cleanup, but any nonzero value fails configuration. Use the
run-timeout and post-action-patience settings for their distinct purposes.
CLI values take precedence over environment values, which take precedence over
these defaults:

| CLI option | Environment variable | Default | Meaning |
| --- | --- | ---: | --- |
| `--action-hook-max-concurrent-runs` | `USEAGENT_ACTION_HOOK_MAX_CONCURRENT_RUNS` | `2` | Maximum running hook jobs per session |
| `--action-hook-max-unretired-actions` | `USEAGENT_ACTION_HOOK_MAX_UNRETIRED_ACTIONS` | `2` | Maximum unresolved gate action records |
| `--action-hook-run-timeout-seconds` | `USEAGENT_ACTION_HOOK_RUN_TIMEOUT_SECONDS` | `300` | Per-job runtime after a worker starts it |
| `--action-hook-post-action-patience-seconds` | `USEAGENT_ACTION_HOOK_POST_ACTION_PATIENCE_SECONDS` | `0` | Optional wait after matching jobs are queued before speculation |
| `--action-hook-intervention-quiesce-seconds` | `USEAGENT_ACTION_HOOK_INTERVENTION_QUIESCE_SECONDS` | `30` | Deadline to stop invalidated later hook work |
| `--action-hook-cleanup-seconds` | `USEAGENT_ACTION_HOOK_CLEANUP_SECONDS` | `30` | Deadline for owned resource termination |
| `--action-hook-finalize-seconds` | `USEAGENT_ACTION_HOOK_FINALIZE_SECONDS` | `60` | Final mandatory gate drain before timeout policy |
| `--action-hook-snapshot-budget-mib` | `USEAGENT_ACTION_HOOK_SNAPSHOT_BUDGET_MIB` | `2048` | Aggregate retained analysis-tree budget |

The CLI also reserves observer queue capacity/overflow settings, but the current
runtime does not execute observer registrations yet. Invalid, non-finite, or
out-of-range values fail configuration instead of being silently clamped.

Python hooks should check `token.cancelled` around expensive awaits. Ordered
gate transport must itself be cancellable; do not hide mandatory work inside a
thread that cannot be joined during session cleanup.

## Command Hooks

Command hooks run out of process. USEagent sends a JSON event on stdin, and the
command may print one JSON decision on stdout.

```bash
useagent local ... --action-hook 'command:python /path/to/hook_command.py'
```

Example command hook:

```python
import json
import sys

payload = json.load(sys.stdin)
event = payload["event"]

if event["action_name"] != "execute_tests":
    print(json.dumps({"kind": "noop", "reason": "not a test action"}))
else:
    print(json.dumps({
        "kind": "intervene",
        "instruction": "Review the test result before continuing.",
        "reason": "command hook observed execute_tests",
        "additional_knowledge": {"command_hook": "saw execute_tests"},
        "restore_to_checkpoint": False,
    }))
```

A command hook may output nothing, `null`, or `{"kind": "noop"}` for no action.
Event payloads also carry the session, epoch, action sequence, hook job,
workspace revision, isolated analysis path, and exact checkpoint tool-call ID.
Each command runs in its own process group; timeout or cancellation terminates
and awaits the group, including descendants. A timeout or nonzero exit raises a
hook failure, which is retired according to the registration's failure policy
instead of being treated as a clean result.

## Practical Guidance

Keep hooks narrow and fast. Python hooks must be asynchronous; command hooks
spawn a process for every matching action. Use the cancellation token before
expensive work and after awaits. Until observer mode is implemented, a hook that
only records information is still a gate registration and should return
`HookDecision.noop()`.

Use `restore_to_checkpoint=True` when the hook wants to discard later agent
actions and retry from the triggering action's completed state. Use
`restore_to_checkpoint=False` when the hook only wants to add guidance without
rewinding the trajectory.

Hook diagnostics are written to `action_hooks.jsonl.log` after the owned
session shuts down.

## Ordered Scheduler Limitations

- Observer mode and external hook manifests are not implemented.
- Each matching hook registration receives a full directory copy of the
  action's captured content. The common one-hook case retains one copy, while
  multiple same-action hooks trade additional copy time and logical snapshot
  budget for write isolation. The aggregate budget fails closed when exceeded.
- Direct Meta-Agent Bash is serialized and treated as a speculation barrier,
  but the current public hook API does not emit a post-Bash event.
- Rollback cannot undo running processes, package installation, remote calls,
  remote VCS effects, or files outside the task working directory.
- The bundled CodeQL hook closes each revision workspace when its exact scan
  finishes. An ordered provider timeout, transport error, or correlation
  mismatch becomes a visible intervention, not a clean result.

The full rationale, state model, implementation record, and remaining roadmap
are in
[ACTION_HOOK_CONCURRENCY_DESIGN.md](ACTION_HOOK_CONCURRENCY_DESIGN.md).
