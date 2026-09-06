"""Boarding must remain visible in the generated operator contract."""
import unittest
from pathlib import Path
from floati.cli import _parser
from floati.command_contract import describe_parser
from floati.helptext import help_for


class SeatBoardHelpTests(unittest.TestCase):
    def test_boarding_pages_describe_and_generated_table_share_the_contract(self):
        rows = {' '.join(r['path']): r for r in describe_parser(_parser())['commands']}
        table = (Path(__file__).resolve().parents[1] / 'AGENTS.md').read_text()
        for topic in ('seat', 'seat board'):
            with self.subTest(topic=topic):
                page = help_for([*topic.split(), '--help'])
                self.assertIsNotNone(page)
                self.assertIn('floati ' + topic + ' - ', page)
                self.assertTrue(rows[topic]['public'])
                self.assertIn('| `' + topic + '` |', table)
        page = help_for(['seat', 'board', '--help'])
        self.assertIsNotNone(page)
        for option in ('--root', '--as', '--workspace', '--session', '--idempotency-key', '--take-over'):
            self.assertIn(option, page)
        self.assertIn('explicit', page)

    def test_boarding_value_labels_are_declared_on_parser_actions(self):
        row = next(r for r in describe_parser(_parser())['commands'] if r['path'] == ['seat', 'board'])
        for option, label in (('--root', 'ROOT'), ('--as', 'NODE'), ('--workspace', 'PATH'),
                              ('--session', 'SESSION'), ('--idempotency-key', 'KEY')):
            with self.subTest(option=option):
                argument = next(a for a in row['arguments'] if option in a['option_strings'])
                self.assertEqual(label, argument['metavar'])
                self.assertTrue(argument['required'])
