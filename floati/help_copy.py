"""Reviewed help prose; argparse owns all command and argument grammar.

Each topic supplies a NAME description, body, operational notes, examples and
semantic option descriptions. Option keys are long flags or positional destinations;
they are annotations, never declarations of supported syntax or requiredness.
"""
from __future__ import annotations

from .copy import GH_AUTHENTICATION_REMEDY


HELP_COPY = {
 'needs': {'description': 'raise and read what needs a person',
           'body': 'One pull read of every review, approval and question agents have raised for a person in this root, and every orchestrator thread Floati observed finishing or waiting on a person, each with its source, freshness and read state.',
           'notes': ('An item bound to a ref, commit or files is checked against that subject each time it is read; a stale item names what moved.',
                     'Marking items read, declaring the attention budget and releasing a held dispatch are a person\'s acts at a controlling terminal; MCP never serves them.'),
           'examples': 'floati needs file --help\nfloati needs show --help', 'option_docs': {}},
 'needs file': {'description': 'raise one item for a person',
                'body': 'Append one review, approval or question for a person. A coordinate binds the item to a ref tip, a commit or a set of files, resolved now. A correction names the item it supersedes. Repeating the same key and request replays its receipt.',
                'notes': ('With --ref the item goes stale when the ref moves; with --path as well, only when a named file changes. With only --path the item goes stale when a named file\'s bytes change. --path with --commit alone is refused.',),
                'examples': 'floati needs file --root /absolute/fleet --as builder-a --kind review --title "review the inbox branch" --repository /absolute/checkout --ref my-branch --idempotency-key review-1',
                'option_docs': {'--root': 'Explicit fleet root.', '--as': 'Registered active node raising the item.',
                                '--kind': 'review, approval or question.', '--title': 'One visible line within 256 UTF-8 bytes.',
                                '--detail': 'Optional visible line within 2048 UTF-8 bytes.',
                                '--repository': 'Absolute path of the checkout the item is about.',
                                '--ref': 'Branch or ref whose tip the item is bound to.',
                                '--commit': 'Full commit id the item is bound to; defaults to the ref tip.',
                                '--path': 'Repository-relative file the item is about.',
                                '--supersedes': 'Exact ID of the item this one corrects; it must be the head of its chain.',
                                '--idempotency-key': 'Stable key for this exact request.'}},
 'needs close': {'description': 'close one item its node raised',
                 'body': 'Append the terminal close of one open item, with its reason. Only the node that filed the item closes it.',
                 'notes': ('A closed item leaves the default read; show --all keeps it visible.',),
                 'examples': 'floati needs close --root /absolute/fleet --as builder-a --item needs-item-00000000000070008000000000000000 --reason "merged" --idempotency-key close-1',
                 'option_docs': {'--root': 'Explicit fleet root.', '--as': 'The node that filed the item.',
                                 '--item': 'Exact item ID.', '--reason': 'One visible line within 1024 UTF-8 bytes.',
                                 '--idempotency-key': 'Stable key for this close.'}},
 'needs show': {'description': 'list what needs a person, with freshness',
                'body': 'Read every open item across the seats of this root with its freshness (current, stale with what moved, unknown with its cause, or unbound), its read state, held dispatches, held inbound mail and the attention budget readout. Writes nothing.',
                'notes': ('Freshness is computed from each item\'s subject at this read, never from elapsed time.',),
                'examples': 'floati needs show --root /absolute/fleet',
                'option_docs': {'--root': 'Explicit fleet root.', '--all': 'Include closed items.'}},
 'needs observe': {'description': 'observe one orchestrator for threads that need a person',
                   'body': 'Read the thread list of the orchestrator a source configuration names, once, and add an item for each thread that finished or waits on a person and is not already in the inbox. The orchestrator is never written to and gets no credentials.',
                   'notes': ('Each item is one event: a thread that finishes a second turn adds a new item, which supersedes the earlier one.',
                             'A thread waiting on its own background task is not waiting on a person.',
                             'A missing or invalid configuration is refused; an orchestrator that cannot be read reports degraded with its cause.',
                             'A seat\'s Stop waiter runs this pull itself when the same file sits at config/needs-observed/NODE.json in the root, at most once every interval_seconds (30 to 3600, default 60), and wakes the seat once for each new item.'),
                   'examples': 'floati needs observe --root /absolute/fleet --as architect-a --source-config /absolute/observed-source.json',
                   'option_docs': {'--root': 'Explicit fleet root.', '--as': 'Registered active node recording what it observed.',
                                   '--source-config': 'Absolute path of the JSON file naming the orchestrator, its CLI, its home and the project.'}},
 'needs admission': {'description': 'preview attention admission for a new dispatch',
                     'body': 'Report the unread review count and the oldest unread review against the declared budget, and whether a new dispatch would be held now. Writes nothing.',
                     'notes': ('Dispatches are held only when the person has switched holding on.',),
                     'examples': 'floati needs admission --root /absolute/fleet',
                     'option_docs': {'--root': 'Explicit fleet root.'}},
 'needs read': {'description': 'mark items read, as a person',
                'body': 'Show the named items at the controlling terminal and record them read when the person types the code shown. The record keeps the freshness each item had when seen.',
                'notes': ('Refused without a controlling terminal and from inside a seat workspace.',),
                'examples': 'floati needs read --root /absolute/fleet --item needs-item-00000000000070008000000000000000 --idempotency-key read-1',
                'option_docs': {'--root': 'Explicit fleet root.', '--item': 'Exact item ID.',
                                '--idempotency-key': 'Stable key for this read.'}},
 'needs budget': {'description': 'declare the attention budget, as a person',
                  'body': 'Declare how many unread review items you can carry, or clear it, at the controlling terminal. Every new dispatch then shows the count against it.',
                  'notes': ('Holding new dispatches while over budget is off unless --hold-dispatches is passed.',
                            'Refused without a controlling terminal and from inside a seat workspace.'),
                  'examples': 'floati needs budget --root /absolute/fleet --limit 3 --idempotency-key budget-1',
                  'option_docs': {'--root': 'Explicit fleet root.', '--limit': 'Unread review items you can carry.',
                                  '--clear': 'Remove the declared budget.',
                                  '--hold-dispatches': 'Hold new dispatches while the queue is over budget.',
                                  '--idempotency-key': 'Stable key for this declaration.'}},
 'needs release': {'description': 'release one held dispatch, as a person',
                   'body': 'Show one held dispatch at the controlling terminal and record its release when the person types the code shown. The sender\'s next identical send then goes out.',
                   'notes': ('Refused without a controlling terminal and from inside a seat workspace.',),
                   'examples': 'floati needs release --root /absolute/fleet --hold needs-dispatch-hold-00000000000070008000000000000000 --idempotency-key release-1',
                   'option_docs': {'--root': 'Explicit fleet root.', '--hold': 'Exact held dispatch ID.',
                                   '--idempotency-key': 'Stable key for this release.'}},
 'dispatch': {'description': 'record and recover local dispatch evidence',
              'body': 'Record what a seat asked, what came back, and what is still out.',
              'notes': (), 'examples': 'floati dispatch show --help', 'option_docs': {}},
 'dispatch record': {'description': 'record a brief revision as you send it',
                     'body': 'File an external dispatch as you send it, or bind the exact envelope receipt from a Floati send.',
                     'notes': ('Keep secrets out of briefs. Floati stores the full text locally.',
                               'A correction names the current receipt with --supersedes.'),
                     'examples': 'floati dispatch record --help', 'option_docs': {}},
 'dispatch return': {'description': 'file a returned artifact against its brief',
                     'body': 'The sending seat records the artifact and delivery branch.',
                     'notes': ('Leave --revision unset only if the artifact did not name a brief revision.',),
                     'examples': 'floati dispatch return --help', 'option_docs': {}},
 'dispatch close': {'description': 'close a dispatch with a reason',
                    'body': 'Close the current revision and retain the journal for later reads.',
                    'notes': (), 'examples': 'floati dispatch close --help', 'option_docs': {}},
 'dispatch show': {'description': 'read dispatch revisions, returns and outstanding work',
                   'body': 'Rebuild the roster from local journals and check each returned coordinate.',
                   'notes': (), 'examples': 'floati dispatch show --help', 'option_docs': {}},
 'dispatch brief': {'description': 'read the local text of one brief revision',
                    'body': 'The sending seat can recover the original text of an exact revision.',
                    'notes': ('Keep secrets out of briefs. Floati stores the full text locally.',),
                    'examples': 'floati dispatch brief --help', 'option_docs': {}},
 'territory': {'description': 'declare leased working paths and preview overlap',
               'body': 'A node declares the absolute paths it is working in as a lease with an expiry. Work dispatched with --edits carries an advisory preview of the live leases and observed changes it would overlap. Nothing is refused.',
               'notes': ('Paths compare canonically: symlinks and dot segments resolve, case follows the filesystem, and every worktree of one repository shares one territory space.',
                         'An expired or released lease, or one whose holder is retired or past its node lease, is not a live territory.'),
               'examples': 'floati territory declare --help\nfloati territory preview --help',
               'option_docs': {}},
 'territory declare': {'description': 'lease working paths for one node',
                       'body': 'Append one territory lease naming absolute paths and an expiry. Overlapping leases are listed as advisory, never refused. Repeating the same key and request replays its receipt.',
                       'notes': ('Lease length is 60 seconds to 7 days. Declare again with a fresh key to extend.',),
                       'examples': 'floati territory declare --root /absolute/fleet --as builder-a --path /absolute/repo/pkg --ttl-seconds 3600 --idempotency-key territory-1',
                       'option_docs': {'--root': 'Explicit fleet root.', '--as': 'Registered active node that holds the territory.',
                                       '--path': 'Absolute working path; repeat for each.',
                                       '--ttl-seconds': 'Lease length in seconds.',
                                       '--idempotency-key': 'Stable key for this exact request.',
                                       '--now': 'Optional earlier UTC start time ending in Z; never later than now.'}},
 'territory release': {'description': 'end one territory lease',
                       'body': 'Append a terminal release for one of this node\'s leases. The lease record stays unchanged.',
                       'notes': ('A matching replay key returns the original release receipt.',),
                       'examples': 'floati territory release --root /absolute/fleet --as builder-a --territory territory-lease-00000000000070008000000000000000 --idempotency-key release-1',
                       'option_docs': {'--root': 'Explicit fleet root.', '--as': 'Node that holds the lease.',
                                       '--territory': 'Exact territory lease ID.', '--idempotency-key': 'Stable key for this release.'}},
 'territory show': {'description': 'list territory leases and their state',
                    'body': 'List every territory lease with its paths, expiry and state: live, expired, released, holder_inactive or holder_lease_expired.',
                    'notes': ('Read-only.',),
                    'examples': 'floati territory show --root /absolute/fleet',
                    'option_docs': {'--root': 'Explicit fleet root.'}},
 'territory preview': {'description': 'preview the territories a dispatch would enter',
                       'body': 'List each live lease of another node that the given edit paths enter, and the changes other worktrees of the same repository already hold there, each with the overlap evidence for the two worktrees. Advisory and read-only.',
                       'notes': ('The owner\'s own territories never conflict with its own work.',),
                       'examples': 'floati territory preview --root /absolute/fleet --owner builder-b --edits /absolute/repo/pkg/service.py',
                       'option_docs': {'--root': 'Explicit fleet root.', '--owner': 'Node the work would be dispatched to.',
                                       '--edits': 'Absolute path the work would edit; repeat for each.',
                                       '--base-ref': 'Base each worktree is compared against; origin/HEAD when omitted.'}},
 'backup': {'description': 'export one retained epoch archive',
           'body': 'Copy the exact selected member bytes of one named epoch into DIR.',
           'notes': ('An expired epoch returns no_result from its removal receipt. The command does not change fleet authority.',),
           'examples': 'floati backup /absolute/epoch-copy --root /absolute/fleet --epoch EPOCH_ID',
           'option_docs': {'directory': 'Absent destination directory outside the fleet root.',
                           '--root': 'Explicit fleet root.',
                           '--epoch': 'Epoch identifier from the roll receipt.'}},
 'hold': {'description': 'manage project holds and inbound mail holds',
          'body': 'Record a versioned project hold or hold incoming mail for a node. Doctor and the seat board show active holds.',
          'notes': ('Project holds do not pause wake hooks or change work authority.',),
          'examples': 'floati hold start --help\nfloati hold release --help\nfloati hold inbound --help', 'option_docs': {}},
 'quiet': {'description': "hold enrolled seats' builds and tests for one quiet window",
           'body': 'Settle the host load, then pause the enrolled seats that are building or testing until a touch, a cap or a signal releases them. Each seat gets an exposure receipt with its CPU time inside the window.',
           'notes': ('Floati holds only its own enrolled seats. Work outside them is observed and listed, never held; any agent can read the window with floati quiet status.',),
           'examples': 'floati quiet status --root /absolute/fleet --json\nfloati quiet run --help', 'option_docs': {}},
 'quiet run': {'description': 'settle, hold and release one quiet window',
               'body': 'Wait for the 15-minute load average to reach --load-bound, giving up without holding anything after --settle-cap-seconds. Then pause every enrolled seat whose process tree is building or testing, rescanning each interval, until the release file is touched, --cap-seconds passes or the command gets SIGINT, SIGTERM or SIGHUP. The release resumes only the processes this window paused and writes one exposure receipt per seat, plus one for work outside every seat.',
               'notes': ('CPU is the change in cumulative CPU time over the window. A total that could not be observed whole says it is a lower bound.',
                         'With --trigger memory_pressure the window waits for measured memory pressure to reach the threshold instead of settling the load, and releases itself when the reading falls under --pressure-release-below. An unreadable reading never releases it; the cap still does.',
                         'The operator seat named by --as, exempt seats and the window\'s own processes are never paused. Seats are found only through the root\'s workspace map.'),
               'examples': 'floati quiet run --root /absolute/fleet --as architect-a --idempotency-key window-1 --load-bound 8 --settle-cap-seconds 5400 --cap-seconds 3600 --release-file /absolute/window-done',
               'option_docs': {'--root': 'Explicit fleet root.', '--as': 'Operator node; the declared coordinator when the fleet names one.',
                               '--idempotency-key': 'Stable key for this window.',
                               '--load-bound': 'Highest 15-minute load average at which the window may open.',
                               '--settle-cap-seconds': 'How long to wait for the load to settle before giving up.',
                               '--cap-seconds': 'Longest time the window holds seats.',
                               '--interval-seconds': 'Seconds between load checks and seat rescans.',
                               '--release-file': 'Absolute path; touching it releases the window.',
                               '--exempt': 'Active node never paused by this window. Repeatable.',
                               '--trigger': 'operator settles the load, then holds; memory_pressure holds while measured memory pressure is at or above the threshold.',
                               '--pressure-metric': 'Measured pressure metric: Linux PSI memory avg10 (percent) or the macOS memory pressure level.',
                               '--pressure-threshold': 'Reading at or above which the window holds.',
                               '--pressure-release-below': 'Reading under which the window releases; defaults to the threshold.'}},
 'quiet status': {'description': 'say whether a quiet window holds this fleet now',
                  'body': 'Report the current window, its cap and what it holds. An agent that Floati does not run can read this before heavy work.',
                  'notes': ('The read takes no lock and needs no hook.',),
                  'examples': 'floati quiet status --root /absolute/fleet --json',
                  'option_docs': {'--root': 'Explicit fleet root.', '--json': 'Add the versioned machine contract marker.',
                                  '--command': 'A command line to check: says whether it is covered and whether it waits now.'}},
 'quiet show': {'description': 'list quiet windows or show one with its exposure receipts',
                'body': 'List every recorded window with its state, or show one window with its pauses, resumes and per-seat exposure receipts.',
                'notes': ('A window whose driver died before it released is shown as orphaned.',),
                'examples': 'floati quiet show --root /absolute/fleet --window quiet-window-00000000000070008000000000000000',
                'option_docs': {'--root': 'Explicit fleet root.', '--window': 'Exact window ID.'}},
 'quiet release': {'description': 'release a quiet window whose driver died',
                   'body': 'Resume confirmed holds after checking each process identity. An unconfirmed hold needs the exact resolution command from quiet status.',
                   'notes': ('A live window releases itself; touch its release file instead.',),
                   'examples': 'floati quiet release --root /absolute/fleet --as architect-a --window quiet-window-00000000000070008000000000000000 --idempotency-key release-1',
                   'option_docs': {'--root': 'Explicit fleet root.', '--as': 'Registered active node.',
                                   '--window': 'Exact orphaned window ID.', '--idempotency-key': 'Stable key for this release.',
                                   '--resolve-pid': 'Continue this unconfirmed target. Pass its recorded start token.',
                                   '--start-token': 'Start identity shown for the selected process.'}},
 'hold start': {'description': 'append one versioned project hold',
                'body': 'Append a hold with its reason, start time and optional expected release. Repeating the same key and request replays its receipt.',
                'notes': ('Reason is visible text within 1024 UTF-8 bytes. Historical readers remain tolerant of older shapes.',),
                'examples': 'floati hold start --root /absolute/fleet --as builder-a --reason "waiting for review" --idempotency-key review-1',
                'option_docs': {'--root': 'Explicit fleet root.', '--as': 'Registered active node.',
                                '--reason': 'Visible reason for this hold.', '--expected-release': 'Optional UTC timestamp ending in Z.',
                                '--idempotency-key': 'Stable key for this exact request.'}},
 'hold release': {'description': 'append one terminal project-hold release',
                  'body': 'Release the selected open hold, or the only open hold when selection is omitted. Historical start records remain unchanged.',
                  'notes': ('Multiple open holds require --hold. A matching replay key returns the original terminal receipt.',),
                  'examples': 'floati hold release --root /absolute/fleet --as builder-a --hold hold-00000000000070008000000000000000 --idempotency-key release-1',
                  'option_docs': {'--root': 'Explicit fleet root.', '--as': 'Node that owns the hold.',
                                  '--hold': 'Exact open hold receipt ID.', '--idempotency-key': 'Stable key for this release.'}},
 'hold inbound': {'description': 'start or inspect a node’s inbound mail hold',
                  'body': 'Withhold incoming mail for a node or inspect its holds and decisions.',
                  'notes': ('Approving or rejecting a held item and releasing a hold require a person at a terminal.',),
                  'examples': 'floati hold inbound start --help\nfloati hold inbound show --help', 'option_docs': {}},
 'hold inbound start': {'description': 'hold incoming mail for an active node',
                        'body': 'Record an inbound mail hold. Repeating the same request with the same key returns the original receipt.',
                        'notes': ('The expected release time is advisory. A person releases the hold at a terminal.',),
                        'examples': 'floati hold inbound start --root /absolute/fleet --node builder-a --reason "review inbound mail" --idempotency-key review-1',
                        'option_docs': {'--root': 'Explicit fleet root.', '--node': 'Registered active node.',
                                        '--idempotency-key': 'Stable key for this exact request.',
                                        '--reason': 'Visible reason for holding inbound mail.',
                                        '--expected-release': 'Optional UTC timestamp ending in Z.'}},
 'hold inbound show': {'description': 'inspect a node’s inbound mail holds and decisions',
                       'body': 'List the node’s holds, items awaiting a decision and recorded decisions.',
                       'notes': ('This read does not change holds or present mail.',),
                       'examples': 'floati hold inbound show --root /absolute/fleet --node builder-a',
                       'option_docs': {'--root': 'Explicit fleet root.', '--node': 'Node whose inbound holds to inspect.'}},
 '': {'description': 'inspect and operate an explicit fleet root',
      'body': 'Operate the local receipt-ledger bus and orchestration log. Every durable command requires an '
              'explicit root.',
      'notes': ('Use COMMAND --help for its contract.',),
      'examples': 'floati status --root /absolute/fleet\n'
                  'floati effects --root /absolute/fleet\n'
                  'floati work --help',
      'option_docs': {}},
 'describe': {'description': 'project the live command contract',
              'body': 'Project every registered command and argument from the same argparse objects the CLI '
                      'executes.',
              'notes': ('The projection is read-only and contains no hand-maintained command table.',),
              'examples': 'floati describe --json',
              'option_docs': {'--json': 'Machine-readable schema-versioned artifact.'}},
 'overlap': {'description': 'derive local overlap evidence',
             'body': 'Derive advisory semantic-overlap evidence from one explicit local Git repository.',
             'notes': ('The command is read-only, opens no network connection, and never merges, cancels, or '
                       'acquires a lock.',),
             'examples': 'floati overlap report --help',
             'option_docs': {}},
 'overlap observe': {'description': 'name worktree pairs that changed one path',
                     'body': 'Read every worktree registered with one repository, take what each changed against its merge base with the base ref, committed or not, and list each pair that changed the same path. Nothing needs to be declared. A branch that has landed has no changes left, so its overlap ends.',
                     'notes': ('Advisory and read-only. Coverage ends at the registered worktrees of this repository.',),
                     'examples': 'floati overlap observe --repository /absolute/repo --base-ref origin/main',
                     'option_docs': {'--repository': 'Any worktree of the repository.',
                                     '--base-ref': 'Branch, tag or commit each worktree is compared against.'}},
 'overlap report': {'description': 'emit one local overlap fact',
                    'body': 'Emit the existing schema-v1 overlap report fact for two local refs against one '
                            'explicit base.',
                    'notes': ('MEASURED same-symbol and same-schema signals may inform a consumer; this verb '
                              'remains advisory and read-only.',),
                    'examples': 'floati overlap report --repository /repo/floati --base-ref main --left-ref '
                                'branch-a --right-ref branch-b',
                    'option_docs': {'--repository': 'Exact canonical Git repository root.',
                                    '--base-ref': 'Common comparison base resolved by local Git.',
                                    '--left-ref': 'First local ref resolved by local Git.',
                                    '--right-ref': 'Second local ref resolved by local Git.'}},
 'init': {'description': 'create a direct fleet home',
          'body': 'Create or validate one explicit direct-home fleet root. Solo bootstrap registers one '
                  'harness and a bounded work authority.',
          'notes': ('The four governance flags are given together or not at all.',),
          'examples': 'floati init --root ~/floati-demo --solo\n'
                      'floati init --root ~/my-sessions --solo me --harness Codex',
          'option_docs': {'--root': 'Absolute direct-home path. Namespace roots are refused.',
                          '--solo': 'With NODE, configure one explicit identity; without NODE, open the TTY '
                                    'door.',
                          '--harness': 'Recorded harness role.',
                          '--topology': 'Declared fleet topology.',
                          '--coordinator': "The declared topology's coordinator node.",
                          '--coordinator-authority': 'Coordinator authority granted in writing.',
                          '--owner-tier': 'Owner-tier decisions reserved to the human.'}},
 'setup': {'description': 'set up one seat in a new local fleet',
           'body': 'Preview the files for one selected seat, its local MCP entry, and its skill.',
           'notes': ('Without --executable, setup leaves the harness installation unchecked.',
                     'Use the workspace reported by setup with the selected harness.',
                     'Codex hook opt-in requires native trust and a fresh session after installation.',
                     'Teardown restores a setup whose files still match the installation receipt.'),
           'examples': 'floati setup --root ~/my-fleet --seat claude --session SESSION\n'
                       'floati setup --root ~/my-fleet --seat claude --session SESSION --confirm TOKEN\n'
                       'floati setup --root ~/my-fleet --teardown',
           'option_docs': {'--root': 'Absolute path for the new local fleet.',
                           '--seat': 'Select one harness for the seat.',
                           '--session': 'The exact acting session bound to the local MCP process.',
                           '--executable': 'Check the canonical absolute executable path for the selected harness. '
                                           'Pass the same path when confirming.',
                           '--hook': 'Install hooks for this harness. It must match the selected seat.',
                           '--confirm': 'Apply the exact preview named by this token.',
                           '--teardown': 'Restore the pre-setup files for this fleet.'}},
 'journal': {'description': 'exact-byte journal testimony',
             'body': 'Create and verify bounded exact-byte continuity checkpoints for an explicitly selected '
                     'JSONL journal.',
             'notes': ('Legacy lines remain unchanged and are named as pre-chain testimony.',),
             'examples': 'floati journal verify --help',
             'option_docs': {}},
 'journal checkpoint': {'description': 'write one bounded checkpoint',
                        'body': 'Measure the selected chained journal and durably replace one standalone '
                                'checkpoint file inside its explicit root.',
                        'notes': ('The checkpoint binds sequence, exact-line head digest, and byte length; '
                                  'it is not an authenticity or freshness proof.',),
                        'examples': 'floati journal checkpoint --root ~/fleet --journal '
                                    'receipts/events.jsonl --journal-id events --kind message_envelope '
                                    '--output checkpoints/events.json --json',
                        'option_docs': {'--journal': 'Root-relative journal path.',
                                        '--output': 'Root-relative output path.'}},
 'journal verify': {'description': 'verify one bounded checkpoint',
                    'body': 'Verify exact-byte continuity through one checkpoint and retain the highest '
                            'accepted sequence inside the explicit root.',
                    'notes': ('Pre-chain history is anchored but carries typed absence of seq/prev '
                              'testimony. A checkpoint alone cannot prove authenticity or freshness.',),
                    'examples': 'floati journal verify --root ~/fleet --journal receipts/events.jsonl '
                                '--journal-id events --kind message_envelope --checkpoint '
                                'checkpoints/events.json --json',
                    'option_docs': {'--historical': 'Permits an older valid checkpoint without lowering '
                                                    'retained rollback state.'}},
 'repair': {'description': 'govern explicit ledger repair',
            'body': 'Repair one selected direct-home ledger through an evidence-preserving governed '
                    'operation.',
            'notes': ('Repair replaces the ledger inode and invalidates tail followers, waiters, and '
                      'monitors.',),
            'examples': 'floati repair quarantine --help',
            'option_docs': {}},
 'repair quarantine': {'description': 'quarantine one exact event frame',
                       'body': 'Preserve one selected physical frame beneath the tenant and atomically '
                               'replace the event ledger with retained frames plus a repair receipt.',
                       'notes': (),
                       'examples': 'floati repair quarantine --root ~/fleet --ledger events.jsonl '
                                   '--record-id msg-... --idempotency-key repair-1',
                       'option_docs': {'--root': 'Existing explicit direct-home root.',
                                       '--ledger': 'The only governed repair coordinate.',
                                       '--record-id': 'Stable envelope id selecting exactly one frame.',
                                       '--idempotency-key': 'Stable retry key bound to the exact request.'}},
 'epoch': {'description': 'govern one whole bus epoch',
           'body': 'Rotate the selected event, delivery, and acknowledgment ledgers as one governed epoch.',
           'notes': ('The archived epoch remains byte-exact and live readers must rebuild from the '
                     'replacement epoch.',),
           'examples': 'floati epoch roll --help',
           'option_docs': {}},
 'epoch roll': {'description': 'archive and replace one bus epoch',
                'body': 'Archive the selected ledger family and replace it with one closed roll receipt '
                        'under exact active authority.',
                'notes': ('The archive path, span, digest, file count, and per-plane counts are derived from '
                          'durable bytes.',),
                'examples': 'floati epoch roll --root ~/fleet --as architect-a --idempotency-key '
                            'epoch-roll-1',
                'option_docs': {'--root': 'Existing explicit direct-home root.',
                                '--as': 'Exact active holder of bus-epoch-roll authority.',
                                '--idempotency-key': 'Stable retry key bound to this actor and roll.'}},
 'signature': {'description': 'Minisign artifact testimony',
               'body': 'Create or verify one detached Minisign signature over explicit artifact bytes and a '
                       'signed filename/version binding.',
               'notes': ('Floati never generates a release key and never fetches a public key.',),
               'examples': 'floati signature verify --help',
               'option_docs': {}},
 'signature sign': {'description': 'sign one explicit artifact',
                    'body': 'Ask one operator-declared Minisign binary to create a detached signature with a '
                            'strict signed binding.',
                    'notes': ('Artifact and signature paths stay inside ROOT. The secret key must be an '
                              'existing absolute file outside ROOT.',
                              'Minisign must be an explicit canonical executable; Floati never resolves '
                              'PATH. Omitting it is a named refusal with the declaration remedy.',
                              'Journal id and sequence are all-or-none. This command never generates, '
                              'rotates, copies, or fetches a key.'),
                    'examples': 'floati signature sign --root ~/release --artifact floati.tar.gz --signature '
                                'floati.tar.gz.minisig --secret-key /offline/release.key --version 2.0.0 '
                                '--minisign-executable /usr/local/bin/minisign --json',
                    'option_docs': {}},
 'signature verify': {'description': 'verify one explicit artifact',
                      'body': 'Verify exact artifact bytes and compare the signed trusted-comment binding '
                              'with one operator-declared Minisign binary.',
                      'notes': ('All three files are explicit root-relative paths; no key or signature is '
                                'fetched.',
                                'Minisign must be an explicit canonical executable; Floati never resolves '
                                'PATH. Omitting it is a named refusal with the declaration remedy.',
                                'Journal id and sequence are all-or-none.'),
                      'examples': 'floati signature verify --root ~/release --artifact floati.tar.gz '
                                  '--signature floati.tar.gz.minisig --public-key trust/floati-release.pub '
                                  '--version 2.0.0 --minisign-executable /usr/local/bin/minisign --json',
                      'option_docs': {}},
 'node': {'description': 'node administration',
          'body': 'Preview exact registry rows before committing lifecycle changes and project live node '
                  'context without cached topology.',
          'notes': (),
          'examples': 'floati node add --root ~/fleet --node builder-a --harness Codex --lifetime permanent',
          'option_docs': {}},
 'node add': {'description': 'add one node',
              'body': 'With no flags, open the TTY door using FLOATI_BUS_ROOT. With complete flags, preview '
                      'and atomically commit an active registry row and optional lease. With --plan FILE, '
                      'the same commit from one JSON object.',
              'notes': ('Temporary nodes require --lease-minutes; permanent nodes refuse it.',
                        'Optional Tide policy step for the node; the three are given together or not at '
                        'all.',
                        '--plan is the non-interactive twin of the wizard; it cannot mix with identity or '
                        'tide flags. Optional survey and adopt booleans in the file are the same choices '
                        'the wizard prompts for.'),
              'examples': 'floati node add\n'
                          'floati node add --root ~/fleet --node builder-a --harness Codex --lifetime '
                          'temporary --lease-minutes 60\n'
                          'floati node add --root ~/fleet --plan ~/node.json',
              'option_docs': {'--tide-idempotency-key': 'Keys the Tide policy append; generated when '
                                                        'absent.',
                              '--plan': 'Absolute JSON plan; the same mutation as the flagged or '
                                        'interactive add. Optional survey and adopt keys are booleans.'}},
 'node spawn': {'description': 'create one numbered role instance',
                'body': 'Allocate the first ordinal free in the live registry and provision the derived seat '
                        'before emitting its boot prompt.',
                'notes': (),
                'examples': 'floati node spawn --root ~/fleet --as architect-a --profile sre',
                'option_docs': {'--ordinal': 'Refuses a live collision; automatic allocation is serialized '
                                             'by the registry append. No model call occurs.'}},
 'node retire': {'description': 'retire one node or numbered instance',
                 'body': 'Existing --node retirement retains its workspace; numbered --instance retirement '
                         'drains work and mail, proves Git reachability, and receipts removal.',
                 'notes': (),
                 'examples': 'floati node retire --root ~/fleet --instance sre-2 --as architect-a --drain',
                 'option_docs': {'--instance': 'Requires --as and --drain. Records and receipts outlive a '
                                               'removed numbered workspace.'}},
 'node drain': {'description': "empty one node's inbox without retiring it",
                'body': 'Acknowledge remaining mail for one registered node and leave the node registered. '
                        'The acting session on those acknowledgments is the operator-declared --session.',
                'notes': ('Outstanding work is not a drain refusal; numbered instance retirement still '
                          'requires an empty inbox.',),
                'examples': 'floati node drain --root ~/fleet --node builder-a --session drain-session',
                'option_docs': {'--root': 'Existing explicit fleet root.',
                                '--node': 'Exact active node whose inbox is emptied.',
                                '--session': 'Exact acting harness session; the verb never mints one.'}},
 'node switch': {'description': 'switch provider assignment',
                 'body': 'Preview and atomically commit the replacement registry row and provider receipt.',
                 'notes': ('No model call, fallback, or credential operation occurs.',),
                 'examples': 'floati node switch --root ~/fleet --node builder-a --harness Cursor --model '
                             'gpt-5.6',
                 'option_docs': {}},
 'node person': {'description': 'record explicit person membership',
                 'body': 'Set or clear the operator-recorded person for one active node.',
                 'notes': ('Membership is never inferred from a name, harness, workspace, or Git author.',),
                 'examples': 'floati node person set --root ~/fleet --node builder-a --person chris',
                 'option_docs': {}},
 'node person set': {'description': 'record one node person',
                     'body': 'Append a person receipt for the exact active node. The latest record wins.',
                     'notes': ('This local operator verb uses the same authority as node registration.',),
                     'examples': 'floati node person set --root ~/fleet --node builder-a --person chris',
                     'option_docs': {'--root': 'Existing explicit fleet root.',
                                     '--node': 'Exact active registered node.',
                                     '--person': 'Recorded handle using node identifier syntax.'}},
 'node person clear': {'description': 'clear one node person',
                       'body': 'Append a clear receipt; the node no longer belongs to a person group.',
                       'notes': ('Earlier records remain in the journal as testimony.',),
                       'examples': 'floati node person clear --root ~/fleet --node builder-a',
                       'option_docs': {'--root': 'Existing explicit fleet root.',
                                       '--node': 'Exact active registered node.'}},
 'node role': {'description': 'assign a shipped role',
               'body': 'Answer only declared template questions and preview the exact typed role record.',
               'notes': ('Omit --template and --answer to interview for this exact node on stdin; prompts and the exact preview go to stderr.',
                         'Answers must exactly match the selected template questions. No role or authority is inferred.',),
               'examples': 'floati node role --root ~/fleet --node builder-a --template builder --answer '
                           'repo=floati --answer never_touch=foreign-bus --answer reports_to=architect-a',
               'option_docs': {}},
 'node boot': {'description': 'board and hold the current declared seat',
               'body': 'Without --session, project the ritual using --root, --node, --declared-roots, '
                       '--managed-executable and --profile. With --session, resolve the node and fleet '
                       'from the current workspace SEAT.json, install and '
                       'verify its governed hook, arm consent, board, and perform the native first hold.',
               'notes': ('Cursor, Codex, zcode and Claude are supported; Claude requires an explicit --settings path (--hooks is an alias).',
                         'Codex preserves the exact Stop-definition acknowledgment requirement.'),
               'examples': 'floati node boot --session current-native-session --json',
               'option_docs': {}},
 'node teardown': {'description': 'end one declared seat session',
                   'body': 'Without --session, project the ritual using --root, --node, --declared-roots, '
                           '--managed-executable and --profile. With --session, require --repo and --doc, '
                           'perform the governed banked-work and mail checks, pause and report the '
                           'session, then remove only its marker, consent and workspace mapping.',
                   'notes': ('The node remains registered and shared hook files are preserved.',),
                   'examples': 'floati node teardown --session current-native-session --repo floati '
                               '--doc docs/evidence/session-report.md --json',
                   'option_docs': {}},
 'node explain': {'description': 'explain one live node',
                  'body': 'Explain one node from a fresh boot projection and typed role provenance.',
                  'notes': ('No state-file content, model, network, or foreign root is read.',),
                  'examples': 'floati node explain --root ~/fleet --node builder-a --declared-roots '
                              '~/declared.json --managed-executable /usr/local/bin/floati-fleet --profile '
                              'builder-a --json',
                  'option_docs': {}},
 'node prep-clear': {'description': 'wind one seat down',
                     'body': 'Refuse an incomplete stop, post the checkpoint envelope at the pushed tip, '
                             'release the wake claim, and receipt all three.',
                     'notes': ('The envelope names the pushed tip, never HEAD; an unbanked sha is refused '
                               'before anything is sent.',),
                     'examples': 'floati node prep-clear --root ~/fleet --as builder-a --session session-1 '
                                 '--workspace ~/code/floati --repo floati --doc docs/status/QUEUE.md '
                                 '--note "seat winding down; V7-PC banked"',
                     'option_docs': {'--complement': 'What this stop does not cover; required when the '
                                                     'workspace is dirty or holds unpushed commits, and '
                                                     'recorded verbatim.',
                                     '--to': "Checkpoint recipient; defaults to the fleet's one active "
                                             'architect.',
                                     '--idempotency-key': 'Stable replay key; the envelope is posted once and '
                                                          'the claim released once.',
                                     '--git-executable': 'Explicit Git; otherwise one fixed candidate is used '
                                                         'and PATH is never consulted.'}},
 'node prompts': {'description': 'project per-seat lifecycle command files',
                  'body': 'Generate the seat\'s boot/board/drain/pause/resume/prep-clear prompt '
                          'files from the role template, the harness adapter table and the LC-1 '
                          'parity table; every projection opens with a generator header and '
                          'regeneration is byte-stable.',
                  'notes': ('Writes only under --out; it never installs. Reserved names '
                            '(product verbs, other bus families) are refused.',),
                  'examples': 'floati node prompts --root ~/fleet --as builder-a --harness codex '
                              '--out ~/out',
                  'option_docs': {'--root': 'Existing explicit fleet root.',
                                  '--as': 'Exact active node whose role drives the projections.',
                                  '--harness': 'One of the adapters: codex, claude, zcode.',
                                  '--out': 'Directory the projections are written to.'}},
 'node state-flush': {'description': 'receipt one state flush',
                      'body': 'Observe canonical STATE.md metadata and return a content-free flush receipt.',
                      'notes': ('The file is never parsed or copied; an optional prior mtime must be '
                                'exceeded.',),
                      'examples': 'floati node state-flush --root ~/fleet --node builder-a',
                      'option_docs': {}},
 'role': {'description': 'inspect and author root-local role templates',
 'body': 'Read immutable shipped roles and author custom roles inside one declared root.',
 'notes': ('Subcommands: list, show, new, import, edit, validate.',),
 'examples': '    floati role list --root ~/fleet',
 'option_docs': {}},
 'role list': {'description': 'list available roles',
 'body': 'List shipped and validated custom role names.',
 'notes': (),
 'examples': '    floati role list --root ~/fleet',
 'option_docs': {'--root': 'Existing explicit fleet root.'}},
 'role show': {'description': 'show one available role',
 'body': 'Show one immutable typed template and its digest.',
 'notes': (),
 'examples': '    floati role show --root ~/fleet architect',
 'option_docs': {'role': 'Exact shipped or root-local custom role name.'}},
 'role new': {'description': 'create one root-local role',
 'body': 'Clone one available template into roles/custom under a new unreserved name.',
 'notes': (),
 'examples': '    floati role new --root /absolute/fleet --name specialist --from builder '
             '--idempotency-key new-specialist',
 'option_docs': {'--root': 'Existing explicit fleet root.',
                 '--name': 'New custom role name; shipped and existing names refuse.',
                 '--from': 'Existing source template.',
                 '--idempotency-key': 'Exact request replay key.'}},
 'role import': {'description': 'import one local role file',
 'body': 'Validate one local JSON file and write its declared role into the root-local library.',
 'notes': ('Shipped and existing names refuse; invalid input changes no template.',),
 'examples': '    floati role import --root /absolute/fleet --from /absolute/custom-role.json '
             '--idempotency-key import-specialist',
 'option_docs': {'--root': 'Existing explicit fleet root.',
                 '--from': 'Explicit local regular JSON file; no URL or network source.',
                 '--idempotency-key': 'Exact request replay key.'}},
 'role edit': {'description': 'replace one custom role through validated edits',
 'body': 'Apply declarative field edits or an explicit local replacement file. Validate before '
         'replacing any bytes.',
 'notes': ('No editor or other executable is started. No implicit version increment occurs.',),
 'examples': '    floati role edit --root /absolute/fleet --name specialist --set '
             'cadence=on-demand --idempotency-key edit-specialist',
 'option_docs': {'--root': 'Existing explicit fleet root.',
                 '--name': 'Existing custom role; shipped roles cannot be edited.',
                 '--set': 'Repeatable unique top-level field; lists and objects use strict JSON.',
                 '--from': 'Local JSON replacement with the same role name.',
                 '--idempotency-key': 'Exact request replay key.'}},
 'role validate': {'description': 'validate one local role without writing',
 'body': 'Read one local JSON file through the same template validator used by every role '
         'consumer.',
 'notes': ('A refusal names the invalid field; successful validation writes no file or receipt.',),
 'examples': '    floati role validate --root /absolute/fleet --from /absolute/custom-role.json',
 'option_docs': {'--root': 'Existing explicit fleet root.',
                 '--from': 'Explicit local JSON template.'}},
 'role transfer-architect': {'description': 'move the architect role to one active node',
               'body': 'Write the target architect role record, the vacated node next role record, '
                       'and one transfer receipt under one idempotency key; the composed state is '
                       'checked before anything writes and the entry role field is never touched.',
               'notes': ('A second run under the same key is a receipted no-op naming the first.',),
               'examples': 'floati role transfer-architect --root ~/fleet --to lane-b '
                           '--idempotency-key transfer-1',
               'option_docs': {'--root': 'Existing explicit fleet root.',
                               '--to': 'Exact active node that becomes the architect.',
                               '--idempotency-key': 'Key naming this transfer; replays are no-ops.'}},
 'quota': {'description': 'inspect or collect cited local quota testimony',
           'body': 'Operate only the six ruled local quota adapters.',
           'notes': ('Neither discovers credentials, settings, roots, or network surfaces.',),
           'examples': 'floati quota show --root ~/fleet --provider openai_codex',
           'option_docs': {}},
 'quota collect': {'description': 'collect one local quota receipt',
                   'body': 'Append one citation-bound quota receipt from a ruled local intake.',
                   'notes': ('Claude and Gemini read bounded standard input; Codex requires an explicit '
                             'local app-server executable; honest UNKNOWN cells read no provider surface.',),
                   'examples': 'floati quota collect --root ~/fleet --provider openai_codex --observed-at '
                               '2026-08-29T12:00:00Z --idempotency-key quota-1 --executable '
                               '/opt/homebrew/bin/codex',
                   'option_docs': {}},
 'quota show': {'description': 'inspect one provider quota receipt',
                'body': 'Read the latest durable quota receipt for one ruled provider.',
                'notes': ('This command is read-only and returns typed no_result when no receipt exists.',),
                'examples': 'floati quota show --root ~/fleet --provider cursor_individual',
                'option_docs': {}},
 'context': {'description': 'inspect context evidence and manage Tide signals',
             'body': 'Inspect cited harness evidence, project the read-only turnover ritual, or manage '
                     'optional Tide policy and testimony.',
             'notes': ('Status and turnover are read-only; policy and reading use durable Tide ledgers.',),
             'examples': 'floati context status --root ~/fleet --as builder-a --json',
             'option_docs': {}},
 'context status': {'description': 'report harness evidence',
                    'body': "Report the selected node's citation-bound external context evidence.",
                    'notes': ('This physically read-only projection never invents a measurement.',),
                    'examples': 'floati context status --root ~/fleet --as builder-a --json',
                    'option_docs': {'--root': 'Existing explicit fleet root.',
                                    '--as': 'Exact active node whose recorded harness is projected.',
                                    '--json': 'Emit the compact artifact twin.'}},
 'context turnover': {'description': 'project the turnover ritual',
                      'body': 'Project teardown, metadata-only state flush, and successor boot in order.',
                      'notes': ('The recipe prints command argv and performs no step.',),
                      'examples': 'floati context turnover --root ~/fleet --as builder-a --json',
                      'option_docs': {'--root': 'Existing explicit fleet root.',
                                      '--as': 'Exact active node whose live role provenance is projected.',
                                      '--json': 'Emit the compact artifact twin.'}},
 'context policy': {'description': 'manage Tide context policy',
                    'body': 'Set, inspect, or clear one optional node-bound Tide policy.',
                    'notes': ('Mutations require an explicit idempotency key.',),
                    'examples': 'floati context policy show --root ~/fleet --node builder-a --json',
                    'option_docs': {}},
 'context policy set': {'description': 'set one Tide policy',
                        'body': 'Append one exact node-bound policy to the durable Tide policy ledger.',
                        'notes': (),
                        'examples': 'floati context policy set --root ~/fleet --node builder-a --metric '
                                    'context_fraction --threshold 20% --action recommend --idempotency-key '
                                    'tide-1 --json',
                        'option_docs': {'--root': 'Existing explicit fleet root.',
                                        '--node': 'Exact active node.',
                                        '--metric': 'Ruled Tide metric.',
                                        '--threshold': 'Metric threshold text.',
                                        '--action': 'Ruled action.',
                                        '--idempotency-key': 'Stable replay key.',
                                        '--json': 'Emit the compact artifact twin.'}},
 'context policy show': {'description': 'show one Tide policy',
                         'body': 'Read the active node-bound Tide policy or return its typed absence.',
                         'notes': (),
                         'examples': 'floati context policy show --root ~/fleet --node builder-a --json',
                         'option_docs': {'--root': 'Existing explicit fleet root.',
                                         '--node': 'Exact active node.',
                                         '--json': 'Emit the compact artifact twin.'}},
 'context policy clear': {'description': 'clear one Tide policy',
                          'body': 'Append an exact clearing record for one active node-bound Tide policy.',
                          'notes': (),
                          'examples': 'floati context policy clear --root ~/fleet --node builder-a '
                                      '--idempotency-key tide-clear-1 --json',
                          'option_docs': {'--root': 'Existing explicit fleet root.',
                                          '--node': 'Exact active node.',
                                          '--idempotency-key': 'Stable replay key.',
                                          '--json': 'Emit the compact artifact twin.'}},
 'context reading': {'description': 'record seated context testimony',
                     'body': 'Manage bounded Class-B context testimony from a seated node.',
                     'notes': ('A reading records testimony; it does not create an external measurement.',),
                     'examples': 'floati context reading record --root ~/fleet --as builder-a --metric '
                                 'self_reported_context_fraction --value 42% --command /context '
                                 '--idempotency-key reading-1 --json',
                     'option_docs': {}},
 'context reading record': {'description': 'append context testimony',
                            'body': 'Append one exact seated-node testimony row to the durable Tide '
                                    'testimony ledger.',
                            'notes': (),
                            'examples': 'floati context reading record --root ~/fleet --as builder-a '
                                        '--metric self_reported_context_fraction --value 42% --command '
                                        '/context --idempotency-key reading-1 --json',
                            'option_docs': {'--root': 'Existing explicit fleet root.',
                                            '--as': 'Exact seated node.',
                                            '--metric': 'Ruled Tide metric.',
                                            '--value': 'Testified value text.',
                                            '--command': 'Exact supported slash-command source.',
                                            '--idempotency-key': 'Stable replay key.',
                                            '--json': 'Emit the compact artifact twin.'}},
 'chart': {'description': 'multi-bus Harbor Chart',
           'body': 'Read only explicitly declared roots and validated ledgers; never discover roots. Use the '
                   'live Harbor Map only when explicitly requested. add-root and remove-root rewrite the '
                   'declared-roots file with schema validation and dedup.',
           'notes': ('With no subcommand, --declared-roots FILE is required.',),
           'examples': 'floati chart --declared-roots ~/declared.json --live',
           'option_docs': {'--declared-roots': 'Absolute declarations file.',
                           '--live': 'Run the interactive Harbor Map in a TTY; otherwise render its text '
                                     'twin.',
                           '--json': 'Emit only the compact artifact twin.'}},
 'chart add-root': {'description': 'add one declared root',
                    'body': 'Append one validated root to the declared-roots JSON, refusing duplicate bus '
                            'ids and paths.',
                    'notes': (),
                    'examples': 'floati chart add-root --declared-roots ~/declared.json --bus-id gamma '
                                '--root ~/gamma --architect-node architect-c',
                    'option_docs': {'--declared-roots': 'Absolute declarations file.',
                                    '--bus-id': 'Unique bus identifier.',
                                    '--root': 'Existing absolute non-symlink directory.',
                                    '--architect-node': 'Active architect node on that bus.',
                                    '--downstream': 'Repeatable downstream bus id already declared after '
                                                    'the rewrite.'}},
 'chart remove-root': {'description': 'remove one declared root',
                       'body': 'Remove one bus from the declared-roots JSON. The file must still contain at '
                               'least one root.',
                       'notes': (),
                       'examples': 'floati chart remove-root --declared-roots ~/declared.json --bus-id gamma',
                       'option_docs': {'--declared-roots': 'Absolute declarations file.',
                                       '--bus-id': 'Exact declared bus identifier.'}},
 'chart timings': {'description': 'derived timing percentiles for instrumented verbs',
                   'body': 'Read the timing receipt ledgers under the root and print, per command, the '
                           'measured count, wall-clock percentiles, the slowest receipt, and the refused '
                           'share. Every number is derived from receipts and stamped so; percentiles over '
                           'fewer than twenty receipts print as insufficient rather than a number nobody '
                           'should quote.',
                   'notes': ('A row exists only for invocations whose root and named nodes resolved '
                             '(post-bind); pre-bind refusals write nothing anywhere.',),
                   'examples': 'floati chart timings --root ~/fleet --command inbox',
                   'option_docs': {'--root': 'Existing absolute non-symlink fleet root.',
                                   '--command': 'Only this verb path, as one string ("wake resume").',
                                   '--since': 'Only receipts at or after this UTC RFC3339 timestamp.'}},
 'survey': {'description': 'read-only foreign-bus survey',
            'body': 'Survey one explicit bounded request without writing, draining, acknowledging, '
                    'registering, or locking a foreign bus.',
            'notes': (),
            'examples': 'floati survey --declared-roots ~/declared.json --search-path ~ --json',
            'option_docs': {'--search-path': 'Explicit directory.',
                            '--hooks': 'Explicit hook JSON.',
                            '--targets': 'Explicit target JSON.'}},
 'confluence': {'description': 'the read seam for a consuming observer app',
                'body': 'Grant, sever, list, or exercise one explicit per-root read grant; materialize the '
                        'receipts-read bundle under the grant it was produced under.',
                'notes': ('One grant is one root and one consumer identity. No discovery, no watcher, no '
                          'network, no mutation API.',),
                'examples': 'floati confluence grant --root ~/fleet --consumer puddle --idempotency-key c-1',
                'option_docs': {}},
 'confluence grant': {'description': 'record one explicit read grant',
                      'body': 'Record one receipted per-root read grant for one consumer identity.',
                      'notes': ('The grant names the exact root; never a home scan, never a glob.',),
                      'examples': 'floati confluence grant --root ~/fleet --consumer puddle '
                                  '--idempotency-key c-1',
                      'option_docs': {}},
 'confluence revoke': {'description': 'sever one explicit read grant',
                       'body': 'Sever the active grant for one consumer; reads refuse from the next '
                               'exercise.',
                       'notes': ('Revoking a consumer without an active grant is a typed refusal.',),
                       'examples': 'floati confluence revoke --root ~/fleet --consumer puddle '
                                   '--idempotency-key c-2',
                       'option_docs': {}},
 'confluence adopt': {'description': 'adopt one session into managed mode',
                      'body': "Record one MANAGED-mode session adoption under the consumer's grant and the "
                              "manager's exact active authority lease.",
                      'notes': ('Two trust gates apply and neither substitutes for the other: the read-seam '
                                'grant and the L1 authority binding.',),
                      'examples': 'floati confluence adopt --root ~/fleet --consumer puddle --session sess-1 '
                                  '--manager manager-a --authority-subject subj-1 --authority-epoch 1 '
                                  '--authority-expires-at 2026-08-31T00:00:00.000Z',
                      'option_docs': {}},
 'confluence release': {'description': 'release one adopted session',
                        'body': 'Record one release binding the exact active adoption, manager, and lease '
                                'epoch.',
                        'notes': ('Both trust gates apply here exactly as on adopt.',),
                        'examples': 'floati confluence release --root ~/fleet --consumer puddle --session '
                                    'sess-1 --manager manager-a --authority-epoch 1',
                        'option_docs': {}},
 'confluence status': {'description': 'list recorded read grants',
                       'body': 'List every recorded grant and revocation for one root, in ledger order.',
                       'notes': ('The listing is physically read-only and names each grant it shows.',),
                       'examples': 'floati confluence status --root ~/fleet',
                       'option_docs': {}},
 'confluence bundle': {'description': 'materialize the receipts-read bundle',
                       'body': 'Materialize the complete retained message and receipt history permitted by '
                               'the selected bundle schema as a grant-stamped receipts-read bundle to one '
                               'explicit absolute path.',
                       'notes': ('Reads by an ungranted consumer refuse naming the grant act. The only write '
                                 'is the requested output path.',),
                       'examples': 'floati confluence bundle --root ~/fleet --consumer puddle --out '
                                   '~/bundle.json',
                       'option_docs': {'--schema-version': 'Bundle schema version: 1 (default) or 2 (receipts).'}},
 'hook': {'description': 'install explicit native harness hooks',
          'body': 'Write explicit native harness hooks. Node, root, workspace, and runtime are never '
                  'guessed.',
          'notes': ('Use a subcommand --help for its exact contract.',),
          'examples': 'floati hook install --help',
          'option_docs': {}},
 'hook install': {'description': 'install Cursor, Codex, zcode, or Claude native hooks',
                  'body': 'Cursor writes the seat project .cursor/hooks.json with a Stop timeout of '
                          '1800, an explicit numeric loop_limit and a fast postToolUseFailure observer. '
                          'Cursor also writes one seat marker in that workspace and one user-level stop '
                          'hook. The user-level hook names no seat: it reads the marker for the chat '
                          'workspace roots and writes a typed journal line when the marker is missing, '
                          'unreadable, or ambiguous. '
                          'Codex writes the explicitly named --hooks file with one Stop waiter and '
                          'eleven synchronous host observers through the stable installed launcher. '
                          'Zcode writes the seat project .zcode/hooks.json Stop command with the '
                          'explicit root, node, workspace, and runtime. '
                          'Claude requires an explicit --hooks settings path, preserves foreign JSON bytes, '
                          'and arms the registered Claude workspace through waiter consent. Its Stop command '
                          'uses the shared zcode hold constant. SessionStart and PostToolUse also install '
                          'synchronous context relays; idle wake retains the Stop waiter. The relay '
                          'skips unchanged event metadata and reads at most once per session interval '
                          '(FLOATI_CLAUDE_RELAY_INTERVAL_SECONDS, default 30, integer range 5–3600).',
                  'notes': ('Codex requires all five options below; Claude accepts only --hooks. Cursor and zcode reject them.',
                            'Codex --prepare-only prepares the global Stop hook before any seat exists: omit --as, --workspace and --wait-deadline-seconds. It creates no seat mapping, consent, lifecycle commands or trust state. Seat add prints the exact acknowledged preparation command when needed.',
                            'Claude never discovers settings in the account home. Reinstall is byte-identical; changed settings refuse.',
                            'Codex Stop changes require the exact preimage:target acknowledgment printed '
                            'by the refusal. Installation does not establish observer trust or live adoption: '
                            'the owner reviews Settings → Hooks trust, relaunches every seat, then runs /boardbus.',
                            'No LaunchAgent is edited. Foreign handlers are preserved.',
                            'For Cursor, evidence.scope_notice is: this hook is scoped to '
                            '<workspace>; re-rooting the chat leaves it behind — work other '
                            'directories by absolute path.',
                            'The user-level Cursor hook admits one marker among the payload workspace '
                            'roots and does not guess a seat.',),
                  'examples': 'floati hook install --harness cursor --root /absolute/fleet --as builder-a '
                              '--workspace /absolute/seat --runtime /absolute/floati\n'
                              'floati hook install --harness codex --root /absolute/fleet --as builder-a '
                              '--workspace /absolute/seat --runtime /absolute/floati '
                              '--hooks /absolute/.codex/hooks.json --destination /absolute/.codex/floati-wake '
                              '--hook-timeout-seconds 1800 --wait-deadline-seconds 1500 '
                              '--acknowledge-stop-hook-change PREIMAGE:TARGET\n'
                              'floati hook install --harness zcode --root /absolute/fleet --as builder-a '
                              '--workspace /absolute/seat --runtime /absolute/floati\n'
                              'floati hook install --harness claude --root /absolute/fleet --as builder-a '
                              '--workspace /absolute/seat --runtime /absolute/floati --hooks /absolute/seat-settings.json',
                  'option_docs': {'--harness': 'Choose cursor, codex, zcode, or claude; installation contracts differ.',
                                  '--prepare-only': 'Codex global Stop preparation without seat bindings or observers; requires the exact Stop-definition acknowledgment.',
                                  '--root': 'Absolute fleet root embedded in the hook command.',
                                  '--as': 'Exact node for the workspace binding; embedded in Cursor and zcode commands.',
                                  '--workspace': 'Absolute participating project directory; owns .cursor/hooks.json or .zcode/hooks.json.',
                                  '--runtime': 'Absolute Floati source tree for the Codex bundle, or the Cursor or zcode checkout/install.',
                                  '--hooks': 'Required for Codex and Claude: explicit absolute hooks.json or Claude settings JSON; rejected for Cursor and zcode.',
                                  '--destination': 'Required for Codex only: absolute installed bundle store; rejected for Cursor and zcode.',
                                  '--hook-timeout-seconds': 'Required for Codex only: Stop timeout; rejected for Cursor and zcode.',
                                  '--wait-deadline-seconds': 'Required for Codex only: wait bound below the Stop timeout; rejected for Cursor and zcode.',
                                  '--acknowledge-stop-hook-change': 'Required for Codex only: exact PREIMAGE:TARGET pair; rejected for Cursor and zcode.'}},
 'hook uninstall': {'description': 'remove one cursor seat marker or restore one zcode or Claude Stop hook',
                    'body': 'Cursor removes the seat marker hook install wrote for the workspace. The '
                            'user-level hook file stays, so a later chat with no marker journals and does '
                            'not wake. Zcode puts the project .zcode/hooks.json back to the bytes from '
                            'before zcode hook install. A zcode hook file that did not exist is removed. '
                            'Claude restores the exact saved pre-install bytes of the explicit --hooks settings '
                            'file, or removes a file created by install. Changed settings refuse restoration.',
                    'notes': ('Cursor removes only that workspace marker. Zcode restores the project hook '
                              'file. Codex registrations are not edited.',
                              'A second zcode install does not replace the saved pre-install bytes.'),
                    'examples': 'floati hook uninstall --harness cursor --root /absolute/fleet --as builder-a '
                                '--workspace /absolute/seat --runtime /absolute/floati\n'
                                'floati hook uninstall --harness zcode --root /absolute/fleet --as builder-a '
                                '--workspace /absolute/seat --runtime /absolute/floati\n'
                                'floati hook uninstall --harness claude --root /absolute/fleet --as builder-a '
                                '--workspace /absolute/seat --runtime /absolute/floati --hooks /absolute/seat-settings.json',
                    'option_docs': {'--harness': 'Choose cursor, zcode, or claude.',
                                    '--hooks': 'Required for Claude: the same explicit absolute settings path used to install.',
                                    '--root': 'Absolute fleet root. Zcode reads the install snapshot there. Cursor checks the path.',
                                    '--as': 'Node named by the install.',
                                    '--workspace': 'Absolute project directory. Cursor removes its seat marker there. Zcode restores .zcode/hooks.json.',
                                    '--runtime': 'Absolute Floati checkout or install. Checked for both harnesses.'}},
 'wake': {'description': 'control exact wake coordinates',
          'body': 'Arm one exact acting session, hold one Cursor stop until mail or a deadline, control one '
                  'exact session marker, or manage one consented local wake daemon coordinate.',
          'notes': ('Global and wildcard selectors do not exist. Use a subcommand --help for its exact '
                    'contract.',
                    'The installed Codex Stop hook runs scripts/floati-codex-wait --root ROOT; node, '
                    'workspace, and acting session come only from its validated hook payload.'),
          'examples': 'floati wake arm --root ~/fleet --as builder-a --session codex-session --workspace '
                      '/repo --idempotency-key arm-1',
          'option_docs': {}},
 'wake arm': {'description': 'arm one exact acting session',
              'body': 'Append an initial arm or predecessor-bound takeover for one consented workspace '
                      'binding.',
              'notes': ('The actor must own the exact mapped workspace. A live predecessor claim refuses '
                        'unless --take-over is passed; a paused claim is adoptable without it. Hook '
                        'registration is unchanged.',),
              'examples': 'floati wake arm --root ~/fleet --as builder-a --session codex-session --workspace '
                          '/repo --idempotency-key arm-1',
              'option_docs': {
                  '--take-over': 'Explicitly replace a different live recorded claimant.',
              }},
 'wake wait': {'description': 'hold a Cursor, zcode or Claude stop until mail or a deadline',
               'body': 'Read one native stop payload on stdin and emit the chosen harness response. Root and '
                       'node are explicit; there is no session flag. The deadline must be strictly under '
                       'the hook timeout. Empty inbox is typed silence and is not an exit.',
               'notes': ('Zcode uses its native session_id payload and the actual working directory mapping, '
                         'then delegates to the governed Stop waiter under recorded consent bounds. '
                         'Its response is a block decision for fresh work, or silence when the hold ends.',
                         'Cursor stdout is hook JSON, not a floati artifact. The one exception is a '
                         'configuration-time refusal (bad stdin payload, bad deadline): it prints a '
                         'typed floati artifact on stdout, which Cursor reads as an empty body, and '
                         'the same refusal as one line on stderr so the person installing sees it.',
                         'status=error always re-arms with a drain followup. status=aborted gets one '
                         're-arm whose text carries: if a person stopped this turn, reply exactly: stop. '
                         'A second consecutive abort in the same conversation_id, or abort at the '
                         'loop_limit, prints an empty body. An inbox that cannot be read gets a '
                         'followup naming the node, the root and the typed reason.',
                         'Claude validates the payload cwd and session_id against the explicit node and armed '
                         'hold, then delegates to the native Stop waiter and its block/reason protocol.',),
               'examples': 'floati wake wait --harness cursor --root /absolute/fleet --as builder-a '
                           '--runtime /absolute/floati',
               'option_docs': {'--harness': 'Choose cursor, zcode or claude; each keeps its native hook payload and response.',
                               '--root': 'Absolute fleet root.',
                               '--as': 'Exact node; never inferred from the workspace.',
                               '--runtime': 'Absolute floati checkout or install used in the drain followup.',
                               '--deadline-seconds': 'Cursor wait bound, strictly under the hook timeout; zcode uses recorded consent.',
                               '--hook-timeout-seconds': 'Cursor hook timeout; zcode uses recorded consent.',
                               '--poll-seconds': 'Inbox poll interval.',
                               '--loop-limit': 'The loop_limit the hook was installed with; an abort at '
                                               'or above it is reported on stderr as a dormant seat.'}},
 'wake pause': {'description': 'pause one exact session',
                'body': 'Record a pause receipt and commit one exact session marker.',
                'notes': ('Paused is a recorded state, not absence or deafness.',),
                'examples': 'floati wake pause --root ~/fleet --as builder-a --session 01a0...',
                'option_docs': {'--idempotency-key': 'Carries your replay key; when absent the CLI mints one '
                                                     'and echoes it in the artifact.'}},
 'wake resume': {'description': 'resume one exact session',
                 'body': 'Record the symmetric resume request and remove only the exact pause marker.',
                 'notes': ('Hook registration and every other session remain unchanged.',),
                 'examples': 'floati wake resume --root ~/fleet --as builder-a --session 01a0...',
                 'option_docs': {'--idempotency-key': 'Carries your replay key; when absent the CLI mints '
                                                      'one and echoes it in the artifact.'}},
 'wake status': {'description': 'inspect one exact session',
                 'body': 'Report active or paused, the wake-daemon breaker from the runtime (open, closed, '
                         'or underivable with reason runtime_missing, runtime_symlink, or '
                         'runtime_malformed), and name the running session cache and harness trust gate '
                         'as unknown.',
                 'notes': ('Status is physically read-only and never infers deafness from a pause.',
                           'The breaker is read from the daemon runtime, never from the one-shot notice '
                           'file. Nothing on this verb resets the breaker; only a consent re-grant does.'),
                 'examples': 'floati wake status --root ~/fleet --as builder-a --session 01a0...',
                 'option_docs': {}},
 'wake daemon': {'description': 'manage one local wake daemon',
                 'body': 'Manage one exact node and harness coordinate with explicit consent and '
                         'digest-bound local evidence.',
                 'notes': ('Public subcommands do not accept arbitrary commands, fallback roots, or global '
                           'selectors.',),
                 'examples': 'floati wake daemon status --root ~/fleet --as builder-a --harness cursor',
                 'option_docs': {}},
 'wake daemon consent': {'description': 'record exact activation consent',
                         'body': 'Record bounded polling consent for one exact adapter contract.',
                         'notes': ('Consent records evidence only; it does not install or invoke '
                                   'launchctl.',),
                         'examples': 'floati wake daemon consent --root ~/fleet --as builder-a --harness '
                                     'cursor --min-poll-seconds 1 --max-poll-seconds 30 '
                                     '--max-backoff-seconds 120 --activation-epoch 1',
                         'option_docs': {}},
 'wake daemon bind': {'description': 'bind one exact session',
                      'body': 'Record one exact session, workspace, and executable digest.',
                      'notes': ('Codex bindings are accepted only from the trusted waiter and are refused '
                                'here.',),
                      'examples': 'floati wake daemon bind --root ~/fleet --as builder-a --harness cursor '
                                  '--session SESSION --workspace /repo --executable '
                                  '/opt/homebrew/bin/cursor-agent --binding-epoch 1',
                      'option_docs': {'--yes': 'Consent to the turn-costing resume probe without the '
                                               'interactive ask.',
                                      '--zcode-node-executable': 'Explicit canonical zcode node interpreter.',
                                      '--zcode-entry-executable': 'Explicit canonical zcode entry '
                                                                  'executable.'}},
 'wake daemon install': {'description': 'install the exact LaunchAgent',
                         'body': 'Install deterministic digest-bound plist bytes without starting them.',
                         'notes': ('Installation requires active consent and an exact adapter binding; it '
                                   'does not invoke launchctl.',),
                         'examples': 'floati wake daemon install --root ~/fleet --as builder-a --harness '
                                     'cursor',
                         'option_docs': {}},
 'wake daemon start': {'description': 'start the exact LaunchAgent',
                       'body': 'Bootstrap and kickstart only the deterministic user-domain LaunchAgent.',
                       'notes': ('A missing or changed plist is a typed refusal.',),
                       'examples': 'floati wake daemon start --root ~/fleet --as builder-a --harness cursor',
                       'option_docs': {}},
 'wake daemon status': {'description': 'inspect one daemon coordinate',
                        'body': 'Report inactive before consent and otherwise inspect only the exact '
                                'installed supervisor coordinate.',
                        'notes': ('Inactive status is physically read-only and does not create daemon '
                                  'state.',),
                        'examples': 'floati wake daemon status --root ~/fleet --as builder-a --harness '
                                    'cursor',
                        'option_docs': {}},
 'wake daemon stop': {'description': 'stop the exact LaunchAgent',
                      'body': 'Request bootout and prove process absence for one exact user-domain label.',
                      'notes': ('Unproven process absence is reported as unknown.',),
                      'examples': 'floati wake daemon stop --root ~/fleet --as builder-a --harness cursor',
                      'option_docs': {}},
 'wake daemon remove': {'description': 'remove the exact LaunchAgent',
                        'body': 'Quarantine and remove only matching deterministic plist bytes.',
                        'notes': ('Removal refuses symlinks, inode changes, and digest drift.',),
                        'examples': 'floati wake daemon remove --root ~/fleet --as builder-a --harness '
                                    'cursor',
                        'option_docs': {}},
 'wake daemon revoke': {'description': 'revoke exact daemon consent',
                        'body': 'Stop and remove matching supervisor bytes, then append exact consent '
                                'revocation.',
                        'notes': ('If process absence cannot be proved, final state remains unknown.',),
                        'examples': 'floati wake daemon revoke --root ~/fleet --as builder-a --harness '
                                    'cursor',
                        'option_docs': {}},
 'register': {'description': 'register one named node',
              'body': 'Append one active registration for caller-selected NODE. NODE is not bound to the calling seat. '
                      'Self-registration is an operating convention, not an identity check.',
              'notes': (),
              'examples': 'floati register --root ~/fleet builder-a --harness Codex',
              'option_docs': {'--root': 'Explicit direct-home fleet root.',
                              'node': 'Caller-selected bounded node identifier; use your own seat by convention.',
                              '--harness': 'Harness role recorded on disk.',
                              '--create-workspace': "Also create the node's workspace layout during "
                                                    'registration.'}},
 'retire': {'description': 'retire one named registry row',
            'body': 'Append one retirement row for caller-selected NODE, under the same lock register uses. '
                    'NODE is not bound to the calling seat. Self-retirement is an operating convention, not an identity check. '
                    'The registered role carries forward and the ledger stays '
                    'append-only. A retired node leaves the active roster; its history remains readable.',
            'notes': (),
            'examples': 'floati retire --root ~/fleet builder-a',
            'option_docs': {'--root': 'Explicit direct-home fleet root.',
                            'node': 'Caller-selected node identifier; use your own seat by convention. '
                                    '--as and --actor are unsupported options, not caller-binding checks.'}},
 'ask': {'description': 'DRAFT - ask one node and wait for its correlated reply',
         'body': 'DRAFT - Append one addressed question on this fleet ledger and wait until the linked answer or decline is recorded. A timeout or an unreadable answer is UNKNOWN. The command opens no listener and sends nothing off this machine.',
         'notes': ('DRAFT - Pass --reply-to to record an answer or a decline for an existing question. Answered is printed only with the answer receipt.',
                   'Groups use everyone, repo:NAME, or person:HANDLE and exclude the sender. Preview first, save the JSON artifact, then pass --recipient-preview PATH. Changed recipients or person records refuse before any question is sent.',
                   'A group ask publishes one question per recipient, then waits within one total --wait-seconds budget. Results name each question and its answer, decline, or UNKNOWN.',
                   'Answered results include a cost field attributed to the asking node. Without Puddle measurements for the pair, it says unmeasured.'),
         'examples': 'floati ask --root /absolute/fleet --from red --to blue --repo floati --sha <sha> --doc note.md --note question --session sess-red --idempotency-key question-1 --wait-seconds 30',
         'option_docs': {'--root': 'DRAFT - Explicit fleet root.',
                         '--from': 'DRAFT - Registered node asking the question, or the replying node.',
                         '--to': 'Exact node, everyone, repo:NAME, or person:HANDLE. Replies require an exact node.',
                         '--preview': 'Print the resolved recipient list and its digest without sending or waiting.',
                         '--recipient-preview': 'Saved preview artifact binding this group to the reviewed recipient list.',
                         '--repo': 'DRAFT - Repository label stored on the question or answer.',
                         '--sha': 'DRAFT - Banked commit the question or answer names.',
                         '--repository': 'DRAFT - Explicit checkout for the banked SHA check.',
                         '--doc': 'DRAFT - Repository-relative document the question or answer names.',
                         '--note': 'DRAFT - Question text, or the answer text in reply mode.',
                         '--session': 'DRAFT - Acting session recorded on the new ledger row.',
                         '--idempotency-key': 'DRAFT - Stable replay key for this question or reply.',
                         '--wait-seconds': 'DRAFT - Bounded local wait, a whole number of seconds from 0 through 86400.',
                         '--reply-to': 'DRAFT - Question id this reply is linked to.',
                         '--disposition': 'DRAFT - Reply disposition: answer or decline.',
                         '--reason': 'DRAFT - Decline explanation.'}},
 'replay': {'description': 'read an ask or work receipt chain',
            'body': 'Trace a question or work item through the current and retained fleet ledgers. The result cites the recorded steps and names any missing links.',
            'notes': ('For questions, replay includes timeouts and the recorded answer or decline. The current ask protocol has no presentation receipt.',
                      'For work, replay cites the request, claim, and completion. Presentation and consumption use exact dispatch links.',
                      'The Git section cites the commit and document references supplied by the parties.'),
            'examples': 'floati replay --root /absolute/fleet --question QUESTION_ID\n'
                        'floati replay --root /absolute/fleet --work-item WORK_ID',
            'option_docs': {'--root': 'Explicit fleet root.',
                            '--question': 'Exact question ID returned by ask.',
                            '--work-item': 'Exact item ID returned by work add.'}},
 'send': {'description': 'append a Git notification',
          'body': 'Send a Git-authoritative notification, optionally with one typed delivery claim. Delivery '
                  'and acknowledgment remain separate receipts.',
          'notes': ('All routing and Git fields are required.',
                    'Groups use everyone, repo:NAME, or person:HANDLE and exclude the sender. Repository membership comes from open lanes and claimed car bindings. Person membership comes only from node person set/clear records; changed records invalidate the preview.',
                    'Run with --preview, review and save the JSON artifact, then pass --recipient-preview PATH and --idempotency-key KEY. Changed recipients refuse before sending. Every envelope records the full preview. A partial send returns degraded with the completed receipts.'),
          'examples': 'floati send --root ~/fleet --from a --to b --repo floati --sha <sha> --doc '
                      'docs/evidence/checkpoint.md --note checkpoint --claim ~/claim.json',
          'option_docs': {'--to': 'Exact node, everyone, repo:NAME, or person:HANDLE. Replies require an exact node.',
                          '--preview': 'Print the resolved recipient list and its digest without sending.',
                          '--recipient-preview': 'Saved preview artifact binding this group to the reviewed recipient list.',
                          '--repository': 'Explicit checkout for the banked SHA check; omission uses the caller working directory.',
                          '--claim': 'Strict JSON delivery-claim document; floati binds the new envelope id.',
                          '--reply-to': 'Existing reversed-party message binding.',
                          '--message-class': 'fyi waits for inbox or the next Stop without waking an idle seat; normal preserves ordinary wake behavior.',
                          '--idempotency-key': 'Stable replay key; the effective key is echoed.'}},
 'verify': {'description': 'reproduce a typed delivery claim',
            'body': 'Reproduce one claimed test bank and artifact set in a fresh exact-SHA local worktree, '
                    'then append a typed verification receipt.',
            'notes': ('Floati never fetches; absent or unbanked SHAs refuse with a human remedy.',),
            'examples': 'floati verify --root ~/fleet --as gate-a --claim delivery-claim-<id> --json',
            'option_docs': {'--root': 'Explicit fleet root whose ledger holds the claim.',
                            '--as': 'Registered verifier identity.',
                            '--claim': 'Exact delivery-claim id.',
                            '--json': 'Emit the stable JSON artifact.'}},
 'inbox': {'description': 'drain pending mail',
           'body': 'Present pending notifications and, by default, acknowledge exactly the returned batch in '
                   'the same guarded operation. --peek is the explicit non-acknowledging variant.',
           'notes': (),
           'examples': 'floati inbox --root ~/fleet --as builder-a --session codex-session',
           'option_docs': {'--root': 'Explicit fleet root.',
                           '--as': 'Exact registered recipient.',
                           '--session': 'Exact acting harness session for the default acknowledgment.',
                           '--peek': 'Append delivery evidence without acknowledgment; cannot combine with '
                                     '--session.'}},
 'ack': {'description': 'acknowledge presented messages',
         'body': 'Append one actor-bound acknowledgment receipt for an exact non-empty message batch. '
                 'Acknowledgment does not claim action or completion.',
         'notes': (),
         'examples': 'floati ack --root ~/fleet --as builder-a --session codex-session --id msg-1 --id msg-2',
         'option_docs': {'--root': 'Explicit fleet root.',
                         '--as': 'Exact recipient.',
                         '--session': 'Exact acting harness session.',
                         '--id': 'Previously presented message identifier; repeat for a batch.'}},
 'consume': {'description': 'record action on an acknowledged message',
             'body': 'Append one recipient-bound consumption receipt attesting action on an exact '
                     'previously acknowledged envelope. Delivery and acknowledgment do not imply action.',
             'notes': ('A prior acknowledgment is required; this command never acknowledges automatically.',
                       'The same recipient and envelope replay the original consumption receipt.',),
             'examples': 'floati consume msg-... --root ~/fleet --as builder-a '
                         '--session codex-session --idempotency-key acted-1',
             'option_docs': {'message_id': 'Exact acknowledged envelope identifier.',
                             '--root': 'Explicit fleet root.',
                             '--as': 'Exact recipient.',
                             '--session': 'Exact acting harness session.',
                             '--idempotency-key': 'Explicit key binding this authored act.'}},
 'sent': {'description': 'project sender receipt state',
          'body': 'Read the event, delivery, and acknowledgment ledgers to project each sent message without '
                  'mutating any plane.',
          'notes': ('States distinguish sent, delivered_unacknowledged, and acknowledged; measured ages and '
                    'latencies remain null until their receipts exist.',),
          'examples': 'floati sent --root ~/fleet --as architect-a',
          'option_docs': {'--root': 'Explicit fleet root.', '--as': 'Exact registered sender.'}},
 'grant': {'description': 'append exact work authority',
           'body': 'Grant one exact holder, subject, and epoch coordinate when GRANTOR has an active '
                   'architect role record.',
           'notes': ('Exact active-coordinate replay is idempotent; a higher epoch supersedes the prior '
                     'coordinate.',),
           'examples': 'floati grant --root ~/fleet --as architect-a --holder builder-a --subject '
                       'work-claims --epoch 1',
           'option_docs': {'--root': 'Existing explicit fleet root.',
                           '--as': 'Active node with the architect role template.',
                           '--holder': 'Exact active authority holder.',
                           '--subject': 'Exact authority subject; wildcards do not exist.',
                           '--epoch': 'Exact positive authority epoch.'}},
 'grant revoke': {'description': 'revoke exact work authority',
                  'body': 'Revoke one exact authority coordinate under the same architect-role gate as '
                          'grant.',
                  'notes': ('Exact repeated revocation returns the durable revoking record. For credential '
                            'aliases, revocation blocks new delivery but cannot retract a value already held '
                            'by a running process.',),
                  'examples': 'floati grant revoke --root ~/fleet --as architect-a --holder builder-a '
                              '--subject work-claims --epoch 1',
                  'option_docs': {'--root': 'Existing explicit fleet root.',
                                  '--as': 'Active node with the architect role template.',
                                  '--holder': 'Exact authority holder.',
                                  '--subject': 'Exact authority subject.',
                                  '--epoch': 'Exact current authority epoch.'}},
 'log': {'description': 'read mail or replay orchestration evidence',
         'body': 'Read canonical ledgers. Without --replay, return the complete retained message history. '
                 'Replay renders receipt-derived orchestration events in '
                 'deterministic order.',
         'notes': (),
         'examples': 'floati log --root ~/fleet\n'
                     'floati log --root ~/fleet --replay --speed 4\n'
                     'floati log --root ~/fleet --replay --plain',
         'option_docs': {'--root': 'Explicit fleet root.',
                         '--replay': 'Render claims, turns, degradations, denials, and completions.',
                         '--speed': 'Interactive playback speed from 0.1 through 100.',
                         '--plain': 'Emit one faithful append-only timeline without sleeping.'}},
 'status': {'description': 'summarize the fleet',
            'body': 'Emit one compact whole-fleet snapshot with three separate plane states, work counts, '
                    'and receipt counts. --json selects the documented stable version-zero read contract.',
            'notes': (),
            'examples': 'floati status --root ~/fleet\nfloati status --root ~/fleet --json',
            'option_docs': {'--root': 'Explicit fleet root.',
                            '--destination': 'Installed bundle for the installer-shadow observation; '
                                             'defaults to the FLOATI_INSTALL_DESTINATION environment '
                                             'variable when unset.',
                            '--json': 'Declare machine consumption of the version-zero contract.'}},
 'snapshot': {'description': 'build one consented maintainer support bundle',
              'body': 'Collect one identity-scrubbed diagnostic bundle that an operator can hand to a '
                      'maintainer.',
              'notes': ('Without --yes, a non-interactive terminal refuses instead of blocking.',),
              'examples': 'floati snapshot --root ~/fleet --out ~/floati-support.tar.gz',
              'option_docs': {'--root': 'Exact explicit fleet root.',
                              '--out': 'Operator-selected tarball path outside .floati-snapshots.',
                              '--lines': 'Last lines retained from every derived ledger plane.',
                              '--yes': 'Print the same disclosure and skip only the interactive question.'}},
 'effects': {'description': 'list projected effect status',
             'body': 'Read the deterministic version-one effect projection without creating files or locks.',
             'notes': (),
             'examples': 'floati effects --root ~/fleet\n'
                         'floati effects --root ~/fleet --run run-... --attempt attempt-...',
             'option_docs': {'--root': 'Existing explicit fleet root.',
                             '--run': 'Exact run UUIDv7 identifier.',
                             '--attempt': 'Exact attempt UUIDv7 identifier.'}},
 'effect': {'description': 'inspect or operate one effect',
            'body': 'Use one typed operation identifier for read-only inspection, frozen-adapter '
                    'reconciliation, or fail-closed compensation planning.',
            'notes': ('No raw record, adapter, result, credential, request body, or authority input '
                      'exists.',),
            'examples': 'floati effect show --root ~/fleet --operation effect-op-...',
            'option_docs': {}},
 'effect show': {'description': 'inspect one exact effect',
                 'body': 'Read one exact projected effect operation without creating files or locks.',
                 'notes': (),
                 'examples': 'floati effect show --root ~/fleet --operation effect-op-...',
                 'option_docs': {'--root': 'Existing explicit fleet root.',
                                 '--operation': 'Exact effect operation UUIDv7 identifier.'}},
 'effect reconcile': {'description': 'reconcile one effect',
                      'body': 'Run only the reconciliation adapter frozen in durable effect intent, using '
                              'the canonical repository policy at ROOT/FLOATI.toml.',
                      'notes': ('No adapter or result override exists.',),
                      'examples': 'floati effect reconcile --root ~/fleet --operation effect-op-...',
                      'option_docs': {'--root': 'Existing explicit fleet root.',
                                      '--operation': 'Exact effect operation UUIDv7 identifier.'}},
 'effect compensate': {'description': 'request compensation planning',
                       'body': 'Fail closed because this CLI has no durable compensation action '
                               'specification from which to construct or recover the exact plan. Preview and '
                               'confirmation report typed unavailability without writing.',
                       'notes': ('Exactly one mode is required; neither mode accepts plan fields or '
                                 'authority.',),
                       'examples': 'floati effect compensate --root ~/fleet --operation effect-op-... '
                                   '--preview\n'
                                   'floati effect compensate --root ~/fleet --operation effect-op-... '
                                   '--confirm <digest>',
                       'option_docs': {'--root': 'Existing explicit fleet root.',
                                       '--operation': 'Exact effect operation UUIDv7 identifier.',
                                       '--preview': 'Request a physically read-only preview.',
                                       '--confirm': 'Name one lowercase SHA-256 plan digest.'}},
 'threads': {'description': 'list registered thread observations',
             'body': 'Read registered threads only through a pull-only projection; provider status is not '
                     'task state.',
             'notes': (),
             'examples': 'floati threads --root ~/fleet',
             'option_docs': {'--root': 'Existing explicit fleet root. No provider inventory or mutation is '
                                       'performed.'}},
 'thread': {'description': 'operate one registered thread attachment',
            'body': 'Operate registered threads only through a pull-only source; provider status is not task '
                    'state.',
            'notes': ('No raw provider status, content, prompt, or task-state input exists.',),
            'examples': 'floati thread show --root ~/fleet --attachment thread-attachment-...',
            'option_docs': {}},
 'thread attach': {'description': 'register one explicit provider thread',
                   'body': 'Register one exact Codex task coordinate without sampling provider state; '
                           'registered threads only are observable later.',
                   'notes': ('Supply a work item alone, or the exact run, work item, and attempt. The actor '
                             'must be registered.',),
                   'examples': 'floati thread attach --root ~/fleet --as observer --thread <uuid> '
                               '--work-item work-...',
                   'option_docs': {}},
 'thread observe': {'description': 'pull one registered provider status',
                    'body': 'Perform one bounded pull-only read. Provider status is not task state.',
                    'notes': ('No status, timestamp, flag, title, text, or raw JSON option exists.',),
                    'examples': 'floati thread observe --root ~/fleet --attachment thread-attachment-... '
                                '--codex-executable /opt/homebrew/bin/codex',
                    'option_docs': {'--codex-executable': 'Explicit canonical local Codex executable.'}},
 'thread detach': {'description': 'stop future observations',
                   'body': 'Append one durable detachment for a registered thread; provider state is not '
                           'mutated.',
                   'notes': (),
                   'examples': 'floati thread detach --root ~/fleet --as observer --attachment '
                               'thread-attachment-...',
                   'option_docs': {'--as': 'Must name the exact registered detaching actor.'}},
 'thread show': {'description': 'inspect one exact attachment',
                 'body': 'Read one registered thread attachment without provider access or writes.',
                 'notes': ('The exact attachment view may reveal its registered provider coordinate.',),
                 'examples': 'floati thread show --root ~/fleet --attachment thread-attachment-...',
                 'option_docs': {}},
 'graph': {'description': 'render the Harbor Chart',
           'body': 'Render a deterministic fleet map from durable ledgers only. Human output composes frozen '
                   'topology with counts-only traffic; --json emits frozen topology v0.',
           'notes': (),
           'examples': 'floati graph --root ~/fleet\nfloati graph --root ~/fleet --json',
           'option_docs': {'--root': 'Explicit fleet root.',
                           '--json': 'Emit the frozen version-zero topology artifact.'}},
 'plan': {'description': 'explain read-only plan admission',
          'body': 'Read-only plan admission reports admitted, refused, or needs_operator from hard limits.',
          'notes': (),
          'examples': 'floati plan --plan ~/plan.json --policy ~/FLOATI.toml --explain --json',
          'option_docs': {'--root': 'Explicit fleet root.',
                          '--plan': 'Explicit absolute plan path.',
                          '--policy': 'Explicit absolute FLOATI.toml path.',
                          '--explain': 'Read-only explanation.',
                          '--json': 'Declare machine consumption of the version-zero contract.'}},
 'doctor': {'description': 'diagnose root and bundle integrity',
            'body': 'Run physically read-only typed checks for root, registry, manifest, currency, symlink '
                    'identity, the sole consumption coordinate, sandbox write coordinates, and an explicitly '
                    'supplied local gateway config. Sandbox checks run by default.',
            'notes': ('The installer-shadow check reads PATH as supplied; a PATH that omits the install '
                      'scripts directory yields launcher_not_on_path; an unreadable entry yields path_entry_unreadable. '
                      'Both name the coordinate and remedy; partial scans never claim no-shadowing.',
                      'Remediation is printed only when its currency prerequisites are established.'),
            'examples': 'floati doctor --root ~/fleet --source /repo/floati --ref origin/lane/hm0 '
                        '--gateway-config ~/gateway.json',
            'option_docs': {'--root': 'Explicit fleet direct home.',
                            '--source': 'Explicit Floati source checkout.',
                            '--ref': 'Named deployment currency ref.',
                            '--gateway-config': 'Local stdio gateway v0 config; no discovery.',
                            '--profile': 'Ruled profile (bus-only, orchestration), never inferred; bus-only '
                                         'expects live directories to be absent.',
                            '--no-sandbox': 'Skip the default sandbox write-set checks. There is no enabling '
                                            'flag because checks are on by default.',
                            '--probe': 'Send a loopback envelope to every registered node and verify each '
                                       'drains it within the budget (appends probe mail; per-node '
                                       'PASS/DEAF).',
                            '--probe-budget': 'Probe drain budget in seconds per node.',
                            '--destination': 'Installed bundle for the installer-shadow observation; '
                                             'defaults to the FLOATI_INSTALL_DESTINATION environment '
                                             'variable when unset.',
                            '--codex-hooks': 'Exact Codex hooks.json path for per-hook trust measurement.',
                            '--codex-config': 'Exact Codex config.toml path containing hook trust state.',
                            '--json': 'Preserve the machine artifact in an interactive terminal.'}},
 'watch': {'description': 'poll for fleet deltas',
           'body': 'Stream one artifact for the initial observation and each changed observation.',
           'notes': (),
           'examples': 'floati watch --root ~/fleet --interval 0.25 --iterations 20',
           'option_docs': {'--root': 'Explicit fleet root.',
                           '--destination': 'Installed bundle for the installer-shadow observation; defaults '
                                            'to the FLOATI_INSTALL_DESTINATION environment variable when '
                                            'unset.',
                           '--interval': 'Poll interval from 0.05 through 60.',
                           '--iterations': 'Bounded poll count.'}},
 'wait': {'description': 'hold a turn until a named condition',
          'body': 'Block the calling turn until one named condition is decided, whichever harness ends '
                  'the turn. The only condition today is fresh-work: the turn is held until this '
                  "workspace's node has unread inbox work, or until the armed wait deadline runs out. "
                  'A held turn ends with one JSON decision on standard output and one receipt in the '
                  'fleet root; a workspace with no armed consent is a silent non-participant.',
          'notes': ('The root is exactly the directory --root names. No environment variable can '
                    'redirect a held turn to another fleet.',
                    'Consent is armed per node and carries the deadline; this verb never arms it.'),
          'examples': 'floati wait --for fresh-work --root ~/fleet --workspace ~/code/app --session-id turn-1',
          'option_docs': {'--for': 'Named condition to hold the turn for; fresh-work is the only one today.',
                          '--root': 'Explicit fleet root, read as written.',
                          '--workspace': 'Workspace whose consent binding decides the node; read from the '
                                         'stop payload on standard input when unset.',
                          '--session-id': 'Turn identity the harness supplies; read from the stop payload '
                                          'on standard input when unset.'}},
 'receipts': {'description': 'inspect node receipt history',
              'body': 'Read delivery, acknowledgment, consumption, and denial histories as distinct evidence.',
              'notes': (),
              'examples': 'floati receipts builder-a --root ~/fleet',
              'option_docs': {'node': 'Exact registered node.', '--root': 'Explicit fleet root.',
                              '--since': 'Include receipts at or after this UTC timestamp.'}},
 'supervise': {'description': 'report fleet health',
               'body': 'Run a physically read-only report of liveness, authority, mutex, inbox depth, and '
                       'stale leases.',
               'notes': (),
               'examples': 'floati supervise --root ~/fleet',
               'option_docs': {'--root': 'Explicit fleet root. No action option exists.'}},
 'presence': {'description': 'declare fleet presence or report node liveness',
              'body': "Set the fleet's operator presence, record your node's liveness, or inspect node reports.",
              'notes': ('Expiry means no report since the named time; it never means down.',),
              'examples': 'floati presence show --root ~/fleet',
              'option_docs': {}},
 'presence report': {'description': "record this node's own liveness",
                     'body': 'Append one self-report for the exact active acting node. There is no '
                             'target-node argument.',
                     'notes': (),
                     'examples': 'floati presence report --root ~/fleet --as builder-a --ttl-seconds 120',
                     'option_docs': {'--root': 'Existing explicit fleet root.',
                                     '--as': 'Exact active node reporting about itself.',
                                     '--ttl-seconds': 'Explicit report TTL from 1 through 86400 seconds.'}},
 'presence show': {'description': 'inspect node self-reports',
                   'body': "List every active node's last reported time, TTL, expiry, and honest report "
                           'state without inferring that any node is down.',
                   'notes': (),
                   'examples': 'floati presence show --root ~/fleet',
                   'option_docs': {'--root': 'Existing explicit fleet root. This operation is read-only.'}},
 'presence set': {'description': 'declare the fleet AWAY or PRESENT',
                  'body': 'Record operator presence for the fleet. Seats see it when Floati next presents work.',
                  'notes': ('Use PRESENT when you return. Seats receive a command for their receipts since AWAY.',),
                  'examples': 'floati presence set --root ~/fleet --as architect --mode AWAY --idempotency-key overnight',
                  'option_docs': {'--root': 'Explicit fleet root.',
                                  '--as': 'Registered node acting for the operator.',
                                  '--mode': 'AWAY or PRESENT.',
                                  '--idempotency-key': 'Reuse this key to replay the same declaration.'}},
 'board': {'description': 'open the TUI harbor board',
           'body': 'Render the keyboard-first fleet board. Live acknowledgment is actor-bound. Dumb '
                   'terminals receive a distinguishable plain dump.',
           'notes': (),
           'examples': 'floati board --root ~/fleet --session codex-session\n'
                       'floati board --demo --no-animation',
           'option_docs': {'--root': 'Explicit fleet root.',
                           '--session': 'Exact acting harness session for live acknowledgment.',
                           '--demo': 'Use a throwaway synthetic fleet.',
                           '--no-animation': 'Emit one final frame without terminal control.'}},
 'orchestrate': {'description': 'seed and run a worker fleet',
                 'body': 'Seed a bounded work DAG, launch at least three workers, stream the receipt-derived '
                         'board, and emit one final artifact.',
                 'notes': (),
                 'examples': 'floati orchestrate --root ~/fleet --plan ~/plan.json --adapter codex '
                             '--deadline 120',
                 'option_docs': {'--root': 'Explicit prepared fleet root with registrations and authority.',
                                 '--plan': 'Absolute version-zero orchestration plan path.',
                                 '--adapter': 'Exact governed live-worker adapter.',
                                 '--deadline': 'Overall deadline from 0.1 through 3600.',
                                 '--no-animation': 'Stream distinguishable plain text frames.'}},
 'work': {'description': 'operate the orchestration log',
          'body': 'Operate work items independently from mail.',
          'notes': (),
          'examples': 'floati work show --root ~/fleet',
          'option_docs': {}},
 'work add': {'description': 'append a work item',
              'body': 'Append one open work item with optional dependency edges and Git artifact binding.',
              'notes': ('Artifact options are all-or-none. --owner may be omitted only in a solo root.',
                        'With --as and an artifact, notify every currently eligible node through ordinary mail.',
                        'For a persistent ad-hoc worker, use --adhoc-profile ID instead of --workspace. The profile must retain its exact active lease, owned lane and node.',
                        'Put the returned work ID into the coordinator dispatch before banking and sending it; work and dispatch records are immutable.'),
              'examples': "floati work add --root ~/my-sessions --title 'record this session'\n"
                          "floati work add --root ~/fleet --title 'build board' --owner builder-a --needs "
                          'work-...\n'
                          "floati work add --root /absolute/fleet --title 'native task' --owner builder-a --adhoc-profile adhoc-profile-...",
              'option_docs': {'--as': 'Dispatcher authoring the notification intent before the item.',
                              '--adhoc-profile': 'Bind work to the exact validated profile and its borrowed workspace; cannot be combined with --workspace.',
                              '--edits': 'Absolute path this work would edit; repeat for each. The artifact carries an advisory territory preview; nothing is refused.',
                              '--needs': 'Require one existing work item; repeat for multiple dependencies.',
                              '--workspace': 'Add the ruled absolute workspace under '
                                             '/private~/floati-work/<work-id>/; the worker creates it.'}},
 'work claim': {'description': 'claim with authority',
                'body': 'Append a claim only when the exact authority holder and epoch are active. Solo '
                        'roots may resolve the one configured identity and grant.',
                'notes': ('The acting identity and authority coordinate may be omitted only when solo '
                          'resolution is unambiguous.',),
                'examples': 'floati work claim --root ~/my-sessions --id work-...\n'
                            'floati work claim --root ~/fleet --id work-... --as builder-a '
                            '--authority-subject build --authority-epoch 1',
                'option_docs': {'--now': 'UTC RFC3339 observation for deterministic operation.'}},
 'work complete': {'description': 'append completion',
                   'body': 'Complete only a work item claimed by the exact actor and retain artifact '
                           'bindings. Solo roots may resolve the configured actor.',
                   'notes': ('Artifact options are all-or-none. --as may be omitted only in a solo root. '
                             '--now accepts UTC RFC3339.',
                             'Repeated --consume names 1–1000 unique acknowledged envelopes and requires '
                             '--session. Omitted --consume preserves completion alone.',
                             'Consumption is written before completion. A consumed envelope without '
                             'completion is legal; retry completes without duplicate consumption.',),
                   'examples': 'floati work complete --root ~/my-sessions --id work-...\n'
                               'floati work complete --root ~/fleet --id work-... --as builder-a\n'
                               'floati work complete --root ~/fleet --id work-... --as builder-a '
                               '--session codex-session --consume msg-...',
                   'option_docs': {'--consume': 'Exact acknowledged envelope identifier; repeat for a batch.',
                                   '--session': 'Exact acting harness session; required with --consume.'}},
 'work show': {'description': 'project work state',
               'body': 'Project work state, eligibility and notification journal rows without sending mail.',
               'notes': (),
               'examples': 'floati work show --root ~/fleet',
               'option_docs': {'--id': 'Exact work item identifier.'}},
 'work notify': {'description': 'replay pending work notifications',
                 'body': 'Finish pending journal notifications authored by the acting node.',
                 'notes': ('Only the recorded sender authors mail. Other senders receive a pending finding.',),
                 'examples': 'floati work notify --root ~/fleet --id work-... --as dispatcher\n'
                             'floati work notify --root ~/fleet --all --as dispatcher',
                 'option_docs': {'--id': 'Exact work item identifier.',
                                 '--all': 'Every pending item whose recorded sender is the acting node.',
                                 '--as': 'Recorded notification sender.'}},
 'work handoff': {'description': 'record or inspect work handoffs',
                  'body': 'Attach evidence and remaining work to a work claim.',
                  'notes': (),
                  'examples': '',
                  'option_docs': {}},
 'work handoff record': {'description': 'capture a work handoff',
                         'body': 'Record local command results and artifact digests at the checked-out commit.',
                         'notes': ('Commands run locally in the named checkout with standard input closed.',
                                   'The handoff stores arguments and results. Keep secrets out of arguments.',
                                   'A zero count means zero tests ran.',
                                   'Repeating an exact recorded request with the same key returns the saved handoff.'),
                         'examples': 'floati work handoff record --root /absolute/fleet --id work-... '
                                     '--as builder-a --repository /absolute/checkout --unittest tests.test_example '
                                     '--artifact result.json --idempotency-key review',
                         'option_docs': {'--run': 'Run one JSON argument array with an absolute executable path. Repeat to run more commands.',
                                         '--unittest': 'Run these unittest selectors together and record the observed test count. Repeat to select more tests.',
                                         '--artifact': 'Record a regular file relative to the checkout. Repeat for each file.',
                                         '--remaining': 'Record one claimed item of unfinished work. Repeat for each item.',
                                         '--statement': 'Record an author statement as claimed evidence. Repeat for each statement.',
                                         '--supersedes': 'Require this handoff to still be the latest before recording its replacement.'}},
 'work handoff show': {'description': 'revalidate a work handoff',
                       'body': 'Read a saved handoff and check its checkout and artifacts for changes.',
                       'notes': ('The latest handoff is shown by default.',),
                       'examples': 'floati work handoff show --root /absolute/fleet --id work-... --json',
                       'option_docs': {'--handoff-id': 'Read a specific handoff for the work item.'}},
 'intake': {'description': 'adopt bounded work-queue sources',
            'body': 'Inspect and adopt explicitly selected intake sources through immutable snapshots and '
                    'the existing work queue.',
            'notes': (),
            'examples': 'floati intake scan --root ~/fleet --from ~/tasks',
            'option_docs': {}},
 'intake scan': {'description': 'inspect local Markdown intake',
                 'body': 'Read every direct *.md entry in an explicit directory and report eligible or '
                         'typed-refused verdicts without writing anything.',
                 'notes': ('Symlinks, malformed titles, invalid UTF-8, and files over 256 KiB are refused '
                           'with stable codes.',),
                 'examples': 'floati intake scan --root ~/fleet --from ~/tasks',
                 'option_docs': {'--root': 'Existing explicit fleet root; it is required for command binding '
                                           'and is not modified.',
                                 '--from': 'Explicit local queue directory; scanning is non-recursive.'}},
 'intake show': {'description': 'inspect immutable snapshots',
                 'body': 'Read local intake snapshot records and their payloads, refusing when a payload is '
                         'missing, altered, or a visible orphan work item has no snapshot row.',
                 'notes': (),
                 'examples': 'floati intake show --root ~/fleet',
                 'option_docs': {'--root': 'Existing explicit fleet root.',
                                 '--id': 'Exact immutable snapshot identity.'}},
 'intake adopt': {'description': 'adopt one explicit intake source',
                  'body': 'Write one immutable local Markdown or one-shot GitHub issue snapshot and its work '
                          'item. This durable verb is not reachable from MCP; only the explicit GitHub shape '
                          'starts a network-capable subprocess.',
                  'notes': ('For --source local, supply --from and --path to read one safe Markdown path relative to an explicit queue.',
                            'For --source github, supply --repo, --issue and --gh; the executable is explicit and absolute, and the issue is named.',
                            'Ambient GH_TOKEN or GITHUB_TOKEN may reach only the fixed subprocess '
                            'environment and is never recorded.',
                            GH_AUTHENTICATION_REMEDY),
                  'examples': 'floati intake adopt --root ~/fleet --source local --from ~/tasks --path '
                              'good.md --owner builder-a\n'
                              'floati intake adopt --root ~/fleet --source github --repo owner/repo --issue '
                              '123 --gh /opt/homebrew/bin/gh --owner builder-a',
                  'option_docs': {'--root': 'Existing explicit fleet root.',
                                  '--owner': 'Active work owner; solo roots may omit it.',
                                  '--now': 'Aware UTC RFC3339 retrieval time.'}},
 'intake preview': {'description': 'preview one GitHub mutation',
                    'body': 'Return the exact target, GitHub REST body, digest, risk, and approval '
                            'requirement without writing or dispatching. This read-only command is '
                            'MCP-exposed.',
                    'notes': ('For --operation comment, supply --body or --body-file. For label_add, supply --label for each selected label; label_remove requires exactly one --label. For close, supply --reason. For pr_link, supply --pr.',
                              'label_add is sorted and deduplicated; label_remove accepts exactly one label '
                              'because GitHub removal is not atomic across a set. pr_link uses the fixed '
                              "comment body 'Linked pull request: #N'."),
                    'examples': 'floati intake preview --root ~/fleet --snapshot intake-snapshot-... '
                                "--operation comment --body 'Acknowledged.'",
                    'option_docs': {}},
 'intake dispatch': {'description': 'bind a preview to one effect intent',
                     'body': 'Recompute the preview and append through the existing sealed effect controller '
                             'only when the carried-back digest and live run context match. This outbound '
                             'command is not reachable from MCP.',
                     'notes': ('REQUEST uses the exact operation-specific flags documented by intake '
                               'preview.',
                               'Existing durable run and attempt context.',
                               'Existing effect approval evidence when policy requires it.'),
                     'examples': 'floati intake dispatch --root ~/fleet --snapshot intake-snapshot-... '
                                 "--operation comment --body 'Acknowledged.' --confirm-digest <digest> "
                                 '--run-id run-... --item-id work-... --attempt-id attempt-... --fence-token '
                                 '<token>',
                     'option_docs': {'--body-file': 'Operation body read from a file; --body TEXT is the '
                                                    'inline alternative.',
                                     '--label': 'Label for label_add (repeatable, sorted, deduplicated) or '
                                                'label_remove (exactly one).',
                                     '--reason': 'Close reason.',
                                     '--pr': 'Pull request number for pr_link.',
                                     '--confirm-digest': 'Exact recomputed preview digest.'}},
 'worker': {'description': 'run one authority-checked worker',
            'body': 'Claim one owned open work item and record every worker transition as durable evidence.',
            'notes': ('Use worker run for the exact command contract.',),
            'examples': 'floati worker run --root ~/fleet --as builder-a --adapter claude',
            'option_docs': {}},
 'worker run': {'description': 'run one authority-checked worker',
                'body': 'Claim one owned open work item and record typed degradation without silent '
                        'fallback.',
                'notes': (),
                'examples': 'floati worker run --root ~/fleet --as builder-a --adapter claude '
                            '--claude-executable /opt/homebrew/bin/claude',
                'option_docs': {'--root': 'Explicit fleet root.',
                                '--as': 'Exact registered worker node.',
                                '--adapter': 'Exact governed adapter.',
                                '--claude-executable': 'Explicit canonical Claude executable.',
                                '--codex-executable': 'Explicit canonical Codex executable.',
                                '--pi-executable': 'Explicit canonical pi executable.'}},
 'worker adhoc': {'description': 'operate one governed persistent native worker',
                  'body': 'Create a leased native worker profile, execute an authenticated dispatch, inspect its durable state, or tear down its owned row.',
                  'notes': ('Use profile, run, show, or teardown for the exact command contract.',),
                  'examples': 'floati worker adhoc profile --help\nfloati worker adhoc run --help',
                  'option_docs': {}},
 'worker adhoc profile': {'description': 'preview one leased profile and an explicit human launch grant',
                          'body': 'Derive an exact native profile and preview its harness, workspace, lease and launch capability.',
                          'notes': ('Workspace, provider storage, and executable coordinates come from governed records.',
                                    'Without --grant-launch, this command creates no policy or approval. The explicit human flag records the previewed launch capability through existing approval owners.',
                                    'A missing root policy is created only by that human grant. Run consumes existing authority and never creates approvals.'),
                          'examples': 'floati worker adhoc profile --root /absolute/fleet --as builder-a --adapter codex --lane-opening lane-workspace-... --repository project --to coordinator --idempotency-key profile-1',
                          'option_docs': {'--root': 'Explicit absolute fleet root.',
                                          '--as': 'Exact active temporary node.',
                                          '--adapter': 'Native adapter: codex, claude, or pi.',
                                          '--lane-opening': 'Exact owned lane-opening receipt ID.',
                                          '--repository': 'Repository alias recorded for that lane.',
                                          '--to': 'Declared result recipient.',
                                          '--grant-launch': 'Explicit human approval of the exact previewed native launch capability.',
                                          '--authority-subject': 'Existing approval authority subject; required with --grant-launch.',
                                          '--authority-epoch': 'Exact existing approval authority epoch; required with --grant-launch.',
                                          '--idempotency-key': 'Stable key for this exact profile request.'}},
 'worker adhoc run': {'description': 'execute one authenticated native dispatch',
                      'body': 'Run or recover one exact coordinator dispatch using the selected profile and acting session.',
                      'notes': ('Operation, permission, duration, work item, and result document come from the authenticated dispatch JSON.',
                                'Resume uses the saved provider coordinate. Repeating an admitted key recovers its durable phases.',
                                'Create work with work add --adhoc-profile ID, then use its returned ID in the dispatch. An exact existing CAR binding is also accepted.',
                                'Authentication refusals distinguish work ownership/binding from authority and name a remedy. Replacement work or a changed authority epoch requires a newly banked coordinator dispatch; expired or revoked epochs cannot be revived.',
                                'Authority remedies must be run by the named active architect. Run never creates a grant or launch approval.'),
                      'examples': 'floati worker adhoc run --root /absolute/fleet --as builder-a --session parent-session --profile adhoc-profile-... --envelope msg-... --idempotency-key dispatch-1',
                      'option_docs': {'--root': 'Explicit absolute fleet root.',
                                      '--as': 'Exact node addressed by the dispatch.',
                                      '--session': 'Exact acting parent session.',
                                      '--profile': 'Recorded ad-hoc profile ID.',
                                      '--envelope': 'Exact authenticated coordinator envelope ID.',
                                      '--idempotency-key': 'Stable key for this exact dispatch.'}},
 'worker adhoc show': {'description': 'inspect one durable native dispatch',
                       'body': 'Read one dispatch admission and its persisted lifecycle, provider coordinate, cleanup, and retained history metadata.',
                       'notes': ('Provider identity does not establish current provider status. This command does not poll the provider.',),
                       'examples': 'floati worker adhoc show --root /absolute/fleet --dispatch adhoc-dispatch-...',
                       'option_docs': {'--root': 'Explicit absolute fleet root.',
                                       '--dispatch': 'Exact admitted dispatch ID.'}},
 'worker adhoc teardown': {'description': 'close one proved complete native worker row',
                           'body': 'Close the exact owned row after work, mail, publication, and physical cleanup are proved complete, retaining private provider history.',
                           'notes': ('Dirty, unbanked, replaced, or process-uncertain rows remain retained. Repeating the key replays its teardown receipt.',),
                           'examples': 'floati worker adhoc teardown --root /absolute/fleet --as builder-a --session parent-session --dispatch adhoc-dispatch-... --idempotency-key teardown-1',
                           'option_docs': {'--root': 'Explicit absolute fleet root.',
                                           '--as': 'Exact admitted node.',
                                           '--session': 'Exact acting parent session.',
                                           '--dispatch': 'Exact completed dispatch ID.',
                                           '--idempotency-key': 'Stable key for this teardown request.'}},
 'mcp': {'description': 'expose launch-bound agent tools',
         'body': 'Serve only the generated Floati tool surface over local standard input and output.',
         'notes': ('No network transport or identity override exists.',),
         'examples': 'floati mcp serve --root ~/fleet --as builder-a --session codex-session',
         'option_docs': {}},
 'mcp serve': {'description': 'serve one launch-bound MCP session',
               'body': 'Read newline-delimited MCP requests from standard input and write only MCP responses '
                       'to standard output.',
               'notes': ('The transport opens no listener and emits no non-protocol standard output.',),
               'examples': 'floati mcp serve --root ~/fleet --as builder-a --session codex-session',
               'option_docs': {'--root': 'Existing explicit fleet root.',
                               '--as': 'Exact registered node bound for the process lifetime.',
                               '--session': 'Exact acting harness session bound for the process lifetime.'}},
 'sequencer': {'description': 'manage the optional local writer',
               'body': 'Observe or explicitly transition the host-local run sequencer. No raw record '
                       'operation exists.',
               'notes': (),
               'examples': 'floati sequencer status --root ~/fleet',
               'option_docs': {}},
 'sequencer status': {'description': 'observe local writer mode',
                      'body': 'Read durable epoch evidence and host-local lock/socket testimony without '
                              'creating files.',
                      'notes': (),
                      'examples': 'floati sequencer status --root ~/fleet',
                      'option_docs': {'--root': 'Explicit direct-home fleet root.'}},
 'sequencer serve': {'description': 'run the local managed writer',
                     'body': 'Hold one exclusive owner lease for the service lifetime and release it on '
                             'graceful shutdown.',
                     'notes': (),
                     'examples': 'floati sequencer serve --root ~/fleet --as local-writer',
                     'option_docs': {'--root': 'Explicit direct-home fleet root.',
                                     '--as': 'Bounded sequencer identity.',
                                     '--takeover': 'Only when open durable evidence remains after owner '
                                                   'absence.'}},
 'sequencer direct': {'description': 'restore daemonless writer mode',
                      'body': 'Prove exclusive host-local owner absence and atomically close abandoned '
                              'managed evidence before returning.',
                      'notes': (),
                      'examples': 'floati sequencer direct --root ~/fleet --as operator-a',
                      'option_docs': {'--root': 'Explicit direct-home fleet root.',
                                      '--as': 'Bounded operator transition identity.'}},
 'install': {'description': 'install the exact governed bundle',
             'body': 'Install only the exact files named by the source bundle manifest after a currency '
                     'check in the writer.',
             'notes': ('Foreign files are preserved; the writer never-prune-foreign.',),
             'examples': 'floati install --source /repo/floati --destination /opt/floati --ref origin/main\n'
                         'floati install --source /repo/floati --destination ~/floati --committed-tree',
             'option_docs': {'--source': 'Absolute committed source checkout.',
                             '--destination': 'Absolute install destination.',
                             '--ref': 'Named Git ref.',
                             '--committed-tree': 'Explicit committed-tree CI mode.',
                             '--json': 'Emits the same safe lifecycle artifact without terminal rendering.'}},
 'update': {'description': 'update the exact governed bundle',
            'body': 'Update one exact governed bundle, roll back the last applied update through the same '
                    'apply path, or plan/apply one explicit fleet-wide update against caller-named '
                    'external bindings.',
            'notes': ('rollback --to SHA reverts the last applied update to that previous source SHA; it '
                      'refuses when no prior apply exists.',
                      'Foreign files are preserved; the writer never-prune-foreign.',
                      'For a governed Codex transport, pair --profile-registry with --fleet-profile to validate operations and repin the exact installed source.',),
            'examples': 'floati update --source /repo/floati --destination /opt/floati --ref origin/main\n'
                        'floati update fleet preview --help',
            'option_docs': {'--source': 'Absolute committed source checkout for legacy single-install '
                                        'update.',
                            '--destination': 'Absolute install destination.',
                            '--ref': 'Named Git ref.',
                            '--committed-tree': 'Explicit committed-tree CI mode.',
                            '--profile-registry': 'Canonical Codex fleet profile registry for this ordinary update.',
                            '--fleet-profile': 'Declared profile whose transport destination is being updated.',
                            '--json': 'Emits the same safe lifecycle artifact without terminal rendering.'}},
 'update prepare': {'description': 'prepare one root for sends after an update',
                    'body': 'Run the updated launcher with the root you use for sends. Each invocation validates another bounded section of its event history. Repeat until the state is done.',
                    'notes': ('An interrupted step resumes from the last validated section. A send made before preparation is done still goes out; it validates unprepared history itself.',),
                    'examples': 'floati update prepare --root ~/fleet\nfloati update prepare --root ~/fleet --status',
                    'option_docs': {'--root': 'Absolute fleet root.', '--status': 'Report the current preparation state.'}},
 'update fleet': {'description': 'plan or apply one explicit fleet-wide update',
                  'body': 'Operate only the fleet root, installation, waiter inventory, registry, and '
                          'transport named by the caller; no ambient discovery occurs.',
                  'notes': ('Preview is physically read-only. Apply requires the exact preview digest and an '
                            'idempotency key.',),
                  'examples': 'floati update fleet preview --help\nfloati update fleet apply --help',
                  'option_docs': {}},
 'update fleet preview': {'description': 'derive one immutable fleet update plan',
                          'body': 'Re-open and digest every explicitly named installation, waiter, '
                                  'configuration, inventory, and registry byte without locking or writing.',
                          'notes': ('No automatic, discovery, or mutation option exists.',),
                          'examples': 'floati update fleet preview --root ~/fleet --as builder-a '
                                      '--destination /opt/floati --channel '
                                      'https://updates.example/release-index.v0 --version 2.0.0 '
                                      '--waiter-binding ~/BINDING.json --transport-registry ~/profiles.json '
                                      '--transport floati-installed-v0 --json',
                          'option_docs': {'--root': 'Exact fleet direct home.',
                                          '--as': 'Exact acting node.',
                                          '--destination': 'Exact shared installation.',
                                          '--channel': 'Exact signed HTTPS release channel.',
                                          '--version': 'Exact release version.',
                                          '--waiter-binding': 'Strict caller-named waiter inventory.',
                                          '--transport-registry': 'Exact host transport registry.',
                                          '--transport': 'Exact selected transport.',
                                          '--json': 'Emit the compact artifact contract.'}},
 'update fleet apply': {'description': 'apply one consented immutable fleet update plan',
                        'body': 'Re-plan every named byte, require an exact digest match and active AU-1 '
                                'consent, then enter the resumable receipt saga.',
                        'notes': ('No automatic, discovery, raw-pin, or implicit-consent option exists.',),
                        'examples': 'floati update fleet apply --root ~/fleet --as builder-a --destination '
                                    '/opt/floati --channel https://updates.example/release-index.v0 '
                                    '--version 2.0.0 --waiter-binding ~/BINDING.json --transport-registry '
                                    '~/profiles.json --transport floati-installed-v0 --plan-digest <digest> '
                                    '--idempotency-key fu1-001 --json',
                        'option_docs': {'--root': 'Exact fleet direct home.',
                                        '--as': 'Exact acting node.',
                                        '--destination': 'Exact shared installation.',
                                        '--channel': 'Exact signed HTTPS release channel.',
                                        '--version': 'Exact release version.',
                                        '--waiter-binding': 'Strict caller-named waiter inventory.',
                                        '--transport-registry': 'Exact host transport registry.',
                                        '--transport': 'Exact selected transport.',
                                        '--plan-digest': 'Exact digest emitted by preview.',
                                        '--idempotency-key': 'Exact resumable operation key.',
                                        '--json': 'Emit the compact artifact contract.'}},
 'uninstall': {'description': 'remove exact owned tool bytes',
               'body': 'Remove only unchanged manifest-owned tool files while retaining bus roots, ledgers, '
                       'and foreign files.',
               'notes': (),
               'examples': 'floati uninstall --destination /opt/floati --dry-run',
               'option_docs': {'--destination': 'Existing absolute installed bundle.',
                               '--dry-run': 'Validate and preview without removing files.',
                               '--json': 'Emits the same safe lifecycle artifact without terminal '
                                         'rendering.'}},
 'purge': {'description': 'move the exact roots you list into the account Trash; never deletes',
           'body': 'Move the exact roots you list into the account Trash; never deletes.',
           'notes': (),
           'examples': 'floati purge --root ~/fleet --dry-run',
           'option_docs': {'--root': 'one absolute preserved root to move; repeat for more',
                           '--preserved-root': 'alias of --root',
                           '--dry-run': 'list every file that would move; move nothing'}}}


