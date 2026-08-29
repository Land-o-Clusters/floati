# Advanced Terminal Feature Compatibility in 2026

## Scope, terminology, and confidence

This matrix reflects the state I could verify **as of August 28, 2026**. I treated a feature as **Supported (S)** when a primary project source documents a usable implementation, **Partial (P)** when the implementation is incomplete, gated, or only works by proxy/passthrough, and **No (N)** when project sources show it is unimplemented or deliberately unsupported.

There are two important qualifiers:

- **†** — current support is verified, but I could not establish the **first tagged release** from a primary changelog. A commit date or “present by” version is given instead.
- **‡** — even the current state could not be satisfactorily established from a primary vendor compatibility document. This applies most seriously to **Apple Terminal.app**, because Apple's current Terminal documentation describes the app and profile/UI behavior but does not publish a VT-protocol compatibility table or detailed control-sequence changelog. citeturn27search12turn27search13

For the **tmux** row, “Supported” does not mean tmux renders the feature itself. It means applications inside tmux can use the feature through tmux's native parser/mediation; “Partial” generally means an outer terminal plus explicit passthrough is required. The current tmux line at this cutoff is **3.7c, released August 17, 2026**. fileciteturn6file0L1-L2

### Graphics protocols

| Terminal | Kitty graphics protocol | SIXEL | iTerm2 OSC 1337 inline image |
|---|---|---|---|
| **macOS Terminal.app** | **N‡** — no Apple primary documentation of support; landing version unverified. Apple publishes no equivalent protocol matrix. citeturn27search12 | **N‡** — same verification limitation. citeturn27search12 | **N‡** — no Apple primary support statement located. citeturn27search12 |
| **iTerm2** | **P — 3.5.4beta2**. iTerm2 added Kitty graphics but its release notes explicitly qualified support as excluding animation. I found no later primary note establishing full animation support. citeturn15view3 | **S — 3.3.0**. SIXEL was added in the 3.3 line; stable 3.3.0 was built July 31, 2019. citeturn15view2turn16view3 | **S — ≤2.9.20150512†**. OSC 1337 `File=` is iTerm2's own protocol; its docs record later additions such as animated GIF support in 2.9.20150512, Retina behavior in 3.2.0, and multipart transfer in 3.5. The original basic protocol predates the first version I could pin down. citeturn12search0 |
| **kitty** | **S — 0.4.0**. The graphics protocol was introduced in kitty 0.4.0 on October 22, 2017. citeturn3search4turn3search0 | **N†**. Kitty's project discussion/RFC history has not adopted SIXEL; kitty instead defines its own acknowledged graphics protocol. citeturn5search18turn3search0 | **N‡**. No native OSC 1337 image implementation was verified in kitty's primary protocol documentation; use the Kitty graphics protocol instead. citeturn3search0 |
| **WezTerm** | **P — 20220101-133340-7edc5b5a**. Kitty image support became enabled by default with “most” of the protocol supported; animation was explicitly not yet implemented in that release. I found no subsequent primary statement declaring full protocol completeness. citeturn19view2 | **P — 20200620-160318-e00b076c**. WezTerm calls its SIXEL implementation “preliminary and incomplete.” citeturn19view1turn17search0 | **S†**. Current WezTerm escape-sequence documentation explicitly supports the iTerm2 file/image protocol; it also shipped `wezterm imgcat` using that protocol very early in the project, but I could not establish the first exact release from the available changelog. citeturn17search0turn19view4 |
| **Ghostty** | **S†**. Kitty graphics is explicitly one of Ghostty's supported modern terminal protocols; exact pre-1.0 implementation landing was not isolated. citeturn20search0turn21search6 | **N**. Ghostty's maintainer explicitly rejected SIXEL support for the foreseeable implementation direction. citeturn21search0 | **N — through June 2026**. Ghostty issue #13011 states that Ghostty supports Kitty graphics but **does not implement iTerm2 OSC 1337 inline images**; the request was closed as not planned. citeturn22search0 |
| **Alacritty** | **N†**. No Kitty graphics implementation is present in the current feature line; Alacritty's project has historically declined integrated image-rendering features. Current release history through 0.17.0 does not document adding one. citeturn27search2 | **N†**. No SIXEL implementation is documented through the current Alacritty release line. citeturn27search2 | **N†**. No OSC 1337 inline-image implementation is documented in the current project. citeturn27search2 |
| **Konsole** | **P†**. Kitty's protocol documentation lists Konsole among implementations, establishing current support, but I could not verify the first Konsole release or the implementation's full coverage of optional Kitty graphics features from a KDE release note. citeturn3search0 | **S — 22.04**. KDE's SIXEL tracking bug is resolved fixed with “Version Fixed/Implemented In: 22.04.” citeturn23search1 | **S†**. Current Konsole source contains a dedicated iTerm `1337;File=` parser/state machine and cell-size reporting; first packaged release was not established. fileciteturn34file0 fileciteturn34file1 |
| **Windows Terminal** | **N — through the 1.26 development line**. Kitty graphics remains a feature request rather than an implemented protocol. citeturn9search19turn9search5 | **S — 1.22**. Microsoft introduced SIXEL image rendering in the 1.22 generation. citeturn9search18turn9search23 | **N†**. Microsoft's OSC/iTerm-style image request has not been documented as implemented; SIXEL is the image protocol Microsoft subsequently shipped. citeturn9search13turn9search18 |
| **VS Code integrated terminal** | **P — 2026†**. Current VS Code source exposes Kitty graphics through `terminal.integrated.enableImages`, while xterm.js merged a Kitty-graphics **MVP** in February 2026. Image rendering remains setting/renderer dependent rather than a baseline terminal guarantee. citeturn25search2 | **P — ≤2024†**. VS Code supports SIXEL through its image feature, but the feature is configuration/platform/ConPTY dependent. citeturn25search0turn25search2 | **P — ≤2024†**. The same image setting supports iTerm inline images, again with host/renderer restrictions. citeturn25search0turn25search2 |
| **tmux** | **P — ≥3.3†**. tmux does not natively implement the Kitty image model; Kitty payloads must be passed to a capable outer terminal using tmux passthrough, governed by `allow-passthrough`. The option was introduced in 2022 and is intentionally controlled rather than transparent. citeturn21search3 | **S — ≥3.4†**. Modern tmux has native SIXEL parsing/storage/forwarding and a `sixel` terminal feature; the SIXEL branch merged in 2023. Current 3.7 continues to contain native SIXEL handling. citeturn26search0 fileciteturn0file0 | **P — ≥3.3†**. There is no native iTerm `File=` image model in tmux comparable with SIXEL; an application has to use terminal passthrough to reach an outer OSC-1337-capable emulator. citeturn26search0 |

