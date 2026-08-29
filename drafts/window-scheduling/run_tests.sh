#!/bin/bash
# FROZEN-RUNTIME LAW: the package's own runner never writes bytecode.
export PYTHONDONTWRITEBYTECODE=1
cd "$(dirname "$0")"
python3 -m unittest discover -s tests -t . -v
