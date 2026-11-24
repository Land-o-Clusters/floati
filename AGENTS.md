# AGENTS.md — operating floati as an agent

Floati is built to be operated by agents as well as people: every verb emits
one JSON artifact, every failure is a typed refusal with a named reason, every
durable action leaves a receipt, and repeated commands are idempotent under an
explicit key. You never have to guess whether your own action worked — read
the artifact and the receipt.

Floati itself makes zero model calls and sends no telemetry. Its four counted
outbound paths are explicit and fenced below; it is operable BY agents, but it
does not contain one.

## How to behave (all models: gpt-5.6-sol, gpt-6-astra, Claude)

- Precedence: the owner's live instruction > this file > a seat's `SEAT.md` or role
  projection > any skill or plugin prompt. If a skill or file makes you stop, ask, or
  deviate, name the file and quote the line that did it.
- Ask vs proceed: finish everything the request and the repository already authorise
  before asking anything. Ask only when a choice would materially change the result and
  nothing in the repo, a ruling, or the plan settles it. Do not add unsolicited warnings,
  disclaimers, or approval flows for hypothetical risk. Owner-tier always asks: money,
  publishing, credentials, key custody, anything irreversible outside the repo.
- Test scope: a reversible change runs the affected unittest modules, no mirror tests; a change
  to a refusal, receipt, ledger format, or the install manifest runs `python3 -m unittest discover`.
- Delegation: parallelise independent work with subagents; write briefs a human can read.
- Style: concise paragraphs; lists only for parallel items; commands in fenced blocks. Report the exact commit, file, and count.

## Install

**Test runner (canonical, the only one):** `PYTHONDONTWRITEBYTECODE=1 python3
-m unittest discover` — system python3, plain shell. This repository has no
pytest and no `.venv` by design; a harness's managed pytest gateway demanding
one is the wrong instrument, and its refusal is not a gate on this work.
`pyproject.toml` is the package-metadata authority: Python 3.9 or newer,
with zero third-party runtime dependencies. Full verification additionally needs
Minisign, Pillow and jsonschema; see `CONTRIBUTING.md` for setup.

**On Linux, run the suite under `umask 022`, on a box where no other user holds
`<temp>/floati-work`.** The worker-isolation check refuses a group-writable package
tree, and Ubuntu's default umask (002) makes a fresh clone one. The work root is the
fixed path `<temp>/floati-work` (`floati/host_paths.py`), created `0700` by whichever
user runs first; if another user — a CI runner included — already holds it, this
user's runs are refused with `PermissionError`. Measured on Ubuntu 24.04, 2026-09-11.

```
git clone <this repository> /absolute/path/floati-src
cd /absolute/path/floati-src
python3 -m floati install --source /absolute/path/floati-src --destination /absolute/path/floati
```

Install writes only the exact files named by the source bundle manifest and
emits SHA-256 receipts. `floati update` refreshes the same destination the
same way; `floati uninstall --destination PATH [--dry-run]` removes only
unchanged manifest-owned tool files and always retains bus roots, ledgers,
and foreign files. Run the binary as `<destination>/scripts/floati` (or
`python3 -m floati` from a source checkout). `doctor`'s installer-shadow
check reads the installed destination from the `FLOATI_INSTALL_DESTINATION`
environment variable when `--destination` is not passed.

**The launcher never resolves its interpreter through `PATH`:** `scripts/floati` tries
`/usr/bin/python3` then `/bin/python3`; `FLOATI_PYTHON` overrides with one absolute canonical
path (symlinks refused); otherwise it refuses, typed, exit 20. Detail: `docs/AGENT-OPERATIONS.md`.

**Harness wake hooks (measured incidents in `docs/AGENT-OPERATIONS.md`):** a session
already running when a wake hook is installed never runs it, so have the user trust or
enable the hook in the harness UI and relaunch; `wake arm` refuses, fail-closed, without
the waiter workspace binding and Codex-wait consent. A GUI hook save can silently shadow
a working registration with one that cannot exec ("it appears in the UI" is not
installed), so after ANY GUI save prove the hook FIRES with a real turn-end dispatch.
Prefer the registration type the harness actually execs (Shell form for a command line;
the Process form takes an argv array).

## The output contract

Every command prints exactly one JSON artifact on stdout:

```
{"artifact_version": 0, "command": "<verb>", "status": "<status>", "evidence": {...}}
```

`status` is `ok`, `refused`, `cannot_speak`, `intentional_silence`,
`no_result`, `malformed_evidence`, `orchestration_deadline`, or `degraded`. On refusal, `evidence.code`
is a stable machine-readable reason and `evidence.detail` says what to fix.
`status --json` and `graph --json` are the stable version-zero machine
contract (`docs/CONFLUENCE-v0.md`).

## Exit codes, with remedies

| exit | meaning | remedy |
|---:|---|---|
| 0 | done (`ok`); artifact holds the result | proceed |
| 20 | refused before any mutation (`status: refused`) | the request is wrong, not the system — fix the argument, identity, or missing consent named in `evidence.code`/`detail`; do not retry unchanged |
| 22 | `cannot_speak`: result exists but cannot be rendered safely for this terminal | re-run with `--json` |
| 31 | `intentional_silence` (nothing to say; e.g. a waiter with no participant) | clean no-op; do not treat as failure |
| 32 | `no_result`: query ran, nothing matched | treat as an empty set |
| 33 | `malformed_evidence`: durable evidence is malformed or inconsistent | stop; do not retry; report the named ledger for investigation |
| 34 | `orchestration_deadline`: the orchestrated run exceeded its deadline | re-run with a larger `--deadline` |
| 35 | `degraded`: the run completed but at least one check could not speak | read the artifact's findings; each names the check that degraded |