The strongest distinction in the image-protocol data is that **Kitty graphics has a request/reply capability probe**, whereas SIXEL and classic iTerm `File=` output do not provide comparable per-image acknowledgement semantics. That difference matters substantially for safe runtime negotiation. citeturn3search0turn12search0

## Rendering, mouse, keyboard, and color matrix

For mouse reporting, **S** means both ordinary SGR mouse mode **1006** and pixel-coordinate **SGR-Pixels mode 1016** were verified. **P** means ordinary SGR is available but 1016 is absent, incomplete, or not adequately verified.

| Terminal | DEC private mode 2026 synchronized output | SGR mouse, including 1016 | Kitty keyboard protocol | 24-bit RGB SGR |
|---|---|---|---|---|
| **macOS Terminal.app** | **N‡** — no primary Apple protocol documentation located. citeturn27search12 | **P‡** — conventional mouse support exists in practice, but I could not establish 1006/1016 behavior from an Apple primary source; do not rely on this row for capability negotiation. citeturn27search12 | **N‡** — no Apple primary KKP support statement found. citeturn27search12 | **S‡** — current Terminal.app is commonly usable for truecolor applications, but I could not locate an Apple release note giving the control-sequence landing version; treat the first-version field as **unverified**. Apple's own documentation only discusses profile colors, not SGR truecolor capability. citeturn27search13 |
| **iTerm2** | **S — 3.5.0beta1; stable 3.5.0**. DECSET 2026 support appeared in the 3.5 beta series; stable 3.5.0 was released in May 2024. citeturn15view1turn16view2 | **S — 1016 present in 3.5-era releases†**. iTerm2 documents SGR-Pixels/DEC mode 1016 in the 3.5 release lineage; ordinary SGR mouse predates it. citeturn15view0 | **S — 3.5.4beta2**. Release notes explicitly add Kitty key reporting. citeturn15view3 | **S†**. Current iTerm2 capability reporting explicitly includes a 24-bit-color capability; the first tagged release could not be pinned down from the release notes I verified. citeturn13search2 |
| **kitty** | **S†**. Current kitty defines pending/synchronized-update private mode **2026**. Its original 2018 atomic-update implementation used an earlier form, so I do **not** claim that 2018 as the DECSET-2026 landing version. fileciteturn10file0L1-L13 fileciteturn24file0L1-L2 | **S†**. Current kitty source implements 1006 and 1016 independently. Pixel-coordinate reporting was added in September 2021; the exact first release tag was not independently established here. fileciteturn10file0L1-L13 | **S — 0.20.0**. Kitty introduced its keyboard protocol in the 0.20.0 release line. citeturn3search4turn3search1 | **S†**. Kitty's terminal/terminfo interface supports direct RGB color; exact first release is older than the changelog boundary I could establish. citeturn0search5turn3search11 |
| **WezTerm** | **S — 20210814-124438-54e29167**. The escape-sequence reference identifies that exact version for synchronized rendering. citeturn17search0turn19view0 | **S — 1016 implemented January 2022†**. The original 1016 request notes that 1006 was already supported and is now closed as **completed**; implementation work landed in January 2022. The precise subsequent packaged release was not isolated. fileciteturn38file0L1-L13 | **S — 20220624-141144-bd1b7c5d†**. WezTerm added its Kitty-keyboard option in the 2022 release line. citeturn19view3 | **S†**. Current WezTerm documentation explicitly supports 24-bit `38;2`/`48;2` RGB SGR forms. citeturn17search0 |
| **Ghostty** | **S†**. Synchronized rendering is listed as a native modern terminal feature; first implementation release was not isolated. citeturn20search0turn21search6 | **S†**. Ghostty's own 2025–2026 discussion shows pixel SGR mouse mode 1016 in real use, including pixel coordinates outside the content area; exact landing version is unverified. citeturn22search1turn22search3 | **S†**. Kitty keyboard is listed among Ghostty's supported protocols; first release unverified. citeturn20search0 | **S†**. Truecolor is part of Ghostty's current terminal implementation; first version unverified. citeturn20search0 |
| **Alacritty** | **S — 0.13.0**. Alacritty 0.13 switched synchronized updates to the standardized CSI/DEC private mode 2026 form and also added DECRQM/DECRPM support. citeturn27search0 | **P‡**. Ordinary xterm/SGR mouse handling is established in Alacritty's implementation history, but I could not verify a current mode-1016 implementation from primary source; no 1016 implementation entry was located. Treat pixel precision as unsupported unless an active DECRQM probe proves otherwise. citeturn27search0 | **P — 0.13.0**. Alacritty officially added KKP in 0.13.0, but current project issues document remaining protocol-conformance/flag-reporting discrepancies, including an open issue targeted at 0.18.0. citeturn27search0turn27search1turn27search7 | **S — ≤0.5.0†**. Alacritty's own changelog already documents truecolor-related behavior by 0.5.0; the original landing predates that verified boundary. citeturn6search4 |
| **Konsole** | **S — code landed Jan. 26, 2026†**. KDE added explicit handling for DECSET/DECRST **2026**, buffering terminal redraws with a timeout. The first KDE Gear package containing the commit was not independently mapped. fileciteturn31file0L1-L2 | **S†**. Current Konsole source explicitly handles both mode **1006** and mode **1016**, and has a separate exact/pixel mouse-event path when 1016 is set. First release not isolated. fileciteturn34file0 fileciteturn34file2 | **S — code landed May 10, 2026†**. KDE implemented KKP push/pop/query/set state, flags, event types, alternate keys, and CSI-u encoding; the first packaged KDE Gear tag was not independently verified. fileciteturn32file0L1-L2 | **S†**. Konsole is a longstanding direct-RGB terminal, but a primary changelog entry identifying the very first version was not located; landing version therefore remains unverified. |
| **Windows Terminal** | **S — stable 1.23.20211.0**. DEC mode 2026 first appeared in the 1.24 preview development work and was backported/shipped in the stable 1.23.20211.0 release in January 2026. citeturn8search4turn8search6 | **S — 1006 older; 1016 merged for the 1.26 line†**. Microsoft's mode-1016 feature request was followed by a successful implementation PR in March 2026. The precise GA 1.26 package carrying it was not independently re-verified. citeturn9search2turn9search10 | **S — 1.25**. Windows Terminal 1.25 added Kitty Keyboard Protocol support. citeturn8search0turn8search3 | **S†**. Microsoft explicitly documents Windows Terminal truecolor/24-bit support; it predates the versions investigated here, so first landing is unverified. citeturn9search3turn9search7 |
| **VS Code integrated terminal** | **S† — xterm.js 6.0.0**. xterm.js 6.0.0 release notes list synchronized-output mode 2026; the exact VS Code desktop release in which that xterm.js version first became the integrated-terminal baseline was not independently mapped. fileciteturn22file0L1-L2 | **P†**. Normal SGR mouse reporting is part of the xterm.js terminal stack, but I could not verify mode 1016 as a VS Code-integrated capability from current primary VS Code documentation; active probing is required before treating it as pixel-capable. | **S†**. Current VS Code source exposes `terminal.integrated.enableKittyKeyboardProtocol`, enabled by default; first VS Code release was not established. citeturn25search2 | **S†**. The integrated terminal has long supported truecolor through xterm.js; first VS Code release not isolated. citeturn25search2 |
| **tmux** | **S — 3.7** for applications inside panes. tmux 3.7 added explicit handling whereby an application entering DECSET 2026 causes pane output to be buffered until DECRST 2026 or a one-second timeout. fileciteturn0file0 fileciteturn6file0L1-L2 | **P†**. tmux supports ordinary terminal mouse capabilities and exposes a `mouse` terminal feature, but I found no current tmux implementation of SGR-Pixels mode 1016. Do not infer pixel support merely because the outer emulator has it. citeturn26search0 | **P†**. tmux has `extkeys` and a `csi-u` extended-key format, which covers an important encoding subset, but that is not the same thing as implementing Kitty's complete progressive-enhancement/query/stack protocol end-to-end. citeturn26search0turn3search1 | **S†**. Current tmux has an `RGB` terminal feature and recognizes `Tc` as the legacy equivalent for direct RGB colors. citeturn26search0 |

