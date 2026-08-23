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
# Safe to re-run: on a machine that already holds a vault this never runs the
# wizard, never creates or imports a wallet, and never writes to .env. It pulls
# the latest source, reinstalls dependencies, runs the tests, and restarts the
# service only if they pass.

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
FRESH_CLONE=0
if [ ! -f "$INSTALL_DIR/daemon.py" ]; then
  [ -n "$REPO" ] || die "$INSTALL_DIR has no daemon.py. Set SLCW_REPO=<git-url> to clone it."
  say "Cloning $REPO into $INSTALL_DIR"
  git clone "$REPO" "$INSTALL_DIR"
  FRESH_CLONE=1
fi
cd "$INSTALL_DIR"

# An install that already has a vault is an update, not a first run. Re-running
# the wizard there would ask for things already answered and risks a second
# wallet nobody wanted — so the vault alone decides, not the Telegram token: an
# operator who declined Telegram still has wallets that must not be touched.
CONFIGURED=0
if [ "$FRESH_CLONE" -eq 0 ] && [ -f data/wallets.enc ]; then
  CONFIGURED=1
fi

if [ "$CONFIGURED" -eq 1 ] && [ -d .git ]; then
  say "Existing install detected — updating source only"

  # Never touch a dirty working tree. Stashing here would move someone's
  # uncommitted work out from under them, and an installer is exactly the
  # program you least expect to do that.
  if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
    warn "uncommitted changes present — skipping the pull, your files are untouched"
    say "Commit or stash them yourself, then re-run to pick up the update."
  else
    BEFORE="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
    git pull --rebase --quiet || warn "git pull failed; keeping the current tree"
    AFTER="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
    if [ "$BEFORE" = "$AFTER" ]; then
      say "Already at the latest commit ($AFTER)"
    else
      say "Updated $BEFORE → $AFTER"
    fi
  fi
fi

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

# slcwctl ships with the default install path in its shebang, so running
# ./slcwctl directly from an SLCW_HOME elsewhere would reach for a venv that is
# not there. Point it at the interpreter this install actually built. Every
# call from this script goes through .venv/bin/python explicitly, so this only
# affects a human typing ./slcwctl.
sed -i "1s|^#!.*|#!$INSTALL_DIR/.venv/bin/python|" slcwctl 2>/dev/null || true

# --- update path: no wizard, just restart --------------------------------
if [ "$CONFIGURED" -eq 1 ]; then
  echo
  say "Running the test suite"
  if .venv/bin/python -m unittest discover -s tests -t . >/dev/null 2>&1; then
    say "Tests pass"
  else
    warn "tests failed — the service was left running on the previous code"
    say "Investigate with: cd $INSTALL_DIR && .venv/bin/python -m unittest discover -s tests -t ."
    exit 1
  fi

  if command -v systemctl >/dev/null 2>&1 \
     && systemctl list-unit-files slcw-fleet.service >/dev/null 2>&1; then
    cp -f slcw-fleet.service /etc/systemd/system/ 2>/dev/null || true
    systemctl daemon-reload
    systemctl restart slcw-fleet
    sleep 3
    state="$(systemctl is-active slcw-fleet || true)"
    if [ "$state" = "active" ]; then
      say "slcw-fleet restarted and running"
    else
      warn "slcw-fleet is $state — check: journalctl -u slcw-fleet -n 50"
    fi
  fi

  echo
  say "Update complete. Wallets and credentials were left untouched."
  printf '\n    Wallets:  %s\n    Logs:     journalctl -u slcw-fleet -f\n\n' \
    "$(ls data/wallets.enc >/dev/null 2>&1 && echo 'vault intact' || echo 'none')"
  exit 0
fi

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
