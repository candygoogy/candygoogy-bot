#!/usr/bin/env bash
# Update a systemd install (see deploy/candygoogy-bot.service) to the latest main:
#   pull -> install locked deps -> check the configuration -> compare unit -> restart -> verify
#
# Run from anywhere:  /opt/candygoogy-bot/deploy/deploy.sh
# as an admin, or as the service user if it may use sudo for systemctl.
# git and pip run as the owner of the checkout, so it never gets root-owned
# files; sudo is used for that and for systemctl/journalctl. Nothing is
# restarted if the pull, the install or a check fails.
#
# The configuration is read from /etc/candygoogy/bot.env, or from the file
# named by CANDYGOOGY_ENV_FILE. Only variable names are ever printed.
set -euo pipefail

SERVICE=candygoogy-bot
UNIT_DST=/etc/systemd/system/$SERVICE.service
ENV_FILE=${CANDYGOOGY_ENV_FILE:-/etc/candygoogy/bot.env}

# Everything runs inside main(), so bash has read the whole script before
# `git pull` can replace this file.
main() {
  local repo owner
  repo=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)
  cd "$repo"
  owner=$(stat -c %U "$repo")
  local unit_src=$repo/deploy/$SERVICE.service

  # Runs a command as the checkout's owner (through sudo only if that's someone else).
  as_owner() {
    if [[ $(id -un) == "$owner" ]]; then
      "$@"
    else
      sudo -u "$owner" -- "$@"
    fi
  }

  echo "==> Pulling latest code"
  as_owner git pull --ff-only

  echo "==> Installing locked dependencies"
  # No cache: the service user's home may not exist yet, and it isn't needed.
  as_owner ./venv/bin/pip install --quiet --no-cache-dir --require-hashes -r requirements.txt

  # After the pull, so variables required by this update are checked too.
  # Run through bash so a lost executable bit can't masquerade as a config
  # problem. check-env.sh exits 1 for missing or empty variables; anything
  # else means the checker itself failed (e.g. 127: script missing).
  echo "==> Checking $ENV_FILE has the required variables"
  local rc=0
  as_owner bash ./deploy/check-env.sh .env.example "$ENV_FILE" || rc=$?
  if ((rc == 1)); then
    echo "Not restarting: fix the problems above first." >&2
    exit 1
  elif ((rc != 0)); then
    echo "Not restarting: deploy/check-env.sh itself failed (exit $rc)." >&2
    exit 1
  fi

  # The new code's own check of every value (bot.py --check-config): exit 2
  # lists the problems by variable name, without values. It sees what the
  # service gets: the installed unit's Environment= defaults (e.g.
  # SSH_KEY_PATH), overridden by the env file, as systemd does.
  echo "==> Validating the configuration"
  local unit_env
  unit_env=$(systemctl show -p Environment --value "$SERVICE" 2>/dev/null || true)
  rc=0
  as_owner ./venv/bin/python -c '
import os
import shlex
import sys
from dotenv import load_dotenv
load_dotenv(sys.argv[1])
for assignment in shlex.split(sys.argv[2]):
    name, equals, value = assignment.partition("=")
    if equals:
        os.environ.setdefault(name, value)
import bot
sys.exit(bot.start(["--check-config"]))
' "$ENV_FILE" "$unit_env" || rc=$?
  if ((rc == 2)); then
    echo "Not restarting: fix the configuration problems above first." >&2
    exit 1
  elif ((rc != 0)); then
    echo "Not restarting: the configuration check itself failed (exit $rc)." >&2
    exit 1
  fi

  echo "==> Comparing systemd unit"
  if cmp -s "$unit_src" "$UNIT_DST"; then
    echo "OK: installed unit matches the repo."
  else
    echo "WARNING: $UNIT_DST differs from the repo's unit (or is missing)."
    echo "  Review:   diff -u $UNIT_DST $unit_src"
    echo "  Install:  sudo cp $unit_src $UNIT_DST"
    echo "            sudo systemctl daemon-reload"
    echo "  Not done automatically; restarting with the currently installed unit."
  fi

  echo "==> Restarting $SERVICE"
  sudo systemctl restart "$SERVICE"
  sleep 3
  if systemctl is-active --quiet "$SERVICE"; then
    echo "OK: $SERVICE is running."
  else
    echo "FAILED: $SERVICE is not running. Recent logs:" >&2
    sudo journalctl -u "$SERVICE" -n 30 --no-pager >&2
    exit 1
  fi
  sudo journalctl -u "$SERVICE" -n 10 --no-pager
}

main "$@"
