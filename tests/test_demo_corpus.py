from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "docs" / "demo" / "corpus.v0.jsonl"
BUS_KEYS = {
    "kind",
    "class",
    "root",
    "tenant",
    "ids",
    "frozen",
    "ledger_sha256",
    "note",
}
CAPTURE_KEYS = {
    "kind",
    "class",
    "path",
    "asset_sha256",
    "captured_from",
    "source_sha",
    "frozen",
    "note",
}
ID = re.compile(r"^(?:msg|delivery|ack)-[0-9a-f]+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
SELECTED_SOURCE_SHA = "b5b8987f253b1f9245035edb8dd426c83e9d5d79"
SELECTED_RESTAMP_SOURCE_SHA = "e2d996407f63a0d72a2427ebdbf5f3c0d248797e"
SELECTED_CAPTURE_ROWS = [
    {
        "kind": "capture_ref",
        "class": "capture-gif",
        "path": "docs/demo/hero-three-fault-replay.gif",
        "asset_sha256": "94af4a20c8f95e2b8250eaba9f92185e4bcd3cfc5acfa492e29916dc5ddc9e89",
        "captured_from": "deterministic three-fault replay fixture",
        "source_sha": SELECTED_SOURCE_SHA,
        "frozen": True,
        "note": "Fable-selected loop-clean hero GIF.",
    },
    {
        "kind": "capture_ref",
        "class": "capture-gif",
        "path": "docs/demo/board-glow.gif",
        "asset_sha256": "e376dabb63d05ee10218149ddd0406d3a44929174f5d85befb4d5ec8d0635111",
        "captured_from": "deterministic Floati fleet fixture",
        "source_sha": SELECTED_RESTAMP_SOURCE_SHA,
        "frozen": True,
        "note": "Fable-selected Harbor Board glow GIF.",
    },
    {
        "kind": "capture_ref",
        "class": "capture-gif",
        "path": "docs/demo/harbor-chart-map.gif",
        "asset_sha256": "f2423e8b2b0b941ceff565cab79fef67e499755bcc5215ec92d4e2da104b4ddc",
        "captured_from": "deterministic Floati fleet fixture",
        "source_sha": SELECTED_SOURCE_SHA,
        "frozen": True,
        "note": "Fable-selected Harbor Chart map GIF.",
    },
    {
        "kind": "capture_ref",
        "class": "capture-gif",
        "path": "docs/demo/install-moment.gif",
        "asset_sha256": "6af24cefe63038b11ec4986db472da7dddcf4e9646686846b80ab88f97cdbfa2",
        "captured_from": "committed-tree Floati install receipt",
        "source_sha": SELECTED_RESTAMP_SOURCE_SHA,
        "frozen": True,
        "note": "Fable-selected install moment GIF.",
    },
    {
        "kind": "capture_ref",
        "class": "capture-master",
        "path": "/private/tmp/floati-demo-masters/b5b8987f253b1f9245035edb8dd426c83e9d5d79/hero-three-fault-replay.mp4",
        "asset_sha256": "25d35c7c6abaea1de8180b1e8e2d2bd3315e5d123bbabb24d0cbac0b8e291296",
        "captured_from": "deterministic three-fault replay fixture",
        "source_sha": SELECTED_SOURCE_SHA,
        "frozen": True,
        "note": "Local-only 4K master; not committed.",
    },
]


def _require_one_line(value: object, label: str) -> None:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise AssertionError(f"{label} must be one non-empty line")


def _is_canonical_bus_root(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if value.startswith("~/"):
        components = value[2:].split("/")
    elif value.startswith("/"):
        components = value[1:].split("/")
    else:
        return False
    return bool(components) and all(
        component and component not in {".", ".."} for component in components
    )


def validate_row(row: object) -> None:
    if not isinstance(row, dict):
        raise AssertionError("corpus row must be an object")
    if row.get("kind") != "capture_ref":
        raise AssertionError("kind must be capture_ref")

    row_class = row.get("class")
    if row_class == "bus-evidence":
        if set(row) != BUS_KEYS:
            raise AssertionError("corpus row keys must match v0 exactly")
        if not _is_canonical_bus_root(row["root"]):
            raise AssertionError("root must be canonical absolute or home-relative")
        if not isinstance(row["tenant"], str) or not row["tenant"]:
            raise AssertionError("tenant must be non-empty")
        ids = row["ids"]
        if (
            not isinstance(ids, list)
            or not ids
            or len(ids) != len(set(ids))
            or any(
                not isinstance(item, str) or ID.fullmatch(item) is None
                for item in ids
            )
        ):
            raise AssertionError("ids must be a non-empty unique durable-id list")
        if type(row["frozen"]) is not bool:
            raise AssertionError("frozen must be boolean")
        digest = row["ledger_sha256"]
        if row["frozen"]:
            if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
                raise AssertionError("frozen rows require one lowercase SHA-256")
        elif digest is not None:
            raise AssertionError("live rows must not hash a growing ledger")
        _require_one_line(row["note"], "note")
        return

    if row_class not in {"capture-gif", "capture-master"}:
        raise AssertionError("capture class is not ruled")
    if set(row) != CAPTURE_KEYS:
        raise AssertionError("corpus row keys must match v0 capture class exactly")

    asset_path = row["path"]
    if not isinstance(asset_path, str) or not asset_path:
        raise AssertionError("capture path must be non-empty")
    candidate = Path(asset_path)
    if row_class == "capture-gif":
        if (
            candidate.is_absolute()
            or candidate.parts[:2] != ("docs", "demo")
            or len(candidate.parts) < 3
            or any(part in {".", ".."} for part in candidate.parts)
            or candidate.suffix != ".gif"
        ):
            raise AssertionError("capture-gif path must be canonical under docs/demo")
    elif not candidate.is_absolute() or candidate.suffix != ".mp4":
        raise AssertionError("capture-master path must be an absolute MP4 path")

    digest = row["asset_sha256"]
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
        raise AssertionError("capture assets require one lowercase SHA-256")
    source_sha = row["source_sha"]
    if not isinstance(source_sha, str) or SOURCE_SHA.fullmatch(source_sha) is None:
        raise AssertionError("capture source_sha must be lowercase 40-hex")
    if row["frozen"] is not True:
        raise AssertionError("capture rows must be frozen")
    _require_one_line(row["captured_from"], "captured_from")
    _require_one_line(row["note"], "note")


def valid_live_row() -> dict[str, object]:
    return {
        "kind": "capture_ref",
        "class": "bus-evidence",
        "root": "/absolute/root",
        "tenant": "puddle-fleet",
        "ids": ["msg-01a0088c08207e31b874d517621e164b"],
        "frozen": False,
        "ledger_sha256": None,
        "note": "One live durable reference.",
    }


def valid_capture_gif_row() -> dict[str, object]:
    return {
        "kind": "capture_ref",
        "class": "capture-gif",
        "path": "docs/demo/hero-three-fault-replay.gif",
        "asset_sha256": "1" * 64,
        "captured_from": "deterministic three-fault replay fixture",
        "source_sha": "2" * 40,
        "frozen": True,
        "note": "Selected loop-clean hero candidate.",
    }


def valid_capture_master_row() -> dict[str, object]:
    return {
        "kind": "capture_ref",
        "class": "capture-master",
        "path": "/private/tmp/floati-demo-masters/hero-three-fault-replay.mp4",
        "asset_sha256": "3" * 64,
        "captured_from": "deterministic three-fault replay fixture",
        "source_sha": "2" * 40,
        "frozen": True,
        "note": "Local-only 4K master; not committed.",
    }


class DemoCorpusContractTests(unittest.TestCase):
    def test_committed_manifest_matches_exact_v0_contract(self) -> None:
        self.assertTrue(CORPUS.is_file(), "committed v0 corpus must exist")
        rows = [
            json.loads(line)
            for line in CORPUS.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(11, len(rows))
        for row in rows:
            validate_row(row)
        self.assertEqual(8, sum(row["frozen"] for row in rows))
        self.assertEqual(3, sum(not row["frozen"] for row in rows))
        self.assertEqual(SELECTED_CAPTURE_ROWS, rows[6:])
        for row in SELECTED_CAPTURE_ROWS[:4]:
            asset = ROOT / str(row["path"])
            self.assertTrue(asset.is_file(), f"selected asset missing: {asset}")
            self.assertEqual(
                row["asset_sha256"],
                hashlib.sha256(asset.read_bytes()).hexdigest(),
            )

    def test_guard_rejects_unknown_keys(self) -> None:
        with self.assertRaisesRegex(AssertionError, "keys must match v0"):
            validate_row({**valid_live_row(), "extra": True})

    def test_guard_enforces_frozen_hash_pairs(self) -> None:
        invalid = [
            {**valid_live_row(), "frozen": True},
            {**valid_live_row(), "ledger_sha256": "0" * 64},
            {
                **valid_live_row(),
                "frozen": True,
                "ledger_sha256": "A" * 64,
            },
        ]
        for row in invalid:
            with self.subTest(row=row):
                with self.assertRaises(AssertionError):
                    validate_row(row)

    def test_guard_accepts_neutral_home_reference_and_refuses_noncanonical_variants(self) -> None:
        validate_row({**valid_live_row(), "root": "~/.floati-bus/local"})

        for root in ("~", "~/", "~operator/root", "~/../private", "relative/root"):
            with self.subTest(root=root):
                with self.assertRaisesRegex(AssertionError, "root must be canonical"):
                    validate_row({**valid_live_row(), "root": root})

    def test_guard_accepts_ruled_capture_classes(self) -> None:
        validate_row(valid_capture_gif_row())
        validate_row(valid_capture_master_row())

    def test_guard_refuses_unknown_and_cross_class_keys(self) -> None:
        invalid = [
            {**valid_capture_gif_row(), "class": "capture-video"},
            {**valid_capture_gif_row(), "extra": True},
            {
                key: value
                for key, value in valid_capture_master_row().items()
                if key != "asset_sha256"
            },
            {**valid_capture_gif_row(), "ledger_sha256": "4" * 64},
        ]
        for row in invalid:
            with self.subTest(row=row):
                with self.assertRaises(AssertionError):
                    validate_row(row)

    def test_guard_enforces_capture_path_classes(self) -> None:
        invalid = [
            {**valid_capture_gif_row(), "path": "/docs/demo/hero.gif"},
            {**valid_capture_gif_row(), "path": "docs/demo/../hero.gif"},
            {**valid_capture_gif_row(), "path": "docs/evidence/hero.gif"},
            {**valid_capture_gif_row(), "path": "docs/demo/hero.png"},
            {**valid_capture_master_row(), "path": "docs/demo/hero.mp4"},
            {**valid_capture_master_row(), "path": "/private/tmp/hero.mov"},
        ]
        for row in invalid:
            with self.subTest(row=row):
                with self.assertRaises(AssertionError):
                    validate_row(row)

    def test_guard_enforces_capture_provenance_and_freeze(self) -> None:
        invalid = [
            {**valid_capture_gif_row(), "asset_sha256": "A" * 64},
            {**valid_capture_gif_row(), "asset_sha256": "1" * 63},
            {**valid_capture_gif_row(), "source_sha": "2" * 39},
            {**valid_capture_gif_row(), "source_sha": "Z" * 40},
            {**valid_capture_gif_row(), "frozen": False},
            {**valid_capture_master_row(), "captured_from": ""},
            {**valid_capture_master_row(), "captured_from": "line one\nline two"},
            {**valid_capture_master_row(), "note": ""},
        ]
        for row in invalid:
            with self.subTest(row=row):
                with self.assertRaises(AssertionError):
                    validate_row(row)


if __name__ == "__main__":
    unittest.main()
