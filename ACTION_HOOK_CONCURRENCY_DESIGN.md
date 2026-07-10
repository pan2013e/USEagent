# Bounded Concurrent Action Hook Analysis With Ordered Retirement

Status: ordered runtime implemented and required; observer and manifest phases remain planned

This document records both the target concurrency model and the implementation
plan used to build the ordered runtime. Ordered scheduling is the sole
supported mode and implements the gate-hook correctness path: bounded
parallel execution, bounded speculation, captured per-action revisions with
callback-private analysis trees,
ordered retirement, protocol-safe stateful tool dispatch, trajectory
restoration, final draining, and awaited cleanup. Observer hooks, external
hook manifests, and a versioned command-hook wire schema are still future
work. See [HOOK.md](HOOK.md) for the user-facing API and
[TOP_LEVEL_ACTION_HOOKS.md](TOP_LEVEL_ACTION_HOOKS.md) for the concise status.

## Implemented Runtime Boundary

Ordered scheduling is enabled by default. `--action-hook-scheduler ordered` and
`USEAGENT_ACTION_HOOK_SCHEDULER=ordered` remain accepted for explicit
configuration; `legacy` is rejected.

Implemented now:

- one ordered hook session per USEagent task, with monotonic action sequence
  numbers and exact tool-call, hook-job, epoch, and workspace-revision identity;
- a fixed per-session hook worker pool and a separate bound on unretired gate
  actions;
- queued excess hook jobs plus blocking new-action admission when the unretired
  action bound prevents safe progress;
- isolated per-hook full-copy analysis trees and retained post-action restore
  points;
- deterministic action-order retirement and priority-order aggregation for
  multiple hooks on one action;
- public Pydantic AI deferred-approval dispatch that approves one stateful call
  in model-response order and returns protocol-valid denials for its siblings;
- barriers for direct Bash and the non-edit top-level actions;
- intervention quiescence, later-action invalidation, post-action trajectory
  restoration, final output draining, and awaited session cleanup;
- exact correlation and workspace cleanup for the bundled CodeQL feedback hook;
- strict CLI-over-environment configuration validation with ordered-only mode
  selection and rejection of the retired per-action wait.

Deliberately deferred from the broader target design:

- observer hook execution and observer overflow/coalescing policies;
- a versioned external hook manifest and versioned command-hook event schema;
- an OS-backed, path-keyed workspace lease across independent managers or
  processes. The implemented manager owns one process-local active session and
  that session serializes its own stateful actions, but it cannot exclude an
  unrelated writer;
- read-only action classes or concurrent stateful tool execution;
- transactional rollback for processes, remote services, installations, or
  other effects outside the documented local restore boundary.

## Decision Summary

USEagent should support parallel hook analysis across consecutive agent
actions, while applying hook decisions in deterministic action order. The
design retains trajectory rollback and restoration.

The selected model is:

- Run hook analysis concurrently, subject to a per-agent-session concurrency
  limit.
- Bound speculation separately from execution concurrency. Only a configured
  number of admitted, running, or completed gate actions may remain unretired.
- Queue gate jobs when every hook worker is occupied. Worker saturation alone
  does not delay protocol finalization; the speculation window blocks the next
  action before mutation when the configured unretired-action limit is full.
- Assign a monotonic sequence and captured workspace revision identity to every
  hooked action, and give every matching hook job a private materialization of
  that revision.
- Buffer out-of-order hook results and retire them strictly in action order.
- If an action's retired decision is a restoring intervention, cancel and await
  later work, restore that action's post-action restore point, discard later
  speculative trajectory, and resume the Meta-Agent with the hook instruction.
- If the intervention is non-restoring, preserve the current trajectory but
  wait for a protocol-safe boundary before delivering its instruction.
- Treat final agent output as another gate: mandatory hook work must retire or
  reach an explicit timeout policy before the session can finish.

This is a hybrid of concurrent analysis and serialized commit. Hook completion
order must never decide which restore point wins.

## Motivation

The predecessor hook manager launched one task for every registered hook after
a top-level action. USEagent core used a zero post-action wait while the bundled
repository runner injected a 60-second wait. The manager had one global
intervention slot in either case: the first hook to complete with an
intervention won and later intervention decisions were ignored.

That behavior creates several correctness risks:

- A delayed hook for action 1 can complete after action 2 and roll the agent
  back to action 1, while a faster hook for action 2 would cause the opposite
  result.
- The current generation value is a runtime reset epoch, not an action
  revision. Starting or completing a newer action does not make an older hook
  result stale.
- Hook events contain copied in-memory state and rollback snapshot metadata,
  but integrations may analyze the live workspace path. A scan associated with
  action 1 can therefore observe changes from action 2.
- The agent can finish while hooks are still running. Current finalization
  requests cancellation but does not await hook task and external resource
  termination.
- Multiple top-level tool calls from one model response may execute in
  parallel, so state-mutating actions can bypass an otherwise sequential
  trajectory assumption.
- A concurrency limit alone bounds resource use but does not bound rollback
  depth. A slow action-1 hook and fast later hooks could allow an arbitrarily
  long speculative trajectory.

The target design makes resource bounds, action ordering, rollback ownership,
and terminal behavior explicit.

## Alternatives Considered

### Fully serialize action and hook completion

This is the simplest consistency model: after every action, wait for all hooks
and apply any decision before continuing. It is a useful compatibility and
debugging configuration (`concurrency=1`, `speculation=1`), but it gives up the
cross-edit analysis overlap that motivates the redesign.

### Allow unrestricted concurrent hooks and use first completion

This maximizes overlap but makes the trajectory depend on latency. A later
action can win merely because its analysis is faster, or an older result can
restore over newer work after observing the wrong live workspace revision.
Adding only a semaphore limits CPU/process pressure; it does not fix ordering,
revision identity, or rollback depth.

### Cancel older or newer analysis when another edit arrives

Always cancelling older work discards the analysis most likely to explain the
first problematic edit. Always cancelling newer work wastes completed analysis
and still needs an ordering rule. Cancellation is appropriate after a selected
intervention invalidates a branch, not as the normal decision policy.

### Selected hybrid

Run analyses in parallel over immutable revisions, but serialize state mutation
and retire decisions in action order. This preserves useful overlap while
making the outcome deterministic. The speculation window and hook-run limit
provide separate bounds for rollback liability and physical resource use.

## Goals

- Preserve useful overlap between agent execution and hook analysis.
- Preserve post-action trajectory rollback for intervention decisions.
- Make decision application independent of hook completion timing.
- Bound the number of running hook jobs for one agent session.
- Bound the number of unresolved speculative action checkpoints.
- Prevent concurrent mutation of shared `TaskState`, message history, and the
  task workspace by top-level actions.
- Bind every hook result to the exact action and workspace revision it
  analyzed.
- Make timeouts and hook failures visible policy outcomes rather than implicit
  clean results.
- Give every hook task, subprocess, snapshot, and backend request a clear
  session owner and terminal cleanup path.
- Preserve current hook APIs where compatibility does not compromise the new
  invariants.

## Non-Goals

- Roll back processes, installed packages, remote services, remote VCS effects,
  or files outside the task working directory.
- Make arbitrary synchronous hook code safely preemptible.
- Guarantee useful parallel speedup from a backend that internally serializes
  all analysis.
- Preserve first-completion-wins intervention behavior.
- Allow unbounded background work in the name of non-blocking hooks.

## Terminology

- **Agent session:** one execution of a USEagent task. All mutable hook
  scheduler state belongs to this session rather than to a process-global
  runtime singleton. Initial ordered-mode support allows one active session per
  process and workspace; broader concurrent-session support requires the
  surrounding runtime globals to be sessionized too.
- **Epoch:** a branch of the agent trajectory. Applying a restoring
  intervention advances the epoch. Results from an older epoch are stale and
  cannot affect the new trajectory.
- **Action sequence:** a monotonically increasing integer assigned when a
  top-level action is admitted. Sequence numbers provide the total order used
  for decision retirement.
- **Pre-action checkpoint:** the message, `TaskState`, and bash-history boundary
  captured before the triggering action starts. It identifies where that exact
  tool call begins.
