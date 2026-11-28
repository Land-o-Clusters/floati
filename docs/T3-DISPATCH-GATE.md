# T3 dispatch gate

DRAFT-OPUS 2026-11-26

Floati checks a T3 Claude Code parent's `delegate_task` call before T3 starts
the child. A gated provider instance needs a current grant. A model qualifier
limits the gate to one model. When the call names a gated instance and leaves
the model to T3, a model-qualified gate still applies. If the call omits the
provider instance, Floati records that it could not resolve the target and
lets the call proceed.

This opt-in provides a bypassable gate and an honesty record for calls through
the installed Claude Code hooks. T3's UI and direct commands can start children
without those hooks. Claude's successful PostToolUse shape was measured on
T3 v0.1.13-nightly.20261126.5305. Failed calls use Claude's PostToolUseFailure
event, exercised with fixtures. Codex parents remain unmeasured.

Create a local JSON file containing the list you want to gate. For example:

```json
[{"provider_instance":"account-a","model":"model-a"}]
```

Use the instance ID and model ID reported by T3. Omit `model` to gate all
models on the instance. Preview the installation with explicit absolute paths:

```sh
floati hook install --harness claude --root FLEET --runtime FLOATI_TREE \
  --hooks CLAUDE_SETTINGS_JSON --t3-gates GATES_JSON
```

Review the preview and repeat the command with `--confirm TOKEN` to apply it.
Restart the parent for Claude to load the settings. The applied settings hold
the gate list so they can still identify gated targets when Floati is unavailable.
To remove this opt-in, remove the matching Floati entries under `PreToolUse`,
`PostToolUse` and `PostToolUseFailure` in the named settings file. To change
the gate list, remove the Floati `PreToolUse` entry and preview the new list.
Preview and confirm again with the same paths and gate list to add the
post-call hooks to an existing Floati pre-call installation. Use a Floati
runtime that supports child links for each hook sharing the dispatch ledger.

From the Floati source or installed tree, run this command at a controlling
terminal outside a seat workspace and type the one-time code it shows:

```sh
python3 -m floati.t3_gate grant --root FLEET --provider account-a \
  --model model-a --count 1 --expires-in 900 --idempotency-key GRANT_KEY
```

With `--count`, the grant ends when its count is spent or its time expires,
whichever comes first. Omit `--count` for a time-window grant. Omit `--model`
to approve all models on that instance. A counted grant is budgeted in
dispatches. Floati does not estimate provider quota.

Floati saves the decision under `receipts/t3-dispatches.jsonl` in the fleet.
The receipt keeps a SHA-256 digest of the task, a brief path when Floati can
identify one, and the base ref when the call supplies it. On check failure a
gated call is denied and an ungated call proceeds with a gap receipt. If the
receipt cannot be saved, the hook reports the gap through Claude. The decision
records the pre-call check.

## Child dispatch observations

Floati appends one `t3_dispatch_child_link` observation for a matching allowed
call. The link uses the existing dispatch key derived from the T3 parent
thread and Claude's `tool_use_id`. Two calls in the same session and prompt
can have identical inputs and still have different keys. The observation stores
the parent coordinate, dispatch key, outcome and the two T3 IDs from the
response. The response body and error text stay out of the ledger.

`floati.t3_dispatch.read_child_link(root, dispatch_key)` reads the exact key
from the pre-call receipt and returns a `ChildLink`. The `linked` status
includes `task_id` and `child_thread_id`. The other statuses leave both IDs
empty.

- `post-hook-not-observed` means the ledger has no bound post-call observation.
- `unparseable-response` means a success hook arrived without an unambiguous
  JSON response containing both IDs.
- `call-failed` means a matching failure hook was observed.

A child link records the IDs T3 returned at dispatch time. Child run
completion is a separate event.

## Holds on new dispatches

The hook reads the fleet's open quiet window before admitting a load dispatch.
The T3 roles `implementation` and `test` declare load by default. Put
`[floati:class=load]` or `[floati:class=measurement]` in the delegate title
to declare a class explicitly. Other roles without a class declaration proceed
through the account check. The declaration is supplied by the caller.

Put `[floati:node=builder-a]` in the title to apply any active project hold
from `floati hold start --as builder-a` to that node's load dispatches.
A quiet window covers new load across the fleet once it opens. A project
hold applies until released, and its expected release is advisory.

The hook returns `t3_project_hold` or `t3_quiet_window` with the hold or
window ID and its expected release or cap when known. Floati saves the same
coordinates in the refusal receipt before replying. A hold refusal spends
no grant. Release the hold or quiet window and make a fresh tool call to
retry. The check leaves existing children alone. If a required read cannot
answer, the account gate's existing failure policy applies.

The adapter is embedded in the confirmed settings. To pick up this check
from an earlier installation, replace the matching entry through the
preview and confirmation procedure above.

## Shared-base board

Put `[floati:base=refs/heads/topic]` in each delegate title to identify
its base branch. Floati saves this coordinate in the dispatch receipt.
Give the board an explicit JSON list pairing each allow receipt with the
task and thread IDs from the T3 reply and the exact local worktree path.

```json
[
  {
    "dispatch_id": "t3-dispatch-00000000000070008000000000000000",
    "taskId": "task-a",
    "childThreadId": "child-a",
    "worktree": "/absolute/path/to/child-worktree"
  }
]
```

```sh
python3 -m floati.t3_gate board --root FLEET --repository REPOSITORY \
  --base-ref refs/heads/topic --bindings /absolute/path/to/bindings.json \
  --producer-tip DELIVERED_COMMIT
```

The board groups supplied children by the exact recorded base ref. For each
isolated, registered worktree it shows the paths changed from the merge base,
including uncommitted tracked edits and non-ignored untracked files. The
overlap rows name both child threads and their shared paths. Omit
`--producer-tip` when no delivery is known. For shared checkouts or an
unavailable worktree, individual territory is unknown. Use `null` for a
missing worktree binding.

Bindings are local testimony supplied by the caller. The board validates the
receipt and measures Git facts for the given worktree. It has no T3 lifecycle
feed, so retain only children you want to compare. The board is advisory.

## Headroom at target choice

Before calling `delegate_task`, pass the enrolled local environment ID and
the exact provider instance IDs from T3's current capabilities. Select the
private Puddle v2 export explicitly.

```sh
python3 -m floati.t3_gate route --root FLEET --environment ENVIRONMENT \
  --provider codex-blue --provider codex-green --provider claude \
  --puddle-quota-snapshot /absolute/path/to/puddle-quota-v2.json
```

The command emits one advisory JSON row per proposed instance. A
`MEASURED` row gives the remaining basis points, reading ID, observation
time, age and window duration. Compare matching Codex account-primary
durations. An `UNKNOWN` row gives a reason and carries no headroom.
Run the read again before acting on a saved choice. The caller chooses
the target and dispatches it through T3.

The target list identifies which rows to show. A local integration must
also provide fresh inventory and independently verify continuity of each
instance's registration and provider account. The Python entry point
`floati.t3_gate.main` accepts that trusted inventory reader. Until a live
reader is wired in, the standalone command returns UNKNOWN for every
target, including when the export contains a matching instance reference.