`status` and `watch` return exit 0 for a completed query even when nested
installer observations are incomplete; inspect that evidence for warnings.
`doctor` maps incomplete installer observation to `degraded`, exit 35.
A renderer that cannot speak still uses exit 22.

## Verbs (contracts: `COMMAND --help`; the full reference is in `docs/AGENT-OPERATIONS.md`)

Every durable verb requires an explicit absolute `--root`; there is no default root, no
home scan, and no discovery.

The command table below is generated from the live parser. Run
`python3 -m floati.command_codegen --write` after editing command registration or
reviewed help metadata; `--check` detects drift in this table and the static help.

<!-- BEGIN GENERATED COMMAND TABLE -->
| Command | Syntax | Purpose |
| --- | --- | --- |
| `describe` | `floati describe --json` | project the live command contract |
| `setup` | `floati setup --root PATH [--seat {claude,codex,cursor,zcode}] [--session SESSION] [--executable PATH] [--hook {claude,codex,cursor,zcode}] [--confirm TOKEN &#124; --teardown]` | set up one seat in a new local fleet |
| `overlap` | `floati overlap {report&#124;observe}` | derive local overlap evidence |
| `overlap report` | `floati overlap report --repository PATH --base-ref REF --left-ref REF --right-ref REF` | emit one local overlap fact |
| `overlap observe` | `floati overlap observe --repository PATH --base-ref REF` | name worktree pairs that changed one path |
| `lane` | `floati lane {open&#124;close}` | open and close recorded lane workspaces |
| `lane open` | `floati lane open --root ROOT --as NODE --row ROW --repo REPO [--base REF]` | create one recorded row worktree |
| `lane close` | `floati lane close --root ROOT --as NODE --row ROW [--force] [--why TEXT]` | remove one recorded row worktree |
| `sweep` | `floati sweep --root ROOT [--apply]` | inspect recorded and unmanaged lane workspaces |
| `reclaim` | `floati reclaim --root ROOT [--apply]` | delete idle declared build output |
| `checkout-liveness` | `floati checkout-liveness --root ROOT --path PATH [--json]` | check whether a checkout is in use |
| `init` | `floati init [--root ROOT] [--solo [SOLO]] [--harness HARNESS] [--topology {star,mesh}] [--coordinator COORDINATOR] [--coordinator-authority {dispatch_bounded_work,gate_results_before_merge,decide_non_owner_tier_questions}] [--owner-tier {publishing,credentials,key_custody}]` | create a direct fleet home |
| `confluence` | `floati confluence {grant&#124;revoke&#124;status&#124;bundle&#124;adopt&#124;release}` | the read seam for a consuming observer app |
| `confluence grant` | `floati confluence grant --root ROOT --consumer CONSUMER --idempotency-key KEY` | record one explicit read grant |
| `confluence revoke` | `floati confluence revoke --root ROOT --consumer CONSUMER --idempotency-key KEY` | sever one explicit read grant |
| `confluence status` | `floati confluence status --root ROOT` | list recorded read grants |
| `confluence bundle` | `floati confluence bundle --root ROOT --consumer CONSUMER --out PATH [--schema-version {1,2}]` | materialize the receipts-read bundle |
| `confluence adopt` | `floati confluence adopt --root ROOT --consumer CONSUMER --session SESSION --manager NODE --authority-subject SUBJECT --authority-epoch N --authority-expires-at TIMESTAMP` | adopt one session into managed mode |
| `confluence release` | `floati confluence release --root ROOT --consumer CONSUMER --session SESSION --manager NODE --authority-epoch N` | release one adopted session |
| `register` | `floati register [--root ROOT] NODE --harness HARNESS [--create-workspace]` | register one named node |
| `retire` | `floati retire [--root ROOT] NODE` | retire one named registry row |
| `journal` | `floati journal {checkpoint&#124;verify}` | exact-byte journal testimony |
| `journal checkpoint` | `floati journal checkpoint --root ROOT --journal JOURNAL --journal-id ID --kind KINDS --output OUTPUT [--json]` | write one bounded checkpoint |
| `journal verify` | `floati journal verify --root ROOT --journal JOURNAL --journal-id ID --kind KINDS --checkpoint CHECKPOINT [--historical] [--json]` | verify one bounded checkpoint |
| `repair` | `floati repair {quarantine}` | govern explicit ledger repair |
| `repair quarantine` | `floati repair quarantine --root ROOT --ledger LEDGER --record-id ID --idempotency-key KEY` | quarantine one exact event frame |
| `signature` | `floati signature {sign&#124;verify}` | Minisign artifact testimony |
| `signature sign` | `floati signature sign --root ROOT --artifact ARTIFACT --signature SIGNATURE --secret-key PATH --version VERSION [--journal-id ID] [--through-seq N] [--minisign-executable EXE] [--json]` | sign one explicit artifact |
| `signature verify` | `floati signature verify --root ROOT --artifact ARTIFACT --signature SIGNATURE --public-key PATH --version VERSION [--journal-id ID] [--through-seq N] [--minisign-executable EXE] [--json]` | verify one explicit artifact |
| `ask` | `floati ask --root ROOT --from NODE --to ADDRESS [--preview &#124; --recipient-preview PATH] [--repo REPO] [--sha SHA] [--repository PATH] [--doc DOC] [--note NOTE] [--session SESSION] [--idempotency-key KEY] [--wait-seconds N] [--reply-to ID] [--disposition {answer,decline}] [--reason REASON]` | DRAFT - ask one node and wait for its correlated reply |
| `replay` | `floati replay --root ROOT (--question ID &#124; --work-item ID)` | read an ask or work receipt chain |
| `send` | `floati send [--root ROOT] --from NODE --to ADDRESS [--preview &#124; --recipient-preview PATH] --repo REPO --sha SHA [--repository PATH] --doc DOC --note NOTE [--reply-to ID] [--message-class {normal,fyi}] [--idempotency-key KEY] [--claim PATH]` | append a Git notification |
| `verify` | `floati verify --root ROOT --as NODE --claim PATH [--json]` | reproduce a typed delivery claim |
| `inbox` | `floati inbox [--root ROOT] --as NODE [--session SESSION] [--peek]` | drain pending mail |
| `ack` | `floati ack [--root ROOT] --as NODE --id ID --session SESSION` | acknowledge presented messages |
| `sent` | `floati sent [--root ROOT] --as NODE` | project sender receipt state |
| `log` | `floati log [--root ROOT] [--replay] [--speed SPEED] [--plain]` | read mail or replay orchestration evidence |
| `status` | `floati status [--root ROOT] [--destination DESTINATION] [--json]` | summarize the fleet |
| `snapshot` | `floati snapshot --root ROOT --out PATH [--lines N] [--yes]` | build one consented maintainer support bundle |
| `effects` | `floati effects [--root ROOT] [--run ID] [--attempt ID]` | list projected effect status |
| `effect` | `floati effect {show&#124;reconcile&#124;compensate}` | inspect or operate one effect |
| `effect show` | `floati effect show [--root ROOT] --operation ID` | inspect one exact effect |
| `effect reconcile` | `floati effect reconcile [--root ROOT] --operation ID` | reconcile one effect |
| `effect compensate` | `floati effect compensate [--root ROOT] --operation ID (--preview &#124; --confirm CONFIRM)` | request compensation planning |
| `threads` | `floati threads [--root ROOT]` | list registered thread observations |
| `thread` | `floati thread {attach&#124;observe&#124;detach&#124;show}` | operate one registered thread attachment |
| `thread attach` | `floati thread attach [--root ROOT] --as NODE --thread THREAD [--work-item ID] [--run ID] [--attempt ID]` | register one explicit provider thread |
| `thread observe` | `floati thread observe [--root ROOT] --attachment ID [--codex-executable EXE]` | pull one registered provider status |
| `thread detach` | `floati thread detach [--root ROOT] --as NODE --attachment ID` | stop future observations |
| `thread show` | `floati thread show [--root ROOT] --attachment ID` | inspect one exact attachment |
| `graph` | `floati graph [--root ROOT] [--json]` | render the Harbor Chart |
| `plan` | `floati plan [--root ROOT] --plan PLAN --policy POLICY --explain [--json]` | explain read-only plan admission |
| `doctor` | `floati doctor [--root ROOT] --source SOURCE [--ref REF] [--gateway-config PATH] [--profile PROFILE] [--no-sandbox] [--probe] [--probe-budget SECONDS] [--destination DESTINATION] [--codex-hooks PATH] [--codex-config PATH] [--json]` | diagnose root and bundle integrity |
| `watch` | `floati watch [--root ROOT] [--destination DESTINATION] [--interval INTERVAL] [--iterations N]` | poll for fleet deltas |
| `wait` | `floati wait --for CONDITION --root PATH [--workspace PATH] [--session-id ID]` | hold a turn until a named condition |
| `waiter` | `floati waiter {arm}` | arm exact waiter consent for a declared workspace |
| `waiter arm` | `floati waiter arm --root PATH --node NODE --workspace PATH --harness HARNESS --hook-timeout-seconds N --wait-deadline-seconds N` | map one workspace and arm its waiter consent |
| `receipts` | `floati receipts NODE [--root ROOT] [--since TIMESTAMP]` | inspect node receipt history |
| `consume` | `floati consume ID --root ROOT --as NODE --session SESSION --idempotency-key KEY` | record action on an acknowledged message |
| `supervise` | `floati supervise [--root ROOT]` | report fleet health |
| `presence` | `floati presence {report&#124;show&#124;set}` | declare fleet presence or report node liveness |
| `presence report` | `floati presence report [--root ROOT] --as NODE --ttl-seconds N` | record this node's own liveness |
| `presence show` | `floati presence show [--root ROOT]` | inspect node self-reports |
| `presence set` | `floati presence set --root ROOT --as NODE --mode {AWAY,PRESENT} --idempotency-key KEY` | declare the fleet AWAY or PRESENT |
| `board` | `floati board [--root ROOT &#124; --demo] [--no-animation] [--session SESSION]` | open the TUI harbor board |
| `orchestrate` | `floati orchestrate [--root ROOT] --plan PLAN --adapter {codex} --deadline DEADLINE [--no-animation]` | seed and run a worker fleet |
| `sequencer` | `floati sequencer {status&#124;serve&#124;direct}` | manage the optional local writer |
| `sequencer status` | `floati sequencer status [--root ROOT]` | observe local writer mode |
| `sequencer serve` | `floati sequencer serve [--root ROOT] --as NODE [--takeover]` | run the local managed writer |
| `sequencer direct` | `floati sequencer direct [--root ROOT] --as NODE` | restore daemonless writer mode |
| `work` | `floati work {add&#124;claim&#124;complete&#124;show&#124;notify&#124;handoff}` | operate the orchestration log |
| `work add` | `floati work add [--root ROOT] --title TITLE [--owner NODE] [--as NODE] [--workspace] [--adhoc-profile ID] [--needs NEEDS] [--edits PATH] [--repo REPO] [--sha SHA] [--doc DOC]` | append a work item |
| `work claim` | `floati work claim [--root ROOT] --id ID [--as NODE] [--authority-subject SUBJECT] [--authority-epoch N] [--now NOW]` | claim with authority |
| `work complete` | `floati work complete [--root ROOT] --id ID [--as NODE] [--now NOW] [--consume ID] [--session SESSION] [--repo REPO] [--sha SHA] [--doc DOC]` | append completion |
| `work show` | `floati work show [--root ROOT] [--id ID] [--as NODE]` | project work state |
| `work notify` | `floati work notify --root ROOT (--id ID &#124; --all) --as NODE` | replay pending work notifications |
| `work handoff` | `floati work handoff {record&#124;show}` | record or inspect work handoffs |
| `work handoff record` | `floati work handoff record --root ROOT --id ID --as NODE --repository PATH [--run ARGV] [--unittest SELECTOR] [--artifact PATH] [--remaining TEXT] [--statement TEXT] [--supersedes ID] --idempotency-key KEY` | capture a work handoff |
| `work handoff show` | `floati work handoff show --root ROOT --id ID [--handoff-id ID] [--json]` | revalidate a work handoff |
| `dispatch` | `floati dispatch {record&#124;return&#124;close&#124;show&#124;brief}` | record and recover local dispatch evidence |
| `dispatch record` | `floati dispatch record --root ROOT --id ID --as NODE --idempotency-key KEY --to NODE --branch BRANCH --brief-file PATH (--external LABEL &#124; --envelope-id ID) [--supersedes ID]` | record a brief revision as you send it |
| `dispatch return` | `floati dispatch return --root ROOT --id ID --as NODE --idempotency-key KEY [--revision N] --repository PATH --ref REF --artifact PATH [--statement TEXT]` | file a returned artifact against its brief |
| `dispatch close` | `floati dispatch close --root ROOT --id ID --as NODE --idempotency-key KEY --reason TEXT` | close a dispatch with a reason |
| `dispatch show` | `floati dispatch show --root ROOT [--id ID] [--json]` | read dispatch revisions, returns and outstanding work |
| `dispatch brief` | `floati dispatch brief --root ROOT --id ID --as NODE [--json] --revision N` | read the local text of one brief revision |
| `intake` | `floati intake {scan&#124;show&#124;adopt&#124;preview&#124;dispatch}` | adopt bounded work-queue sources |
| `intake scan` | `floati intake scan --root ROOT --from DIR` | inspect local Markdown intake |
| `intake show` | `floati intake show --root ROOT [--id ID]` | inspect immutable snapshots |
| `intake adopt` | `floati intake adopt --root ROOT --source {local,github} [--from DIR] [--path RELATIVE] [--repo O/R] [--issue N] [--gh EXE] [--owner NODE] [--now NOW]` | adopt one explicit intake source |
| `intake preview` | `floati intake preview --root ROOT --snapshot ID --operation {comment,label_add,label_remove,close,pr_link} [--body BODY] [--body-file PATH] [--label NAME] [--reason {completed,not_planned}] [--pr N]` | preview one GitHub mutation |
| `intake dispatch` | `floati intake dispatch --root ROOT --snapshot ID --operation {comment,label_add,label_remove,close,pr_link} [--body BODY] [--body-file PATH] [--label NAME] [--reason {completed,not_planned}] [--pr N] --confirm-digest SHA256 --run-id ID --item-id ID --attempt-id ID --fence-token TOKEN [--approval-request ID] [--approval-decision ID] [--approval-consumption ID]` | bind a preview to one effect intent |
| `worker` | `floati worker {adhoc&#124;run}` | run one authority-checked worker |
| `worker adhoc` | `floati worker adhoc {profile&#124;run&#124;show&#124;teardown}` | operate one governed persistent native worker |
| `worker adhoc profile` | `floati worker adhoc profile --root ROOT --as NODE --adapter {codex,claude,pi} --lane-opening ID --repository REPO --to NODE --idempotency-key KEY [--authority-subject SUBJECT] [--authority-epoch N] [--grant-launch]` | preview one leased profile and an explicit human launch grant |
| `worker adhoc run` | `floati worker adhoc run --root ROOT --as NODE --session SESSION --profile ID --envelope ID --idempotency-key KEY` | execute one authenticated native dispatch |
| `worker adhoc show` | `floati worker adhoc show --root ROOT --dispatch ID` | inspect one durable native dispatch |
| `worker adhoc teardown` | `floati worker adhoc teardown --root ROOT --as NODE --session SESSION --dispatch ID --idempotency-key KEY` | close one proved complete native worker row |
| `worker run` | `floati worker run [--root ROOT] --as NODE --adapter {claude,codex,pi} [--claude-executable EXE] [--codex-executable EXE] [--pi-executable EXE]` | run one authority-checked worker |
| `mcp` | `floati mcp {serve}` | expose launch-bound agent tools |
| `mcp serve` | `floati mcp serve --root ROOT --as NODE --session SESSION` | serve one launch-bound MCP session |
| `install` | `floati install --source SOURCE --destination DESTINATION [--ref REF] [--committed-tree] [--json]` | install the exact governed bundle |
| `update` | `floati update [--source SOURCE] [--destination DESTINATION] [--ref REF] [--committed-tree] [--json] [--profile-registry PATH] [--fleet-profile PROFILE] [{prepare&#124;fleet}]` | update the exact governed bundle |
| `update prepare` | `floati update [--source SOURCE] [--destination DESTINATION] [--ref REF] [--committed-tree] [--json] [--profile-registry PATH] [--fleet-profile PROFILE] prepare --root ROOT [--status]` | prepare one root for sends after an update |
| `update fleet` | `floati update [--source SOURCE] [--destination DESTINATION] [--ref REF] [--committed-tree] [--json] [--profile-registry PATH] [--fleet-profile PROFILE] fleet {preview&#124;apply}` | plan or apply one explicit fleet-wide update |
| `update fleet preview` | `floati update [--source SOURCE] [--destination DESTINATION] [--ref REF] [--committed-tree] [--json] [--profile-registry PATH] [--fleet-profile PROFILE] fleet preview --root ROOT --as NODE --destination DESTINATION --channel CHANNEL --version VERSION --waiter-binding PATH --transport-registry PATH --transport TRANSPORT [--json]` | derive one immutable fleet update plan |
| `update fleet apply` | `floati update [--source SOURCE] [--destination DESTINATION] [--ref REF] [--committed-tree] [--json] [--profile-registry PATH] [--fleet-profile PROFILE] fleet apply --root ROOT --as NODE --destination DESTINATION --channel CHANNEL --version VERSION --waiter-binding PATH --transport-registry PATH --transport TRANSPORT [--json] --plan-digest SHA256 --idempotency-key KEY` | apply one consented immutable fleet update plan |
| `epoch` | `floati epoch {roll}` | govern one whole bus epoch |
| `epoch roll` | `floati epoch roll --root ROOT --as NODE --idempotency-key KEY` | archive and replace one bus epoch |
| `grant` | `floati grant [--root ROOT] [--as NODE] [--holder NODE] [--subject SUBJECT] [--epoch N] [{revoke}]` | append exact work authority |
| `grant revoke` | `floati grant [--root ROOT] [--as NODE] [--holder NODE] [--subject SUBJECT] [--epoch N] revoke [--root ROOT] --as NODE --holder NODE --subject SUBJECT --epoch N` | revoke exact work authority |
| `node` | `floati node {person&#124;add&#124;spawn&#124;retire&#124;drain&#124;switch&#124;role&#124;boot&#124;teardown&#124;explain&#124;prep-clear&#124;state-flush&#124;prompts}` | node administration |
| `node person` | `floati node person {set&#124;clear}` | record explicit person membership |
| `node person set` | `floati node person set --root ROOT --node NODE --person PERSON` | record one node person |
| `node person clear` | `floati node person clear --root ROOT --node NODE` | clear one node person |
| `node add` | `floati node add [--root ROOT] [--node NODE] [--harness HARNESS] [--lifetime {permanent,temporary}] [--lease-minutes N] [--tide-metric METRIC] [--tide-threshold VALUE] [--tide-action {recommend,direct}] [--tide-idempotency-key KEY] [--plan FILE]` | add one node |
| `node spawn` | `floati node spawn --root ROOT --as NODE --profile PROFILE [--ordinal N]` | create one numbered role instance |
| `node retire` | `floati node retire --root ROOT (--node NODE &#124; --instance INSTANCE) [--as NODE] [--drain]` | retire one node or numbered instance |
| `node drain` | `floati node drain --root ROOT --node NODE --session SESSION` | empty one node's inbox without retiring it |
| `node switch` | `floati node switch --root ROOT --node NODE --harness HARNESS --model MODEL` | switch provider assignment |
| `node role` | `floati node role --root ROOT --node NODE [--template TEMPLATE] [--answer ANSWERS] [--expected-predecessor EXPECTED_PREDECESSOR]` | assign a shipped role |
| `node boot` | `floati node boot [--root ROOT] [--node NODE] [--declared-roots FILE] [--managed-executable EXE] [--profile PROFILE] [--json] [--session SESSION] [--runtime RUNTIME] [--hooks HOOKS] [--destination DESTINATION] [--acknowledge-stop-hook-change ACKNOWLEDGE_STOP_HOOK_CHANGE]` | board and hold the current declared seat |
| `node teardown` | `floati node teardown [--root ROOT] [--node NODE] [--declared-roots FILE] [--managed-executable EXE] [--profile PROFILE] [--json] [--session SESSION] [--repo REPO] [--doc DOC]` | end one declared seat session |
| `node explain` | `floati node explain --root ROOT --node NODE --declared-roots FILE --managed-executable EXE --profile PROFILE [--json]` | explain one live node |
| `node prep-clear` | `floati node prep-clear --root ROOT --as ACTOR --session SESSION --workspace WORKSPACE --repo REPO --doc DOC --note NOTE [--complement COMPLEMENT] [--to TO] [--idempotency-key IDEMPOTENCY_KEY] [--git-executable GIT_EXECUTABLE]` | wind one seat down |
| `node state-flush` | `floati node state-flush --root ROOT --node NODE [--prior-mtime-ns N]` | receipt one state flush |
| `node prompts` | `floati node prompts --root ROOT --as NODE --harness HARNESS --out DIR` | project per-seat lifecycle command files |
| `role` | `floati role {list&#124;show&#124;transfer-architect&#124;new&#124;import&#124;edit&#124;validate}` | inspect and author root-local role templates |
| `role list` | `floati role list --root ROOT` | list available roles |
| `role show` | `floati role show --root ROOT ROLE` | show one available role |
| `role transfer-architect` | `floati role transfer-architect --root ROOT --to NODE --idempotency-key IDEMPOTENCY_KEY` | move the architect role to one active node |
| `role new` | `floati role new --root ROOT --name ROLE --from ROLE --idempotency-key KEY` | create one root-local role |
| `role import` | `floati role import --root ROOT --from PATH --idempotency-key KEY` | import one local role file |
| `role edit` | `floati role edit --root ROOT --name ROLE (--set FIELD=VALUE &#124; --from PATH) --idempotency-key KEY` | replace one custom role through validated edits |
| `role validate` | `floati role validate --root ROOT --from PATH` | validate one local role without writing |
| `quota` | `floati quota {collect&#124;show}` | inspect or collect cited local quota testimony |
| `quota collect` | `floati quota collect --root ROOT --provider {anthropic_claude_code,openai_codex,google_gemini,cursor_individual,xai_grok,github_copilot} --observed-at TIMESTAMP --idempotency-key KEY [--executable EXE]` | collect one local quota receipt |
| `quota show` | `floati quota show --root ROOT --provider {anthropic_claude_code,openai_codex,google_gemini,cursor_individual,xai_grok,github_copilot}` | inspect one provider quota receipt |
| `chart` | `floati chart [--declared-roots FILE] [--live] [--json] [{add-root&#124;remove-root&#124;timings}]` | multi-bus Harbor Chart |
| `chart add-root` | `floati chart [--declared-roots FILE] [--live] [--json] add-root --declared-roots FILE --bus-id ID --root PATH --architect-node NODE [--downstream ID]` | add one declared root |
| `chart remove-root` | `floati chart [--declared-roots FILE] [--live] [--json] remove-root --declared-roots FILE --bus-id ID` | remove one declared root |
| `chart timings` | `floati chart [--declared-roots FILE] [--live] [--json] timings --root ROOT [--command C] [--since ISO]` | derived timing percentiles for instrumented verbs |
| `survey` | `floati survey --declared-roots FILE [--search-path PATH] [--hooks PATH] [--targets PATH] [--json]` | read-only foreign-bus survey |
| `seat` | `floati seat {add&#124;remove&#124;board&#124;teardown&#124;migrate&#124;retire}` | create, remove, and board explicit seats |
| `seat add` | `floati seat add --root ROOT --home PATH --source PATH --gateway-config PATH --codex-config PATH --transport TRANSPORT --coordinator NODE --repo REPO [--mode {desktop,cli}] [--node NODE] [--profile PROFILE] [--codex-executable EXE] [--codex-hooks PATH] [--warm-build-script FILE] [--rehome]` | prepare one owned Codex seat home |
| `seat remove` | `floati seat remove --root ROOT --home PATH` | retire one unchanged owned seat home |
| `seat board` | `floati seat board --root ROOT --as NODE --workspace PATH --session SESSION --idempotency-key KEY [--take-over]` | arm, resume, and drain one declared session |
| `seat teardown` | `floati seat teardown --root ROOT --as NODE --home HOME --session SESSION --repo REPO --doc DOC --idempotency-key KEY` | pause and report one completed declared session |
| `seat migrate` | `floati seat migrate --root ROOT --as NODE --workspace PATH --idempotency-key KEY [--coordinator NODE]` | migrate one registered pre-governance seat once |
| `seat retire` | `floati seat retire --root ROOT [--apply]` | offer git worktree remove for finished row trees |
| `hook` | `floati hook {install&#124;uninstall}` | install explicit native harness hooks |
| `hook install` | `floati hook install --harness {cursor,codex,zcode,claude} --root ROOT [--as NODE] [--workspace WORKSPACE] [--prepare-only] --runtime RUNTIME [--hooks HOOKS] [--destination DESTINATION] [--hook-timeout-seconds HOOK_TIMEOUT_SECONDS] [--wait-deadline-seconds WAIT_DEADLINE_SECONDS] [--acknowledge-stop-hook-change ACKNOWLEDGE_STOP_HOOK_CHANGE]` | install Cursor, Codex, zcode, or Claude native hooks |
| `hook uninstall` | `floati hook uninstall --harness {cursor,zcode,claude} [--hooks HOOKS] --root ROOT --as NODE --workspace WORKSPACE --runtime RUNTIME` | remove one cursor seat marker or restore one zcode or Claude Stop hook |
| `wake` | `floati wake {pause&#124;resume&#124;status&#124;arm&#124;wait&#124;daemon}` | control exact wake coordinates |
| `wake pause` | `floati wake pause --root ROOT --as NODE --session SESSION [--idempotency-key KEY]` | pause one exact session |
| `wake resume` | `floati wake resume --root ROOT --as NODE --session SESSION [--idempotency-key KEY]` | resume one exact session |
| `wake status` | `floati wake status --root ROOT --as NODE --session SESSION` | inspect one exact session |
| `wake arm` | `floati wake arm --root ROOT --as NODE --session SESSION --workspace WORKSPACE --idempotency-key KEY [--take-over]` | arm one exact acting session |
| `wake wait` | `floati wake wait --harness {cursor,zcode,claude} --root ROOT --as NODE --runtime RUNTIME [--deadline-seconds N] [--hook-timeout-seconds N] [--poll-seconds N] [--loop-limit N]` | hold a Cursor, zcode or Claude stop until mail or a deadline |
| `wake daemon` | `floati wake daemon {consent&#124;bind&#124;install&#124;start&#124;status&#124;stop&#124;remove&#124;revoke}` | manage one local wake daemon |
| `wake daemon consent` | `floati wake daemon consent --root ROOT --as NODE --harness {codex,cursor,grok-build,zcode} --min-poll-seconds N --max-poll-seconds N --max-backoff-seconds N --activation-epoch N` | record exact activation consent |
| `wake daemon bind` | `floati wake daemon bind --root ROOT --as NODE --harness {codex,cursor,grok-build,zcode} --session SESSION --workspace WORKSPACE --executable EXE --binding-epoch N [--yes] [--zcode-node-executable EXE] [--zcode-entry-executable EXE]` | bind one exact session |
| `wake daemon install` | `floati wake daemon install --root ROOT --as NODE --harness {codex,cursor,grok-build,zcode}` | install the exact LaunchAgent |
| `wake daemon start` | `floati wake daemon start --root ROOT --as NODE --harness {codex,cursor,grok-build,zcode}` | start the exact LaunchAgent |
| `wake daemon status` | `floati wake daemon status --root ROOT --as NODE --harness {codex,cursor,grok-build,zcode}` | inspect one daemon coordinate |
| `wake daemon stop` | `floati wake daemon stop --root ROOT --as NODE --harness {codex,cursor,grok-build,zcode}` | stop the exact LaunchAgent |
| `wake daemon remove` | `floati wake daemon remove --root ROOT --as NODE --harness {codex,cursor,grok-build,zcode}` | remove the exact LaunchAgent |
| `wake daemon revoke` | `floati wake daemon revoke --root ROOT --as NODE --harness {codex,cursor,grok-build,zcode}` | revoke exact daemon consent |
| `uninstall` | `floati uninstall --destination DESTINATION [--dry-run] [--receipt-dir DIR] [--json]` | remove exact owned tool bytes |
| `context` | `floati context {status&#124;turnover&#124;policy&#124;reading}` | inspect context evidence and manage Tide signals |
| `context status` | `floati context status --root ROOT --as NODE [--json]` | report harness evidence |
| `context turnover` | `floati context turnover --root ROOT --as NODE [--json]` | project the turnover ritual |
| `context policy` | `floati context policy {set&#124;show&#124;clear}` | manage Tide context policy |
| `context policy set` | `floati context policy set --root ROOT --node NODE --metric METRIC --threshold THRESHOLD --action {recommend,direct} --idempotency-key KEY [--json]` | set one Tide policy |
| `context policy show` | `floati context policy show --root ROOT --node NODE [--json]` | show one Tide policy |
| `context policy clear` | `floati context policy clear --root ROOT --node NODE --idempotency-key KEY [--json]` | clear one Tide policy |
| `context reading` | `floati context reading {record}` | record seated context testimony |
| `context reading record` | `floati context reading record --root ROOT --as NODE --metric METRIC --value VALUE --command {/context,/status,/usage,/cost} --idempotency-key KEY [--json]` | append context testimony |
| `purge` | `floati purge --root ROOTS [--dry-run]` | move the exact roots you list into the account Trash; never deletes |
| `hold` | `floati hold {start&#124;release&#124;inbound}` | manage project holds and inbound mail holds |
| `hold start` | `floati hold start --root ROOT --as NODE --idempotency-key KEY --reason TEXT [--expected-release TIMESTAMP]` | append one versioned project hold |
| `hold release` | `floati hold release --root ROOT --as NODE --idempotency-key KEY [--hold HOLD]` | append one terminal project-hold release |
| `hold inbound` | `floati hold inbound {start&#124;show}` | start or inspect a node’s inbound mail hold |
| `hold inbound start` | `floati hold inbound start --root ROOT --node NODE --idempotency-key KEY --reason TEXT [--expected-release TIMESTAMP]` | hold incoming mail for an active node |
| `hold inbound show` | `floati hold inbound show --root ROOT --node NODE` | inspect a node’s inbound mail holds and decisions |
| `needs` | `floati needs {file&#124;close&#124;show&#124;observe&#124;admission&#124;read&#124;budget&#124;release}` | raise and read what needs a person |
| `needs file` | `floati needs file --root ROOT --as NODE --kind {approval,question,review} --title TEXT [--detail TEXT] [--repository PATH] [--ref REF] [--commit SHA] [--path PATH] [--supersedes ID] --idempotency-key KEY` | raise one item for a person |
| `needs close` | `floati needs close --root ROOT --as NODE --item ID --reason TEXT --idempotency-key KEY` | close one item its node raised |
| `needs show` | `floati needs show --root ROOT [--all]` | list what needs a person, with freshness |
| `needs observe` | `floati needs observe --root ROOT --as NODE --source-config PATH` | observe one orchestrator for threads that need a person |
| `needs admission` | `floati needs admission --root ROOT` | preview attention admission for a new dispatch |
| `needs read` | `floati needs read --root ROOT --item ID --idempotency-key KEY` | mark items read, as a person |
| `needs budget` | `floati needs budget --root ROOT (--limit N &#124; --clear) [--hold-dispatches] --idempotency-key KEY` | declare the attention budget, as a person |
| `needs release` | `floati needs release --root ROOT --hold ID --idempotency-key KEY` | release one held dispatch, as a person |
| `territory` | `floati territory {declare&#124;release&#124;show&#124;preview}` | declare leased working paths and preview overlap |
| `territory declare` | `floati territory declare --root ROOT --as NODE --path PATH --ttl-seconds N --idempotency-key KEY [--now TIMESTAMP]` | lease working paths for one node |
| `territory release` | `floati territory release --root ROOT --as NODE --territory ID --idempotency-key KEY` | end one territory lease |
| `territory show` | `floati territory show --root ROOT` | list territory leases and their state |
| `territory preview` | `floati territory preview --root ROOT --owner NODE --edits PATH [--base-ref REF]` | preview the territories a dispatch would enter |
| `quiet` | `floati quiet {run&#124;status&#124;show&#124;release}` | hold enrolled seats' builds and tests for one quiet window |
| `quiet run` | `floati quiet run --root ROOT --as NODE --idempotency-key KEY [--trigger {operator,memory_pressure}] [--load-bound LOAD] [--pressure-metric {psi_memory_full_avg10,psi_memory_some_avg10,vm_pressure_level}] [--pressure-threshold VALUE] [--pressure-release-below VALUE] --settle-cap-seconds N --cap-seconds N [--interval-seconds N] [--release-file PATH] [--exempt NODE]` | settle, hold and release one quiet window |
| `quiet status` | `floati quiet status --root ROOT [--json] [--command TEXT]` | say whether a quiet window holds this fleet now |
| `quiet show` | `floati quiet show --root ROOT [--window ID]` | list quiet windows or show one with its exposure receipts |
| `quiet release` | `floati quiet release --root ROOT --as NODE --window ID --idempotency-key KEY [--resolve-pid PID] [--start-token TOKEN]` | release a quiet window whose driver died |
| `backup` | `floati backup DIR --root ROOT --epoch EPOCH_ID` | export one retained epoch archive |
<!-- END GENERATED COMMAND TABLE -->