- **Post-action restore point:** the state required to preserve the completed
  triggering action while discarding later actions. It contains post-action
  `TaskState`, message and bash-history boundaries, and supported filesystem/VCS
  restoration data. The current implementation combines a pre-action
  `ActionCheckpoint` with post-action state stored on the hook event; the
  ordered design should name these two concepts explicitly.
- **Workspace revision:** a stable identity for the captured post-action
  filesystem state. Each hook job receives a private materialization that
  starts with that content; the materialization itself is writable scratch and
  is deleted after analysis. A mutable live path alone is not a revision.
- **Hook registration:** a hook callback plus scheduler metadata such as action
  filters, mode, priority, timeout, and failure policy.
- **Hook job:** one execution of one hook registration for one action sequence.
- **Action record:** session-owned state for an admitted action, its pre-action
  checkpoint, post-action restore point, matching hook jobs, collected results,
  and retirement outcome.
- **Retirement cursor:** the lowest action sequence whose gate-hook outcome has
  not yet been committed to the trajectory.
- **Speculative action:** a completed action newer than the retirement cursor.
  It is visible to ongoing agent execution but can still be discarded if an
  earlier action intervenes.

## Required Invariants

The implementation must maintain all of the following invariants:

1. At most one state-mutating Meta-Agent action or direct tool executes at a
   time within an agent session.
2. No more than `max_concurrent_hook_runs` hook jobs execute concurrently in a
   session.
3. No more than `max_unretired_actions` admitted, running, or completed gate
   action records remain unresolved in a session.
4. Every admitted action has a unique `(session_id, epoch, action_seq)` identity.
5. Every accepted hook result matches its session, epoch, action sequence,
   pre-action checkpoint, post-action restore point, hook registration, and
   workspace revision.
6. Gate-hook decisions retire strictly by action sequence, never by completion
   order.
7. Only the retirement cursor may select an intervention, and intervention
   application is non-reentrant.
8. Checkpoint and snapshot data remain available until the owning action is
   retired or invalidated by an earlier intervention.
9. A final agent result is not committed while mandatory gate-hook decisions
   remain unresolved.
10. Session shutdown cancels and awaits owned tasks before diagnostics and
    snapshots are finalized.
11. Every retained model tool call has a protocol-valid return, including calls
    deferred or invalidated by scheduler backpressure.
12. Destructive restoration occurs only after process-local action/dispatcher
    ownership is released and later session-owned work is quiescent. Excluding
    independent managers, processes, and external writers requires the deferred
    path-keyed workspace lease.
13. Hook jobs for the same action never share an analysis tree, and callbacks
    never receive the scheduler-owned rollback snapshot path.
14. A backend lease-release failure aborts ordered execution and retains the
    affected revision tree until an operator can establish backend quiescence.

Assertions for these invariants should remain enabled in tests and should be
represented by structured diagnostics in normal runs.

## Hook Modes

### Gate hooks

Gate hooks may request an intervention and may restore a post-action restore
point. They are part of the trajectory consistency protocol.

- Their running jobs count against the concurrency limit, and their owning
  action records count against the speculation limit.
- Their results must retire in action order.
- Their unresolved action record can block the next action when the separate
  speculation bound is full.
- Their timeout and failure behavior must be configured explicitly.
- The final session result must wait for their retirement.

Code analysis, policy enforcement, and guardrail feedback hooks should normally
use this mode.

### Observer hooks

Observer hooks collect telemetry or advisory information but cannot restore an
old trajectory restore point.

- They count against the session's hard hook execution limit.
- They do not hold the gate-hook retirement cursor.
- They may be coalesced or superseded according to their registration policy.
- A late observer result may be logged or attached to current state only after
  revision validation; it must not rewind the trajectory.
- Session shutdown still owns and awaits their cancellation.

Metadata-free hooks are treated as gate hooks because the scheduler cannot know
in advance whether an arbitrary callback will return an intervention. Explicit
observer registration will opt into weaker delivery semantics once observer
execution is implemented.

## Registration Metadata

The public callback signature can remain compatible, but the registry needs
metadata equivalent to:

```text
HookRegistration
  id: stable string
  callback: Python or command hook
  execution: async | process
  actions: set of top-level action names
  mode: gate | observer
  priority: integer
  timeout_seconds: float | null
  failure_policy: continue | intervene | abort
  observer_policy: every | latest
  observer_queue_capacity: integer
  observer_overflow: drop_oldest | drop_newest | fail
  can_restore: bool
  requires_speculation_barrier: bool
```

The pilot exposes the implemented subset through
`ActionHookManager.register(hook, *, options=HookOptions(...))`. A hook loaded
from an existing `module:function` or file-function spec can attach the same
object as `hook.__useagent_hook_options__`; the loader validates it before
registration. A decorator and versioned TOML manifest remain roadmap items.
The pilot `HookOptions` fields are `id`, `actions`, `mode`, `execution`,
`priority`, `timeout_seconds`, `failure_policy`, `can_restore`, and
`requires_speculation_barrier`. Observer-specific policy fields remain part of
the target schema rather than the implemented dataclass.

A metadata-free registration maps to `actions=all`, `mode=gate`,
`priority=0`, `timeout_seconds=null` (use the global run timeout),
`failure_policy=continue`, `can_restore=true`,
`requires_speculation_barrier=false`, and stable CLI/registration order.
Command specs are already represented by an asynchronous, cancellation-aware
subprocess wrapper. Ordered mode rejects synchronous Python callbacks instead
of moving them to a hidden thread or implicit worker. The metadata-free
defaults use `failure_policy=continue`, while guardrail registrations should
explicitly select `failure_policy=intervene`.

Observer queue settings are reserved and validated, but ordered startup rejects
observer registrations in the pilot.
Ordered-mode `execution=async` is a contract that the callback and everything
it awaits are cancellation-aware; hidden thread work violates registration and
is treated as a hook infrastructure error.

An observer result that requests intervention or restoration is a contract
violation. It becomes a failed observer outcome and never mutates trajectory
state. Likewise, a `can_restore=false` gate that requests restoration produces
a failed gate outcome; its configured failure policy then determines whether
retirement continues, intervenes without restoration, or aborts.

Declaring action filters at registration time is important. The current manager
creates a potentially expensive filesystem snapshot for every top-level action
whenever any hook is registered, even when a hook immediately returns a no-op
for most action names. Scheduler-visible filters allow USEagent to avoid unused
snapshots and jobs.

## Session State Model

Mutable scheduler state should move from the process-global manager into an
`ActionHookSession` created for each task run. A compatibility facade may expose
the current public manager name, but it must resolve to the active session and
must not share runtime state between concurrent or successive tasks.

Conceptually, the session owns:

```text
ActionHookSession
  session_id
  epoch
  next_model_turn_seq
  active_model_request
  next_action_seq
  retirement_cursor
  action_records[action_seq]
  running_jobs[job_id]
  queued_jobs
  action_lock
  hook_capacity
  state_changed_condition
  task_group
  diagnostics
  accepting_actions
  closing
```

This sessionization is necessary but not sufficient for two simultaneous tasks
in one Python process. Bash history/tool initialization, the edit tool's project
directory, and `tools.meta.USAGE_TRACKER` are currently process globals, and
checkpoint restoration reads some of that global state. The implemented global
manager owns one process-local ordered session and that session's action lock
serializes its stateful calls. Sequential reuse is supported, but this is not a
canonical-workspace lease and does not exclude another manager, another
process, or an unrelated external writer.

A future multi-session runtime needs a process-wide owner plus an OS advisory
lock keyed by the canonical workspace path and stored outside the task tree, so
the lock itself is never captured in revisions. It must acquire that lease
before mutation, retain it through result commit and cleanup, and emit explicit
acquire/release diagnostics. Those cross-manager/process guarantees and events
are deferred rather than properties of the current runtime.

An action record moves through these states:

```text
reserved
  -> running
  -> action_succeeded | action_failed
  -> snapshotted
  -> return_finalizing
  -> protocol_finalized
  -> hooks_pending
  -> ready_to_retire
  -> retired_noop | intervened | aborted | invalidated
```

The displayed states summarize two coordinated tracks: hook analysis may start
as soon as the immutable post-attempt revision exists, while
`protocol_finalized` is owned by the dispatcher. An action is
`ready_to_retire` only when its exact call/return pair is finalized and every
mandatory hook job has a terminal outcome. A fast hook can therefore be
buffered before the tool wrapper reaches the restoration safe point.

