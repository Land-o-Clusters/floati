"""Explicit local-operator testimony about a node's person, never inferred."""

from pathlib import Path

from .bus_epoch import shared_epoch_operation
from .ids import uuid7_hex
from .jsonl import append_record, read_records_snapshot
from .registry import Registry, utc_now
from .root import validate_identifier


PERSON_PATH = Path("registry/persons.jsonl")
PERSON_KINDS = {"registry_person_record"}


def person_records(root):
    """Latest testimony per node, including clears, in canonical node order."""
    latest = {}
    for record in read_records_snapshot(root, PERSON_PATH, allowed_kinds=PERSON_KINDS):
        latest[record["node_id"]] = record
    return {node: latest[node] for node in sorted(latest)}


class Persons:
    def __init__(self, root):
        self.root = root

    @shared_epoch_operation
    def record(self, node, person):
        # Like register, this is an explicit local operator verb, not a seat
        # assertion. The epoch guard excludes changes during group fanout.
        node = Registry(self.root).resolve_node_id(node)
        if person is not None:
            person = validate_identifier(person, "person")
        record = {
            "schema_version": 1,
            "kind": "registry_person_record",
            "id": "registry-person-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "node_id": node,
            "person": person,
        }
        append_record(self.root, PERSON_PATH, record, allowed_kinds=PERSON_KINDS)
        return record
