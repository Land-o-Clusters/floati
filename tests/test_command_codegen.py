"""WS-I I5: perturb the registry and each derived operator surface."""
from __future__ import annotations

import argparse
import unittest
from unittest import mock
from pathlib import Path

from floati.cli import _parser
from floati.command_contract import describe_parser


class CommandCodegenTests(unittest.TestCase):
    def generator(self):
        from floati import command_codegen
        return command_codegen

    def test_committed_help_and_agents_match_the_live_registry(self):
        generator = self.generator()
        self.assertEqual([], generator.check(Path(__file__).resolve().parents[1]))

    def test_registry_option_change_reaches_help_docs_and_describe(self):
        generator = self.generator()
        parser = _parser()
        commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        commands.choices['describe'].add_argument('--fixture-format', choices=['short', 'long'], required=True)
        pages = generator.help_pages(parser)
        table = generator.verb_table(parser)
        row = next(r for r in describe_parser(parser)['commands'] if r['path'] == ['describe'])
        self.assertIn('--fixture-format', pages['describe'])
        self.assertIn('--fixture-format', table)
        argument = next(a for a in row['arguments'] if '--fixture-format' in a['option_strings'])
        self.assertTrue(argument['required'])
        self.assertEqual(['short', 'long'], argument['choices'])
        for surface in (pages['describe'].split('DESCRIPTION', 1)[0], table):
            self.assertIn('--fixture-format {short,long}', surface)
            self.assertNotIn('[--fixture-format', surface)

    def test_generated_artifact_edits_are_detected(self):
        generator = self.generator()
        import tempfile
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            scratch = Path(temporary)
            (scratch / 'floati').mkdir()
            for name, content in generator.outputs(root).items():
                (scratch / name).write_text(content, encoding='utf-8')
            self.assertEqual([], generator.check(scratch))
            for name in ('floati/generated_help.py', 'AGENTS.md'):
                path = scratch / name
                original = path.read_text(encoding='utf-8')
                # Mutate generated grammar, not the intentionally preserved manual prose.
                if name == 'AGENTS.md':
                    before, generated = original.split(generator.BEGIN, 1)
                    altered = before + generator.BEGIN + generated.replace('--json', '--hand-edited', 1)
                else:
                    altered = original.replace('--json', '--hand-edited', 1)
                self.assertNotEqual(original, altered)
                path.write_text(altered, encoding='utf-8')
                self.assertIn(name, generator.check(scratch))
                path.write_text(original, encoding='utf-8')
            agents = scratch / 'AGENTS.md'
            original = agents.read_text(encoding='utf-8')
            authored = 'Operator-authored guidance stays outside generation.\n' + original
            agents.write_text(authored, encoding='utf-8')
            self.assertEqual([], generator.check(scratch))
            self.assertEqual(authored, generator.outputs(scratch)['AGENTS.md'])

    def test_hidden_grammar_is_absent_from_public_projections(self):
        generator = self.generator()
        parser = _parser()
        pages = generator.help_pages(parser)
        self.assertNotIn('wake-evaluate', pages)
        self.assertNotIn('wake-evaluate', pages[''])
        self.assertNotIn('wake daemon serve', generator.verb_table(parser))


