#!/usr/bin/env bash
# Check that ENV defines every variable EXAMPLE requires, before a restart.
# A quick first check (deploy.sh runs it before `bot.py --check-config`, which
# checks every value, including "a machine or MACHINES_FILE").
#
# Usage: deploy/check-env.sh [EXAMPLE] [ENV]   (defaults: .env.example .env)
#
# Required = every uncommented NAME= line in EXAMPLE (in .env.example: BOT_TOKEN
# and AUTHORIZED_USER_IDS). Only variable NAMES are ever printed, never values.
# Exits 1 if anything is missing or empty.
set -euo pipefail
export LC_ALL=C # stable sort order for comm

example=${1:-.env.example}
envfile=${2:-.env}

# NAME=... or export NAME=..., ignoring comments and blank lines.
names() {
  sed -nE 's/^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=.*/\2/p' "$1" | sort -u
}

# Names whose value is empty: NAME=, NAME="", NAME=''
empty_names() {
  sed -nE "s/^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=[[:space:]]*(\"\"|'')?[[:space:]]*\$/\\2/p" "$1" | sort -u
}

if [[ ! -f "$example" ]]; then
  echo "ERROR: $example not found." >&2
  exit 1
fi
if [[ ! -f "$envfile" ]]; then
  echo "ERROR: $envfile not found." >&2
  exit 1
fi

missing=$(comm -23 <(names "$example") <(names "$envfile"))
empty=$(comm -12 <(names "$example") <(empty_names "$envfile"))

if [[ -n "$missing" || -n "$empty" ]]; then
  if [[ -n "$missing" ]]; then
    echo "ERROR: $envfile is missing these variables (see $example):" >&2
    while IFS= read -r name; do echo "  $name"; done <<<"$missing" >&2
  fi
  if [[ -n "$empty" ]]; then
    echo "ERROR: these variables are empty in $envfile:" >&2
    while IFS= read -r name; do echo "  $name"; done <<<"$empty" >&2
  fi
  exit 1
fi

echo "OK: $envfile defines every variable in $example."
