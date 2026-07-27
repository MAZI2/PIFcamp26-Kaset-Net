#!/usr/bin/env bash

# Source this script to leave the current shell inside the project venv:
#   source ./setup_venv.sh
#
# Running it directly still creates/updates .venv, but activation cannot
# persist in the parent shell after the script exits.

_setup_venv_main() {
    if [ -n "${BASH_SOURCE:-}" ]; then
        _script_path="${BASH_SOURCE[0]}"
    elif [ -n "${ZSH_VERSION:-}" ]; then
        _script_path="${(%):-%x}"
    else
        _script_path="$0"
    fi

    _project_dir="$(CDPATH= cd -- "$(dirname -- "$_script_path")" && pwd)" || return 1
    _venv_dir="$_project_dir/.venv"
    _requirements_file="$_project_dir/requirements.txt"
    _marker_file="$_venv_dir/.requirements.sha256"

    hash -r 2>/dev/null || true

    if [ -n "${PYTHON_BIN:-}" ]; then
        _python_bin="$PYTHON_BIN"
    elif [ "$(uname -s)" = "Linux" ] && [ -x /usr/bin/python3 ]; then
        _python_bin="/usr/bin/python3"
    elif [ "$(uname -s)" = "Darwin" ]; then
        _python_bin=""

        for _candidate in /usr/local/bin/python3 /opt/homebrew/anaconda3/bin/python3 /opt/homebrew/bin/python3 python3; do
            if command -v "$_candidate" >/dev/null 2>&1; then
                _candidate_path="$(command -v "$_candidate")"

                if "$_candidate_path" - <<'PY' >/dev/null 2>&1
import tkinter as tk
raise SystemExit(0 if tk.TkVersion >= 8.6 else 1)
PY
                then
                    _python_bin="$_candidate_path"
                    break
                fi
            fi
        done

        if [ -z "$_python_bin" ]; then
            echo "setup_venv.sh: no macOS Python with Tk 8.6+ was found." >&2
            echo "Install/use a Python.org, Homebrew, or Anaconda Python for the GUI." >&2
            return 1
        fi
    else
        _python_bin="python3"
    fi

    if [ ! -f "$_requirements_file" ]; then
        echo "setup_venv.sh: requirements.txt not found in $_project_dir" >&2
        return 1
    fi

    if [ "$(uname -s)" = "Darwin" ] && [ -f "$_venv_dir/pyvenv.cfg" ] && grep -q "CommandLineTools" "$_venv_dir/pyvenv.cfg"; then
        echo "Recreating .venv: macOS Command Line Tools Python uses deprecated Tk 8.5"
        rm -rf "$_venv_dir" || return 1
    fi

    if [ ! -d "$_venv_dir" ]; then
        echo "Creating .venv with system site packages"
        echo "Using Python: $_python_bin"
        "$_python_bin" -m venv --system-site-packages "$_venv_dir" || return 1
    elif [ -f "$_venv_dir/pyvenv.cfg" ] && ! grep -q "^include-system-site-packages = true" "$_venv_dir/pyvenv.cfg"; then
        echo "setup_venv.sh: existing .venv does not include system site packages." >&2
        echo "This is needed for apt-installed Raspberry Pi modules such as lgpio." >&2
        echo "Run: rm -rf .venv && source ./setup_venv.sh" >&2
        return 1
    fi

    if [ ! -x "$_venv_dir/bin/python" ]; then
        echo "setup_venv.sh: $_venv_dir/bin/python was not created." >&2
        echo "On Raspberry Pi OS, install venv support with: sudo apt-get install -y python3-venv" >&2
        return 1
    fi

    _requirements_hash="$("$_venv_dir/bin/python" -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$_requirements_file")" || return 1

    if [ ! -f "$_marker_file" ] || [ "$(cat "$_marker_file")" != "$_requirements_hash" ]; then
        echo "Installing requirements"
        "$_venv_dir/bin/python" -m pip install -r "$_requirements_file" || return 1
        printf "%s\n" "$_requirements_hash" > "$_marker_file" || return 1
    else
        echo "Requirements already installed"
    fi

    . "$_venv_dir/bin/activate" || return 1
    echo "Activated .venv"

    if [ "$_setup_venv_sourced" != "1" ]; then
        echo "Run 'source ./setup_venv.sh' to activate .venv in your current shell."
    fi
}

_setup_venv_sourced=0

if [ -n "${ZSH_VERSION:-}" ]; then
    case "$ZSH_EVAL_CONTEXT" in
        *:file) _setup_venv_sourced=1 ;;
    esac
elif [ -n "${BASH_SOURCE:-}" ]; then
    if [ "${BASH_SOURCE[0]}" != "$0" ]; then
        _setup_venv_sourced=1
    fi
fi

_setup_venv_main "$@"
_setup_venv_status=$?

unset -f _setup_venv_main
unset _setup_venv_sourced

if [ "$_setup_venv_status" -ne 0 ]; then
    unset _setup_venv_status
    return 1 2>/dev/null || exit 1
fi

unset _setup_venv_status
return 0 2>/dev/null || exit 0
