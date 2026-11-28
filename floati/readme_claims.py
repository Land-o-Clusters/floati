"""Bounded Markdown claim units used by the existing release gate.

Digests cover unit Markdown with whitespace collapsed, not an entire paragraph.
Fences, comments and link destinations cannot create quantitative claims.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit


ALLOWLIST = "docs/release/readme-claims-allowlist.v0.json"
INLINE_LINK = re.compile(r'(!?)\[([^\]]*)\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+"[^"]*")?\s*\)')
REFERENCE_LINK = re.compile(r'(!?)\[([^\]]+)\]\[([^\]]*)\]')
REFERENCE_DEFINITION = re.compile(r'^ {0,3}\[([^\]]+)\]:\s*<?([^\s>]+)>?\s*$', re.M)
INLINE_CODE = re.compile(r'(`+)(.+?)\1', re.S)
HTML_TAG = re.compile(r'</?[A-Za-z][^>]*>')
STAMP = re.compile(r'\b(?:MEASURED|DERIVED|ESTIMATE)\b', re.I)
# Named exclusions: dates, version strings (including the Python version floor),
# list ordinals, and command lines. Decimal quantities remain quantities.
DATE = re.compile(r'\b\d{4}-\d{2}-\d{2}\b')
VERSION = re.compile(r'\b(?:v?\d+(?:\.\d+){2,}(?:[-+][\w.-]+)?|v\d+\.\d+|(?:Python|version)\s+\d+(?:\.\d+)+)\b', re.I)
# Digits in names such as SHA-256, MX-1 and Apache-2.0 are identifiers,
# not quantities; a leading quantity such as 20-surface is still counted.
IDENTIFIER = re.compile(r'\b[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*-\d+(?:\.\d+)*\b')
LIST_ORDINAL = re.compile(r'^\s*\d+[.)]\s+')
COMMAND_LINE = re.compile(r'^\s*(?:\$\s+|(?:python\d*|floati|git|pip\d*|npm|npx|brew|echo|umask|\./\S+)\s+)')
QUANTITY = re.compile(r'(?<![\w/#.])\d[\d,]*(?:\.\d+)?(?:[A-Za-zµμ%]+)?(?!\w|\.\d)')
TEARDOWN_COLUMNS = frozenset({'teardown', 'worker teardown', 'observed lifecycle'})
TEARDOWN_ABSENCE = re.compile(r'NOT_YET_MEASURED: `[a-z][a-z0-9_]*`')
WORKER_TEARDOWN = 'worker teardown'


def normalize_unit(text):
    return " ".join(text.split())


def _blank(match):
    return re.sub(r'[^\n]', ' ', match.group())


def _source(root):
    try:
        text = (root / "README.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    text = re.sub(r'<!--.*?(?:-->|\Z)', _blank, text, flags=re.S)
    lines = []
    fence = None
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not line[marker.end():].strip():
                fence = None
            lines.append(re.sub(r'[^\n]', ' ', line))
        elif marker:
            fence = marker[1]
            lines.append(re.sub(r'[^\n]', ' ', line))
        else:
            lines.append(line)
    return "".join(lines)


def _links(text, references):
    text = INLINE_CODE.sub(_blank, text)
    for match in INLINE_LINK.finditer(text):
        yield match, match[1], match[3].strip('<>')
    for match in REFERENCE_LINK.finditer(text):
        target = references.get((match[3] or match[2]).casefold())
        if target is not None:
            yield match, match[1], target


def _local_target(reference):
    try:
        parsed = urlsplit(reference)
    except ValueError:
        return None  # Malformed external URLs cannot discharge a local receipt.
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    return unquote(parsed.path)


def _file_in_tree(root, relative):
    if Path(relative).is_absolute():
        return False
    try:
        target = (root / relative).resolve()
        target.relative_to(root.resolve())
        return target.is_file()
    except (OSError, ValueError, RuntimeError):
        return False


def _visible(text):
    text = INLINE_CODE.sub(lambda m: ' ' if COMMAND_LINE.match(m[2]) else m[2], text)
    text = INLINE_LINK.sub(lambda m: ' ' if m[1] else m[2], text)
    text = REFERENCE_LINK.sub(lambda m: ' ' if m[1] else m[2], text)
    return HTML_TAG.sub('', text)


def _observation_id(value):
    return isinstance(value, str) and bool(value.strip())


def _completed_worker_boot(arm, node, harness):
    boot = arm['boot']
    evidence = boot['evidence']
    board = evidence['board']
    hold = evidence['first_hold']
    session = arm['next_session']
    consent = evidence['consent_receipt_id']
    presented = board['item_ids']
    return (
        boot['status'] == 'ok'
        and evidence['node_id'] == board['node_id'] == hold['node_id'] == node
        and evidence['harness'] == hold['harness'] == harness
        and evidence['acting_session_id'] == board['acting_session_id'] == hold['acting_session_id'] == session
        and board['step'] == 'completed'
        and all(map(_observation_id, (board['id'], board['arm_receipt_id'], board['delivery_receipt_id'])))
        and isinstance(presented, list) and all(map(_observation_id, presented))
        and set(arm['after_close']['pending_mail']).issubset(presented)
        and evidence['hook']['state'] == 'verified'
        and hold['phase'] == 'start'
        and _observation_id(hold['id'])
        and _observation_id(consent)
        and hold['consent_receipt_id'] == consent
    )


def _clear_worker_residue(snapshot):
    return (snapshot['claim_session'] is None and snapshot['consent_armed'] is False
            and snapshot['hook_active'] is False)


def _worker_teardown_verdict(path, harness):
    """Replay the comparison in a retained native receipt without running a harness."""
    if path.suffix != '.json':
        return None
    try:
        pair = json.loads(path.read_text(encoding='utf-8'))
        if (harness not in {'codex', 'claude', 'cursor', 'zcode'} or pair['harness'] != harness
            or not re.fullmatch(r'[a-f0-9]{40}', pair['source_sha'])
            or not _observation_id(pair['native_version']) or not _observation_id(pair['node'])):
            return None
        node = pair['node']
        omission, control = pair['omission'], pair['control']
        sessions = [arm[key] for arm in (omission, control)
                    for key in ('previous_session', 'next_session')]
        if not all(map(_observation_id, sessions)) or len(set(sessions)) != len(sessions):
            return None
        for arm in (omission, control):
            snapshot = arm['after_close']
            mail = snapshot['pending_mail']
            if (not re.fullmatch(r'[a-f0-9]{64}', arm['transcript_sha256'])
                or snapshot['claim_session'] not in (None, arm['previous_session'])
                or type(snapshot['consent_armed']) is not bool
                or type(snapshot['hook_active']) is not bool
                or not isinstance(mail, list) or not all(map(_observation_id, mail))):
                return None
        # Each arm needs a pending message witness to prove that a fresh board
        # can still see mail. A contested arm keeps its witness pending.
        if not all(arm['after_close']['pending_mail'] for arm in (omission, control)):
            return None
        teardown = control['teardown']
        evidence = teardown['evidence']
        if not (
            teardown['status'] == 'ok' and evidence['node_id'] == node
            and evidence['harness'] == harness and evidence['acting_session_id'] == control['previous_session']
            and evidence['teardown']['step'] == 'completed'
            and _observation_id(evidence['teardown']['id'])
            and evidence['activation']['state'] == 'disarmed'
            and _observation_id(evidence['activation']['consent_receipt_id'])
            and _clear_worker_residue(control['after_close'])
            and _completed_worker_boot(control, node, harness)
        ):
            return None
        if 'teardown' in omission:
            return None
        boot = omission['boot']
        if (boot['status'] == 'refused' and boot['evidence']['code'] == 'seat_board_claim_contested'
            and omission['after_close']['claim_session'] == omission['previous_session']
            and boot['evidence']['remedy'] == 'pass --take-over to replace claimant ' + omission['previous_session']
            and omission['after_refusal']['pending_mail'] == omission['after_close']['pending_mail']):
            return 'teardown required'
        if _clear_worker_residue(omission['after_close']) and _completed_worker_boot(omission, node, harness):
            return 'no teardown needed'
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        # Absent, unreadable and malformed observations cannot support a verdict.
        return None
    return None


def _units(source):
    """Yield (line, sentence-or-cell, verdict kind, harness) in document order."""
    paragraph = []
    paragraph_line = 1
    verdict_columns = {}
    table = False

    def cells_of(raw):
        line = HTML_TAG.sub('', raw).strip()
        masked = INLINE_CODE.sub(_blank, line)
        cuts = [m.start() for m in re.finditer(r'(?<!\\)\|', masked)]
        if not cuts:
            return []
        starts = [0] + [c + 1 for c in cuts]
        ends = cuts + [len(line)]
        cells = [line[a:b].strip() for a, b in zip(starts, ends)]
        if cuts[0] == 0:
            cells.pop(0)
        if cuts[-1] == len(line) - 1:
            cells.pop()
        return cells

    def separator(cells):
        return bool(cells) and all(re.fullmatch(r':?-+:?', c) for c in cells)

    def sentences():
        text = "".join(paragraph)
        # Mask links/code/tags to keep punctuation in them out of sentence splitting.
        masked = text
        for pattern in (INLINE_CODE, INLINE_LINK, REFERENCE_LINK, HTML_TAG):
            masked = pattern.sub(_blank, masked)
        start = 0
        for end in [m.end() for m in re.finditer(r'''[.!?][*_~"'”’»)\]]*(?=\s|$)''', masked)] + [len(text)]:
            raw = text[start:end]
            if raw.strip():
                lead = len(raw) - len(raw.lstrip())
                yield paragraph_line + text[:start + lead].count('\n'), raw.strip(), None, None
            start = end

    lines = source.splitlines(keepends=True)
    for line_number, raw in enumerate(lines, 1):
        line = HTML_TAG.sub('', raw).strip()
        if REFERENCE_DEFINITION.match(raw):
            continue
        cells = cells_of(raw)
        header = bool(cells) and line_number < len(lines) and separator(cells_of(lines[line_number]))
        if cells and (table or header):
            yield from sentences()
            paragraph = []
            table = True
            if separator(cells):
                continue
            if header:
                verdict_columns = {}
                if 'harness' in cells[0].casefold():
                    for i, cell in enumerate(cells):
                        name = _visible(cell).strip('*_ ').casefold()
                        if name in TEARDOWN_COLUMNS:
                            verdict_columns[i] = name if name == WORKER_TEARDOWN else 'teardown'
                        elif name in {'bus', 'work', 'wake'}:
                            verdict_columns[i] = 'capability'
            # A shortened Markdown row still renders missing verdict cells.
            # Do not let dropping the trailing separators erase the claim.
            teardown_width = max((i + 1 for i, kind in verdict_columns.items()
                                  if kind in ('teardown', WORKER_TEARDOWN)), default=0)
            cells.extend([''] * max(0, teardown_width - len(cells)))
            for i, cell in enumerate(cells):
                verdict = verdict_columns.get(i) if not header else None
                if cell or verdict in ('teardown', WORKER_TEARDOWN):
                    harness = _visible(cells[0]).strip('*_ ').casefold() if verdict == WORKER_TEARDOWN else None
                    yield line_number, cell, verdict, harness
            continue
        table = False
        verdict_columns = {}
        if not line or COMMAND_LINE.match(line):
            yield from sentences()
            paragraph = []
            continue
        if re.match(r'^\s*(?:[-*+] |\d+[.)] |#{1,6} )', raw):
            yield from sentences()
            paragraph = []
            raw = re.sub(r'^\s*(?:[-*+] |\d+[.)] |#{1,6} )', '', raw)
        if not paragraph:
            paragraph_line = line_number
        paragraph.append(HTML_TAG.sub('', raw))
    yield from sentences()


def readme_claim_census(repo_root):
    """Enumerate claims with stable text digests and unit-local receipt status."""
    root = Path(repo_root)
    source = _source(root)
    references = {m[1].casefold(): m[2] for m in REFERENCE_DEFINITION.finditer(source)}
    claims = []
    for line, unit, verdict, harness in _units(source):
        # This exact cell is an absence, excluded from the claim population.
        # Extra prose, a link, an empty reason or an empty cell is a verdict
        # requiring its own receipt, even when it contains no number.
        if verdict in ('teardown', WORKER_TEARDOWN) and TEARDOWN_ABSENCE.fullmatch(normalize_unit(unit)):
            continue
        visible = _visible(unit)
        numeric = IDENTIFIER.sub('', VERSION.sub('', DATE.sub('', LIST_ORDINAL.sub('', visible))))
        if not (verdict or STAMP.search(visible) or QUANTITY.search(numeric)):
            continue
        text = normalize_unit(unit)
        local_links = (
            (match.group(), _local_target(target))
            for match, image, target in _links(unit, references) if not image
        )
        linked = any(
            target is not None and _file_in_tree(root, target)
            and (verdict != WORKER_TEARDOWN or (
                normalize_unit(markdown) == text
                and _worker_teardown_verdict(root / target, harness) == normalize_unit(visible).casefold()
            ))
            for markdown, target in local_links
        )
        claim = {"line": line, "text": text,
                 "claim_digest": hashlib.sha256(text.encode('utf-8')).hexdigest(), "linked": linked}
        if verdict == WORKER_TEARDOWN:
            claim['requires_replay'] = True
        claims.append(claim)
    return claims


def readme_claim_findings(repo_root):
    """Keep dead-link detection and add unlinked/stale exceptions to one gate."""
    root = Path(repo_root)
    source = _source(root)
    references = {m[1].casefold(): m[2] for m in REFERENCE_DEFINITION.finditer(source)}
    targets = [target for _match, _image, target in _links(source, references)]
    targets.extend(re.findall(r'\b(?:src|srcset)="([^"]+)"', source))
    local = {_local_target(target) for target in targets}
    missing = sorted(target for target in local if target is not None and not _file_in_tree(root, target))
    findings = ['readme_claim_receipt_missing:%s' % target for target in missing]
    # The export policy and its classifier are Harbor-only. Published trees and
    # installed bundles do not carry them; there, the ordinary file check applies.
    policy_path = root / '.github/public-export-policy.v0.json'
    if policy_path.exists():
        from .public_export_policy import ExportPolicy, ExportRefusal, classify_path, load_published_baseline

        try:
            policy = ExportPolicy.load(policy_path)
            baseline = set(load_published_baseline(root / '.github/public-export-baseline.v0.json'))
            findings.extend('readme_claim_receipt_not_public:%s' % target
                for target in sorted(local - {None})
                if _file_in_tree(root, target)
                and classify_path(target, baseline, policy).disposition != 'include')
        except (OSError, ValueError, ExportRefusal):
            findings.append('readme_claim_export_policy_invalid')
    claims = readme_claim_census(root)
    allowed = set()
    try:
        rows = json.loads((root / ALLOWLIST).read_text(encoding='utf-8'))
        if not isinstance(rows, list):
            raise ValueError('not a list')
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {'claim_digest', 'excerpt', 'reason'}
                or not isinstance(row['claim_digest'], str) or not re.fullmatch('[0-9a-f]{64}', row['claim_digest'])
                or not isinstance(row['excerpt'], str) or not 0 < len(row['excerpt']) <= 80
                or not isinstance(row['reason'], str) or not row['reason'].strip()
                or row['claim_digest'] in allowed):
                raise ValueError('invalid entry')
            allowed.add(row['claim_digest'])
    except FileNotFoundError:
        pass
    except (OSError, UnicodeDecodeError, ValueError):
        findings.append('readme_claim_allowlist_invalid')
        allowed = set()
    current = {claim['claim_digest'] for claim in claims}
    findings.extend('readme_claim_allowlist_stale:%s' % digest for digest in sorted(allowed - current))
    findings.extend('readme_claim_unlinked:%s:%s' % (c['line'], c['text'][:80])
        for c in claims if not c['linked'] and (c.get('requires_replay') or c['claim_digest'] not in allowed))
    return findings
