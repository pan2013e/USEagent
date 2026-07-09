# Top-Level Action Hooks

This document tracks the implementation status for non-blocking hooks that run
after USEagent top-level actions. These hooks are intentionally scoped to the
Meta-Agent action wrappers only, not low-level tools such as bash, file edit, or
git helpers.

## Scope

Top-level actions currently wired in this repository:

- `probe_environment`
- `search_code`
- `execute_tests`
- `edit_code`
- `vcs`

Out of scope:

- Low-level tool calls inside sub-agents.
- Pydantic AI framework-level tool hooks.
- Full runtime rollback of shell processes, installed dependencies, and
  filesystem changes outside the task working directory.
- Git metadata rollback for `.git` contents such as commits, refs, and index
  state.

## Implemented

- [x] Non-blocking hook registry and background task execution.
- [x] Hook cancellation on run end or intervention.
- [x] Per-action checkpoints before top-level actions.
- [x] Post-action snapshots for in-memory `TaskState`, recorded bash history, and
  project working-tree contents.
- [x] Cooperative intervention requests from completed hook runs.
- [x] Meta-Agent loop support for restoring the state immediately after the
  triggering action completed, preserving that action while excluding later
  actions from replay.
- [x] Project filesystem rollback for files under the task working directory,
  excluding `.git` metadata and external process or dependency state.
- [x] Cooperative mid-action cancellation at top-level action boundaries when a
  previously queued hook intervention is noticed while another action is
  running.
- [x] Launch-time loading of external hooks via `--action-hook` and
  `USEAGENT_ACTION_HOOKS`.
- [x] Out-of-process hook command transport via `command:<shell command>` specs.
- [x] Restore policy controls via `--action-hook-disable-restore`,
  `--action-hook-restore-actions`, `USEAGENT_ACTION_HOOK_ALLOW_RESTORE`, and
  `USEAGENT_ACTION_HOOK_RESTORE_ACTIONS`.
- [x] Structured hook diagnostics persisted as `action_hooks.jsonl.log` in run
  output directories.
- [x] Hook scheduling for top-level actions that exit with uncaught exceptions,
  with the exception attached to the hook event.
- [x] Tests for top-level-only scheduling, non-blocking execution, cancellation,
  intervention restore behavior, filesystem restore, command hooks, policy
  downgrade, mid-action cancellation, and uncaught-exception hook events.

## Still Needed

No unchecked implementation items are currently tracked here. See Known
Limitations for behavior that remains intentionally scoped.

## Design Notes

Hooks are LSP-like advisory workers: they should run in the background and
return either no action or an intervention request. Interventions are cooperative
at top-level action boundaries. If a hook requests intervention, the agent loop
restores the post-action snapshot captured immediately after the triggering
action completed, cancels remaining hook jobs, and resumes the Meta-Agent with
the hook-provided instruction. This preserves the triggering action and removes
later action attempts from replay.

The current rollback restores in-memory `TaskState`, saved message history,
recorded bash history length, and task working-tree files. Filesystem restore
excludes `.git` metadata and does not restore shell process state, dependency
installations, services, environment variables, or effects outside the task
working directory.

Mid-action cancellation is cooperative. USEagent polls for queued interventions
while awaiting top-level sub-agent runs and cancels the current asyncio task when
an intervention is found. It cannot forcibly undo already completed subprocess
or external service side effects; those are handled only through the rollback
scope above.

## Known Limitations

- Filesystem rollback excludes `.git`, so commits, index mutations, refs, and
  other VCS metadata changes are not rolled back.
- Rollback does not undo package installations, running processes, remote
  service calls, or files outside the task working directory.
- Mid-action cancellation depends on cooperative asyncio cancellation and may
  wait until the current awaited operation responds to cancellation.
- Command hooks receive JSON-serializable event data and return JSON decisions;
  they do not share Python object identity with the USEagent process.

## External Hooks

External hook specs use one of these forms:

- `package.module:hook_function`
- `/absolute/path/to/hooks.py:hook_function`
- `command:<shell command>`

Specs can be supplied at launch:

```bash
useagent local --project-directory /repo --task-description "..." \
  --action-hook /tmp/my_hooks.py:review_after_action
```

or through the environment:

```bash
USEAGENT_ACTION_HOOKS=/tmp/my_hooks.py:review_after_action,package.mod:hook \
  useagent local --project-directory /repo --task-description "..."
```

Hook functions receive `(event, token)` and may return `None`,
`HookDecision.noop()`, or `HookDecision.intervene(...)`.

Command hooks receive a JSON event on stdin and may print one JSON decision on
stdout. A no-op response may be empty, `null`, or `{"kind": "noop"}`. An
intervention response has this shape:

```json
{
  "kind": "intervene",
  "instruction": "Revise the next step using this feedback.",
  "reason": "optional short reason",
  "additional_knowledge": {"key": "value"},
  "restore_to_checkpoint": true
}
```

Restore policy can disable checkpoint restore globally or limit restore to a
named action set. When policy disallows restore, the intervention still runs but
is downgraded to continue from the current state without rollback.