A hook job moves through these states:

```text
queued
  -> running
  -> completed | failed | cancelled | stale

running -> timing_out -> timed_out | cleanup_failed

queued -> timed_out_before_start | cancelled | superseded
```

All state transitions occur on the session event loop. Done callbacks may wake
the scheduler, but they should not directly apply interventions or destroy
checkpoints.

## Action Admission And Serialization

The scheduler must be authoritative even if the model or agent framework emits
multiple tool calls in one response.

Before a top-level action starts:

1. Reject admission if the session is closing or an intervention is being
   applied.
2. At the model-response dispatcher, assign each call a response-batch identity
   and part index. Stateful siblings enter admission in part-index order; event
   loop scheduling order is not a trajectory-ordering primitive.
3. Enter the action-admission queue and acquire the session's top-level action
   ownership token.
4. Atomically wait until the number of unretired gate action records is below
   `max_unretired_actions`, then reserve an action record before releasing the
   scheduler condition. Admitted and running records count against the limit,
   not only completed actions.
5. Assign `(epoch, action_seq)` and capture the pre-action message and task
   checkpoint.
6. Execute the action and capture either its value or its exception.
7. Capture the post-attempt restore point and materialize its workspace
   revision.
8. Create and enqueue the matching hook jobs. Jobs beyond the fixed worker
   capacity remain queued; lack of a worker slot does not delay protocol
   finalization.
9. Hand the value or error to the dispatcher, retaining action ownership while
   it records or synthesizes the exact `ToolReturnPart` for the triggering
   `ToolCallPart`.
10. After the dispatcher signals `protocol_finalized`, release action ownership.
    Give the scheduler a retirement transition before starting the next model
    turn. Only then may ordered retirement apply an intervention or restoration;
    if the cursor is still pending, normal bounded speculation rules decide
    whether the model may continue.

Serializing all top-level actions is the safe first implementation because
nominally read-only actions can invoke shell commands or sub-agents with side
effects. A later optimization may introduce explicit read-only action classes,
but model-provided intent is not sufficient to classify mutation safely.

Provider-side controls that discourage parallel tool calls should also be used
where supported, but the runtime action lock remains the correctness boundary.

This safe point requires integration above the individual wrapper in
`useagent/tools/meta.py`: during a tool call, `RunContext.messages` does not yet
contain that call's return, and a wrapper cannot know when a sibling batch has
been committed. Ordered mode therefore needs a dispatcher/toolset coordinator
in the agent runtime. It must retain the serialized triggering call, actual or
synthesized return, batch identity, and response-part index. A hook may finish
before that point, but its decision remains buffered and cannot restore the
workspace while the dispatcher owns the action.

An uncaught action exception is a speculation barrier. USEagent captures the
post-attempt state and exact exception on the hook event, runs the matching gate
hooks, and waits for that action to retire before allowing later stateful
actions. An intervention supersedes normal propagation and resumes the agent
through the intervention path; an `abort` policy raises the hook infrastructure
error with the original exception chained; otherwise USEagent propagates the
original action error after cleanup. If an older intervention cancels an action
before it reaches its own post-attempt boundary, that action is `invalidated`,
creates no hook jobs, produces the dispatcher-required cancellation return, and
releases its action slot and partial snapshot data exactly once.

### Parallel tool-call batches

The action lock prevents simultaneous mutation, but it does not by itself make
a model response containing several stateful tool calls protocol-safe. The
framework may create all sibling tool tasks together and expect one return for
each call in the same response batch.

Ordered mode therefore needs a tool-dispatch rule implemented before Pydantic
AI launches concurrent tool wrappers:

1. Identify stateful top-level calls in the model response using exact
   tool-call identifiers and response-part indexes.
2. Admit at most one stateful call from that response into trajectory execution.
3. Return a protocol-valid deferred/retry result for later sibling stateful
   calls, instructing the model to resubmit them after the admitted action and
   its scheduler boundary.
4. Permit genuinely read-only calls to run concurrently only after they have an
   explicit classification and cannot observe partially restored state.
5. Preserve enough serialized data to reconstruct every exact call/return pair
   when constructing replay history; a tool-call identifier alone is not
   sufficient before the framework appends batch returns.

If an earlier intervention invalidates a sibling before the framework produces
its normal return, the coordinator synthesizes a deterministic cancellation or
deferral return for that call. The batch remains protocol-valid even though the
invalidated action never receives hooks.

Disabling provider-side parallel tool calls reduces how often this path is
needed, but runtime handling remains mandatory because model behavior and
provider support are not universal.

The pilot implements this rule with Pydantic AI's public deferred-tool API; no
framework fork or private-graph monkey patch is required. Stateful tools are
registered with `requires_approval=True`. When the model emits one or more of
them, USEagent reads the retained `ModelResponse`, sorts the deferred calls by
their response-part position, approves the first call, and returns
`ToolDenied` for its siblings with an instruction to resubmit. It then drives
`CallToolsNode` through the following `ModelRequestNode`, captures the exact
call/return message boundary, and calls `protocol_finalized` before allowing a
decision to restore trajectory state. `parallel_tool_calls=false` remains
defense in depth; exact response ordering and deferred approval are the runtime
invariant.

### Speculation barriers

USEagent must not speculate across an action whose important effects are
outside the supported rollback boundary. The effective barrier is the logical
OR of built-in action policy and `requires_speculation_barrier` on every
matching registration. A hook manifest can strengthen but never weaken the
built-in policy. Reclassifying a built-in barrier requires a separate trusted
repository policy and evidence that all of the action's effects are isolated
and disposable.

At a barrier, the scheduler waits for all earlier gate decisions to retire,
runs the action without later speculative siblings, and retires its required
hooks before admitting the next stateful action. Built-in barriers initially
include `probe_environment`, `search_code`, `execute_tests`, and `vcs`, because
their sub-agents can execute unrestricted shell commands, plus the Meta-Agent's
direct `bash_tool`. The direct Bash call is routed through scheduler admission
even though the current hook API does not emit a post-action hook for it.
`edit_code` is the initial speculatable action because it is the path for which
the captured-analysis and local-restoration contract is being built.

The dispatcher must cover both the five wrappers in `useagent/tools/meta.py`
and direct stateful tools registered in `useagent/agents/meta/agent.py`.
Wrapping only the former would allow Bash to mutate files, install packages, or
start services while an earlier gate remains unresolved.

This does not make external effects transactional. It prevents an earlier
hook's local rollback from pretending those later effects were undone.

## Bounded Hook Execution

`max_concurrent_hook_runs` is a positive, per-session hard limit. Every running
Python or command hook consumes one permit. A permit is released only after the
hook coroutine and its owned external resources reach a terminal state.

After an action snapshot is ready:

1. Create gate and observer jobs for registrations matching the action.
2. Enqueue jobs in deterministic order: action sequence, gate before observer,
   registration priority, then registration order.
3. Workers start jobs while execution capacity is available; excess jobs remain
   in the deterministic session queue.
4. Once every matching job is durably queued, the action may reach protocol
   finalization without waiting for a worker slot or hook completion, unless the
   configured post-action patience period requires a longer wait.
5. The separate unretired-action bound supplies agent backpressure: when that
   window is full, the next stateful action blocks before mutation until ordered
   retirement advances or invalidation frees a record.

This separation caps physical hook execution without turning a same-action job
queue into an implicit serialized wait. The queue remains bounded by the finite
registration set and the configured number of unretired action records.

The scheduler must release permits in `finally` paths for success, exception,
timeout, and cancellation. Observer coalescing must happen before admission so
superseded observer jobs do not consume permits.

Observer queues must also be bounded. A latest-only observer replaces its
queued predecessor. An every-event observer uses a configured finite queue and
an explicit overflow policy; it must not create unbounded tasks or retain
unbounded event snapshots. `drop_oldest` and `drop_newest` give the displaced
job a terminal `superseded` result; `fail` records a failed observer outcome.
None of these policies can create an intervention.

An action with no matching gate registrations does not occupy the gate
speculation window. If it has observer jobs only, those jobs follow observer
queue policy and the action can retire immediately from the trajectory
consistency protocol.

