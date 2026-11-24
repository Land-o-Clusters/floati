/**
 * Owner-configured OpenCode event observer, never a permission decision hook.
 *
 * An owner-created plugin imports this factory and exports its result with an
 * explicit root and observerPath. This library is outside OpenCode's automatic
 * plugin directories; importing it alone installs nothing and starts nothing.
 * Contract: OpenCode 1.18.23 packages/opencode/src/plugin/index.ts supplies
 * {event: {id, type, properties}} after its native directory filter.
 * https://opencode.ai/docs/plugins/
 */
import { spawn } from "node:child_process";
import { lstatSync, realpathSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";

const MAX_INPUT_BYTES = 131072;
const CHILD_TIMEOUT_MS = 2000;
const TERMINATION_GRACE_MS = 250;

function ordinaryPath(value, kind) {
  if (typeof value !== "string" || !isAbsolute(value) || resolve(value) !== value ||
      realpathSync(value) !== value) {
    throw new TypeError(`opencode_observer_${kind}_invalid: use an explicit canonical absolute path`);
  }
  const metadata = lstatSync(value);
  if (metadata.isSymbolicLink() || !(kind === "script" ? metadata.isFile() : metadata.isDirectory())) {
    throw new TypeError(`opencode_observer_${kind}_invalid: ordinary ${kind} required`);
  }
  return value;
}

function absence(code) {
  // No native text, tool data, root paths or child output is exposed to the host.
  process.stderr.write(JSON.stringify({status: "no_result", source: "desktop_host",
    harness: "opencode", code}) + "\n");
}

function observe(root, observerPath, directory, input) {
  return new Promise((done) => {
    let child;
    try {
      child = spawn("/usr/bin/python3", ["-B", observerPath, "--root", root,
        "--harness", "opencode"], {cwd: directory, stdio: ["pipe", "ignore", "ignore"]});
    } catch {
      absence("opencode_observer_spawn_unavailable");
      done();
      return;
    }
    let settled = false;
    let timedOut = false;
    let forceTimer;
    const deadlineTimer = setTimeout(() => {
      timedOut = true;
      absence("opencode_observer_deadline");
      // Only the child handle created above is signalled, never a supplied pid.
      child.kill("SIGTERM");
      forceTimer = setTimeout(() => child.kill("SIGKILL"), TERMINATION_GRACE_MS);
    }, CHILD_TIMEOUT_MS);
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(deadlineTimer);
      clearTimeout(forceTimer);
      done();
    };
    child.once("error", () => {
      absence("opencode_observer_spawn_unavailable");
      finish();
    });
    child.once("close", (code) => {
      if (code !== 0 && !timedOut && !settled) absence("opencode_observer_exit_unavailable");
      finish();
    });
    // An early receiver exit can close stdin. Its exit remains observational.
    child.stdin.on("error", () => {});
    child.stdin.end(input);
  });
}

export function createFloatiHostObserver({root, observerPath}) {
  ordinaryPath(root, "root");
  ordinaryPath(observerPath, "script");
  return async ({directory, worktree}) => {
    ordinaryPath(directory, "directory");
    ordinaryPath(worktree, "worktree");
    let pending = Promise.resolve();
    let disposed = false;
    return {
      event: async ({event}) => {
        if (disposed || !event || !["permission.asked", "session.idle"].includes(event.type)) return;
        let input;
        try {
          input = Buffer.from(JSON.stringify({cwd: directory, worktree, event}), "utf8");
        } catch {
          absence("opencode_observer_payload_invalid");
          return;
        }
        if (input.byteLength > MAX_INPUT_BYTES) {
          absence("opencode_observer_payload_oversized");
          return;
        }
        // The Python producer joins the unmodified native session to current
        // workspace consent, claim and car receipts; this bridge mints no IDs.
        // OpenCode invokes event handlers without awaiting their promises.
        // Capture input now, but launch each receiver only after the previous
        // child has closed so an idle event cannot overtake pending approval.
        const delivery = pending.then(() => observe(root, observerPath, directory, input));
        pending = delivery.catch(() => {
          absence("opencode_observer_transport_unavailable");
        });
        await pending;
      },
      dispose: async () => {
        // Native instance finalization rejects new callbacks immediately and
        // waits for all accepted receivers, each with its own bounded deadline.
        disposed = true;
        await pending;
      },
    };
  };
}
