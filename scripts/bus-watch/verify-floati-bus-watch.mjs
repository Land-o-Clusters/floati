#!/usr/bin/env node

import assert from "node:assert/strict"
import { execFileSync } from "node:child_process"
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs"
import { tmpdir } from "node:os"
import { basename, dirname, join, resolve } from "node:path"
import { pathToFileURL } from "node:url"

const pluginSource = resolve(
  process.argv[2] || `${process.env.HOME}/.config/opencode/plugins/floati-bus-watch.ts`,
)
const repositoryRoot = resolve(dirname(pluginSource), "../..")
const floatiExecutable = join(repositoryRoot, "scripts/floati")
const selected = process.argv[3] || "all"
const allowed = new Set(["all", "identity", "delivery", "exhaustion", "single-consumer"])
if (!allowed.has(selected)) {
  throw new Error(`unknown scenario: ${selected}`)
}
if (!existsSync(pluginSource)) {
  throw new Error(`plugin not found: ${pluginSource}`)
}

const scratch = mkdtempSync(join(tmpdir(), "floati-bus-watch-verify-"))
process.on("exit", () => rmSync(scratch, { recursive: true, force: true }))

function git(...args) {
  return execFileSync("/usr/bin/git", args, { stdio: ["ignore", "pipe", "pipe"] })
    .toString()
    .trim()
}

const repository = join(scratch, "repository")
const worktree = join(scratch, "worktree")
mkdirSync(repository)
git("-C", repository, "init", "-q")
writeFileSync(join(repository, "seed"), "seed\n")
git("-C", repository, "add", "seed")
git(
  "-C",
  repository,
  "-c",
  "user.name=Floati Probe",
  "-c",
  "user.email=floati-probe.invalid",
  "commit",
  "-qm",
  "probe seed",
)
git("-C", repository, "worktree", "add", "-qb", "probe-worktree", worktree)

function rows(path) {
  if (!existsSync(path)) return []
  return readFileSync(path, "utf8")
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line))
}

function eventCount(journal, names) {
  return rows(journal).filter((row) => names.includes(row.event)).length
}

async function until(description, predicate, timeoutMs = 4000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 20))
  }
  throw new Error(`timed out waiting for ${description}`)
}

const messageIds = [
  "msg-018f7e9b3c117abc8def0123456789ab",
  "msg-018f7e9b3c127abc8def0123456789ab",
  "msg-018f7e9b3c137abc8def0123456789ab",
  "msg-018f7e9b3c147abc8def0123456789ab",
  "msg-018f7e9b3c157abc8def0123456789ab",
]
let messageSequence = 0
function appendEnvelope(events, recipient, label) {
  const id = messageIds[messageSequence++]
  assert(id, "verifier exhausted deterministic message ids")
  const tenant = basename(dirname(events))
  const row = {
    attempt_binding: "absent_legacy",
    doc: `docs/evidence/${label}.md`,
    id,
    idempotency_key: label,
    kind: "message_envelope",
    note: label,
    recipient,
    repo: "floati",
    schema_version: 0,
    sender: "architect",
    sha: "a".repeat(40),
    tenant_id: tenant,
    timestamp: new Date().toISOString(),
  }
  appendFileSync(
    events,
    JSON.stringify(Object.fromEntries(Object.entries(row).sort(([a], [b]) => a.localeCompare(b)))) + "\n",
  )
  return id
}

