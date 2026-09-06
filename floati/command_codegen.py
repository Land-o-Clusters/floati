"""Generate operator documentation from the same registry as describe --json.

Run python3 -m floati.command_codegen --write after changing the registry or
help_copy metadata; --check detects stale checked-in projections without writes.
"""
from __future__ import annotations

import argparse
import json
import pprint
from pathlib import Path

BEGIN = '<!-- BEGIN GENERATED COMMAND TABLE -->'
END = '<!-- END GENERATED COMMAND TABLE -->'


def _registry(parser):
    rows = {'': (parser, ())}
    def visit(current, path, chain):
        for action in current._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            for name, child in action.choices.items():
                if not getattr(child, 'floati_public', True):
                    continue
                child_path = (*path, name)
                child_chain = (*chain, current)
                rows[' '.join(child_path)] = (child, child_chain)
                visit(child, child_path, child_chain)
    visit(parser, (), ())
    return rows


def _formatter():
    # Width is fixed: neither terminal dimensions nor COLUMNS affect artifacts.
    return argparse.HelpFormatter('floati', width=10000)


def _arguments(parser):
    return [a for a in parser._actions if not isinstance(a, argparse._SubParsersAction)]


def _grammar(parser, include_children=True):
    arguments = _formatter()._format_actions_usage(_arguments(parser), parser._mutually_exclusive_groups)
    children = []
    if include_children:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                names = [name for name, child in action.choices.items()
                         if getattr(child, 'floati_public', True)]
                if names:
                    choices = '{' + '|'.join(names) + '}'
                    children.append(choices if action.required else '[' + choices + ']')
    return ' '.join(part for part in (arguments, *children) if part)


def _synopsis(topic, current, chain):
    parts = ['floati']
    for name, parent in zip(topic.split(), chain):
        grammar = _grammar(parent, include_children=False)
        if grammar:
            parts.append(grammar)
        parts.append(name)
    grammar = _grammar(current)
    if grammar:
        parts.append(grammar)
    return ' '.join(parts)


def help_pages(parser):
    from .help_copy import HELP_COPY
    registry = _registry(parser)
    stale = set(HELP_COPY) - set(registry)
    if stale:
        raise ValueError('help metadata has unregistered public topics: ' + ', '.join(sorted(stale)))
    pages = {}
    exit_lines = '\n'.join('    {code} {status}'.format(**row)
                           for row in parser.floati_exit_codes)
    for topic, (current, chain) in registry.items():
        copy = HELP_COPY.get(topic)
        if copy is None:
            raise ValueError('help metadata is missing public topic: ' + topic)
        option_docs = copy['option_docs']
        actions = [action for member in (*chain, current) for action in _arguments(member)]
        declared = {name for action in actions for name in (action.option_strings or [action.dest])}
        stale_options = set(option_docs) - declared
        if stale_options:
            raise ValueError('help metadata has unregistered options for ' + topic + ': ' + ', '.join(sorted(stale_options)))
        details = []
        for action in actions:
            invocation = _formatter()._format_action_invocation(action)
            qualifiers = []
            if action.required:
                qualifiers.append('required')
            if isinstance(action, argparse._AppendAction):
                qualifiers.append('repeatable')
            if action.type in (int, float):
                qualifiers.append('integer' if action.type is int else 'number')
            if action.default is not None and action.default != argparse.SUPPRESS:
                qualifiers.append('default=' + json.dumps(action.default, sort_keys=True))
            explanation = next((option_docs[name] for name in (action.option_strings or [action.dest]) if name in option_docs), '')
            suffix = '; '.join(filter(None, [*qualifiers, explanation]))
            details.append('    ' + invocation + (' — ' + suffix if suffix else ''))
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                names = [name for name, child in action.choices.items() if getattr(child, 'floati_public', True)]
                if names:
                    details.append('    Subcommands: ' + ', '.join(names))
        notes = '\n'.join('    ' + line for line in copy['notes'])
        examples = '\n'.join('    ' + line for line in copy['examples'].splitlines())
        pages[topic] = (
            'NAME\n    floati' + (' ' + topic if topic else '') + ' - ' + copy['description']
            + '\n\nSYNOPSIS\n    ' + _synopsis(topic, current, chain)
            + '\n\nDESCRIPTION\n    ' + copy['body']
            + '\n\nOPTIONS AND ARGUMENTS\n' + '\n'.join(details)
            + ('\n\nOPERATION NOTES\n' + notes if notes else '')
            + '\n\nEXIT STATUS\n' + exit_lines
            + '\n\nEXAMPLES\n' + examples + '\n'
        )
    return pages


def verb_table(parser):
    from .help_copy import HELP_COPY
    def cell(value):
        return value.replace('|', '&#124;').replace('\n', ' ')
    lines = ['| Command | Syntax | Purpose |', '| --- | --- | --- |']
    for topic, (current, chain) in _registry(parser).items():
        if not topic:
            continue
        lines.append('| `' + topic + '` | `' + cell(_synopsis(topic, current, chain))
                     + '` | ' + cell(HELP_COPY[topic]['description']) + ' |')
    return '\n'.join(lines) + '\n'


def _agents(source, table):
    if BEGIN not in source or END not in source:
        raise ValueError('AGENTS.md requires exactly one generated command-table marker pair')
    if source.count(BEGIN) != 1 or source.count(END) != 1 or source.index(BEGIN) > source.index(END):
        raise ValueError('AGENTS.md has ambiguous generated command-table markers')
    before, rest = source.split(BEGIN, 1)
    _, after = rest.split(END, 1)
    return before + BEGIN + '\n' + table + END + after


def outputs(root):
    from .cli import _parser
    parser = _parser()
    pages = help_pages(parser)
    return {
        'floati/generated_help.py': '"""Generated by floati.command_codegen; edit the registry or help_copy instead."""\n\nPAGES = '
            + pprint.pformat(pages, width=100, sort_dicts=False) + '\n',
        'AGENTS.md': _agents((root / 'AGENTS.md').read_text(encoding='utf-8'), verb_table(parser)),
    }


def check(root):
    return [name for name, content in outputs(root).items()
            if not (root / name).is_file() or (root / name).read_text(encoding='utf-8') != content]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--write', action='store_true')
    mode.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    if args.write:
        for name, content in outputs(root).items():
            (root / name).write_text(content, encoding='utf-8')
        return 0
    stale = check(root)
    for name in stale:
        print('stale generated surface: ' + name)
    return int(bool(stale))


if __name__ == '__main__':
    raise SystemExit(main())
