#!/bin/bash
#
# This file is part of GitPython and is released under the
# 3-Clause BSD License: https://opensource.org/license/bsd-3-clause/
#
# This script builds a release. If run in a venv, it auto-installs its tools.
# You may want to run "make release" instead of running this script directly.

set -eEu

function release_with() {
    "$1" -m build --sdist --wheel
}

function suggest_venv() {
    local venv_cmd='python -m venv env && source env/bin/activate'
    printf "HELP: To avoid this error, use a virtual-env with '%s' instead.\n" "$venv_cmd"
}

if test -n "${VIRTUAL_ENV-}"; then
    deps=(build twine)  # Install twine along with build, as we need it later.
    echo "Virtual environment detected. Adding packages: ${deps[*]}"
    pip install --quiet --upgrade "${deps[@]}"
    echo 'Starting the build.'
    release_with python
else
    trap suggest_venv ERR  # This keeps the original exit (error) code.
    echo 'Starting the build.'
    release_with python3  # Outside a venv, use python3.
fi