## Bounded Speculation

`max_unretired_actions` is independent of the execution limit. It bounds the
number of admitted, running, or completed gate action records at or after the
retirement cursor.

This limit is necessary even when hook execution is bounded. Consider a slow
hook for action 1 while hooks for actions 2, 3, and 4 finish quickly. Completed
jobs release concurrency permits, but action 1 still prevents later decisions
from retiring. Without a separate speculation limit, the agent could continue
creating checkpoints and rollback liability indefinitely.

When the speculation window is full, the next top-level action blocks before it
mutates state. It resumes only after retirement advances, an intervention
invalidates the speculative branch, or the session closes.

Useful configurations include:

- concurrency `1`, speculation `1`: fully serialized gate behavior;
- concurrency `N`, speculation `N`: a simple bounded analysis pipeline;
- concurrency `N`, speculation greater than `N`: deeper bounded speculation
  when analysis latency and rollback cost justify it.

The initial ordered-scheduler default should be concurrency `2` and speculation
`2`, subject to benchmark validation. Both remain configurable.

## Immutable Analysis Revisions

Concurrent analysis is correct only when each hook analyzes the state named by
its event.

Each post-action record must therefore contain:

- a stable workspace revision identifier;
- one private materialized path per matching hook job, or an equivalent
  copy-on-write input that prevents sibling hooks from observing writes;
- the post-action `TaskState` copy;
- the post-action bash-history boundary;
- the message/tool-call identity of the exact triggering action;
- restoration data for supported filesystem and VCS state.

A digest of a mutable working directory is not sufficient unless the analyzer
also starts from captured content matching that digest. Sharing one writable
materialization between concurrent hooks is also insufficient. For Git
workspaces, possible implementations include private detached worktrees plus
captured dirty patches, private copy-on-write trees, or fully materialized
snapshot trees. Non-Git workspaces require private copies or another isolated
filesystem snapshot mechanism.

The current implementation creates one full private directory copy per
matching hook registration before any of those jobs are queued. Consequently,
the common bundled-CodeQL configuration still retains one analysis copy, while
N same-action hooks retain N logical copies and charge all N against the
snapshot budget. Filesystem-native copy-on-write clones are a possible future
optimization. Python callbacks are trusted in-process extensions, not a
security boundary: isolation applies when they analyze the supplied path and
does not prevent deliberate access to external symlink targets or the live path
discoverable through other process APIs.

If an integration cannot analyze an immutable revision, it must use a strict
per-action barrier with a speculation limit of one for that hook. Live-workspace
background analysis must not retain rollback authority over an older
checkpoint.

The existing snapshot and restore helpers perform blocking copies and Git
subprocess work. The async scheduler must execute that work in an owned worker
or asynchronous subprocess path while retaining exclusive action/workspace
ownership; cancellation is shielded until the operation reaches a known
boundary, and session shutdown awaits it. Large snapshot creation or restore
must not block the event loop that drives hook completion and cancellation.

If creation of a required isolated analysis input or rollback restore point
fails, USEagent records a scheduler-integrity failure and aborts ordered mode
after structured cleanup. Hook `failure_policy` governs callback execution, not
missing scheduler state, so even `continue` cannot make this fail open. A future
explicit serial-live mode could analyze before releasing action ownership when
only the isolated analysis copy failed and restoration data remains valid, but
that fallback is not part of the initial ordered contract.

Analysis inputs and restoration data may have different lifetimes. Delete each
only after all hook, backend, and rollback consumers have released it. Enforce a
session snapshot disk budget; budget exhaustion follows the same explicit
abort policy as required snapshot creation failure.

## Result Collection And Ordered Retirement

Hook tasks never write directly to a global intervention slot. They submit a
`HookJobResult` to their owning action record. A result is accepted only after
validating all identity fields.

Out-of-order results remain buffered. The retirement loop examines only the
current cursor:

1. If the cursor's dispatcher-owned call/return pair is not yet finalized, or
   mandatory jobs are still queued or running, stop.
2. Convert failures and timeouts according to registration policy.
3. Aggregate all decisions for that action deterministically.
4. If the aggregate is a no-op, mark the action retired, release its rollback
   data when no longer needed, advance the cursor, and examine the next action.
5. If the aggregate intervenes, freeze admission and execute the intervention
   protocol.

Completion of action `N + 1` cannot retire while action `N` is unresolved, even
if every action-`N + 1` hook completed first.

Retirement is a scheduler transition, not a sequence of condition wake-ups.
Once the cursor is ready, retire every contiguous ready no-op record under the
same scheduler lock before notifying blocked action admission. If any record in
that contiguous prefix selects an intervention, freeze admission without first
waking it. This prevents action `N + 2` from slipping between a no-op for
action `N` and an already-buffered intervention for action `N + 1`.

Restoration is also safe-point gated. An immediately completed action-`N` hook
may select a decision in memory, but destructive application waits until the
dispatcher has finalized action `N`'s return and released action ownership.

### Same-action decision aggregation

First convert each terminal job through its failure policy, then aggregate with
the fixed precedence `abort > intervene > continue/noop`. Thus one abort cannot
be hidden by a faster intervention or fail-open result. When multiple gate
hooks intervene for one action:

1. Sort interventions by descending registration priority and stable
   registration order.
2. Use the first intervention as the controlling decision for the primary
   instruction.
3. Attach lower-priority findings as supplemental feedback in the same stable
   order.
4. Merge `additional_knowledge` deterministically; the controlling decision
   wins key collisions, and collisions are recorded diagnostically.
5. Request restoration if any accepted intervention for the action requests
   it, subject to the configured restore policy. This prevents a valid restore
   request from being lost merely because another hook has higher instruction
   priority.
6. Apply restore-policy downgrades after aggregation so the logged aggregate
   matches the instruction actually delivered to the agent.

This avoids both completion-order nondeterminism and silent loss of independent
findings.

## Intervention And Trajectory Restoration

The proposal retains the current post-action restoration semantics. If action
`N` intervenes with restoration enabled, action `N` remains part of the
trajectory and actions newer than `N` are discarded.

Any selected intervention first freezes tool dispatch and invalidates the
active model request, if one exists. Model requests are session-owned tasks
tagged with `(session_id, epoch, model_turn_seq)`. USEagent requests
cancellation, awaits the task through the quiescence deadline, and discards its
response and any provisional response parts rather than dispatching stale tool
calls. A response is revalidated immediately before it is appended or executed,
so a provider that ignores cancellation still cannot continue the pre-feedback
turn. Failure to quiesce the request aborts the task. This rule applies to both
restoring and non-restoring interventions.

After the shared model-request quiescence above, a restoring intervention uses
this protocol:

1. Set the session state to `intervening` and stop admitting actions and jobs.
2. Advance the epoch immediately so late results from the old branch are
   rejected.
3. After the model request is quiescent, request cancellation of the currently
   running later action, if any.
4. Cancel all queued and running hook jobs belonging to later action sequences.
5. Await action quiescence and owned resource cleanup up to
   `intervention_quiesce_seconds`. If action ownership cannot be recovered,
   abort the task without attempting destructive restoration.
6. Verify that no process-local dispatcher/action token is live. A content
   comparison with the latest completed revision is diagnostic only: a
   cancelled newer action may have made legitimate partial changes, so equality
   cannot distinguish those from an external writer. The current runtime cannot
   exclude external writers; a future path-keyed workspace lease must close that
   ownership gap.
7. Restore post-action `TaskState`, bash history, messages, filesystem content,
   and supported local VCS state for action `N`.
8. Remove later action records, snapshots, and buffered results.
9. Merge the intervention's additional knowledge.
10. Resume the Meta-Agent in the new epoch using the aggregated instruction.

If restoration itself fails or verification cannot establish the selected
post-action state, USEagent records the partial-restore diagnostics, retains the
relevant snapshot for debugging, and aborts the task. It never resumes the
agent on a workspace whose trajectory is unknown.

Message replay must be anchored by the triggering tool-call identifier, not
only by action name. This is required to distinguish repeated or concurrently
emitted calls to the same top-level action.