Two caveats matter here. First, **“Kitty keyboard support” is not binary in practice**: the protocol has progressive-enhancement flags, event types, alternate keys, associated text, and state query/push/pop behavior. Alacritty is a concrete example where a terminal can legitimately say “supports Kitty keyboard” while still having open conformance bugs. citeturn27search1turn27search7turn3search1 Second, **SGR-Pixels 1016 is not a different event framing from 1006**: it uses the SGR mouse representation but changes coordinate interpretation to pixels and causes per-pixel movement reporting. This is why applications must negotiate 1016 explicitly rather than guessing from an incoming SGR mouse packet. citeturn26search1

## Safe runtime detection

The central rule is: **prefer a protocol response over terminal-name recognition, and prefer terminal-name recognition over branding environment variables only as a final fallback**. DA2 and environment variables tell you *who something resembles*; a protocol query tells you *what the current endpoint says it can do*. This distinction becomes especially important across tmux and SSH. citeturn26search1turn26search0

| Feature | Recommended runtime detection | Interpretation |
|---|---|---|
| **Kitty graphics** | Send a **valid Kitty graphics `a=q` query** with a unique image ID, immediately followed by **DA1 (`CSI c`)**. Kitty's protocol explicitly specifies the DA1 query as a synchronization barrier. citeturn3search0 | A Kitty graphics response before the DA1 response proves the current endpoint understood the graphics query. If DA1 comes back without the graphics response, treat Kitty graphics as unsupported on that path. citeturn3search0 |
| **SIXEL** | Send **DA1 (`CSI c`)** and examine Primary Device Attributes for the SIXEL capability indicator; DEC/xterm semantics use DA1 capability **4** for SIXEL. citeturn26search1 | Presence of the SIXEL DA1 bit is a positive signal. Absence is **not a universally safe negative** because non-DEC-compatible emulators may implement SIXEL without accurately advertising every DA1 capability. |
| **iTerm2 inline images** | In actual iTerm2, prefer its **feature-reporting mechanism**: the documented `OSC 1337;Capabilities` query and/or its `TERM_FEATURES` capability data can report features such as file/image support. citeturn13search2turn12search0 | A positive iTerm capability report is strong evidence. A negative result is **not** evidence that OSC 1337 is absent globally, because WezTerm, Konsole, and VS Code can implement iTerm's image protocol without themselves being iTerm2 or implementing iTerm2's capability-reporting extension. citeturn17search0 fileciteturn34file0 citeturn25search2 |
| **Synchronized output** | Query **DECRQM for private mode 2026**: `CSI ? 2026 $ p`. A conforming implementation answers with DECRPM, `CSI ? 2026 ; Ps $ y`. citeturn26search1 | Any response state indicating a recognized set/reset mode establishes support. `Ps=0`/an unrecognized response means the endpoint does not recognize the queried mode; no response means **unknown**, not necessarily unsupported. |
| **SGR mouse** | Query **1006** with `CSI ? 1006 $ p`; separately query **1016** with `CSI ? 1016 $ p`. Do **not** turn on mouse tracking simply to see what happens. DECRQM is non-disruptive compared with enabling reporting. citeturn26search1 | 1006 recognized ⇒ cell-coordinate SGR mouse available. 1016 recognized ⇒ SGR-Pixels is available. They must be stored as two separate capabilities. Current Konsole, for example, distinguishes the 1016 mode internally. fileciteturn34file0 fileciteturn34file2 |
| **Kitty keyboard** | Send the Kitty keyboard **current-flags query, `CSI ? u`**, followed by **DA1** as the barrier recommended by the Kitty protocol. citeturn3search1 | A `CSI ? flags u` response establishes KKP support and simultaneously tells you which progressive-enhancement flags are active. If DA1 arrives without the Kitty response, treat KKP as unavailable. citeturn3search1 |
| **24-bit color** | First query terminal capabilities using **XTGETTCAP** for `RGB`; also understand legacy `Tc`. If querying locally rather than on-wire, inspect the actual terminfo entry named by `$TERM`. tmux explicitly defines `RGB` and treats `Tc` as equivalent for direct RGB capability. citeturn26search0turn26search1 | A positive `RGB`/`Tc` capability is a strong signal. Failure to answer XTGETTCAP is only **unknown**, because XTGETTCAP itself is optional. A terminal may render `38;2`/`48;2` correctly while advertising a conservative `$TERM` such as a 256-color-compatible entry. |