HELP_COPY.update({
    'seat': {
        'description': 'create, remove, and board explicit seats',
        'body': 'Prepare an owned Codex seat home, retire its recorded capabilities, or board one declared workspace.',
        'notes': ('Boarding is explicit; it never discovers a session or installs a hook.',),
        'examples': 'floati seat board --help',
        'option_docs': {},
    },
    'seat add': {
        'description': 'prepare one owned Codex seat home',
        'body': 'Clone the explicit source into HOME/repo, create wt and scratch directories and SEAT.md, '
                'register the node, map its waiter workspace, and add its gateway profile and Codex project trust entry. '
                'The artifact gives the exact directory and boot line for a desktop session; CLI mode creates a saved, explicitly paused thread and collects its starter.',
        'notes': (
            'Omitting --node suggests an unused node name; omitting --profile derives it from the fleet and node.',
            'The gateway and Codex configurations must already exist. Ownership backups preserve their exact baseline bytes and modes.',
            'TOML validation requires Python 3.11 or newer, or optional tomli on Python 3.9/3.10; absence produces a typed refusal.',
            'A compiled package requires its declared warm-build script. Hearing and a live waiter remain unproven until the new session boards.',
            'CLI startup begins no model turn, materializes native storage through a creator read, then proves the saved ID can resume through a fresh provider without hydrating history. It prints the exact resume command and boot prompt. No starter child remains running.',
            'CLI mode requires the declared config.toml filename and binds CODEX_HOME to its directory for startup and the printed resume command.',
            '--rehome requires explicit --node and --profile for an existing Codex node. It retires stale waiter capabilities and visibly acknowledges prior-life mail as historical.',
            'Rehome replaces a matching profile and its old trust-only table; later removal retires both old and new capabilities. The previous home remains outside the new home\'s ownership.',
            'Repeating the same completed request returns its recorded result; use seat remove to recover a failed owned transaction.',
        ),
        'examples': 'floati seat add --root /absolute/fleet --home /absolute/seats/worker --source /absolute/source '
                    '--gateway-config /absolute/config/profiles.json --codex-config /absolute/config/config.toml '
                    '--transport installed --coordinator architect --repo product',
        'option_docs': {
            '--root': 'Exact existing governed fleet root.',
            '--home': 'New absolute seat home; its parent must exist.',
            '--source': 'Explicit source Git repository; the clone uses its recorded HEAD.',
            '--gateway-config': 'Exact existing schema-2 gateway profile registry to edit.',
            '--codex-config': 'Exact existing Codex TOML configuration to add project trust to.',
            '--codex-hooks': 'Exact existing Codex hooks document for the waiter prerequisite; defaults to hooks.json beside --codex-config.',
            '--transport': 'Existing manifest-pinned transport; its ordinary interpreter, manifest pins and bounded client files are checked before ownership writes and on replay.',
            '--coordinator': 'Coordinator declared by the fleet governance.',
            '--repo': 'Repository label used by the seat\'s send profile.',
            '--mode': 'desktop prints the opening directory and boot line; cli creates a paused, resumable thread.',
            '--node': 'Explicit node name; omitted for an automatically suggested unused name.',
            '--profile': 'Explicit gateway profile name; omitted to derive it from the fleet and node.',
            '--codex-executable': 'Exact absolute ordinary Codex executable, required for CLI mode.',
            '--warm-build-script': 'Executable script relative to the cloned repository, required for a Swift package.',
            '--rehome': 'Explicitly reuse the named Codex node and replace its matching profile; requires --node and --profile.',
        },
    },
    'seat remove': {
        'description': 'retire one unchanged owned seat home',
        'body': 'Read the saved ownership transaction for this home, reverse its owned gateway and project-trust entries, '
                'remove its owned CLI pause marker, retire its waiter and registration, and remove the unchanged owned home. Registry, consent, and transaction history remain.',
        'notes': (
            'Changed or replaced home files and changed owned configuration entries refuse before removal; foreign entries are preserved.',
            'TOML validation requires Python 3.11 or newer, or optional tomli on Python 3.9/3.10.',
            'A fresh seat restores unchanged configuration baselines exactly. A rehomed seat leaves its previous profile and trust retired.',
            'Completed replay returns its recorded removal receipt. Interrupted or failed transactions resume from their recorded steps.',
        ),
        'examples': 'floati seat remove --root /absolute/fleet --home /absolute/seats/worker',
        'option_docs': {
            '--root': 'Exact fleet root holding the seat ownership transaction.',
            '--home': 'Exact owned home recorded by seat add.',
        },
    },
    'seat board': {
        'description': 'arm, resume, and drain one declared session',
        'body': 'Board one explicit session through receipted claim takeover, waiter resume, and exact-batch acknowledgment.',
        'notes': (
            'A different claimant requires explicit takeover; no liveness or orphanhood is inferred.',
            'Completed replay returns its recorded batch and never drains later arrivals.',
            'An unfinished drain refuses as unknown; inspect its native receipts before another boarding request.',
        ),
        'examples': 'floati seat board --root /absolute/fleet --as worker --workspace /absolute/workspace --session declared-session --idempotency-key board-1',
        'option_docs': {
            '--root': 'Exact fleet root.',
            '--as': 'Node owning the declared workspace.',
            '--workspace': 'Workspace with matching seat declaration and waiter binding.',
            '--session': 'Explicit acting session; ambient runtime identity is not used.',
            '--idempotency-key': 'Stable key for replay of the same boarding coordinate.',
            '--take-over': 'Explicitly replace a different recorded claimant.',
        },
    },
    'seat teardown': {
        'description': 'pause and report one completed declared session',
        'body': 'Observe banked workspaces and native process locations in the declared home and leased worktrees, '
                'refuse unacted mail and outstanding work, pause the exact held session, then send its Git-backed TORN DOWN report to the declared architect.',
        'notes': (
            'Teardown never acknowledges mail. Act on and explicitly acknowledge pending messages before retrying.',
            'The report names an event and acknowledgment cutoff; later mail belongs to the next boot.',
            'A process with unreadable cwd is seat-owned only when an exact durable receipt names its PID. Unnamed UNKNOWN processes are reported with reasons.',
            'Completed replay returns historical native testimony without draining later arrivals or pausing a successor.',
            'Interrupted phases resume using exact native receipt references; foreign pause evidence is preserved.',
        ),
        'examples': 'floati seat teardown --root /absolute/fleet --as worker --home /absolute/seats/worker --session declared-session --repo project --doc report.md --idempotency-key teardown-1',
        'option_docs': {
            '--root': 'Exact fleet root.',
            '--as': 'Registered node holding the declared session claim.',
            '--home': 'Canonical declared seat home with its primary checkout at HOME/repo.',
            '--session': 'Explicit acting session; ambient runtime identity is not used.',
            '--repo': 'Repository label for the banked report.',
            '--doc': 'Ordinary report file present at the banked primary checkout tip.',
            '--idempotency-key': 'Stable key bound to the whole teardown coordinate.',
        },
    },
    'seat migrate': {
        'description': 'migrate one registered pre-governance seat once',
        'body': 'Publish fleet governance (if absent) and the workspace seat declaration from the '
                'registry\'s own records, then receipt the act. A seat registered before the governed '
                'path existed boards in one step after this runs once; boarding itself never changes.',
        'notes': (
            'The coordinator derives from the registry\'s architect role when one is active, otherwise pass --coordinator.',
            'Idempotent: an agreeing declaration stands and the migration key replays its receipt; a disagreeing marker refuses.',
            'An unmapped workspace refuses unbound: map it through the waiter consent flow first.',
        ),
        'examples': 'floati seat migrate --root /absolute/fleet --as worker --workspace /absolute/workspace --idempotency-key migrate-1',
        'option_docs': {
            '--root': 'Exact fleet root.',
            '--as': 'Registered active node owning the workspace.',
            '--workspace': 'The mapped boarding workspace to declare.',
            '--idempotency-key': 'Stable key for replay of the same migration coordinate.',
            '--coordinator': 'Coordinator node when the registry records no active architect role.',
        },
    },
    'seat retire': {
        'description': 'offer git worktree remove for finished row trees',
        'body': 'A landed, clean, fully pushed, unclaimed row worktree is offered '
                'for git worktree remove (never --force). Dirty, unpushed, claimed, '
                'not-landed, probe-error, and detached-unreferenced trees are kept by name. '
                'Dry run by default; --apply performs the offered removals.',
        'notes': (
            'Offer only. A probe error means keep.',
            'Never --force. A detached tree with unreferenced commits is never offered.',
        ),
        'examples': 'floati seat retire --root /absolute/fleet\nfloati seat retire --root /absolute/fleet --apply',
        'option_docs': {
            '--root': 'Exact fleet root holding the row-tree repo declaration.',
            '--apply': 'Perform the offered git worktree remove calls. Dry run is the default.',
        },
    },
    'waiter': {
        'description': 'arm exact waiter consent for a declared workspace',
        'body': 'Arm the Stop-waiter consent that `floati wait` holds a turn on. The verb checks the '
                'seat against its registered harness, maps the workspace onto the node, and appends '
                'one armed consent receipt; it writes nothing when a check refuses.',
        'notes': ('Consent is per node and carries the deadline; the bridge and `floati wait` both '
                  'refuse to arm anything, so this verb is the one public act that arms it.',),
        'examples': 'floati waiter arm --help',
        'option_docs': {},
    },
    'waiter arm': {
        'description': 'map one workspace and arm its waiter consent',
        'body': 'Check the node is registered as the named harness, write the workspace map row, and '
                'arm the same consent ledger the Codex installer arms, under the same derived-key '
                'idempotency: the same numbers return the same receipt, a changed number re-arms.',
        'notes': (
            'A harness the node is not registered as refuses before any write.',
            'The wait deadline must be positive and strictly below the hook timeout.',
        ),
        'examples': 'floati waiter arm --root /absolute/fleet --node builder-a --workspace /absolute/workspace --harness zcode --hook-timeout-seconds 30 --wait-deadline-seconds 25',
        'option_docs': {
            '--root': 'Exact fleet root.',
            '--node': 'Registered active node whose consent is armed.',
            '--workspace': 'Existing absolute workspace directory to map onto the node.',
            '--harness': 'Harness the seat must be registered as; compared without case.',
            '--hook-timeout-seconds': 'Hook timeout the armed deadline must stay strictly below.',
            '--wait-deadline-seconds': 'Bounded hold length the consent arms.',
        },
    },
})