For `restore_to_checkpoint=False`, USEagent still pauses at retirement and
delivers the instruction, but it preserves current trajectory state. The
decision remains ordered; non-restoring interventions do not gain permission to
apply out of sequence. The scheduler stops new admission and lets any active
newer action reach a protocol-finalized safe action boundary instead of
canceling it mid-mutation. This wait uses the same quiescence deadline. If the
action does not reach the boundary, USEagent requests cancellation and aborts
the task after cleanup; it must not inject feedback onto an unknown partial
state. Because no restore occurs on the successful path, the epoch need not
change and later exact-revision results may continue through ordered retirement.

Rollback retains its current boundary. It does not undo running processes,
package installation, services, remote API effects, remote VCS changes, ignored
files excluded by Git-aware snapshots, or files outside the task workspace.

## Worked Ordering Example

Assume concurrency `2` and speculation `2`:

```text
edit_code_1 completes -> Hook 1 starts
edit_code_2 completes -> Hook 2 starts
edit_code_3 requested  -> blocks before execution; speculation window is full

Hook 2 completes first -> result is buffered for action 2
Hook 1 still running   -> retirement cursor remains at action 1
```

If Hook 1 returns a no-op, action 1 retires. The scheduler immediately examines
the already completed action-2 result. If Hook 2 also returns a no-op, both
records retire and `edit_code_3` may start.

If Hook 1 intervenes, Hook 2 and action 2 are invalidated, the post-action-1
restore point is applied, and the Meta-Agent resumes from action 1 with Hook 1's
instruction.

If Hook 1 returns a no-op and Hook 2 intervenes, action 1 retires first, then
action 2 applies its intervention. The post-action-2 restore point is applied and
only actions newer than action 2 are discarded.

## Timeout And Failure Policy

Every terminal hook outcome must be distinguishable:

- `noop`
- `intervene`
- `failed`
- `timed_out_before_start`
- `timed_out`
- `cleanup_failed`
- `cancelled`
- `superseded`
- `stale`
- `suppressed_limit`

Gate-hook registrations choose one of these failure policies:

- **continue:** retire the failure as a no-op after recording that validation
  did not complete. This is fail-open and should not be described as a clean
  result.
- **intervene:** synthesize a visible intervention instructing the agent that
  the validator failed or timed out. This is the recommended default for
  guardrails.
- **abort:** end the task with a hook infrastructure error after structured
  cleanup. This is appropriate only when running without the validator would
  invalidate the task result.

Observer failures are diagnostic unless an observer is explicitly promoted to
a gate.

The run-timeout clock starts only when a job enters `running` and acquires its
execution permit. Normal queue time is bounded by admission backpressure, not
misreported as callback execution time. If the session finalization deadline
expires while a job is still queued, it receives
`timed_out_before_start`. A running job first becomes `timing_out`, retains its
permit and revision lease, and receives cancellation/termination. It becomes
`timed_out` only after every owned resource reaches a terminal state; only then
does its registration failure policy run and retirement proceed. Failure to
terminate by the hard cleanup deadline becomes `cleanup_failed` and aborts the
task rather than applying a fail-open policy.

The existing `MAX_ACTION_HOOK_INTERVENTIONS` behavior remains fail-soft. Once
the intervention limit is exceeded, the cursor converts each later aggregate
intervention to the terminal `suppressed_limit` outcome, emits a warning,
retires it as a fail-open no-op, and releases its snapshots. This explicit
outcome advances retirement; it cannot leave the cursor waiting forever. The
counter increments when an aggregate intervention is actually selected,
including one synthesized by `failure_policy=intervene`, not once per hook
finding.

## Session Finalization

An agent result is provisional until session finalization completes.

Finalization follows this sequence:

1. Stop admitting new actions and observer jobs.
2. Wait for all queued or running gate jobs, then run ordered retirement as
   results arrive, up to the configured finalization deadline.
3. At the deadline, mark every unresolved queued job
   `timed_out_before_start`; move every unresolved running job to `timing_out`,
   request cancellation/termination, and retain all permits and leases.
4. Await owned resource termination up to the hard cleanup deadline. Mark a
   terminated job `timed_out`; if any mandatory resource cannot terminate or
   transfer to a bounded observer-only provider, select `cleanup_failed` and
   abort.
5. Apply registration failure policies and run ordered retirement again until
   the cursor reaches an unresolved record, selects an intervention, selects an
   abort, or drains all gate records.
6. If retirement produces an intervention, discard the provisional result,
   finish cleanup of invalidated work, clear `closing`, reopen action admission,
   and resume the same session instead of finishing.
7. If retirement produces an abort, complete structured cleanup and raise the
   hook infrastructure error.
8. Cancel observer jobs and any invalidated gate jobs.
9. Await all remaining session-owned tasks and external resource cleanup.
10. Clean remaining snapshots and backend revision leases.
11. Commit the agent result after the process-local session has drained its
    mandatory work and cleanup.
12. Append `session_finalization_completed`, persist final hook diagnostics,
    and close the process-local session.

A future path-keyed workspace lease must remain held through steps 1-11 and be
released before the final diagnostic is persisted; the current runtime does not
emit lease acquire/release events.

Calling `Task.cancel()` without subsequently awaiting the task is insufficient.
Cancellation is a request delivered cooperatively by the event loop. The
session must remain alive until cleanup handlers have run or the bounded
shutdown policy records a forced-cleanup failure. Failure to terminate a
mandatory gate resource aborts the task; it is not silently left running with
gate authority. Work may outlive the session only after explicit transfer to a
separately bounded detached provider, in which case its result is observer-only.

## External Resource Ownership

### Python hooks

Asynchronous, cancellation-aware hooks run in session-owned tasks. Ordered mode
rejects synchronous Python hooks unless they are adapted to a killable child
process with the same lifecycle contract as a command hook. A thread-pool
timeout cannot stop its underlying thread: releasing the permit would violate
the hard concurrency limit, while retaining it could prevent bounded shutdown.
There is no synchronous in-process fallback. The bundled CodeQL hook must also
remain cancellation-aware and avoid non-preemptible thread work to qualify as a
strict gate hook.

### Command hooks

Each command hook owns a process group. On timeout, intervention invalidation,
or session shutdown, USEagent must terminate the group, wait for a grace period,
kill it if necessary, await process exit, and then release the hook permit.

### Backend requests

Prefer cancellation-aware asynchronous clients. A session-owned remote job
retains its hook permit and revision lease until the backend confirms a terminal
state, even if the local request coroutine is cancelled. The backend therefore
needs cancellation or lease-expiry semantics plus terminal-status polling.
Alternatively, an integration may explicitly detach work to a separately
bounded provider queue; detached work loses gate authority and its eventual
result is observer-only. Identity validation still rejects late or mismatched
results, but identity alone does not satisfy physical resource bounds.

## Backend Correlation Contract

An analysis backend used by a gate hook must accept and return fields equivalent
to:

```text
session_id
epoch
action_seq
hook_job_id
workspace_revision
snapshot_location or immutable content reference
```

The pilot CodeQL wire payload uses the first five identity/reference values
above except `snapshot_location`: `workspace_revision` names the isolated tree,
while the scheduler retains `checkpoint_id` internally as part of the owning
action record.

The response must identify the exact job and revision it analyzed. Responses
such as "latest cached result" are insufficient for rollback-capable hooks.
Versioned diagnostics and backend payloads use `cancelled` consistently as the
wire spelling.

Mandatory gate jobs are not silently coalesced: each must run or reach an
explicit failure outcome governed by its registration policy. Coalescing is
allowed for explicitly latest-only observer work. Every displaced observer
request receives an explicit `superseded` outcome. A caller waiting for action
2 must not wake merely because the action-1 scan completed.

The CodeQL broker runs one scan at a time per workspace. Ordered events use a
unique copied workspace path per hook job, so overlapping hook workers can
spawn isolated workspace children and analyze different edits concurrently.
The USEagent worker limit is therefore the client-side bound on those scans.
The hook calls `/close-workspace` in a cancellation-safe `finally` path before
the scheduler deletes the revision, and the provider terminates the child's
process group. A future provider-wide worker/memory limit may be lower than the
client hook limit; ordered retirement remains correct in either case.

## Configuration

Implemented CLI options and equivalent environment variables:

