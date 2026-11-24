"""Doctor and board project native car execution without elapsed-silence guesses."""
import json
import unittest

import tests
from tests import test_seat_execution as fixture


class SeatExecutionSurfaceTests(unittest.TestCase):
    setUp = fixture.SeatExecutionTests.setUp
    car = fixture.SeatExecutionTests.car
    claim = fixture.SeatExecutionTests.claim
    controller = fixture.SeatExecutionTests.controller
    invocation = fixture.SeatExecutionTests.invocation

    def stalled(self):
        item = self.car(); self.claim(item)
        controller = self.controller()
        invocation = self.invocation(controller, item)
        controller.callback(invocation, {'event': 'ended', 'code': 'process_died',
            'detail': 'exact native cleanup outcome'})

    def test_doctor_retains_native_state_and_cause(self):
        from floati.doctor import project_seat_execution_findings
        self.stalled()
        findings = project_seat_execution_findings(self.root)
        finding = next(row for row in findings if row['subject'] == 'builder')
        self.assertEqual('seat_execution_stalled', finding['code'])
        projection = json.loads(finding['detail'])
        self.assertEqual('process_died', projection['reason'])
        self.assertEqual('exact native cleanup outcome', projection['latest_execution']['detail'])

    def test_board_cached_and_fresh_paths_retain_execution_projection(self):
        from floati.tui import model_from_root
        from floati.tui_render import _detail_lines
        model_from_root(self.root)
        self.stalled()
        for unused in range(2):
            model = model_from_root(self.root)
            index = next(i for i, row in enumerate(model.nodes) if row['node_id'] == 'builder')
            projection = model.nodes[index]['car_execution']
            self.assertEqual('STALLED', projection['state'])
            self.assertEqual('process_died', projection['reason'])
            detail = '\n'.join(_detail_lines(model, 200, index))
            self.assertIn('STALLED', detail)
            self.assertIn('process_died', detail)

    def test_board_mail_does_not_turn_an_unclaimed_item_into_a_car(self):
        from floati.events import EventLog
        from floati.tui import model_from_root
        self.car()
        EventLog(self.root).send('architect', 'builder', repo='floati', sha='a' * 40,
            doc='docs/car.md', note='mail is not a claim')
        model = model_from_root(self.root)
        node = next(row for row in model.nodes if row['node_id'] == 'builder')
        self.assertEqual('NO_CAR', node['car_execution']['state'])

    def test_doctor_large_escaped_body_is_bounded_and_durable_body_stays_exact(self):
        from floati.doctor import project_seat_execution_findings
        from floati.events import EventLog
        item = self.car(); self.claim(item)
        controller = self.controller(); invocation = self.invocation(controller, item)
        body = '\x1b' * 8192
        accepted = controller.callback(invocation, {'event': 'ended',
            'code': 'process_died', 'detail': body})
        finding = next(row for row in project_seat_execution_findings(self.root)
                       if row['subject'] == 'builder')
        self.assertLessEqual(len(finding['detail']), 8192)
        preview = json.loads(finding['detail'])
        self.assertTrue(preview['latest_execution']['detail_truncated'])
        self.assertEqual(accepted['execution']['id'], preview['latest_execution']['id'])
        durable = next(row for row in EventLog(self.root).event_records()
                       if row['id'] == preview['latest_execution']['id'])
        self.assertEqual(body, durable['detail'])
