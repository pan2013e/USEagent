# Top-Level Action Hooks

This document tracks the implementation status for post-action hooks that run
after USEagent top-level actions. Ordered scheduling is the sole supported
runtime. It adds bounded concurrent hook analysis, ordered decision retirement,
separate execution/speculation backpressure, captured analysis revisions with
callback-private trees,
deterministic restoration, and structured cleanup. See
[HOOK.md](HOOK.md) for the user-facing API and
[ACTION_HOOK_CONCURRENCY_DESIGN.md](ACTION_HOOK_CONCURRENCY_DESIGN.md) for the
state model and remaining roadmap.

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
- Remote VCS side effects such as pushed commits, remote branch mutations, and
  external repositories referenced by linked worktrees or submodules.

## Implemented

- [x] Hook registry and bounded worker execution.
- [x] Hook cancellation requested on run end or intervention. Sessions cancel
  and await owned callbacks, provider workspaces, cleanup tasks, and worker
  tasks before diagnostics are finalized.
- [x] Per-action checkpoints before top-level actions.
- [x] Post-action snapshots for in-memory `TaskState`, recorded bash history, and
  project working-tree contents.
- [x] Cooperative intervention requests from completed hook runs.
- [x] Meta-Agent loop support for restoring the state immediately after the
  triggering action completed, preserving that action while excluding later
  actions from replay, including later actions with the same top-level action
  name.
- [x] Project filesystem rollback for files under the task working directory,
  using an optimized Git-aware snapshot for normal in-tree Git repositories and
  a full-copy fallback otherwise.
- [x] Safe-boundary intervention delivery after the active stateful call reaches
  its exact protocol-valid return boundary.
- [x] Launch-time loading of external hooks via `--action-hook` and
  `USEAGENT_ACTION_HOOKS`.
- [x] Out-of-process hook command transport via `command:<shell command>` specs.
- [x] Restore policy controls via `--action-hook-disable-restore`,
  `--action-hook-restore-actions`, `USEAGENT_ACTION_HOOK_ALLOW_RESTORE`, and
  `USEAGENT_ACTION_HOOK_RESTORE_ACTIONS`.
- [x] Structured hook diagnostics persisted as `action_hooks.jsonl.log` in run
  output directories.
- [x] Hook scheduling for exceptions caught at the instrumented awaited
  top-level sub-agent boundaries, with the exception attached to the hook event.
- [x] Tests for top-level-only scheduling, bounded concurrent execution,
  cancellation, intervention restore behavior including repeated-action replay,
  filesystem restore, command hooks, policy downgrade, safe-boundary delivery,
  and uncaught-exception hook events.
- [x] Retired `USEAGENT_ACTION_HOOK_WAIT_SECONDS`; nonzero values are rejected
  in favor of separate run-timeout and post-action-patience controls.

### Ordered scheduler

- [x] Ordered-only scheduler selection with validated CLI and environment
  configuration; `legacy` and nonzero legacy wait values are rejected.
- [x] Per-task ordered session ownership, exact session/epoch/action/job/tool
  call/revision identities, and awaited final cleanup.
- [x] A fixed hook worker pool (`max_concurrent_runs`) plus a separate bound on
  unretired gate actions (`max_unretired_actions`). Excess hook jobs remain
  queued without delaying protocol finalization merely for a worker slot, and a
  new action blocks before mutation when the speculation window is full.
- [x] One stateful Meta-Agent tool approved at a time using public deferred-tool
  results, ordered by model-response position with protocol-valid sibling
  denials and exact call/return safe points.
- [x] Direct Bash and non-edit actions treated as speculation barriers;
  `edit_code` is the initial cross-action speculation path.
- [x] Isolated full-copy analysis workspaces with a per-session disk budget and
  fail-closed snapshot creation.
- [x] Result buffering and strict action-sequence retirement, including stable
  priority/registration-order aggregation for multiple same-action hooks.
- [x] Configurable run timeout, post-action patience, intervention quiescence,
  cleanup deadline, and final mandatory drain.
- [x] Later-action invalidation and awaited hook quiescence before restoring the
  selected post-action TaskState, messages, bash history, and filesystem state.
- [x] Callable `HookOptions` metadata with action filters, gate mode, priority,
  timeout, failure policy, restore capability, and barrier strengthening.
- [x] Bundled CodeQL analysis over the isolated revision, exact request/result
  correlation, visible timeout/transport/mismatch failures, workspace close,
  and descendant process-group cleanup.
- [x] Deterministic tests for concurrency bounds, backpressure, reverse
  completion, aggregation, barriers, invalidation, final drain, restoration,
  settings precedence, safe dispatch, provider correlation, and cleanup.
- [x] Live CodeQL/Docker validation at the default concurrency and speculation
  limits (`2`/`2`), including model-driven single- and multiple-finding loops,
  a concurrent broker scan, and a large-worktree SymPy rollback scenario.

### Live validation baseline (2026-07-10)

The opt-in live suite completed all three scenarios on the content-addressed
USEagent image with `3 passed, 46 deselected` in 189.26 seconds. Both
model-driven sessions reached peak hook concurrency `2` and followed the same
ordered sequence: action 1 intervened, the speculative action-2 scan was
cancelled, filesystem and trajectory were restored, and epoch-1 action 3
retired cleanly. Both sessions finalized with `success=true` and no cleanup or
resource-release error.