HELP_COPY.update({
    'lane': {
        'description': 'open and close recorded lane workspaces',
        'body': 'Create and remove row worktrees under the fleet\'s explicitly declared external lanes root.',
        'notes': ('Only product-created workspaces are recorded and eligible for removal.',),
        'examples': 'floati lane open --help\nfloati lane close --help',
        'option_docs': {},
    },
    'lane open': {
        'description': 'create one recorded row worktree',
        'body': 'Create one row\'s worktree from a declared repository. Floati records the workspace it made.',
        'notes': ('Declare state/lanes-root.json and state/lane-repositories.json before opening a lane. No fetch or implicit root discovery occurs.',
                  'An existing path or branch refuses. Inherited seat-fence keys are overridden only in the new worktree.'),
        'examples': 'floati lane open --root /absolute/fleet --as builder --row row-one --repo product',
        'option_docs': {
            '--root': 'Exact fleet root containing the lane declarations.',
            '--as': 'Active node owning this row workspace.',
            '--row': 'Row identifier used in the workspace path and branch.',
            '--repo': 'Alias in state/lane-repositories.json.',
            '--base': 'Local Git ref; otherwise use the repository declaration\'s default base.',
        },
    },
    'lane close': {
        'description': 'remove one recorded row worktree',
        'body': 'Remove one recorded row worktree and keep its branch. Floati appends a closing receipt.',
        'notes': ('Dirty files, unpublished commits, and runtime references block ordinary closure.',
                  'Force needs a reason, and covers dirty or unpublished work only. Runtime use, and anything Floati cannot inspect, still refuses.'),
        'examples': 'floati lane close --root /absolute/fleet --as builder --row row-one',
        'option_docs': {
            '--root': 'Exact fleet root containing the opening record.',
            '--as': 'Node owning the recorded row workspace.',
            '--row': 'Recorded row identifier to close.',
            '--force': 'Permit removal of dirty or unpublished work only with --why.',
            '--why': 'Explicit reason retained in the forced closing record.',
        },
    },
    'sweep': {
        'description': 'inspect recorded and unmanaged lane workspaces',
        'body': 'List recorded lanes a landed or struck row, or a retired node, makes eligible. Report unmanaged paths separately, with their age and bytes.',
        'notes': ('Preview is read-only. Apply preflights the eligible set and closes only recorded lanes.',
                  'Unmanaged directories are never adopted or removed; their presence produces a degraded result.'),
        'examples': 'floati sweep --root /absolute/fleet\nfloati sweep --root /absolute/fleet --apply',
        'option_docs': {
            '--root': 'Exact fleet root with the explicit lane declarations.',
            '--apply': 'Close the recorded eligible set after all removal preflights pass.',
        },
    },
    'reclaim': {
        'description': 'delete idle declared build output',
        'body': 'Deletes only declared build output in idle fleet-created trees. Dry run by default.',
        'notes': ('A probe that errors means keep.',
                  'The summary reports what df moved.',
                  'Never a worktree, a source file, an unpushed commit or a ref.'),
        'examples': 'floati reclaim --root /absolute/fleet\nfloati reclaim --root /absolute/fleet --apply',
        'option_docs': {
            '--root': 'Exact fleet root.',
            '--apply': 'Delete the idle declared set after every keep rule is re-checked.',
        },
    },
    'checkout-liveness': {
        'description': 'check whether a checkout is in use',
        'body': 'Report a checkout verdict and its reasons from local Git, the named fleet and the host process table.',
        'notes': ('An unreadable input makes the verdict unknown. Known blockers are still reported.',
                  'Remote reachability uses local remote-tracking refs. Refresh them separately when needed.',
                  'When the fleet records an inactive session, the verdict can show what is still in the checkout.',
                  'The verdict describes the observed state and can change before removal.'),
        'examples': 'floati checkout-liveness --root /absolute/fleet --path /absolute/checkout --json',
        'option_docs': {
            '--root': 'Exact fleet root whose sessions and leases will be checked.',
            '--path': 'Absolute checkout path. Symlink aliases are resolved.',
            '--json': 'Emit the machine-readable verdict, which is also the default.',
        },
    },
})


def name_line_description(topic: str) -> str:
    """Return the reviewed NAME description shared by help and MCP."""
    return HELP_COPY[topic]["description"]
