# MX1 M6 — t3/cli wake measurement (2026-08-29)

**Role:** measurement lane · **Brief:** `docs/design/mx1-measurement-campaign-2026-08-29.md`
**Cell:** t3 / cli / wake — claimed `event-driven`, grade `classified` at seed.
**Base:** harbor main `d18d039f`.

## Measured, this machine, today

- **Executable named and launched:** `/opt/homebrew/bin/t3` → `t3 v0.0.35` (matches the
  classified cell). Bounded `t3 serve --no-browser --host 127.0.0.1 --port 13774
  --base-dir <scratch> --log-websocket-events` under `timeout`; scratch data directory;
  server killed by PID at the end of the sitting.
- **Surface enumeration at 0.0.35** (`--help`, `serve --help`, `auth` tree, captured
  whole): HTTP/WebSocket server (`serve`/`start`) · `--log-websocket-events` "outbound
  WebSocket push traffic" · headless auth control plane (`t3 auth pairing
  create|list|revoke`, `t3 auth session issue|list|revoke` — scoped bearer tokens minted
  offline against the data directory) · `pair`, `project`, `service`, `connect`.
- **The wake path exercised end-to-end, push observed TWICE:**
  1. `/ws` enforces auth: bare upgrade → **401**; upgrade with a scoped bearer from
     `t3 auth session issue --token-only` → **101 Switching Protocols** (raw-socket
     client, no libraries).
  2. The shipped web client was paired via the printed headless pairing URL and left
     open, subscribed.
  3. From a SEPARATE process, `t3 project rename …` mutated state — and the subscribed
     client re-rendered the new name immediately, **twice** (`mx1-renamed`, then
     `mx1-push-proven` at 01:36:03Z), with **zero new HTTP requests in the browser's
     network log across both triggers** and no page reload (in-page observer state
     survived). The only live channel was the client's WebSocket: **the change arrived
     by server push. The event-driven wake fired and was observed arriving.**
- **Honest negative, named:** an authenticated raw socket that completes the upgrade but
  sends no app-level subscription receives 0 pushed bytes in 30 s — pushes go to clients
  that complete the client-side subscription handshake, not to every authenticated
  socket. The push observation therefore rides the shipped client, which is the honest
  instrument for "what a subscriber receives."
- **Secrets handling:** pairing tokens and bearer tokens are NOT committed — the serve
  log is committed as an excerpt with the token lines elided and the elision named
  (one-time secrets of a destroyed scratch server; AD-1 t3 precedent).

## Captures (sha256-pinned, committed under `captures/mx1-t3-cli-wake/`)

- `t3-version.txt` 610a294a2246e84be6500cb811c83619559693dd1f5bf1cee6b54bc626be2ac8
- `t3-help.txt` f226c0a3da52e556ff009afe81751b4c09aadb8a9dc17c41f82e09ca53ede63c
- `t3-serve-help.txt` a44f1f85361901935527f8343cfc4b3dcafea2df12b549e8c88b1a11a075285b
- `t3-auth-help.txt` c2b25244438c889950874ccc46cd2aee22098f9600457d5d7dc64476e4d08f87
- `t3-serve-log-excerpt.txt` 81c3338e259af1e88a93fd17294e36d6c0dd8c41727cd4209c67f4e8713c4c04
- `t3-ws-upgrade-status.txt` a7423685b64e50522d222d1480e637c547e8de1ca5334bb2f6688251575b95f0
- `t3-ws-push-observation.txt` 1d5d1184ccbc31125cbc6c13506f51ec5fe794098eeca4cc7ac4e72556231ca1
- `t3-client-push-observation.txt` 05c0006b791bac2f6312470b8cfb9bc580de0d2df5bb9928e97cb89355866d06

## Conclusion

**t3 / cli · wake = event-driven — MEASURED at v0.0.35.** While the server process lives,
a subscribed client is woken by outbound WebSocket push — observed twice, triggered from
a separate process, with zero polling traffic. The posture qualifier stands unchanged:
the push channel exists for the SERVER PROCESS LIFETIME; a cold t3 (no `serve` running)
has nothing to push, which is exactly what the cell's `event-driven` value has always
scoped. Cell value unchanged; stamp edit rides this commit: `classified → measured`,
receipt_path here, `measured_at 2026-08-29`, grids re-rendered from the dataset.