```text
--action-hook-scheduler ordered
USEAGENT_ACTION_HOOK_SCHEDULER

--action-hook-max-concurrent-runs N
USEAGENT_ACTION_HOOK_MAX_CONCURRENT_RUNS

--action-hook-max-unretired-actions N
USEAGENT_ACTION_HOOK_MAX_UNRETIRED_ACTIONS

--action-hook-run-timeout-seconds S
USEAGENT_ACTION_HOOK_RUN_TIMEOUT_SECONDS

--action-hook-post-action-patience-seconds S
USEAGENT_ACTION_HOOK_POST_ACTION_PATIENCE_SECONDS

--action-hook-intervention-quiesce-seconds S
USEAGENT_ACTION_HOOK_INTERVENTION_QUIESCE_SECONDS

--action-hook-cleanup-seconds S
USEAGENT_ACTION_HOOK_CLEANUP_SECONDS

--action-hook-finalize-seconds S
USEAGENT_ACTION_HOOK_FINALIZE_SECONDS

--action-hook-snapshot-budget-mib M
USEAGENT_ACTION_HOOK_SNAPSHOT_BUDGET_MIB

--action-hook-observer-queue-capacity N
USEAGENT_ACTION_HOOK_OBSERVER_QUEUE_CAPACITY

--action-hook-observer-overflow drop_oldest|drop_newest|fail
USEAGENT_ACTION_HOOK_OBSERVER_OVERFLOW
```

The observer settings are parsed and validated as reserved configuration, but
ordered startup currently rejects observer registrations. External manifest
options such as `--action-hook-config` are not implemented in the pilot.

Current ordered defaults are concurrency `2`,
unretired actions `2`, run timeout `300` seconds, post-action patience `0`,
intervention quiescence `30` seconds, hard cleanup `30` seconds, finalization
`60` seconds, snapshot budget `2048` MiB, observer queue capacity `16`, and
observer overflow `drop_oldest`.
These are conservative operational starting points, not claims of optimal
performance; benchmark results may change them after live resource validation.
Immutable inputs are part of the runtime, so the configured unretired
action limit is active rather than being forced to `1`.

Validation rules:

- concurrency and unretired-action limits must be positive integers;
- run, quiescence, and cleanup timeouts and the snapshot budget must be
  positive;
- patience and finalization timeouts must be non-negative;
- observer queue capacity must be positive for `every` observers;
- ordered mode rejects non-preemptible in-process callbacks;
- `legacy` scheduler configuration and nonzero
  `USEAGENT_ACTION_HOOK_WAIT_SECONDS` values are rejected.

The timers have deliberately separate meanings:

- A registration's `timeout_seconds` overrides the global run timeout. Its
  clock starts when the callback or process actually starts, not while queued.
- Post-action patience starts after every gate job for the action is queued.
  Expiration does not cancel or terminalize those jobs; it only permits the
  agent to continue when the speculation window and barriers allow it.
- The quiescence deadline bounds intervention waits for an active action or
  owned mutable resource to reach a safe boundary.
- The cleanup deadline bounds termination after a running job begins timing
  out; exceeding it is a scheduler abort, not a registration-level fail-open.
- The finalization deadline bounds the final mandatory drain. At expiration,
  queued jobs become `timed_out_before_start`, while running jobs enter
  `timing_out` and must finish cleanup before the last retirement pass.

`USEAGENT_ACTION_HOOK_WAIT_SECONDS` is retired. Zero is tolerated while users
clean up inherited environments, but any nonzero value is rejected and users
are directed to the run-timeout and patience settings; silently reusing it
would conflate waiting for overlap with terminating analysis. The bundled
runner never injects this variable.

## Diagnostics And Observability

Structured diagnostics should include the session, epoch, action sequence,
checkpoint, job, hook, and workspace revision on every scheduler event.

Implemented event families include:

- `action_admission_wait_started`
- `action_admitted`
- `action_snapshot_created`
- `action_protocol_finalized`
- `action_invalidated`
- `hook_job_queued`
- `hook_job_started`
- terminal `hook_job_<outcome>` events such as `hook_job_noop`,
  `hook_job_intervene`, `hook_job_failed`, `hook_job_timed_out`, and
  `hook_job_cancelled`
- `action_retired`
- `intervention_suppressed_limit`
- `intervention_selected`
- `intervention_applied`, including whether filesystem restoration occurred
- `session_finalization_started`
- `session_finalization_drained`
- `session_finalization_completed`

The current runtime does not emit workspace-lease events because cross-manager
and cross-process workspace exclusion is deferred. A future path-keyed lease
must add explicit acquire/release/failure events before claiming that guarantee.

Useful measurements include:

- current and peak running hook jobs;
- current and peak unretired actions;
- job queue and admission wait time;
- analysis duration by hook;
- time from action completion to retirement;
- rollback depth and restored snapshot size;
- stale and superseded result counts;
- forced cleanup count;
- finalization duration.

Diagnostics must be written after structured shutdown so cancellation and
cleanup events are included in the correct session log.

## Implementation Plan

The phases below are retained as the engineering record and the roadmap for
the complete design. The gate-hook pilot covers the central path across Phases
1 through 8 rather than shipping each phase as a separately selectable mode.
The following qualifications prevent the roadmap from being read as a claim
that every bullet is complete:

- Phase 2 is complete for programmatic/callable `HookOptions`; the external
  manifest and versioned command schema are deferred.
- Phase 3 uses public deferred approvals and exact tool-call IDs. Direct Bash
  participates in dispatch and barrier gating but intentionally does not emit a
  post-action hook event.
- Phases 4 and 5 are implemented for gate hooks, including configurable worker
  and speculation bounds, ordered aggregation, and rollback.
- Phase 6 uses isolated per-hook full-copy analysis trees and exact bundled-provider
  correlation. Observer/coalescing states and alternative Git materialization
  strategies remain roadmap items.
- Phase 7 implements async HTTP, final draining, intervention quiescence,
  provider descendant termination, and awaited cleanup. General command-hook
  wire versioning remains deferred.
- Phase 8 selected ordered as the only runtime. A live baseline at the current
  `2`/`2` limits is complete; controlled `1`/`2`/`4` comparisons are still
  required before retuning the resource defaults.

### Expected implementation touchpoints

| Area | Primary files | Planned responsibility |
| --- | --- | --- |
| Public hook API | `useagent/action_hooks.py` | Keep event/result types and snapshot helpers; add registration options and delegate mutable scheduling state |
| Ordered scheduler | `useagent/action_hook_scheduler.py` (new) | Own session state, admission, capacities, result buffering, retirement, intervention, and cleanup |
| Top-level action boundary | `useagent/tools/meta.py` | Await admission, capture action outcomes, remove nested synchronous agent runs, and report dispatcher state |
| Model/tool dispatcher | `useagent/agents/meta/agent.py` | Use public deferred approvals, order sibling calls, return protocol-valid denials, finalize exact call/return pairs, and expose safe points |
| Dependency seam | `pyproject.toml` and `uv.lock` | Keep the tested Pydantic AI deferred-tool API and direct async HTTP dependency reproducible |
| Session lifecycle | `useagent/task_runner.py` | Own process-local finalization and persist diagnostics; a path-keyed cross-process lease remains deferred |
| USEagent CLI | `useagent/main.py` | Parse and validate ordered scheduler settings; manifest support remains deferred |
| Bundled integration | `agent_integration/agents/useagent/hook.py`, `runner.py`, and `stub.pyi` | Send immutable correlated jobs, mirror event types, remove thread-backed gate work, and forward ordered-mode settings |
| CodeQL provider | `agent_integration/feedback_provider/` | Preserve exact job correlation, isolate workspace children, and terminate descendant process groups during cleanup |
| Verification | `tests/test_action_hooks.py` and `agent_integration/tests/test_useagent_feedback_provider.py` | Add deterministic state-machine, runner composition, process cleanup, and backend correlation tests |

### Phase 0: Characterize current behavior

Add deterministic tests before changing runtime behavior:

- two action checkpoints whose hook results complete in reverse order;
- two intervention hooks for the same checkpoint;
- an earlier intervention while a later action is running;
- an earlier intervention after a later action and hook are complete;
- an intervention while the next model request is in flight;
- pending hooks when the Meta-Agent returns a final result;
- parallel top-level tool calls mutating shared state;
- a direct Meta-Agent Bash call while an earlier gate is unresolved;
- command-hook cancellation and subprocess cleanup;
- synchronous hook event-loop blocking characterization.