let importSequence = 0
async function start(label, groupConfig, sessionRows, prompt, registeredNodes = []) {
  const busRoot = join(scratch, `bus-${label}`)
  const stateRoot = `${busRoot}-watch`
  const events = join(busRoot, "events.jsonl")
  const journal = join(stateRoot, "logs", "opencode_floati_bus_watch.jsonl")
  mkdirSync(stateRoot)
  execFileSync(floatiExecutable, ["init", "--root", busRoot], { stdio: "ignore" })
  const inferredNodes = new Set(registeredNodes)
  for (const nodes of Object.values(groupConfig)) {
    for (const node of nodes) inferredNodes.add(node)
  }
  for (const session of sessionRows) {
    const marker = session.directory && join(session.directory, ".floati-node")
    if (marker && existsSync(marker)) inferredNodes.add(readFileSync(marker, "utf8").trim())
  }
  for (const node of ["architect", ...inferredNodes]) {
    execFileSync(
      floatiExecutable,
      ["register", "--root", busRoot, node, "--harness", "Verifier"],
      { stdio: "ignore" },
    )
  }
  writeFileSync(join(stateRoot, "wake-groups.json"), JSON.stringify(groupConfig))
  for (const nodes of Object.values(groupConfig)) {
    for (const node of nodes) writeFileSync(join(stateRoot, `${node}.enable`), "")
  }

  const pluginCopy = join(scratch, `${label}-${importSequence++}.ts`)
  const instrumented = readFileSync(pluginSource, "utf8")
    .replace("  watch,\n", "  watch as nodeWatch,\n")
    .replace(
      "const BUS_ROOT =",
      "const watch = process.env.FLOATI_BUS_WATCH_VERIFY === \"1\" " +
        "? (() => undefined) : nodeWatch\n\nconst BUS_ROOT =",
    )
    .replace(
      "const POLL_MS = 60_000",
      "const POLL_MS = process.env.FLOATI_BUS_WATCH_VERIFY === \"1\" ? 20 : 60_000",
    )
  writeFileSync(pluginCopy, instrumented)
  process.env.FLOATI_BUS_ROOT = busRoot
  process.env.FLOATI_EXECUTABLE = floatiExecutable
  process.env.FLOATI_BUS_WATCH_VERIFY = "1"
  delete process.env.OPENCODE_FLOATI_NODE
  const module = await import(`${pathToFileURL(pluginCopy).href}?v=${importSequence}`)
  const hook = await module.default({
    client: {
      session: {
        list: async () => sessionRows,
        prompt,
      },
    },
  })
  await until("session seed", () =>
    rows(journal).some((row) => row.event === "init_seeded_sessions"),
  )
  return { busRoot, stateRoot, events, journal, hook }
}

async function identityScenario() {
  let prompts = 0
  const probe = await start(
    "identity",
    { [repository]: ["lane-probe"] },
    [{ id: "worktree-session", directory: worktree }],
    async () => {
      prompts += 1
    },
  )
  appendEnvelope(probe.events, "lane-probe", "worktree")
  await until("worktree repository wake", () => prompts === 1)

  const identity = rows(probe.journal).find(
    (row) => row.event === "identity_resolved" && row.session === "worktree-session",
  )
  assert(identity, "worktree session did not emit a resolved identity receipt")
  assert.equal(identity.reason, "git_common_dir")
  assert.equal(
    rows(probe.journal).some(
      (row) => row.event === "identity_refused" && row.session === "worktree-session",
    ),
    false,
  )
  return { prompts, common_dir: git("-C", worktree, "rev-parse", "--git-common-dir") }
}

async function deliveryScenario() {
  const sessionDir = join(scratch, "direct-session")
  mkdirSync(sessionDir)
  writeFileSync(join(sessionDir, ".floati-node"), "lane-direct\n")
  let rejectPrompt = true
  let prompts = 0
  const probe = await start(
    "delivery",
    {},
    [{ id: "direct-session", directory: sessionDir }],
    async () => {
      prompts += 1
      if (rejectPrompt) throw new Error("synthetic fetch failure")
    },
  )
  writeFileSync(join(probe.stateRoot, "lane-direct.enable"), "")
  const messageId = appendEnvelope(probe.events, "lane-direct", "direct")
  await until("retryable prompt failure", () =>
    eventCount(probe.journal, ["wake_failed_retryable", "wake_failed"]) >= 1,
  )

  const delivered = join(probe.stateRoot, "delivered_lane-direct", messageId)
  const claim = join(probe.stateRoot, "claims_lane-direct", messageId)
  assert.equal(existsSync(delivered), false, "failed prompt left a delivery tombstone")
  assert.equal(existsSync(claim), false, "failed prompt left a claim lock")

  rejectPrompt = false
  await probe.hook.event({
    event: { type: "session.idle", properties: { sessionID: "direct-session" } },
  })
  await until("successful retry", () => existsSync(delivered))
  assert(prompts >= 2, "failed prompt was not retried")
  return { prompts, failure_unwound: true, retry_delivered: true }
}

