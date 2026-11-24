"""Native producer testimony and its versioned schema share closed shapes."""
from copy import deepcopy
from pathlib import Path
import json
import unittest

import tests
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.records import validate_record
from tests.schema_validation import SchemaValidationError, validate_json_schema
from tests import test_seat_execution as fixture

SCHEMAS = Path(__file__).resolve().parents[1] / 'schemas'


class SeatExecutionSchemaTests(unittest.TestCase):
    setUp = fixture.SeatExecutionTests.setUp
    car = fixture.SeatExecutionTests.car
    claim = fixture.SeatExecutionTests.claim
    controller = fixture.SeatExecutionTests.controller
    invocation = fixture.SeatExecutionTests.invocation

    def record(self):
        item = self.car(); self.claim(item)
        controller = self.controller(); invocation = self.invocation(controller, item)
        return controller.callback(invocation, {
            'event': 'ended', 'code': 'process_died', 'detail': 'BODY\n\x1b[2J'
        })['execution']

    def test_native_running_and_terminal_validate_in_version_one(self):
        self.record()
        for row in EventLog(self.root).event_records():
            if row['kind'] == 'seat_execution':
                validate_record(row, self.root.tenant_id, {'seat_execution'}, integrity=False)
                validate_json_schema(row, SCHEMAS / 'v1/seat-execution-record.schema.json')

    def test_source_event_pair_is_closed_in_native_and_schema(self):
        row = self.record()
        for source, event in (('gateway', 'ended'), ('cursor', 'ended'),
                              ('worker', 'followup_failed'), ('worker', 'refused_action'),
                              ('cursor', 'refused_action'), ('gateway', 'followup_failed')):
            bad = dict(row, source_kind=source, event=event)
            with self.subTest(source=source, event=event):
                with self.assertRaises(ProtocolRefusal):
                    validate_record(bad, self.root.tenant_id, {'seat_execution'}, integrity=False)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(bad, SCHEMAS / 'v1/seat-execution-record.schema.json')

    def test_work_car_is_v1_and_v0_contract_keeps_rejecting_car(self):
        item = self.car()
        validate_json_schema(item, SCHEMAS / 'v1/work-item-record.schema.json')
        downgraded = dict(item, schema_version=0)
        with self.assertRaises(ProtocolRefusal):
            validate_record(downgraded, self.root.tenant_id, {'work_item'}, integrity=False)
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(downgraded, SCHEMAS / 'v0/work-item-record.schema.json')

    def test_execution_shape_rejects_aliases_version_and_extra_coordinates(self):
        row = self.record()
        for changes in ({'schema_version': 0}, {'work_claim_id': 'invented'},
                        {'workspace': '\x2ftmp/../foreign'}, {'authenticated': True},
                        {'source_pin': 'not-a-source-pin'}, {'source_binding': {'run_id': 'forged'}}):
            bad = deepcopy(row); bad.update(changes)
            with self.subTest(changes=changes):
                with self.assertRaises(ProtocolRefusal):
                    validate_record(bad, self.root.tenant_id, {'seat_execution'}, integrity=False)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(bad, SCHEMAS / 'v1/seat-execution-record.schema.json')
