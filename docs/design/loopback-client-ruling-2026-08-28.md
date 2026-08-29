# RULING (RATIFIED) — loopback clients for server-class harnesses (Fable, 2026-08-28; **OWNER-RATIFIED 2026-08-28, in the owner's word: "ratified"**)

**The tension:** t3 (`t3 serve`, 127.0.0.1:3773) and herdr (`herdr server`) are server-class
harnesses — their adapters must be loopback CLIENTS. Floati's standing promise: "opens no
network connection and reads no credential."

**Proposed:** floati MAY open a client socket ONLY when ALL hold:
1. **Loopback literal only** — 127.0.0.1/::1, never a hostname, never resolution. Non-loopback
   remains constitutionally fenced (HM-4 untouched; no discovery, no listener, ever).
2. **User-registered target** — the port comes from the user's explicit adapter configuration
   for a harness THEY registered; floati never scans ports.
3. **Consent-receipted** — arming a client adapter writes a consent receipt in the ledger, same
   posture as wake; the exit door applies (one command disarms, receipted).
4. **Token as pass-through handle** — where the harness requires its own token, the user
   configures a SOURCE (env var / file path); floati passes it through the connection and never
   stores, logs, or ledgers the value. Floati still reads no credential OF the user's providers;
   a local harness session token is the harness's own doorknob, and the receipt records THAT a
   token source was configured, never what it held.
5. **Receipted traffic boundary** — connection attempts and outcomes are receipted; payloads are
   the harness protocol, never mirrored into the ledger.

**Copy consequence (mine, on ratification):** the promise language becomes precise rather than
absolute: "No network beyond consented loopback links to harnesses you registered — and floati
never listens." Zero-telemetry is untouched and stays absolute.

**Status: RATIFIED AND ACTIVE. The t3 and herdr client adapters are unblocked; the promise-copy sharpening is Fable's row in the restamp wave.**