async function exhaustionScenario() {
  let prompts = 0
  const probe = await start(
    "exhaustion",
    { [worktree]: ["lane-exhaust"] },
    [{ id: "exhaust-session", directory: worktree }],
    async () => {
      prompts += 1
      throw new Error("synthetic busy host")
    },
  )
  const exhaustedMessageId = appendEnvelope(probe.events, "lane-exhaust", "exhaust")
  await until("first group attempt", () =>
    eventCount(probe.journal, ["group_wake_failed_retryable", "group_wake_failed"]) >= 1,
  )

  for (let pass = 0; pass < 10; pass += 1) {
    await probe.hook.event({
      event: { type: "session.idle", properties: { sessionID: "exhaust-session" } },
    })
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 20))
  }

  const receiptPath = join(probe.stateRoot, "exhausted_lane-exhaust", exhaustedMessageId)
  const falseDelivery = join(probe.stateRoot, "delivered_lane-exhaust", exhaustedMessageId)
  await until(
    "exhaustion outcome",
    () => existsSync(receiptPath) || existsSync(falseDelivery),
  )
  assert.equal(existsSync(falseDelivery), false, "exhaustion was recorded as delivery")
  assert(existsSync(receiptPath), "exhaustion did not leave a persistent receipt")
  const receipt = JSON.parse(readFileSync(receiptPath, "utf8"))
  assert.equal(receipt.id, exhaustedMessageId)
  assert.equal(receipt.attempts, 11)
  assert.match(receipt.exhausted_at, /^\d{4}-\d{2}-\d{2}T/)

  const promptsAtExhaustion = prompts
  appendEnvelope(probe.events, "lane-exhaust", "rearm")
  await until("exhaustion re-arm", () =>
    !existsSync(receiptPath) &&
      rows(probe.journal).some((row) => row.event === "exhaustion_rearmed"),
  )
  await until("re-armed prompt", () => prompts > promptsAtExhaustion)
  return {
    attempts: receipt.attempts,
    exhausted_at: receipt.exhausted_at,
    retained_and_rearmed: true,
  }
}

async function singleConsumerScenario() {
  let prompts = 0
  const probe = await start(
    "single-consumer",
    { [repository]: ["alice-city"] },
    [
      { id: "alice-city-primary", directory: repository },
      { id: "alice-city-worktree", directory: worktree },
    ],
    async () => {
      prompts += 1
    },
  )
  appendEnvelope(probe.events, "alice-city", "single-consumer")
  await until("one seat wake", () => prompts >= 1)
  await new Promise((resolvePromise) => setTimeout(resolvePromise, 100))
  assert.equal(prompts, 1, "one envelope woke more than one session for one seat")
  const wakeRows = rows(join(probe.busRoot, "receipts", "wakes", "alice-city.jsonl"))
  assert.equal(wakeRows.length, 1, "successful prompt did not produce exactly one wake receipt")
  assert.equal(wakeRows[0].node_id, "alice-city")
  assert.equal(wakeRows[0].outcome, "woke")
  const coordinators = readdirSync(join(probe.busRoot, "receipts", "wake-coordination"))
  assert.deepEqual(coordinators, ["alice-city"], "wake path minted a non-registry coordinator")
  return {
    prompts,
    one_seat_one_wake: true,
    wake_receipts: wakeRows.length,
    coordinator: coordinators[0],
  }
}

const receipt = { plugin: basename(pluginSource) }
if (selected === "all" || selected === "identity") receipt.identity = await identityScenario()
if (selected === "all" || selected === "delivery") receipt.delivery = await deliveryScenario()
if (selected === "all" || selected === "exhaustion") receipt.exhaustion = await exhaustionScenario()
if (selected === "all" || selected === "single-consumer") receipt.single_consumer = await singleConsumerScenario()
console.log(JSON.stringify(receipt, null, 2))