class SyntheticGrammarTests(unittest.TestCase):
    def registry(self):
        parser = argparse.ArgumentParser(prog='floati', add_help=False)
        parser.floati_exit_codes = [{'code': 0, 'status': 'ok'}]
        parser.add_argument('--tenant', required=True)
        commands = parser.add_subparsers(required=True)
        parent = commands.add_parser('fixture', add_help=False)
        parent.add_argument('--scope', required=True)
        children = parent.add_subparsers(required=True)
        child = children.add_parser('inspect', add_help=False)
        group = child.add_mutually_exclusive_group(required=True)
        group.add_argument('--left', action='store_true')
        group.add_argument('--right', action='store_true')
        child.add_argument('--mode', '-m', choices=['brief', 'full'], required=True)
        child.add_argument('--pair', nargs=2, metavar=('FIRST', 'SECOND'))
        child.add_argument('--tag', '-t', action='append')
        child.add_argument('targets', nargs='+')
        hidden = children.add_parser('internal', add_help=False)
        hidden.floati_public = False
        hidden.add_argument('--secret-control')
        metadata = {
            topic: {'description': 'Inspect declared fixture data', 'body': 'No side effects.',
                    'option_docs': {}, 'notes': [], 'examples': 'floati fixture inspect --help'}
            for topic in ('', 'fixture', 'fixture inspect')
        }
        return parser, metadata

    def test_nested_grammar_preserves_constraints_and_ancestor_arguments(self):
        from floati import command_codegen
        parser, metadata = self.registry()
        with mock.patch('floati.help_copy.HELP_COPY', metadata):
            pages = command_codegen.help_pages(parser)
            table = command_codegen.verb_table(parser)
        synopsis = pages['fixture inspect'].split('SYNOPSIS\n    ', 1)[1].split('\n\n', 1)[0]
        self.assertLess(synopsis.index('--tenant'), synopsis.index('fixture'))
        self.assertLess(synopsis.index('--scope'), synopsis.index('inspect'))
        for fragment in ('(--left | --right)', '--mode {brief,full}',
                         '[--pair FIRST SECOND]', '[--tag TAG]', 'targets [targets ...]'):
            self.assertIn(fragment, synopsis)
            self.assertIn(fragment.replace('|', '&#124;'), table)
        self.assertNotIn('[--tenant', synopsis)
        self.assertNotIn('[--scope', synopsis)
        self.assertNotIn('[--mode', synopsis)
        self.assertIn('-m {brief,full}', pages['fixture inspect'])
        self.assertIn('-t TAG', pages['fixture inspect'])
        self.assertIn('repeatable', pages['fixture inspect'])
        parsed = parser.parse_args(['--tenant', 'declared', 'fixture', '--scope', 'local',
                                   'inspect', '--left', '-m', 'brief', '--pair', 'a', 'b',
                                   '-t', 'one', '-t', 'two', 'target'])
        self.assertEqual(['one', 'two'], parsed.tag)
        self.assertEqual(['a', 'b'], parsed.pair)
        self.assertEqual(['target'], parsed.targets)

    def test_hidden_nested_child_never_leaks_into_parent_usage_or_table(self):
        from floati import command_codegen
        parser, metadata = self.registry()
        with mock.patch('floati.help_copy.HELP_COPY', metadata):
            pages = command_codegen.help_pages(parser)
            table = command_codegen.verb_table(parser)
        self.assertEqual({'', 'fixture', 'fixture inspect'}, set(pages))
        for surface in (*pages.values(), table):
            self.assertNotIn('internal', surface)
            self.assertNotIn('--secret-control', surface)
        self.assertIn('{inspect}', pages['fixture'])

    def test_removed_option_metadata_refuses_instead_of_publishing_stale_grammar(self):
        from floati import command_codegen
        parser, metadata = self.registry()
        metadata['fixture inspect']['option_docs'] = {'--removed': 'Retired operation.'}
        with mock.patch('floati.help_copy.HELP_COPY', metadata):
            with self.assertRaisesRegex(ValueError, 'unregistered options.*--removed'):
                command_codegen.help_pages(parser)

    def test_help_routing_uses_parser_token_roles_without_running_handlers(self):
        from floati.helptext import help_for
        from unittest import mock
        cases = [(['role', 'show', 'builder', '--help'], 'role show'),
                 (['register', 'worker', '--help'], 'register'),
                 (['receipts', 'worker', '--help'], 'receipts'),
                 (['update', '--json', 'fleet', '--help'], 'update fleet'),
                 (['update', '--source', 'fleet', '--help'], 'update')]
        for arguments, topic in cases:
            with self.subTest(arguments=arguments):
                page = help_for(arguments)
                self.assertIsNotNone(page)
                self.assertTrue(page.splitlines()[1].startswith('    floati ' + topic + ' - '))
        for arguments in (['wake', 'daemon', 'serve', '--help'],
                          ['role', 'unknown-child', '--help']):
            self.assertIsNone(help_for(arguments))