### Why DA1 is useful as a barrier

The Kitty graphics and keyboard protocols both use a subtle but very useful technique: send the feature query and then send **DA1**. Terminal input and replies are ordered, so receiving the DA1 response establishes that the terminal has processed the preceding feature query. That turns “silence” from an indefinitely ambiguous condition into a bounded negative after the barrier reaches you. Kitty explicitly documents this pattern for graphics and keyboard negotiation. citeturn3search0turn3search1

This is much safer than:

```text
if $TERM == xterm-kitty:
    assume everything
```

Kitty's own documentation warns that terminal-identifying environment variables can become stale in multiplexer situations, while protocol queries describe the endpoint actually processing your escape sequences. citeturn3search11

### DA2 should identify, not negotiate

**DA2 (`CSI > c`) is useful as a fallback identity/version hint, not as a feature-discovery protocol.** xterm's control-sequence definition makes DA2 a Secondary Device Attributes response; it does not carry a standardized bitset for Kitty graphics, KKP, SIXEL, truecolor, or synchronized output. citeturn26search1

A practical implementation may maintain a version allowlist such as “known iTerm2 ≥ X supports feature Y,” but that should be behind actual capability queries. Version tables age, terminal forks can return compatible identifiers, and tmux or another intermediary may be the entity responding rather than the GUI terminal behind it. Those are inference hazards from the semantics of DA queries and terminal multiplexing, not a property encoded by DA2 itself. citeturn26search0turn26search1