These tests should document current first-completion behavior without treating
it as the target contract.

### Phase 1: Introduce per-session async ownership

- Add `ActionHookSession` and separate immutable registration state from mutable
  run state.
- Keep `useagent/action_hooks.py` as the public types and compatibility facade.
- Place scheduler runtime code in a focused module such as
  `useagent/action_hook_scheduler.py`.
- Convert the main agent loop to run within one owned asynchronous lifecycle so
  hook tasks can be awaited between model iterations and at shutdown.
- Convert `advising_on_doubts()` and `_gather_checklist()` in
  `useagent/tools/meta.py` away from nested `run_sync()` calls; invoking
  `run_until_complete` inside the new running loop is invalid.
- Create and close the hook session in `useagent/task_runner.py`.
- Move hook diagnostics into the session and retain one process-local active
  session. A path-keyed process/workspace lease remains follow-up work.
- Offload blocking snapshot/restore work without releasing action ownership,
  and await the owned operation during shutdown.
- Preserve current scheduling semantics temporarily behind the new session API.

Acceptance criteria:

- sequential task runs cannot share pending jobs, interventions, generations,
  snapshots, or diagnostics. Rejecting independent simultaneous managers or
  processes remains part of the deferred path-keyed lease work;
- shutdown awaits all in-process hook tasks;
- existing action-hook tests continue to pass.

### Phase 2: Add identities and registration metadata

- Extend checkpoints, post-action restore points, and events with session,
  epoch, action sequence, hook job, and workspace revision identities. Reserve
  schema fields for batch, response-part, and serialized return identity, but
  populate them only after the Phase-3 dispatcher seam exists.
- Add registration objects with execution, action filters, mode, priority,
  timeout, failure, observer, restoration, and barrier policy.
- Add the programmatic `HookOptions` path and versioned external-hook config;
  map metadata-free `--action-hook` specs to the documented deterministic
  defaults.
- Update command-hook schema to a versioned payload carrying the new identity
  fields.
- Retain deterministic defaults for callbacks registered without metadata.

Acceptance criteria:

- stale epoch and mismatched revision results are rejected diagnostically;
- action filtering prevents unnecessary jobs and snapshots;
- metadata-free registrations resolve to the documented deterministic options.

### Phase 3: Serialize top-level actions

- Make action admission awaitable in `useagent/tools/meta.py`.
- Add the authoritative session action lock around checkpoint creation, action
  execution, post-action snapshot capture, hook job creation, and gate-job
  admission.
- Add a dispatcher/toolset coordinator above individual wrappers that orders
  stateful siblings by response-part index, finalizes exact call/return pairs,
  and exposes the restoration safe point.
- Use Pydantic AI's public `DeferredToolRequests`, `DeferredToolResults`, and
  approval result types; keep a compatibility test around the safe-point graph
  transition used by ordered dispatch.
- Populate each action record with the exact serialized triggering call,
  return, batch identity, and response-part index, then use those fields for
  message replay.
- Disable parallel model tool calls where supported, while retaining dispatcher
  enforcement as the invariant.
- Add protocol-valid deferral, cancellation, or retry returns for later
  stateful sibling calls in one model response.
- Own each model request as a tagged session task and discard its response when
  an intervention invalidates that turn.
- Make intervention cancellation target the exact active action task and await
  its termination before rollback.
- Implement the failed-action and intervention-invalidated action transitions.
- Route the Meta-Agent's direct `bash_tool` through the same admission
  coordinator and implement the built-in plus registration-strengthened barrier
  policy.

Acceptance criteria:

- two concurrently emitted `edit_code` calls never mutate the workspace at the
  same time;
- checkpoints and action sequences reflect one total action order;
- intervention consumption cannot be stolen by a sibling action task;
- every deferred sibling call has a valid model-protocol return;
- restoration cannot start before the exact triggering return is finalized;
- repeated action names and sibling calls cannot be confused during replay;
- an invalidated in-flight model response cannot append messages or dispatch
  tools;
- no action, including direct Bash, starts beyond an unresolved speculation
  barrier.

### Phase 4: Add bounded admission and minimal retirement

- Implement the per-session hook permit pool and deterministic admission queue.
- Implement a minimal ordered no-op retirement cursor before using unretired
  records for backpressure.
- Implement `max_unretired_actions`, initially forced to `1`, and block new
  actions before mutation when the window is full.
- Queue excess gate jobs without delaying the completed action's protocol
  finalization solely for a worker slot.
- Add configuration parsing, validation, and backpressure diagnostics.
- Guarantee permit and speculation-slot release on every terminal path.
- Keep ordered mode internal: Phases 4 and 5 are not independently enableable,
  and cross-action speculation remains disabled until Phase 6.

Acceptance criteria:

- running jobs never exceed the configured concurrency limit;
- a third gate job remains queued when two permits are occupied for a limit of
  two;
- an action cannot start when the unretired window is full;
- the minimal cursor retires contiguous no-op records atomically before waking
  blocked admission;
- cancellations, exceptions, and timeouts cannot leak capacity.

### Phase 5: Implement ordered retirement and rollback

- Replace the global intervention slot with per-action result collection.
- Implement the retirement cursor and deterministic same-action aggregation.
- Buffer later results without applying them.
- Implement the epoch invalidation and structured rollback protocol.
- Retain checkpoints until retirement makes them unreachable.
- Preserve non-restoring interventions as ordered outcomes.
- Keep the speculation window at `1` while hooks still inspect the live
  workspace.

Acceptance criteria:

- reversing completion order among hooks for one action does not change its
  aggregate;
- scheduler-only tests with prebuilt multi-record ledgers prove that later
  results buffer behind the cursor; live integration remains at window `1`;
- intervention application is serialized and every restoring intervention
  advances the epoch exactly once;
- restored files, task state, bash history, and messages match the selected
  post-action restore point.

### Phase 6: Add immutable backend inputs and correlation

- Materialize an analyzer-readable immutable workspace revision per gate action.
- Extend hook and backend protocols with exact job and revision identities.
- Return explicit running, completed, superseded, cancelled, and stale job
  states.
- Update the CodeQL feedback integration to consume only its exact job result.
- Add a bounded backend worker pool if true cross-edit CodeQL parallelism is
  required.
- Isolate backend state and output paths by job.
- Close each backend workspace/revision lease and await its child process before
  deleting the snapshot.
- Unlock configurable speculation greater than `1` only after immutable input,
  restore-point, and cleanup tests pass.

Acceptance criteria:

- a hook for action 1 cannot observe action-2 filesystem content;
- overlapping requests cannot receive or consume another revision's result;
- reversing action-1/action-2 hook completion order does not change the final
  trajectory;
- a restoring action-1 intervention invalidates action 2 and later speculative
  work, while action 2 can intervene only after action 1 retires;
- coalescing produces explicit superseded outcomes;
- configured backend concurrency is never exceeded;
- backend process count returns to its configured bound after revision cleanup;
- snapshot creation and disk-budget failure cannot silently enable speculation.

### Phase 7: Complete structured finalization

- Add the bounded final gate drain before committing agent output.
- Implement `timing_out` cleanup ownership and convert timeout/hook exceptions
  into terminal outcomes only after resources terminate.
- Kill and await command-hook process groups on every cancellation path.
- Replace blocking HTTP-in-thread integration paths; ordered gate hooks cannot
  retain non-preemptible thread work.
- Persist diagnostics only after task, process, request, and snapshot cleanup.

Acceptance criteria:

- a late mandatory intervention prevents the provisional final result from
  being committed;
- no hook task or command process remains after session close;
- all cancellation and cleanup events appear in the correct diagnostic log;
- forced cleanup is bounded and visibly reported.

### Phase 8: Ordered-only rollout

- Make ordered scheduling the sole runtime; retain
  `--action-hook-scheduler ordered` for explicit command lines and reject
  `legacy`.
- Remove runner injection of `USEAGENT_ACTION_HOOK_WAIT_SECONDS` and reject
  nonzero inherited values in favor of the concurrency, speculation,
  run-timeout, patience, quiescence, cleanup, and finalization settings.
