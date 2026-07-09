# Action Hooks

USEagent action hooks are advisory callbacks that run after high-level
Meta-Agent actions. They are useful for adding project-specific checks,
policy feedback, trace inspection, or guardrail feedback without modifying the
agent loop itself.

Hooks are non-blocking by default: after a top-level action finishes, USEagent
schedules registered hooks in the background and the agent is allowed to
continue. When `USEAGENT_ACTION_HOOK_WAIT_SECONDS` is greater than zero,
USEagent waits up to that many seconds for the hooks attached to the completed
action before returning control to the model. This is useful for feedback hooks
that must intervene before the model chooses another action.

If a hook requests an intervention in time, the agent cooperatively stops at the
next top-level action boundary, restores the saved post-action checkpoint when
allowed, and resumes with the hook's instruction. If the configured wait budget
expires, still-running hooks for that checkpoint are cancelled so stale feedback
from an old action cannot interrupt a later step.

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

## Python Hook API

A Python hook receives an `ActionHookEvent` and a `HookCancellationToken`.
It may be synchronous or asynchronous.

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

`HookDecision.intervene` fields:

- `instruction`: required text given to the Meta-Agent.
- `reason`: optional short diagnostic reason.
- `additional_knowledge`: optional key/value notes merged into `TaskState`.
- `restore_to_checkpoint`: defaults to `True`. If true and restore is allowed,
  USEagent restores the state immediately after the triggering action completed.

## Event Data

`ActionHookEvent` exposes:

- `action_name`: one of the top-level action names.
- `action_args`: arguments passed to the action.
- `result`: action result, or `None` if the action failed before returning.
- `error`: exception raised by the action, or `None`.
- `checkpoint`: pre-action checkpoint metadata.
- `current_task_state`: deep copy of `TaskState` immediately after the action.
- `current_bash_history_length`: recorded bash history length after the action.
- `current_filesystem_snapshot`: filesystem snapshot metadata when restore was
  enabled and snapshot creation succeeded.

Treat event objects as read-only. A hook should return a decision instead of
mutating USEagent state directly.

## Intervention And Rollback

When a hook asks for intervention with `restore_to_checkpoint=True`, USEagent
restores to the state immediately after the triggering action completed. The
triggering action is preserved; later actions are removed from replay. This also
works when a later action has the same top-level action name.

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

## Timing Controls

Hook wait behavior is controlled by environment variable:

- `USEAGENT_ACTION_HOOK_WAIT_SECONDS`: seconds to wait after each top-level
  action for that action's hooks to complete. The default is `0`, preserving
  fully non-blocking behavior.

Command hooks have their own execution timeout from USEagent constants. Python
hooks should still check `token.cancelled` before and after expensive work so
they can stop quickly when a run ends or a wait budget expires.

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

## Practical Guidance

Keep hooks narrow and fast. They run in the same event loop when written as
Python hooks, and command hooks spawn a process for every hooked action. Use the
cancellation token before expensive work and after awaits. If a hook only needs
to observe behavior, return `HookDecision.noop()` instead of intervening.

Use `restore_to_checkpoint=True` when the hook wants to discard later agent
actions and retry from the triggering action's completed state. Use
`restore_to_checkpoint=False` when the hook only wants to add guidance without
rewinding the trajectory.

Hook diagnostics are written to `action_hooks.jsonl.log` in the run output
directory.