### XTGETTCAP is useful but not universal

xterm's **XTGETTCAP** request is a substantially better option than parsing `$TERM` text when the requested capability is represented in terminfo. `RGB`/`Tc` is the obvious case here. citeturn26search0turn26search1

On the wire, XTGETTCAP uses a DCS query containing hexadecimal capability names. For example, the ASCII names correspond to:

```text
RGB -> 52 47 42 -> 524742
Tc  -> 54 63    -> 5463
```

The important engineering point is not to hard-code the bytes if a terminal library already exposes XTGETTCAP/terminfo operations. A **positive** result is useful; a **timeout/no-response cannot prove absence of RGB**, because the terminal might support RGB SGR but not XTGETTCAP itself. The xterm specification defines XTGETTCAP, while tmux separately documents its RGB/Tc feature handling. citeturn26search1turn26search0

## False positives, false negatives, and multiplexers

### Detection methods compared

| Detection method | Main false-positive risk | Main false-negative risk | Recommended use |
|---|---|---|---|
| **DA1** | An emulator or multiplexer can deliberately present a DEC-compatible capability identity that does not map perfectly to its underlying renderer. citeturn26search1turn26search0 | An emulator can implement a modern extension without advertising the corresponding historical DA1 bit. | Good for SIXEL; also excellent as an **ordering barrier** behind protocol-specific probes. |
| **DA2** | Matching a known version string can become wrong after configuration changes, forks, proxies, or a multiplexer responding on behalf of the outer terminal. citeturn26search1turn26search0 | A perfectly capable terminal can return an unfamiliar or deliberately compatible DA2 identity. | Identity fallback only; never your primary feature detector. |
| **DECRQM/DECRPM** | tmux or another virtual terminal may answer for **its** capabilities rather than those of the GUI terminal behind it—which is usually correct for the application but surprises code trying to identify the outer emulator. citeturn26search0 | A terminal can implement a private mode yet fail to implement DECRQM for it. | Best generic probe for modes **2026, 1006 and 1016**. |
| **XTGETTCAP** | The answer may represent the current virtual terminal/terminfo contract rather than the physical outer terminal. citeturn26search0turn26search1 | Many terminals do not implement XTGETTCAP even when the underlying capability works. | Excellent positive evidence for capabilities such as `RGB`; lack of response means unknown. |
| **`$TERM` / local terminfo** | `$TERM` can deliberately name a conservative compatibility entry, or a user can force an inaccurate value. Kitty and tmux both rely on terminal descriptions and warn against mismatched terminfo. citeturn0search5turn26search0 | A truecolor terminal using `xterm-256color` can look less capable than it really is; the required terminfo entry can also simply be missing on a remote host. citeturn0search5 | Baseline escape-language contract, not brand detection. |
| **Brand environment variables** | They survive process nesting and can be stale after tmux attachment or other context changes. Kitty explicitly documents this class of tmux problem for Kitty-specific environment information. citeturn3search11 | SSH commonly does not forward arbitrary GUI-terminal branding variables, so the outer terminal can be capable while the remote environment contains no hint. | Weak fallback/optimization only. |
| **Protocol-specific acknowledged query** | An intermediary that filters the escape family may hide a capable outer terminal. | Same intermediary filtering creates a negative even though a directly-connected terminal would work. | **Preferred** for Kitty graphics and KKP because the protocols were designed to be queried. citeturn3search0turn3search1 |

