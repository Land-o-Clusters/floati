"""Reviewed help prose; argparse owns all command and argument grammar.

Each topic supplies a NAME description, body, operational notes, examples and
semantic option descriptions. Option keys are long flags or positional destinations;
they are annotations, never declarations of supported syntax or requiredness.
"""
from __future__ import annotations

from .copy import GH_AUTHENTICATION_REMEDY


HELP_COPY = {'': {'description': 'inspect and operate an explicit fleet root',
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
 'node role': {'description': 'assign a shipped role',
               'body': 'Answer only declared template questions and preview the exact typed role record.',
               'notes': ('Answers must exactly match the selected template questions.',),
               'examples': 'floati node role --root ~/fleet --node builder-a --template builder --answer '
                           'repo=floati --answer never_touch=foreign-bus --answer reports_to=architect-a',
               'option_docs': {}},
 'node boot': {'description': 'project live boot context',
               'body': 'Re-read live registry, role, topology, wake, and managed-bus evidence for one boot '
                       'projection.',
               'notes': ('This command prints a projection; it does not launch a harness.',),
               'examples': 'floati node boot --root ~/fleet --node builder-a --declared-roots '
                           '~/declared.json --managed-executable /usr/local/bin/floati-fleet --profile '
                           'builder-a --json',
               'option_docs': {}},
 'node teardown': {'description': 'project the retention ritual',
                   'body': 'Re-read live evidence and project the ordered state-preserving teardown ritual.',
                   'notes': ('Projection does not retire a node, close a lease, or delete a workspace.',),
                   'examples': 'floati node teardown --root ~/fleet --node builder-a --declared-roots '
                               '~/declared.json --managed-executable /usr/local/bin/floati-fleet --profile '
                               'builder-a --json',
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
                       'body': 'Materialize the grant-stamped receipts-read bundle deterministically to one '
                               'explicit absolute path.',
                       'notes': ('Reads by an ungranted consumer refuse naming the grant act. The only write '
                                 'is the requested output path.',),
                       'examples': 'floati confluence bundle --root ~/fleet --consumer puddle --out '
                                   '~/bundle.json',
                       'option_docs': {}},
 'wake': {'description': 'control exact wake coordinates',
          'body': 'Arm one exact acting session, control one exact session marker, or manage one consented '
                  'local wake daemon coordinate.',
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
 'register': {'description': 'register this node',
              'body': 'Append one active node registration. A node registers only itself.',
              'notes': (),
              'examples': 'floati register --root ~/fleet builder-a --harness Codex',
              'option_docs': {'--root': 'Explicit direct-home fleet root.',
                              'node': 'Bounded node identifier.',
                              '--harness': 'Harness role recorded on disk.',
                              '--create-workspace': "Also create the node's workspace layout during "
                                                    'registration.'}},
 'retire': {'description': "retire this node's registry row",
            'body': 'Append one retirement row for the calling node, under the same lock register uses. '
                    'Self-retirement only; the registered role carries forward and the ledger stays '
                    'append-only. A retired node leaves the active roster; its history remains readable.',
            'notes': (),
            'examples': 'floati retire --root ~/fleet builder-a',
            'option_docs': {'--root': 'Explicit direct-home fleet root.',
                            'node': 'The node retiring itself. Retiring another node (--as, --actor) is '
                                    'refused.'}},
 'send': {'description': 'append a Git notification',
          'body': 'Send a Git-authoritative notification, optionally with one typed delivery claim. Delivery '
                  'and acknowledgment remain separate receipts.',
          'notes': ('All routing and Git fields are required.',),
          'examples': 'floati send --root ~/fleet --from a --to b --repo floati --sha <sha> --doc '
                      'docs/evidence/checkpoint.md --note checkpoint --claim ~/claim.json',
          'option_docs': {'--claim': 'Strict JSON delivery-claim document; floati binds the new envelope id.',
                          '--reply-to': 'Existing reversed-party message binding.',
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
         'body': 'Read canonical ledgers. Replay renders receipt-derived orchestration events in '
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
              'body': 'Read delivery, acknowledgment, and denial histories as distinct evidence.',
              'notes': (),
              'examples': 'floati receipts builder-a --root ~/fleet',
              'option_docs': {'node': 'Exact registered node.', '--root': 'Explicit fleet root.'}},
 'supervise': {'description': 'report fleet health',
               'body': 'Run a physically read-only report of liveness, authority, mutex, inbox depth, and '
                       'stale leases.',
               'notes': (),
               'examples': 'floati supervise --root ~/fleet',
               'option_docs': {'--root': 'Explicit fleet root. No action option exists.'}},
 'presence': {'description': 'self-report or inspect node liveness',
              'body': "Record only the acting node's own bounded liveness report, or inspect what every "
                      'active node last reported.',
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
              'notes': ('Artifact options are all-or-none. --owner may be omitted only in a solo root.',),
              'examples': "floati work add --root ~/my-sessions --title 'record this session'\n"
                          "floati work add --root ~/fleet --title 'build board' --owner builder-a --needs "
                          'work-...',
              'option_docs': {'--needs': 'Require one existing work item; repeat for multiple dependencies.',
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
                             '--now accepts UTC RFC3339.',),
                   'examples': 'floati work complete --root ~/my-sessions --id work-...\n'
                               'floati work complete --root ~/fleet --id work-... --as builder-a',
                   'option_docs': {}},
 'work show': {'description': 'project work state',
               'body': 'Project all work items or one exact item from append-only records.',
               'notes': (),
               'examples': 'floati work show --root ~/fleet',
               'option_docs': {'--id': 'Exact work item identifier.'}},
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
        'description': 'board one declared workspace explicitly',
        'body': 'Compose the existing wake claim, resume, and inbox drain for one declared workspace.',
        'notes': ('Boarding is explicit; it never discovers a session or installs a hook.',),
        'examples': 'floati seat board --help',
        'option_docs': {},
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
})


def name_line_description(topic: str) -> str:
    """Return the reviewed NAME description shared by help and MCP."""
    return HELP_COPY[topic]["description"]
