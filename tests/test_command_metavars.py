"""Readable value labels are declared once on executable parser arguments."""
import unittest
from floati.cli import _parser
from floati.command_contract import describe_parser
from floati.command_codegen import help_pages, verb_table

class CommandMetavarTests(unittest.TestCase):
    def test_reviewed_words_project_from_argument_declarations(self):
        parser = _parser()
        rows = {' '.join(row['path']): row for row in describe_parser(parser)['commands']}
        pages = help_pages(parser)
        table = verb_table(parser)
        for topic, option, word in (
            ('context status', '--as', 'NODE'),
            ('context policy clear', '--idempotency-key', 'KEY'),
            ('snapshot', '--out', 'PATH'),
            ('snapshot', '--lines', 'N'),
            ('intake adopt', '--from', 'DIR'),
            ('intake adopt', '--path', 'RELATIVE'),
            ('intake adopt', '--repo', 'O/R'),
            ('intake adopt', '--issue', 'N'),
            ('intake adopt', '--gh', 'EXE'),
            ('intake preview', '--label', 'NAME'),
            ('intake preview', '--pr', 'N'),
        ):
            with self.subTest(topic=topic, option=option):
                argument = next(a for a in rows[topic]['arguments'] if option in a['option_strings'])
                self.assertEqual(word, argument.get('metavar'))
                self.assertIn(option + ' ' + word, pages[topic])
                self.assertIn(option + ' ' + word, table)
    def test_choices_remain_visible_and_unchanged(self):
        parser = _parser()
        self.assertIn('--source {local,github}', help_pages(parser)['intake adopt'])
        rows = describe_parser(parser)['commands']
        row = next(r for r in rows if r['path'] == ['intake','adopt'])
        self.assertEqual(['local','github'], next(a for a in row['arguments'] if a['name']=='source')['choices'])

    def test_positional_identity_labels_are_declared_readably(self):
        rows = describe_parser(_parser())['commands']
        for path, word in ((['register'],'NODE'), (['retire'],'NODE'), (['receipts'],'NODE'), (['role','show'],'ROLE')):
            row = next(r for r in rows if r['path']==path)
            argument = next(a for a in row['arguments'] if a['positional'])
            self.assertEqual(word, argument['metavar'])
