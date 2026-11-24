"""Authenticated source-local mail facts; observing never repairs a bank.

Membership trees use the same envelope identity and timestamp in every plane.
Equal roots therefore certify equal sets, including their timestamp extrema.
All persistent nodes live in the owning native index's existing node bank.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from . import send_prefix
from .errors import ProtocolRefusal

VERSION = 1
TREE_DOMAIN = "mail-tree-v1"
NODE_DOMAIN = "mail-node-v1"
SENDER_DOMAIN = "mail-sender-v1"


def _utc(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()


def item(row):
    return {"id": row["id"], "timestamp": row["timestamp"], "ordinal": row["_mail_ordinal"]}


def event_item(prefix, identity):
    row = prefix.get("id", identity)
    ordinal = prefix.get("mail-ordinal-v1", identity)
    if row is None or row.get("kind") != "message_envelope" or type(ordinal) is not int:
        raise send_prefix.CacheMiss()
    return dict(row, _mail_ordinal=ordinal)


class Tree:
    def __init__(self, read, write):
        self.read, self.write = read, write
        self.memo = {}

    def node(self, digest):
        if digest not in self.memo:
            value = self.read(digest)
            if (type(value) is not dict
                    or hashlib.sha256(send_prefix._bytes(value)).hexdigest() != digest):
                raise send_prefix.CacheMiss()
            self.memo[digest] = value
        return self.memo[digest]

    def put(self, value):
        digest = hashlib.sha256(send_prefix._bytes(value)).hexdigest()
        self.write(digest, value)
        self.memo[digest] = value
        return digest

    def branch(self, prefix, children):
        rows = [self.node(children[bit]) for bit in ("0", "1")]
        return self.put({"prefix": prefix, "children": children,
            "count": sum(row["count"] for row in rows),
            "oldest_utc": min((row["oldest_utc"] for row in rows), key=_utc),
            "oldest_lex": min((row["oldest_lex"] for row in rows),
                key=lambda row: (row["timestamp"], row["ordinal"]))})

    def add(self, root, envelope):
        selected = item(envelope)
        key = send_prefix._key("mail-member-v1", selected["id"])
        def insert(digest):
            if digest is None:
                return self.put({"prefix": key, "item": selected, "count": 1,
                    "oldest_utc": selected["timestamp"], "oldest_lex": selected})
            node = self.node(digest)
            prefix = node["prefix"]
            common = 0
            while common < min(len(prefix), len(key)) and prefix[common] == key[common]:
                common += 1
            if common < len(prefix):
                leaf = insert(None)
                return self.branch(key[:common], {prefix[common]: digest, key[common]: leaf})
            if prefix == key:
                if node.get("item") != selected:
                    raise send_prefix.CacheMiss()
                return digest
            children = dict(node["children"])
            children[key[len(prefix)]] = insert(children[key[len(prefix)]])
            return self.branch(prefix, children)
        return insert(root)

    def build(self, envelopes):
        """Build only reachable final nodes for one complete cold set."""
        values = sorted((send_prefix._key("mail-member-v1", row["id"]), row)
            for row in envelopes)
        def build(items):
            if not items:
                return None
            if len(items) == 1:
                return self.add(None, items[0][1])
            first, last = items[0][0], items[-1][0]
            common = 0
            while common < len(first) and first[common] == last[common]:
                common += 1
            if common == len(first):
                raise send_prefix.CacheMiss()
            pivot = next(index for index, (key, _row) in enumerate(items) if key[common] == "1")
            return self.branch(first[:common], {"0": build(items[:pivot]), "1": build(items[pivot:])})
        return build(values)

    def count(self, root):
        return 0 if root is None else self.node(root)["count"]

    def difference(self, left, other, right):
        """Exact count and timestamp extrema of left minus right.

        Identical or disjoint subtrees are answered from their authenticated
        summaries. No cardinality subtraction assumes an unproved subset.
        """
        empty = {"count": 0, "oldest_utc": None, "oldest_lex": None}
        def whole(digest):
            if digest is None:
                return dict(empty)
            row = self.node(digest)
            return {name: row[name] for name in empty}
        def merge(rows):
            rows = [row for row in rows if row["count"]]
            return dict(empty) if not rows else {"count": sum(row["count"] for row in rows),
                "oldest_utc": min((row["oldest_utc"] for row in rows), key=_utc),
                "oldest_lex": min((row["oldest_lex"] for row in rows),
                    key=lambda row: (row["timestamp"], row["ordinal"]))}
        def subtract(a, b):
            if a is None or a == b:
                return dict(empty)
            if b is None:
                return whole(a)
            x, y = self.node(a), other.node(b)
            p, q = x["prefix"], y["prefix"]
            if not (p.startswith(q) or q.startswith(p)):
                return whole(a)
            if p == q:
                if "item" in x:
                    if x["item"] != y.get("item"):
                        raise send_prefix.CacheMiss()
                    return dict(empty)
                return merge([subtract(x["children"][bit], y["children"][bit])
                    for bit in ("0", "1")])
            if p.startswith(q):
                return subtract(a, y["children"][p[len(q)]])
            bit = q[len(p)]
            return merge([subtract(x["children"][bit], b), whole(x["children"][str(1-int(bit))])])
        return subtract(left, right)


def memory_tree(entries):
    def read(digest):
        return entries[send_prefix._key(TREE_DOMAIN, digest)]
    def write(digest, value):
        entries[send_prefix._key(TREE_DOMAIN, digest)] = value
    return Tree(read, write)


def bank_tree(prefix):
    from .key_prefix import KeyPrefix
    if isinstance(prefix, KeyPrefix):
        def read(digest):
            # The checkpoint-authenticated root commits this exact key/value.
            # Unlike get(), this remains usable inside the append transaction.
            with prefix._scope():
                return prefix._read_entry(send_prefix._key(TREE_DOMAIN, digest))
    else:
        read = lambda digest: prefix.get(TREE_DOMAIN, digest)
    return Tree(read, lambda digest, value: prefix.set(TREE_DOMAIN, digest, value))


def empty_node():
    return {"ordinary": None, "planes": {}, "last": None}


def event_entries(rows, base_count=None):
    """Cold final trees, followed by the certified ordinary append tail.

    The source checkpoint seals the physical cold cutoff. This retains exact
    append history without manufacturing superseded nodes during recovery.
    """
    if base_count is None:
        base_count = len(rows)
    if type(base_count) is not int or not 0 <= base_count <= len(rows):
        raise send_prefix.CacheMiss()
    entries, nodes, recipients, senders = {}, {}, {}, {}
    tree = memory_tree(entries)
    for ordinal, row in enumerate(rows[:base_count]):
        if row["kind"] != "message_envelope":
            continue
        row = dict(row, _mail_ordinal=ordinal)
        entries[send_prefix._key("mail-ordinal-v1", row["id"])] = ordinal
        for node in sorted({row["sender"], row["recipient"]}):
            nodes.setdefault(node, empty_node())["last"] = row["timestamp"]
        value = nodes[row["recipient"]]
        plane = row.get("worker_session_id")
        label = json.dumps(plane, separators=(",", ":"))
        recipients.setdefault((row["recipient"], label), []).append(row)
        senders.setdefault(row["sender"], []).append(row)
    for (node, label), envelopes in sorted(recipients.items()):
        root = tree.build(envelopes)
        nodes[node]["planes"][label] = root
        if json.loads(label) is None:
            nodes[node]["ordinary"] = root
    entries.update({send_prefix._key(NODE_DOMAIN, node): value for node, value in nodes.items()})
    entries.update({send_prefix._key(SENDER_DOMAIN, node): tree.build(envelopes)
        for node, envelopes in sorted(senders.items())})
    if base_count != len(rows):
        class MemoryPrefix:
            payload = {"count": base_count}
            def get(self, domain, value):
                return entries.get(send_prefix._key(domain, value))
            def set(self, domain, value, row):
                entries[send_prefix._key(domain, value)] = row
        advance_events(MemoryPrefix(), rows[base_count:])
    return entries


def advance_events(prefix, rows):
    tree = bank_tree(prefix)
    for ordinal, row in enumerate(rows, start=prefix.payload["count"]):
        if row["kind"] != "message_envelope":
            continue
        row = dict(row, _mail_ordinal=ordinal)
        prefix.set("mail-ordinal-v1", row["id"], ordinal)
        for node in sorted({row["sender"], row["recipient"]}):
            value = prefix.get(NODE_DOMAIN, node) or empty_node()
            value = json.loads(json.dumps(value))
            value["last"] = row["timestamp"]
            if node == row["recipient"]:
                label = json.dumps(row.get("worker_session_id"), separators=(",", ":"))
                value["planes"][label] = tree.add(value["planes"].get(label), row)
                if row.get("worker_session_id") is None:
                    value["ordinary"] = value["planes"][label]
            prefix.set(NODE_DOMAIN, node, value)
        prefix.set(SENDER_DOMAIN, row["sender"], tree.add(
            prefix.get(SENDER_DOMAIN, row["sender"]), row))


def empty_cursor():
    return {"members": None, "visible": None, "delivery_rows": 0,
        "ack_rows": 0, "unpresented_rows": 0, "latest": None, "latest_utc": None}


def cursor_fold(rows, envelopes, entries, prior=None, tree=None):
    value = dict(empty_cursor() if prior is None else prior)
    tree = memory_tree(entries) if tree is None else tree
    for row in rows:
        value["latest"] = row["timestamp"]
        if value["latest_utc"] is None or _utc(row["timestamp"]) > _utc(value["latest_utc"]):
            value["latest_utc"] = row["timestamp"]
        for identity in row["item_ids"]:
            envelope = envelopes[identity]
            value["members"] = tree.add(value["members"], envelope)
            if row["kind"] in {"delivery_receipt", "delivered_unpresented"}:
                value["visible"] = tree.add(value["visible"], envelope)
        if row["kind"] == "delivery_receipt":
            value["delivery_rows"] += 1
        if row["kind"] == "ack_receipt":
            value["ack_rows"] += 1
        if row["kind"] == "delivered_unpresented":
            value["unpresented_rows"] += 1
    return value


def _load_events(root):
    home = root.tenant_home / ".send-prefix"
    if not (home / "checkpoint.json").is_file() or not (home / "seal.key").is_file():
        raise send_prefix.CacheMiss()
    from .events import EVENT_KINDS
    prefix = send_prefix.Prefix(root, EVENT_KINDS, read_only=True)
    prefix.load()
    if prefix.get("mail-valid-v1", "complete") is not True:
        raise send_prefix.CacheMiss()
    first = prefix.get("mail-first-v1", "physical")
    if (prefix.payload["count"] == 0 and first is not None
            or prefix.payload["count"] != 0 and type(first) is not dict):
        raise send_prefix.CacheMiss()
    if first is not None:
        if prefix.get("id", first["id"]) != first:
            raise send_prefix.CacheMiss()
        # Imported epoch lineage remains the native reader's jurisdiction.
        # Its integrity includes archive files not covered by a live source
        # checkpoint. Do not turn an old archive join into a fresh fact.
        if first.get("kind") == "bus_epoch_roll_receipt":
            raise send_prefix.CacheMiss()
    return prefix


def warm(root):
    """Complete native cursor joins outside the measured observation."""
    from .cursor_prefix import warm as warm_cursor
    from .consumed_prefix import warm as warm_consumed
    from .events import EventLog
    nodes = {row["recipient"] for row in EventLog(root).records()}
    for family in ("deliveries", "acks", "consumed"):
        directory = root.resolve_relative(Path("receipts") / family)
        if directory.is_dir():
            nodes.update(path.stem if path.is_file() else path.name for path in directory.iterdir()
                if path.is_dir() or path.suffix == ".jsonl")
    for node in sorted(nodes):
        rows = EventLog(root).records()
        planes = {None} | {row.get("worker_session_id") for row in rows if row["recipient"] == node}
        for plane in sorted(planes, key=lambda value: "" if value is None else value):
            warm_cursor(root, node, worker_session_id=plane)
        if root.resolve_relative(Path("receipts/consumed") / (node + ".jsonl")).exists() or root.resolve_relative(Path("receipts/consumed") / node).exists():
            warm_consumed(root, node)


class Observation:
    def __init__(self, root, events):
        self.root, self.events = root, events
        self.tree = bank_tree(events)
        self._identities = {}
        self._sources = {}
        self.receipt_counts = {"delivery": 0, "ack": 0}
        self.unrecognized_kinds = []
        self._planes = {}
        self._consumed_proofs = []
        self._remember(events)
        self._discover()

    def current_epoch_has_no_roll_head(self):
        first = self.events.get("mail-first-v1", "physical")
        return self.current() and (first is None or first.get("kind") != "bus_epoch_roll_receipt")

    def _remember(self, prefix):
        for path in (prefix.path, prefix.home, prefix.nodes, prefix.staging,
                     prefix.home / "checkpoint.json", prefix.home / "seal.key"):
            self._identities[path] = send_prefix._identity(path)
        if hasattr(prefix, "bank"):
            self._identities[prefix.bank] = send_prefix._identity(prefix.bank)
        if hasattr(prefix, "directory"):
            self._identities[prefix.buckets] = send_prefix._identity(prefix.buckets)
            path = prefix.home / "directory.bin"
            self._identities[path] = send_prefix._identity(path)
        relative = (prefix.home / "checkpoint.json").relative_to(self.root.tenant_home)
        payload = prefix.payload
        mail = dict(payload.get("folds", {}).get("mail", {}))
        mail.pop("event_generation", None)
        dependencies = mail.pop("dependencies", None)
        if dependencies is not None and dependencies["work"] is not None:
            work = dependencies["work"]
            self._identities[self.root.resolve_relative(work["path"])] = work["identity"]
        witness = {"version": VERSION, "source": prefix.path.relative_to(self.root.tenant_home).as_posix(),
            "count": payload["count"], "head": payload["head"], "mail": mail}
        if "trie" in payload:
            witness["trie"] = payload["trie"]
        self._sources[relative] = send_prefix._bytes(witness) + b"\n"

    def _discover(self):
        # Membership-plane population itself participates in the witness.
        self._population = self._paths()
        from .cursor_prefix import AckPrefix, DeliveryPrefix
        from .cursor import SparseCursor
        from .consumed_prefix import ConsumedPrefix
        cursor = SparseCursor(self.root)
        for relative in self._population:
            parts = relative.parts
            family, node = parts[1], parts[2].removesuffix(".jsonl")
            if len(parts) == 4:
                node = parts[2]
            value = self.events.get(NODE_DOMAIN, node) or empty_node()
            candidates = [json.loads(label) for label in value["planes"]]
            candidates.append(None)
            plane = next((candidate for candidate in candidates if (
                cursor._relative_path_for(node, worker_session_id=candidate) if family == "acks"
                else cursor._delivery_relative_path_for(node, worker_session_id=candidate) if family == "deliveries"
                else Path("receipts/consumed").joinpath(*cursor._relative_path_for(node, worker_session_id=candidate).relative_to("receipts/acks").parts)) == relative), ...)
            if plane is ...:
                raise send_prefix.CacheMiss()
            if family == "consumed":
                prefix = ConsumedPrefix(self.root, relative, node, read_only=True)
                prefix.load()
                prefix.check_mail_sources(self.events)
                self._consumed_proofs.append(prefix)
            else:
                # Constructors obtain their dependency testimony through the
                # explicitly supplied read-only event/delivery certificates.
                proof = send_prefix._SelectedRows([], self.events, None, None)
                delivery_relative = cursor._delivery_relative_path_for(node, worker_session_id=plane)
                delivery = DeliveryPrefix(self.root, delivery_relative, node, plane, proof, read_only=True)
                delivery.load()
                delivery.check_sources()
                prefix = delivery if family == "deliveries" else AckPrefix(self.root, relative,
                    worker_session_id=plane, sources=delivery, read_only=True)
                if family == "acks":
                    prefix.load()
                    prefix.check_sources()
                    if prefix.payload["folds"]["mail"].get("health_safe") is not True:
                        raise send_prefix.CacheMiss()
                self._remember(delivery)
            self._remember(prefix)
            folds = prefix.payload["folds"]["mail"]
            self._planes.setdefault(node, {}).setdefault(plane, {})[family] = (prefix, folds)
            self.receipt_counts["delivery"] += folds.get("delivery_rows", 0) if len(parts) == 3 else 0
            self.receipt_counts["ack"] += folds.get("ack_rows", 0) if len(parts) == 3 else 0

    def _paths(self):
        result = []
        for family in ("deliveries", "acks", "consumed"):
            directory = self.root.resolve_relative(Path("receipts") / family)
            if directory.is_dir():
                result.extend(path.relative_to(self.root.tenant_home) for path in directory.glob("*.jsonl"))
                result.extend(path.relative_to(self.root.tenant_home) for path in directory.glob("*/*.jsonl"))
        return tuple(sorted(result))

    def current(self):
        from .bus_epoch import epoch_guard
        with epoch_guard(self.root, exclusive=False):
            if self._paths() != self._population or any(send_prefix._identity(path) != identity
                    for path, identity in self._identities.items()):
                return False
            try:
                for prefix in self._consumed_proofs:
                    prefix.check_mail_sources(self.events)
            except send_prefix._CACHE_ERRORS:
                return False
            return True

    def sources(self):
        if not self.current():
            raise send_prefix.CacheMiss()
        return tuple(sorted(self._sources.items()))

    def health_projection(self, nodes):
        """Recover exact health inputs only when every receipt is accounted for.

        First-physical message indexes do not certify timestamp minima. Their
        rows are usable here only if their distinct native IDs exhaust each
        sealed source count; otherwise the caller must use native history.
        No new persisted summaries or membership entries are written.
        """
        if not self.current():
            raise send_prefix.CacheMiss()
        # Event mail-tree values are already content-addressed in the event
        # bank. The authenticated recipient root commits these exact values;
        # their hashes need no second lookup through the domain-key trie.
        tree = Tree(self.events._node, None)
        events, receipts = [], {}
        ordinals = set()
        for node in nodes:
            value = self.events.get(NODE_DOMAIN, node) or empty_node()
            if type(value) is not dict or type(value.get("planes")) is not dict:
                raise send_prefix.CacheMiss()
            node_events, delivery_rows, ack_rows = [], [], []
            for label, digest in value["planes"].items():
                plane = json.loads(label)
                envelopes = self._health_members(tree, digest, node, ordinals)
                node_events.extend(envelopes)
                delivery_rows.extend(self._health_receipts(node, plane, "deliveries", envelopes))
                ack_rows.extend(self._health_receipts(node, plane, "acks", envelopes))
            # A source can exist with no current envelopes (including an
            # empty-item receipt). Its physical count still requires coverage.
            for plane in self._planes.get(node, {}):
                if json.dumps(plane, separators=(",", ":")) not in value["planes"]:
                    self._health_receipts(node, plane, "deliveries", [])
                    self._health_receipts(node, plane, "acks", [])
            node_events.sort(key=lambda row: row["_mail_ordinal"])
            events.extend(node_events)
            receipts[node] = {"delivery_times": self._health_times(delivery_rows),
                "acknowledgment_times": self._health_times(ack_rows),
                "latest_delivery_at": max((datetime.fromisoformat(
                    row["timestamp"].replace("Z", "+00:00")) for row in delivery_rows), default=None)}
        events.sort(key=lambda row: row["_mail_ordinal"])
        if not self.current():
            raise send_prefix.CacheMiss()
        return events, receipts

    def _health_members(self, tree, digest, recipient, ordinals):
        root = digest
        result, seen = [], set()
        pending = [] if digest is None else [(digest, "")]
        maximum = self.events.payload["count"]
        if type(maximum) is not int or maximum < 0:
            raise send_prefix.CacheMiss()
        while pending:
            digest, parent = pending.pop()
            if digest in seen or len(seen) >= 2 * maximum:
                raise send_prefix.CacheMiss()
            seen.add(digest)
            row = tree.node(digest)
            prefix = row["prefix"]
            if (type(prefix) is not str or not prefix.startswith(parent)
                    or len(prefix) > 256 or any(bit not in "01" for bit in prefix)):
                raise send_prefix.CacheMiss()
            if "item" in row:
                member = row["item"]
                if (type(member) is not dict or type(member.get("id")) is not str
                        or type(member.get("timestamp")) is not str):
                    raise send_prefix.CacheMiss()
                ordinal = member["ordinal"]
                if (row["count"] != 1 or type(ordinal) is not int
                        or not 0 <= ordinal < maximum or ordinal in ordinals
                        or prefix != send_prefix._key("mail-member-v1", member["id"])):
                    raise send_prefix.CacheMiss()
                ordinals.add(ordinal)
                result.append({"kind": "message_envelope", "recipient": recipient,
                    "id": member["id"], "timestamp": member["timestamp"], "_mail_ordinal": ordinal})
            else:
                children = row["children"]
                if (set(children) != {"0", "1"} or len(prefix) >= 256
                        or row["count"] != sum(tree.node(children[bit])["count"] for bit in ("0", "1"))):
                    raise send_prefix.CacheMiss()
                pending.extend((children[bit], prefix + bit) for bit in ("1", "0"))
        if len(result) != tree.count(root):
            raise send_prefix.CacheMiss()
        return result

    def _health_receipts(self, node, plane, family, envelopes):
        selected = self._planes.get(node, {}).get(plane, {}).get(family)
        if selected is None:
            return []
        prefix, _folds = selected
        domain = "delivered" if family == "deliveries" else "message"
        rows = {}
        for envelope in envelopes:
            row = prefix.get(domain, envelope["id"])
            if row is None:
                continue
            if (type(row) is not dict or type(row.get("id")) is not str
                    or type(row.get("item_ids")) is not list):
                raise send_prefix.CacheMiss()
            identity = row["id"]
            if (row.get("recipient") != node or envelope["id"] not in row["item_ids"]
                    or row.get("kind") not in ({"delivery_receipt", "delivered_unpresented"}
                        if family == "deliveries" else {"ack_receipt"})):
                raise send_prefix.CacheMiss()
            if identity not in rows:
                if prefix.get("id", identity) != row:
                    raise send_prefix.CacheMiss()
                rows[identity] = row
            elif rows[identity] != row:
                raise send_prefix.CacheMiss()
        if len(rows) != prefix.payload["count"]:
            raise send_prefix.CacheMiss()
        prefix._assert_current()
        return list(rows.values())

    @staticmethod
    def _health_times(rows):
        result = {}
        for row in rows:
            if (type(row.get("timestamp")) is not str
                    or any(type(identity) is not str for identity in row["item_ids"])):
                raise send_prefix.CacheMiss()
            moment = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            for identity in row["item_ids"]:
                if identity not in result or moment < result[identity]:
                    result[identity] = moment
        return result

    def node(self, node):
        value = self.events.get(NODE_DOMAIN, node) or empty_node()
        result = {"envelope_count": 0, "inbox_depth": 0,
            "message_progress": {"delivered": 0, "acknowledged": 0, "consumed": 0},
            "undelivered_count": 0, "oldest_unread_at": None, "oldest_waiting": None,
            "last_event_activity": value["last"], "unpresented_count": 0,
            "latest_delivery_at": None}
        for rows in self._planes.get(node, {}).values():
            if "deliveries" in rows:
                result["unpresented_count"] += rows["deliveries"][1]["unpresented_rows"]
        for label, event_root in value["planes"].items():
            plane = json.loads(label)
            rows = self._planes.get(node, {}).get(plane, {})
            result["envelope_count"] += self.tree.count(event_root)
            delivery, delivered = rows.get("deliveries", (None, empty_cursor()))
            ack, acknowledged = rows.get("acks", (None, empty_cursor()))
            consumed, consumption = rows.get("consumed", (None, {"members": None}))
            d_tree = self.tree if delivery is None else bank_tree(delivery)
            a_tree = self.tree if ack is None else bank_tree(ack)
            pending = self.tree.difference(event_root, d_tree, delivered["members"])
            result["undelivered_count"] += pending["count"]
            if pending["oldest_utc"] is not None and (result["oldest_unread_at"] is None
                    or _utc(pending["oldest_utc"]) < _utc(result["oldest_unread_at"])):
                result["oldest_unread_at"] = pending["oldest_utc"]
            if pending["oldest_lex"] is not None and (result["oldest_waiting"] is None
                    or (pending["oldest_lex"]["timestamp"], pending["oldest_lex"]["ordinal"]) <
                    (result["oldest_waiting"]["timestamp"], result["oldest_waiting"]["ordinal"])):
                result["oldest_waiting"] = pending["oldest_lex"]
            if plane is None:
                result["inbox_depth"] -= a_tree.count(acknowledged["members"])
            result["inbox_depth"] += self.tree.count(event_root)
            result["message_progress"]["delivered"] += d_tree.count(delivered["members"])
            result["message_progress"]["acknowledged"] += a_tree.count(acknowledged["members"])
            if consumed is not None:
                c_tree = bank_tree(consumed)
                # Current membership is certified at rebuild and bound to the
                # event generation; the exact difference still prevents an
                # archive-only or cross-plane member becoming a live count.
                outside = c_tree.difference(consumption["members"], self.tree, event_root)
                if outside["count"]:
                    raise send_prefix.CacheMiss()
                result["message_progress"]["consumed"] += c_tree.count(consumption["members"])
            result["latest_delivery_at"] = delivered["latest"] or result["latest_delivery_at"]
        if not self.current():
            raise send_prefix.CacheMiss()
        if result["oldest_waiting"] is not None:
            result["oldest_waiting"] = {key: result["oldest_waiting"][key] for key in ("id", "timestamp")}
        return result


def observe(root):
    try:
        from .bus_epoch import epoch_guard
        with epoch_guard(root, exclusive=False):
            result = Observation(root, _load_events(root))
            return result if result.current() else None
    except send_prefix._CACHE_ERRORS + (ProtocolRefusal,):
        # Optional discovery can need stricter coordinates than a native
        # reader (for example an unrelated empty receipt filename). Preserve
        # that reader's validation and refusal boundary through fallback.
        return None
