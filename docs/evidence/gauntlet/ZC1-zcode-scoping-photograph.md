# ZC-1 STEP 1 — zcode scoping photograph (2026-08-30)

**Role:** measurement lane · **Brief:** `docs/design/zc1-zcode-harness-brief-2026-08-30.md`
**Base:** harbor main `6ed91de`. **Seat:** the executor seat.
**Promo tokens spent: ZERO.** The probe refused before reaching a model.

---

## THE HEADLINE: ZCODE IS NOT GUI-ONLY. IT SHIPS A FULL CLI.

The brief recorded the owner's report — *"a DESKTOP GUI with no CLI"* — and ordered the
photograph to **measure that rather than assume it**, under its own fence: *a belief about a
surface is not a surface.* The fence earned its place on the first row it guarded.

**Measured: `zcode` CLI **0.16.5**, shipped inside the app bundle at
`<app>/Contents/Resources/glm/zcode.cjs`** — a 12.5 MB Node entry point. `doctor` reports
its own identity as `process: zcode-cli`, distinct from the GUI.

**Both questions asked first**, per the brief: nothing in `capability-matrix.v0.json`, the
grids, or the roster mentions zcode / z.ai / glm; no remote branch matches `zc1|zcode`.
Nobody is on it and it is not already there.

## Measured surface

**Bundle:** `dev.zcode.app`, ZCode **3.10.1** (build 3.10.1.6272), Electron 41, min macOS 12.
33 executables inside the bundle, including a **`ZCode Computer Use.app` CUA helper** with a
native accessibility module, and MCP servers for browser-use, cua, ios-simulator and
android-emulator plugins.

**CLI verbs:** `app-server` · `commands` · `doctor` · `login` · `logout` · `plugins` ·
`skills` · `tui` · `version`.

**Headless surface — present and rich:**
- `--prompt <text>` and `-p, --print` — run one prompt without the TUI. **This is the
  WAKE-PROBE shape.**
- `--json` machine-readable output · `--no-color` · `--cwd <path>` · `--attach <path>`
- `--mode build|edit|plan|yolo` permission modes (**default `yolo` for `--prompt`**)
- `--allowed-tools` / `--disallowed-tools` (e.g. `"Bash(git *) Edit"`)
- `--settings <path>` to load config from an explicit file
- **`--surface terminal|desktop`** — presentation surface for headless prompts *and the
  app-server*

**Session surface:** `--resume <sessionId>` (`sess_…`) · `-c, --continue` (latest for cwd) ·
`/fork [latest|checkpointId]` · `/rewind` workspace checkpoints. Rollout state lives under
`~/.zcode/cli/rollout`.

**Protocol surface: `app-server` — "Run the ZCode Protocol stdio app server."**
A stdio protocol server is exactly the shape the brief's template selection turns on.

**MCP client:** `/mcp list|status|connect|disconnect`.
**Plugins:** 8 installed from an official registry, with **hooks** support declared per
plugin. **Skills:** 15.

**GUI-harness sweep (the part the brief added, and it came back negative):**
- **No loopback listener** owned by ZCode, with the GUI running (4 processes live).
- **No launchd agent** matching zcode/z.ai.
- Config under `~/.zcode/{cli,v2,workspace,computer-use,plugin-workspace}` and
  `~/Library/Application Support/ZCode`.

So the driveable surface is **not** a hidden local server. It is an ordinary CLI that the
GUI happens to wrap.

## TYPED ABSENCE — the CLI is not signed in, and the GUI's login does not carry

One bounded probe turn, `--mode plan`, empty scratch cwd, the WAKE-PROBE-OK shape.
**Refused in 0.75 s, before any model call:**

```
Error: Model config is missing. Create ~/.zcode/cli/config.json with an explicit
model provider before running ZCode.
```

