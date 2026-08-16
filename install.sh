#!/usr/bin/env bash
# SLCW fleet installer.
#
#   curl -fsSL https://raw.githubusercontent.com/rygroup-dev/slcw-bot/main/install.sh | bash
#   bash install.sh                       # from a checkout
#   SLCW_HOME=/opt/slcw bash install.sh   # install somewhere else
#
# Installs system prerequisites, builds an isolated virtualenv, then hands over to
# the interactive setup wizard, which collects credentials, creates the encrypted
# vault, adds your first wallet, and installs the systemd unit.
#
# Safe to re-run: existing credentials, vaults, and wallets are left alone.

set -euo pipefail

INSTALL_DIR="${SLCW_HOME:-/root/slcw-bot}"
# Default so the piped one-liner can bootstrap itself; override to install a fork.
REPO="${SLCW_REPO:-https://github.com/rygroup-dev/slcw-bot.git}"
PYTHON_MIN_MINOR=11

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root: the installer writes a systemd unit"

# --- system prerequisites ------------------------------------------------
say "Checking system prerequisites"

need_pkg=()
command -v python3 >/dev/null 2>&1 || need_pkg+=(python3)
python3 -c 'import venv' >/dev/null 2>&1 || need_pkg+=(python3-venv)
command -v curl >/dev/null 2>&1 || need_pkg+=(curl)
command -v git >/dev/null 2>&1 || need_pkg+=(git)

if [ ${#need_pkg[@]} -gt 0 ]; then
  say "Installing: ${need_pkg[*]}"
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq "${need_pkg[@]}"
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q "${need_pkg[@]}"
  elif command -v apk >/dev/null 2>&1; then
    apk add --quiet "${need_pkg[@]}"
  else
    die "no supported package manager found; install manually: ${need_pkg[*]}"
  fi
fi

py_minor="$(python3 -c 'import sys; print(sys.version_info.minor)')"
[ "$py_minor" -ge "$PYTHON_MIN_MINOR" ] \
  || die "Python 3.$PYTHON_MIN_MINOR+ required, found 3.$py_minor"
say "Python $(python3 -c 'import platform; print(platform.python_version())') ok"

command -v systemctl >/dev/null 2>&1 || warn "systemd not found; the daemon unit will be skipped"

# --- source ---------------------------------------------------------------
if [ ! -f "$INSTALL_DIR/daemon.py" ]; then
  [ -n "$REPO" ] || die "$INSTALL_DIR has no daemon.py. Set SLCW_REPO=<git-url> to clone it."
  say "Cloning $REPO into $INSTALL_DIR"
  git clone --depth 1 "$REPO" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

# --- virtualenv -----------------------------------------------------------
# Deliberately inside the project. An earlier deployment kept its venv in /tmp,
# which the next reboot would have wiped, taking the whole bot down with it.
say "Building virtualenv at $INSTALL_DIR/.venv"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
say "Dependencies installed:"
.venv/bin/pip list 2>/dev/null | grep -iE '^(curl_cffi|solders|base58|cryptography) ' | sed 's/^/    /'

chmod +x slcwctl install.sh 2>/dev/null || true

# --- hand over to the wizard ---------------------------------------------
echo

# Piped through `curl | bash`, stdin is the script text rather than the
# terminal, so the wizard's prompts would read garbage and fail. Reattach to the
# controlling terminal — testing by actually opening it, since /dev/tty can look
# readable and still fail to open when there is no controlling terminal.
if [ ! -t 0 ]; then
  if { exec 3< /dev/tty; } 2>/dev/null; then
    exec .venv/bin/python "$INSTALL_DIR/slcwctl" setup <&3
  fi
  warn "no terminal available, so the interactive setup was skipped"
  say "Finish the install with:"
  printf '\n    cd %s && ./slcwctl setup\n\n' "$INSTALL_DIR"
  exit 0
fi

exec .venv/bin/python "$INSTALL_DIR/slcwctl" setup