Delivery and acknowledgment are separate receipts; a successful send proves the
append, never the delivery. For low-level `register` and `retire`, using your own
seat is an operating convention: caller-selected NODE is not bound to the calling
seat. Node changes are preview-first.
Confluence is a read seam with no discovery, watcher, network or mutation API.
Wake control is marker-only and receipted; it never edits hook registration.
`wake status` reports the wake-daemon breaker from the runtime; a coordinate is
underivable with reason runtime_missing, runtime_symlink, or runtime_malformed.
Survey is read-only; purge moves declared roots to Trash and never deletes them.
The node-add wizard offers the same read-only survey inline when an undeclared
bus is in scope, and asks before adopting.
A seat's `node boot` prints its exact managed wrapper shapes for send and ack;
use those verbatim, never a remembered shape.

## Standard workflows

- **Solo:** `init --root R --solo me --harness Codex` → `work`/`log`/`board`.
- **Fleet:** `init` → `node add` per seat → `send`/`inbox`/`ack` between seats → `board`.
- **Manual non-solo work authority:** add the architect first,
  assign its shipped role, add the holder, grant one exact coordinate, then
  add and claim work. The reverse uses the same coordinate and architect gate.

```text
floati init --root /var/tmp/fleet
floati node add --root /var/tmp/fleet --node architect-a --harness Codex --lifetime permanent
floati node role --root /var/tmp/fleet --node architect-a --template architect --answer repo=floati --answer never_touch=foreign-project --answer owner_stops=owner-tier
floati node add --root /var/tmp/fleet --node builder-a --harness Codex --lifetime permanent
floati grant --root /var/tmp/fleet --as architect-a --holder builder-a --subject work-claims --epoch 1
floati work add --root /var/tmp/fleet --title bounded-work --owner builder-a
floati work claim --root /var/tmp/fleet --id work-00000000000070008000000000000000 --as builder-a --authority-subject work-claims --authority-epoch 1
floati grant revoke --root /var/tmp/fleet --as architect-a --holder builder-a --subject work-claims --epoch 1
```