A separate SymPy large-worktree run used a 105,717,760-byte checkout and a
single changed Python file in sparse CodeQL mode. It completed in 60.63 seconds.
The unsafe action-1 scan found both configured diagnostics in 20.40 seconds
(3.25 seconds database creation and 16.84 seconds analysis); the speculative
action-2 scan was cancelled after the earlier intervention; and the restored
action-3 scan was clean in 9.73 seconds. Three analysis snapshots were created,
with 201,491,888 bytes retained at the two-action peak. Container measurements
peaked at 7,115,177,984 bytes of cgroup memory, 1,843 cgroup tasks, and a
34.67-logical-core instantaneous CPU burst. The final patch used
`ast.literal_eval` and `subprocess.run(shlex.split(command), check=False)`.

This SymPy result measures large-worktree snapshot, overlap, cancellation, and
rollback cost. Because the provider intentionally analyzed only the changed
file, it is not a full-repository SymPy CodeQL benchmark. The host was shared
with unrelated Docker jobs, so cgroup measurements and provider stage timings
are more useful than whole-system load averages. Comparative runs at limits
`1` and `4` remain necessary before retuning the defaults.

The scenario is now encoded as the opt-in
`test_profile_sympy_large_worktree_ordered_orchestration` system test in the
parent repository. It pins the matching upstream SymPy tree, reconstructs the
fixture and task, requires an explicit prebuilt image/model, records cgroup and
disk samples, and asserts findings, rollback, a later clean scan, finalization,
and cleanup. Snapshot events also report total materialization time plus
restore creation, source-size walk, analysis-copy, and copied-tree-size phases.
The adjacent deterministic profile uses ten curated validation-passing AST
queries. The resource-variable full 130-query corpus remains excluded.

## Still Needed

- [ ] Observer hook execution, queue coalescing, and overflow policies. Related
  settings are reserved, but ordered startup rejects observer registrations.
- [ ] A versioned external hook manifest and versioned command-hook wire schema.
- [ ] Comparative CodeQL/Docker tuning across concurrency and speculation
  limits `1`, `2`, and `4`; the current `2`/`2` defaults now have a live
  baseline but have not been selected from a controlled sweep.
- [ ] Alternative immutable materialization for very large workspaces if full
  copies prove too expensive.

## Design Notes

Ordered scheduling uses a per-session ledger. It
allows consecutive `edit_code` analyses to overlap until the worker or
unretired-action bound applies, but only the retirement cursor can select an
intervention. A fast result for action 2 therefore waits behind action 1. The
model/tool dispatcher retains one stateful call and its exact return through a
protocol-safe boundary before retirement can restore state.

Interventions are cooperative at top-level action boundaries. If a hook requests
intervention, the agent loop restores the post-action snapshot captured
immediately after the triggering action completed, cancels remaining hook jobs,
and resumes the Meta-Agent with the hook-provided instruction. This preserves
the triggering action and removes later action attempts from replay. Message
replay is anchored at the triggering checkpoint, so a delayed hook for an
earlier `search_code` action does not keep later `search_code` calls merely
because they share the same tool name.

The current rollback restores in-memory `TaskState`, saved message history,
recorded bash history length, task working-tree files, and local VCS state. For
normal in-tree Git repositories, snapshotting stores refs/HEAD, staged and
unstaged binary patches, and untracked non-ignored files, then restores with Git
plumbing instead of copying the whole repository. Non-Git and unusual Git
layouts fall back to a full task-directory copy. Filesystem restore does not
restore shell process state, dependency installations, services, environment
variables, remote VCS side effects, or effects outside the task working
directory.

When an intervention is pending, USEagent freezes new action admission and
delivers the decision after the active stateful call reaches its exact
protocol-safe return boundary. Later speculative hook work is then cancelled
and awaited before restoration. This cannot undo already completed subprocess
or external service side effects.

## Known Limitations

- Ordered scheduling currently supports gate hooks only.
- Python callbacks must be asynchronous so cancellation and cleanup remain
  bounded.
- Ordered analysis isolation uses one full callback-private directory copy per
  matching hook registration. The normal one-hook case retains one copy;
  multiple same-action hooks consume proportionally more copy time and logical
  snapshot budget so their writes cannot leak into sibling analyses.
- Python hooks are trusted in-process callbacks, not sandboxed code. Isolation
  covers the supplied analysis trees, not deliberately accessed live paths or
  external symlink targets.
- Direct Meta-Agent Bash participates in serialization and barrier gating but
  does not emit a post-action hook event.
- Optimized Git rollback preserves tracked changes, staged changes, local
  refs/HEAD, and untracked non-ignored files. Ignored files are not copied or
  cleaned by the Git-aware path, so dependency directories and build caches are
  intentionally left in place.
- Git-aware restore assumes the repository's `.git` metadata still exists and
  is usable when rollback runs. If a later action deletes or corrupts `.git`,
  the optimized restore can fail because the snapshot does not contain a full
  copy of the Git object database.
- Uncommitted `.gitignore` changes can affect the boundary between ignored and
  non-ignored untracked files. Files treated as ignored at snapshot time remain
  outside Git-aware rollback even if later restored `.gitignore` content would
  classify them differently.
- Git rollback does not undo pushes, remote branch mutations, linked worktree
  gitdirs outside the task directory, submodule repositories outside the copied
  tree, or other external VCS effects.
- Rollback does not undo package installations, running processes, remote
  service calls, or files outside the task working directory.
- An intervention may wait for the active action's protocol-safe return.
- Exceptions raised during post-run wrapper processing, outside an instrumented
  awaited sub-agent boundary, can still bypass hook scheduling.
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
