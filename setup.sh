#!/usr/bin/env bash
# Set up cloudshell on Linux or macOS:
#   1. find a Python >= 3.9 (or get one with `uv` if it is installed)
#   2. create .venv next to this script and install cloudshell into it
#   3. check that a system keychain is available for the password
#
# usage: ./setup.sh [--api]      --api also installs the optional Portal API fallback
set -euo pipefail

MIN_MAJOR=3
MIN_MINOR=9
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/.venv"
EXTRAS=""
[[ "${1:-}" == "--api" ]] && EXTRAS="[api]"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31mx\033[0m %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
    Linux|Darwin) ;;
    *) die "unsupported OS '$(uname -s)': cloudshell supports Linux and macOS" ;;
esac

# Is $1 a Python interpreter that is new enough?
python_ok() {
    "$1" -c "import sys; sys.exit(0 if sys.version_info >= ($MIN_MAJOR, $MIN_MINOR) else 1)" 2>/dev/null
}

install_hint() {
    if [[ "$(uname -s)" == Darwin ]]; then
        echo "  brew install python3          (https://brew.sh)"
    elif command -v apt-get >/dev/null; then
        echo "  sudo apt-get install python3 python3-venv"
    elif command -v dnf >/dev/null; then
        echo "  sudo dnf install python3"
    elif command -v pacman >/dev/null; then
        echo "  sudo pacman -S python"
    else
        echo "  install Python $MIN_MAJOR.$MIN_MINOR+ with your package manager"
    fi
    echo "  or install uv (https://docs.astral.sh/uv/) and re-run; it can fetch Python for you"
}

# 1. Reuse an existing, healthy venv; otherwise find a suitable interpreter.
if [[ -x "$VENV/bin/python" ]] && python_ok "$VENV/bin/python"; then
    say "using existing virtualenv $VENV ($("$VENV/bin/python" -V))"
else
    rm -rf "$VENV"
    PY=""
    for cand in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
        if command -v "$cand" >/dev/null && python_ok "$cand"; then
            PY="$(command -v "$cand")"
            break
        fi
    done

    if [[ -n "$PY" ]] && "$PY" -m venv --help >/dev/null 2>&1; then
        say "creating virtualenv with $PY ($("$PY" -V 2>&1))"
        if ! "$PY" -m venv "$VENV" 2>/dev/null; then
            rm -rf "$VENV"
            PY=""                       # e.g. Debian without python3-venv
        fi
    else
        PY=""
    fi

    if [[ -z "$PY" ]]; then
        if command -v uv >/dev/null; then
            say "no usable Python $MIN_MAJOR.$MIN_MINOR+ with venv found; letting uv provide one"
            uv venv --seed --python ">=$MIN_MAJOR.$MIN_MINOR" "$VENV"
        else
            echo "No Python $MIN_MAJOR.$MIN_MINOR+ (with the venv module) was found. Install one:" >&2
            install_hint >&2
            exit 1
        fi
    fi
fi

# 2. Install cloudshell and its dependencies.
say "installing cloudshell$EXTRAS"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet "$DIR$EXTRAS"

# 3. Check for a usable keychain (the password is stored there, never in a file).
backend="$("$VENV/bin/python" - <<'EOF'
import keyring
from keyring.backends import fail
kr = keyring.get_keyring()
print("" if isinstance(kr, fail.Keyring) or getattr(kr, "priority", 1) <= 0 else type(kr).__module__)
EOF
)"
if [[ -z "$backend" ]]; then
    warn "no system keychain found: cloudshell will work, but will ask for your password every run."
    [[ "$(uname -s)" == Linux ]] && warn "install and unlock gnome-keyring or KWallet to fix this."
else
    say "keychain: $backend"
fi

chmod +x "$DIR/cloudshell" 2>/dev/null || true
say "done. Start it with:  $DIR/cloudshell"