### tmux changes what “terminal capability” means

Inside tmux, the terminal your application is directly talking to is **tmux's virtual terminal**, not Kitty, iTerm2, Ghostty, Konsole, or Windows Terminal. That is why it is usually wrong for an application inside tmux to bypass tmux's answer and say “but `$TERM_PROGRAM` tells me the outer terminal is iTerm2.” tmux explicitly maintains its own terminal-feature model, including `RGB`, `sixel`, `sync`, `mouse`, and `extkeys`. citeturn26search0

This creates several protocol-specific cases:

**SIXEL** is now a native tmux feature, so an application should normally let tmux mediate it rather than blindly DCS-wrapping every image. Current tmux contains native SIXEL handling and capability detection. citeturn26search0 fileciteturn0file0

**DECSET 2026** changed materially with tmux 3.7. Before that, tmux could use synchronized updates in its relationship with an outer terminal, but tmux 3.7 specifically added support for an **application inside a pane** entering mode 2026 and having tmux buffer that pane's updates. fileciteturn0file0

**Kitty graphics and OSC 1337** are different: they are not native tmux image models. A client that knows it is inside tmux may need tmux's passthrough facility and appropriate DCS wrapping to get the foreign protocol to the outer emulator. Consequently, “outer terminal supports Kitty graphics” does **not** imply “this pane can safely emit Kitty APC sequences unmodified.” citeturn21search3turn26search0