- **Health check:** `doctor --root R --source S` → chase every red with the receipt it
  names → `doctor --probe` for suspected deafness.
- **Map the estate:** declare roots in a file → `chart --declared-roots F` →
  `survey` when you suspect a bus you did not create.
- **Leave:** `wake pause` per session → `node retire`/`retire` →
  `uninstall --dry-run` → `uninstall`. Leaving is a first-class feature:
  every capability has its reverse, and user records outlive the tool.

## Fences an agent must respect

- Never scan the filesystem for roots; operate only on roots the user named.
- Never read, edit, or shadow another bus's registrations, markers, hooks,
  or state — coexistence, never modification. `survey` is the only lens on
  foreign buses and it is read-only.
- Never wake, or install wake machinery for, a session without the user's
  recorded consent — and never run a production waiter by hand against a
  live root; fixtures exist.
- Never treat a receipt as more than it says: an append is not a delivery,
  a delivery is not an acknowledgment, a poll is not a wake.
- Never invent a number floati did not measure. Absences are typed and
  cited, not filled in.

## Fleet governance — binding rules (detail and incidents: `docs/AGENT-OPERATIONS.md`)

- An ack means SEEN, nothing more. Disagreement is a reply; work is a work receipt. Never
  withhold an ack to signal anything; use `--peek` only for an explicit process-before-ack flow.