- Update deterministic command-composition tests for every forwarded option
  and for the absence of the retired wait.
- Run deterministic and live ordered-hook scenarios.
- Compare latency, rollback count, queue pressure, snapshot cost, and final
  result quality.
- Document registration metadata and migration guidance for external hooks.

## Test Matrix

| Area | Scenario | Required assertion |
| --- | --- | --- |
| Concurrency | Limit two, submit three gate jobs | Third job waits; peak running count is two |
| Multi-hook queueing | One action matches three gates with concurrency two | Two jobs run, the third remains queued, and protocol finalization does not wait merely for that worker slot |
| Backpressure | Two unretired actions, request a third | Third action blocks before mutation |
| Ordering | Action 2 finishes before action 1 | Action-2 result remains buffered |
| Retirement | Action 1 no-ops, action 2 already complete | Cursor retires both in sequence |
| Atomic retirement | Action 1 no-ops while action 2 already intervened | No action 3 admission occurs between the two retirement decisions |
| Rollback | Action 1 intervenes after action 2 | Action 2 is cancelled/discarded; post-action-1 state is restored |
| Later intervention | Action 1 no-ops, action 2 intervenes | Post-action-2 restore point is selected |
| Safe point | Action-1 hook immediately intervenes before its return is recorded | Result buffers; restoration starts only after protocol finalization and action release |
| Same action | Two hooks intervene in reverse completion order | Priority and registration order choose the same aggregate |
| Failure policies | Equivalent hook failure uses continue, intervene, then abort | Outcomes are respectively fail-open no-op, visible intervention, and task error |
| Failure precedence | Same action yields abort, intervene, and continue policies | Aggregate is abort regardless of completion order |
| Epoch | Old job completes after rollback | Result is rejected as stale |
| Revision | Backend returns wrong revision | Result is rejected and cannot retire the action |
| Tool calls | Model emits two mutating actions together | Runtime executes them one at a time |
| Tool protocol | Later sibling action is deferred | Every call retains a protocol-valid return |
| Early replay | Earlier intervention arrives before sibling returns exist | Coordinator synthesizes missing returns and replay remains protocol-valid |
| In-flight model turn | Intervention retires while the next model request runs | Request is cancelled/awaited and its stale response is never appended or dispatched |
| Built-in barrier | Direct Bash or a shell-capable wrapped action is requested | Earlier work retires and later speculation waits |
| Hook barrier | Matching registration requires a barrier for `edit_code` | Manifest strengthens action policy and prevents later speculation |
| Capacity cleanup | Hook fails, times out, or is cancelled | Permit and queued progress are released once |
| Running timeout | Running callback exceeds its run deadline | It becomes `timed_out`; policy runs after owned resource termination |
| Queued timeout | Finalization expires before a queued callback starts | It becomes `timed_out_before_start` without consuming a permit |
| Cleanup failure | Timed-out mandatory resource ignores termination past cleanup deadline | It becomes `cleanup_failed`; task aborts and permit is never reused as if clean |
| Synchronous hook | Ordered mode receives a synchronous callback | Registration is rejected or runs through a killable process adapter |
| Snapshot lifetime | Later result completes first | Earlier and later required snapshots remain until retirement |
| Snapshot failure | Required immutable revision or restore point cannot be created | Speculation stops and ordered session aborts after cleanup |
| Snapshot responsiveness | Large snapshot/restore is in progress | Event loop continues processing hook completion and cancellation |
| Deferred workspace ownership | Independent managers/processes target one workspace | Future path-keyed lease rejects the second owner before mutation or restoration |
| Restore failure | Selected restore point cannot be fully applied or verified | Session aborts and retains diagnostics; agent does not resume |
| Finalization | Final answer arrives with a gate job pending | Final result remains provisional |
| Final intervention | Pending gate intervenes during finalization | Agent resumes instead of finishing |
| Final deadline | Gates outlive the finalization deadline | Timeout policies retire first; resources terminate or forced cleanup is reported |
| Command cleanup | Command hook is cancelled | Process group exits before session close |
| Observer latest | Several observer jobs coalesce | Superseded jobs never run or restore |
| Observer lifetime | Latest-only job replaces a queued revision | Superseded snapshot remains until all consumers release it |
| Invalid observer result | Observer returns an intervention | Outcome is failed and trajectory is unchanged |
| Non-restoring intervention | A newer action is active | Admission stops; instruction waits for its protocol-safe boundary without rollback |
| Non-restoring timeout | Newer action never reaches a safe boundary | It is cancelled; session aborts without injecting feedback onto partial state |
| Action exception | Top-level action raises and gate no-ops | Error event runs once, record retires, original exception propagates |
| Invalidated action | Earlier intervention cancels an unfinished later action | Later record is invalidated and schedules no hooks |
| Intervention cap | Aggregate exceeds `MAX_ACTION_HOOK_INTERVENTIONS` | It retires as `suppressed_limit` and releases all resources |
| Backend lease | Immutable CodeQL workspace is released | Close-workspace completes and child count returns to the bound |
| Configuration rejection | Scheduler is `legacy` or wait is nonzero | Startup fails before an agent action runs |
| Process-local session isolation | Sequential tasks reuse the global manager | Closed sessions share no pending runtime state; independent-manager/process exclusion remains deferred |

Tests should use controlled events and barriers rather than timing-only sleeps.
Add randomized scheduling/property tests after the deterministic state-machine
suite is stable.

## Performance And Resource Validation

The 2026-07-10 baseline validated the current `2`/`2` defaults with the complete
three-scenario live suite and a 105,717,760-byte SymPy worktree. The live suite
passed all scenarios with observed hook concurrency `2`. The SymPy scenario
retained 201,491,888 snapshot bytes at peak, selected an action-1 intervention
after a 20.40-second findings scan, cancelled the speculative action-2 scan,
restored filesystem and trajectory, and completed a clean action-3 scan in
9.73 seconds. End-to-end wall time was 60.63 seconds; peak container cgroup
memory was 7,115,177,984 bytes. That provider run used sparse changed-file mode,
so it measures large-worktree orchestration cost rather than full-SymPy query
analysis.

To tune the ordered defaults, benchmark at least:

- concurrency 1, 2, and 4;
- speculation windows 1, 2, and 4;
- fast no-op hooks and slow intervention hooks;
- filesystem snapshot creation and retention cost;
- backend analysis with isolated revisions;
- rollback after one and several speculative actions;
- session finalization with running hooks.

Measure total task latency, agent idle time, analysis utilization, peak memory,
snapshot disk use, number of discarded speculative actions, and backend
contention. The concurrency default should be based on these results rather
than CPU count alone, because external analyzers may have substantially larger
memory and I/O footprints than Python hook tasks.

## Documentation And Rollout Follow-Ups

The ordered-runtime documentation now:

- describes hook modes, registration metadata, configuration, timeout policy,
  and exact rollback semantics in [HOOK.md](HOOK.md);
- keeps checked implementation items separate from roadmap items in
  [TOP_LEVEL_ACTION_HOOKS.md](TOP_LEVEL_ACTION_HOOKS.md);
- documents backend correlation requirements for integrations that analyze a
  workspace; and
- documents removal of the legacy scheduler and per-action wait.

Still required before a broader compatibility release:

- version the command-hook JSON schema and include migration examples;
- document and implement observer registration/overflow behavior; and
- publish controlled comparative benchmark results for default tuning.

## Open Decisions

The architecture above is fixed, but these implementation parameters require
prototype or benchmark evidence:

- whether immutable Git analysis input should use temporary worktrees plus
  patches or fully materialized snapshot trees;
- the final default concurrency and speculation limits after measurement;
- whether observer hooks need a separately reserved concurrency pool;
- the final instruction format for aggregating multiple same-action
  interventions;
- the appropriate finalization deadline and forced-cleanup policy for each CLI
  task type;
- whether the CodeQL backend should run multiple full workers or retain one
  worker and expose ordered queued job identities first.

None of these open parameters changes the central contract: analysis may run in
parallel, decisions retire in action order, speculation and resource use remain
bounded, and restoration discards only trajectory newer than the selected
post-action restore point.