**Kitty keyboard** should likewise not be inferred from the outer terminal. tmux's `extkeys`/`csi-u` interface is the application-facing contract; it overlaps significantly with Kitty's encoding but does not establish that every Kitty progressive-enhancement operation survives unchanged. citeturn26search0turn3search1

### SSH has the inverse problem

With SSH, the escape byte stream generally travels between the remote application and the local terminal, while `$TERM` is copied into the remote session as the terminal-interface name. The remote machine may lack the corresponding extended terminfo entry; Kitty's documentation specifically discusses installing its terminfo on remote systems for this reason. citeturn0search5

That yields a useful priority rule:

> **A successful active terminal response is generally stronger evidence than an environment-variable guess; a missing environment hint is not evidence of missing terminal support.**

The exception is an intermediary such as tmux that intentionally terminates or translates a protocol. In that situation, the intermediary's capability is exactly what the pane application needs to know. citeturn26search0

### The dangerous `COLORTERM` shortcut

`COLORTERM=truecolor` or `COLORTERM=24bit` is widely used as a practical truecolor hint, but I did **not** find a normative primary protocol specification giving it the same semantics as terminfo `RGB`, an acknowledged query, or an SGR capability negotiation. I would therefore treat it only as a **weak positive heuristic**.

Conversely, the absence of `COLORTERM` should never disable truecolor by itself: tmux explicitly models RGB separately from terminal naming, and many terminal deployments retain compatibility-oriented `$TERM` names. citeturn26search0

## TERM and NO_COLOR conventions

### `$TERM` is an interface contract, not an emulator detector

`$TERM` exists so software can select the correct terminal description. A distinctive value such as `xterm-kitty` is useful when the matching terminfo exists, but changing `$TERM` merely to unlock a feature is dangerous: applications will then emit **all** capabilities from that terminal description, not just the one you wanted. Kitty's documentation explicitly addresses installation and use of its terminfo, while tmux separately has its own terminal-feature overrides. citeturn0search5turn26search0

Therefore, these are poor patterns:

```sh
# Do not do this merely to force truecolor.
export TERM=xterm-256color

# Also do not pretend to be kitty merely to get image support.
export TERM=xterm-kitty
```

A safer architecture is:

