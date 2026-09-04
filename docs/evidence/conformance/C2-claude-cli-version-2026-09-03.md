# C2 — declared Claude CLI version measurement

Date: 2026-09-03

This is a fresh Darwin measurement of the Claude CLI through the canonical
declared-executable predicate introduced by
`docs/evidence/fcd20-r1a-rider-2026-09-01.md`. No PATH lookup or candidate
search selected the executable.

The first declaration named `~/.local/bin/claude`. The
runner refused it because it is a symlink:

```json
{"artifact_version":0,"command":"fcd20-conformance","evidence":{"code":"fcd20_claude_executable_invalid","detail":"executable must be an explicit canonical executable","remedy":"pass --claude-executable with one absolute canonical executable path"},"status":"refused"}
```

Read-only link resolution returned the exact canonical target
`~/.local/share/claude/versions/2.1.251`. Applying the named
contract once produced:

```json
{"artifact_version":0,"command":"fcd20-conformance","evidence":{"cannot_see":["linux_measurements_from_a_non_linux_host","provider_turn_or_authentication","controlled_load_performance","harnesses_outside_c1_c9"],"host":{"machine":"arm64","platform":"darwin","python_version":"3.9.6"},"rows":[{"evidence":{"code":"fcd20_codex_executable_undeclared","detail":"the operator did not declare an executable for codex","paths":[],"remedy":"pass --codex-executable with one absolute canonical executable path"},"harness":"codex","row":"C1","status":"host_condition"},{"evidence":{"duration_ms":137,"exit_code":0,"path":"~/.local/share/claude/versions/2.1.251","stderr_sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","stderr_size":0,"stdout_sha256":"1aaadbe01265e82bd28c2e2639a2a6a0604edbb124f997e9d3a4d09b823c6fb8","stdout_size":22,"timed_out":false,"version":"2.1.251 (Claude Code)"},"harness":"claude","row":"C2","status":"measured"},{"evidence":{"code":"fcd20_opencode_executable_undeclared","detail":"the operator did not declare an executable for opencode","paths":[],"remedy":"pass --opencode-executable with one absolute canonical executable path"},"harness":"opencode","row":"C3","status":"host_condition"},{"evidence":{"code":"fcd20_cursor_executable_undeclared","detail":"the operator did not declare an executable for cursor","paths":[],"remedy":"pass --cursor-executable with one absolute canonical executable path"},"harness":"cursor","row":"C4","status":"host_condition"},{"evidence":{"code":"fcd20_cline_executable_undeclared","detail":"the operator did not declare an executable for cline","paths":[],"remedy":"pass --cline-executable with one absolute canonical executable path"},"harness":"cline","row":"C5","status":"host_condition"},{"evidence":{"code":"fcd20_grok_build_executable_undeclared","detail":"the operator did not declare an executable for grok-build","paths":[],"remedy":"pass --grok-build-executable with one absolute canonical executable path"},"harness":"grok-build","row":"C6","status":"host_condition"},{"evidence":{"code":"fcd20_pi_executable_undeclared","detail":"the operator did not declare an executable for pi","paths":[],"remedy":"pass --pi-executable with one absolute canonical executable path"},"harness":"pi","row":"C7","status":"host_condition"},{"evidence":{"code":"fcd20_herdr_executable_undeclared","detail":"the operator did not declare an executable for herdr","paths":[],"remedy":"pass --herdr-executable with one absolute canonical executable path"},"harness":"herdr","row":"C8","status":"host_condition"},{"evidence":{"code":"fcd20_t3_executable_undeclared","detail":"the operator did not declare an executable for t3","paths":[],"remedy":"pass --t3-executable with one absolute canonical executable path"},"harness":"t3","row":"C9","status":"host_condition"}]},"status":"degraded"}
```

C2 is `measured`, exit 0, at `2.1.251 (Claude Code)`. The aggregate is
`degraded` only because C1 and C3–C9 were deliberately undeclared. This receipt
does not measure Linux, provider turns, authentication, controlled-load
performance, or harnesses outside C1–C9.

No credential, provider configuration, package installation, repository ref,
tag, workflow, release, or remote was changed by the measurement.