`~/.zcode/cli/config.json` is **ABSENT**. `~/.zcode/v2/` (the GUI's state) carries
`config.json`, `credentials.json` and `setting.json` — **the GUI is configured and the CLI
is not.** The owner's prerequisite report is true of the application and false of the
surface we need.

**This is an OWNER ACT and I will not perform it.** Signing in, or writing a file that
names a model provider and carries credentials, is the owner's to do — `zcode login`
(Z.AI OAuth) is the named path. Steps 2–5 are blocked on exactly this and nothing else.

**Stdout was empty — sha256 `e3b0c442…`, the hash of zero bytes.** Pinned deliberately:
an empty capture that is *hashed* is a measurement; an empty capture that is merely absent
is a gap, and the two look identical in a directory listing.

## FINDING ZC1-F1 — an advertised option the binary refuses

`--help` lists **`--max-turns <n>`  Maximum model turns for headless prompts`**. Passing it:

```
Unknown option '--max-turns'.
```

The help advertises a flag the parser does not implement. Minor for us — we can bound turns
another way — but it is the class this project names constantly: **a claim asserted by
documentation and contradicted by the binary.** It matters here because `--max-turns` is
precisely the flag a promo-token budget would lean on, so anyone sizing the burn from the
help text would size it against a control that does not exist.

**Related, and the proof is a hash:** `app-server --help` and the bare `--help` produce
**byte-identical output** — both `82f970f9…`. There is no subcommand help. The app-server
protocol shape is therefore **unmeasured**, not undocumented-but-known, and it stays a typed
absence until it is driven.

## ROUTING — the brief's GUI-only branch does NOT apply

Per the brief's decision table, measured against what is actually installed:

- **NOT** GUI-only → no desktop-surface row, no honest-dash work column, and the wake cell
  does **not** join the M9-M13 GUI sitting wave.
- The row is a **CLI row**, entered from this photograph.
- **Template selection is DEFERRED, and that is the honest answer.** `app-server` is the
  ACP/rpc-shaped candidate (devin/cline or pi template); `-p/--prompt --json` is the
  cursor/claude-shaped candidate (direct headless invoke). **Both require auth to
  distinguish**, so choosing now would be choosing from a help page. The brief says the
  photograph decides; the photograph says *this one needs one more photograph.*

## What this step did NOT do

- **No matrix row added, no cell claimed, no stamp moved.** Step 2 births the row from the
  photograph, and this photograph cannot yet see the values a row needs.
- No adapter, no tests, no seat registered.
- No login, no credential written, no config created.
- The app-server protocol was **not** driven. Whether it speaks ACP is unmeasured.
- I did not read the 8 plugins' hook definitions; hooks are declared, their shape unmeasured.

## Captures (sha256-pinned, `captures/zc1-zcode-scoping/`)

Hashes over the committed bytes; operator identity redacted before hashing; residual-token
scan over the directory returns 0 files.

- `bundle-identity.txt` `302e02867d1d2ac0306f40c84f85c69f65187f6e359b8e1f4a105f161726b4c4`
- `bundle-executables.txt` `2235846bd1b98f153435f42ab18fcc5d4c75b5fbdc07287af0703090f7c8ae76`
- `zcode-help.txt` `82f970f9144e69a9ec999205d91faa9da010adc207fbaa505b4ce825349ff723`
- `zcode-appserver-help.txt` `82f970f9144e69a9ec999205d91faa9da010adc207fbaa505b4ce825349ff723`
  (**identical to the above — that is the finding, not a copy error**)
- `zcode-version.txt` `c919061ac48197a954d4614de8e866613cc15df5a96ee3ffa9433530a34f3354`
- `zcode-doctor.txt` `4fd854b017498890df60be9705d3675a7b672c9ca3a8507e17cc31fe3be237f2`
- `zcode-plugins.txt` `99d6ddc7ec1d230b1a1760bd26e278e87a60c002bfa117959d23184320055243`
- `zcode-skills.txt` `697f0ccabb5474c64fcb8918030748a4014ab8814e6f7c500b09003a00138d0f`
- `zcode-gui-sweep.txt` `ece3b520b569bf2ff260ed7a1ace8b0bc951333b786463f74a9a96c17c8fa148`
- `zcode-probe-stdout.txt` `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`
- `zcode-probe-stderr.txt` `b58361b48a9280b47dab88d4d2866e18daf4acdff01e42422c0794350ecd5f2f`
- `zcode-probe-time.txt` `9e03e84d1758f03ecc57016c7362eb24d7c786fd722f22951c43da8f094b76a6`

## Conclusion and the one thing needed

**zcode / cli = a real headless harness surface, MEASURED at 0.16.5.** Sized as the brief
guessed: roughly one lane-day once it can authenticate, because the surface is
Claude-Code-shaped and our existing templates fit it.

**ONE OWNER ACT UNBLOCKS EVERYTHING: sign the CLI in.** `zcode login`, or a
`~/.zcode/cli/config.json` naming a model provider. Nothing else in ZC-1 is blocked, and no
promo tokens can be spent until it happens.

**No public action. No matrix change. No release action.**