```text
$TERM / terminfo
    -> baseline control-sequence contract

active protocol queries
    -> additional runtime capabilities

user configuration
    -> explicit override

NO_COLOR
    -> output-policy preference
```

That separates four concepts which terminal applications frequently conflate. The distinction between terminfo capabilities, protocol queries, and user color policy follows directly from the respective terminal and NO_COLOR specifications. citeturn26search0turn26search1turn26search2

### `NO_COLOR` is policy, not capability detection

The `NO_COLOR` convention says that when the **`NO_COLOR` environment variable exists and is non-empty**, command-line software should by default avoid adding ANSI color. The convention explicitly allows user configuration or command-line options to override that default. citeturn26search2

It does **not** mean:

- the terminal lacks 24-bit color;
- `$TERM` should be changed to `dumb`;
- bold, underline, or other non-color formatting is inherently forbidden;
- graphics, mouse, synchronized output, or keyboard extensions should be disabled.

Those are separate capability/policy dimensions; the NO_COLOR specification is specifically a convention for suppressing colorized output by default. citeturn26search2

A robust application should therefore detect truecolor normally and preserve that information:

```text
terminal.rgb_capable = true
user.no_color = true
```

Then rendering policy can decide not to emit colors. That is preferable to poisoning the capability model with `rgb_capable = false`.

## Recommended decision flow

For a new terminal application in 2026, the safest implementation is a **capability object populated in layers**, not a large `$TERM` switch statement.

Start by recognizing whether an intermediary such as tmux is the immediate endpoint. Treat its advertised terminal contract as authoritative for the pane and do not blindly assume outer-terminal features pass through. tmux deliberately models `RGB`, SIXEL, synchronized output, mouse and extended keys, while foreign image protocols require different handling. citeturn26search0

Then issue non-destructive active probes where they exist:

```text
Kitty graphics    -> Kitty a=q query + DA1 barrier
Kitty keyboard    -> CSI ? u + DA1 barrier
Sync output       -> DECRQM ?2026
SGR mouse         -> DECRQM ?1006
Pixel mouse       -> DECRQM ?1016
SIXEL             -> DA1 SIXEL capability bit
24-bit color      -> XTGETTCAP RGB, then Tc
iTerm image       -> iTerm feature report when available
```

These mechanisms are defined by the Kitty, xterm/DEC, iTerm2 and tmux documentation. citeturn3search0turn3search1turn26search1turn13search2turn26search0

Where a protocol has **no portable acknowledgement mechanism**—most notably classic OSC-1337 image output outside iTerm2's own feature-reporting environment—use a small, maintained terminal/version capability table only after active methods fail. WezTerm, Konsole, and VS Code demonstrate why checking only for “iTerm2” would cause false negatives: all can understand iTerm's image protocol without being iTerm2. citeturn17search0 fileciteturn34file0 citeturn25search2

Finally apply user policy such as `NO_COLOR`; it should override **whether you use color**, not rewrite what you believe the terminal can do. citeturn26search2

The practical 2026 baseline is consequently quite strong: **24-bit color is effectively universal among the modern emulators in this set; synchronized output and Kitty keyboard have spread rapidly; SGR-Pixels 1016 is increasingly common but still must be negotiated independently from 1006; and image protocols remain the least portable part of the terminal stack.** Kitty, iTerm2, WezTerm, Ghostty, Konsole, Windows Terminal and VS Code have materially different image-protocol choices, while tmux introduces an additional mediation boundary. citeturn3search0turn17search0turn20search0turn23search1turn9search18turn25search2turn26search0

The largest unresolved primary-source gap in this matrix is **macOS Terminal.app**: Apple does not currently publish the kind of escape-sequence compatibility/changelog information that the other projects expose. Accordingly, every Terminal.app cell marked **‡** should be treated as **not independently verified for use in an automated compatibility allowlist** rather than as the same evidentiary quality as the corresponding iTerm2, Kitty, WezTerm, KDE, Microsoft, or tmux entries. citeturn27search12turn27search13