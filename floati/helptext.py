"""Static operator help generated from command registration and reviewed copy."""
from __future__ import annotations

from typing import Dict, Optional, Sequence

from .copy import register
from .generated_help import PAGES as _RAW
from .help_copy import name_line_description

HELP: Dict[str, str] = {
    topic: register('help.root' if not topic else 'help.' + topic.replace(' ', '.'), text, 'CLI help')
    for topic, text in _RAW.items()
}


def help_for(arguments: Sequence[str]) -> Optional[str]:
    if '--help' not in arguments:
        return None
    import argparse
    from .cli import _parser
    from .errors import ProtocolRefusal

    class Requested(Exception):
        def __init__(self, topic):
            self.topic = topic

    class HelpAction(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            raise Requested(self.const)

    # Use argparse's own token roles: ancestor option values and leaf positional
    # arguments cannot masquerade as child command names. Handlers are never run.
    parser = _parser()
    def attach(current, topic):
        current.add_argument('--help', action=HelpAction, nargs=0, const=topic)
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    if getattr(child, 'floati_public', True):
                        attach(child, (topic + ' ' + name).strip())
    attach(parser, '')
    try:
        parser.parse_args(list(arguments))
    except Requested as requested:
        return HELP.get(requested.topic)
    except ProtocolRefusal:
        return None
    return None