- A question typed into your own session is invisible to every other node. Put the wait on
  the bus as an envelope, then keep working on what does not depend on the answer.
- Answer with coordinates (exact commit, file, count), never summaries; copy SHAs by command
  substitution; say what you did NOT touch. Silence is evidence only when you name the root
  and node you drained.
- Your identity comes from your workspace seat declaration, never from memory. A remembered
  command that names no root belongs to another fleet: stop and ask.
- A reassignment is a dispatch: old and new owner both get an envelope. Do not take a claimed
  task; do not assume an unclaimed one is blocked; check the mail and the log.
- A refusal that names its contract is an instruction (apply the printed shape once); a
  refusal that names a policy is a stop. Never retry a refusal unchanged, never retry a
  policy refusal at all. A wrapper's contract can be stricter than the bare CLI; the wrapper
  is the contract for that seat.
- Boarding order is fixed: attach, take over the wake claim, then drain. Re-run
  `floati wake arm --root ROOT --as NODE --session SESSION --workspace PATH --idempotency-key KEY` at every
  session turnover; a live predecessor requires `--take-over`, a paused claim does not.
- Most seats run with no human watching. Never wait for operator approval that will never
  come: route decisions to the fleet's DECLARED coordinator as an envelope and keep working;
  owner-tier questions park with the coordinator. Topology and coordinator authority are
  declared at fleet setup, in writing; authority is per-fleet, never inherited.
- On Codex, HEALTHY idle is a turn that does not visibly end (the Stop waiter holds it). A
  turn that ends promptly is a diagnostic flag (stale wake claim, tripped breaker, pause
  marker, exhaustion): check `doctor` before assuming quiet.
- Safe fixes are reads and governed verbs; breaking fixes are raw file edits, wrapper
  bypasses, and identity guesses. When a refusal names no remedy, envelope your coordinator.
  `malformed_evidence` on an unknown record kind is usually version skew: update the reader,
  never edit a ledger.

## Where the rest lives

- `docs/AGENT-OPERATIONS.md`: the full verb reference and the incident-backed long form of
  every rule above (hook shadowing, message/ack hygiene, topology, troubleshooting), verbatim.
- `docs/CONFLUENCE-v0.md` (the `status --json` / `graph --json` machine contract),
  `docs/FLEET.md`, `docs/FLEET-AUTONOMY.md`, `docs/TRUTH-GUARANTEES.md`.
