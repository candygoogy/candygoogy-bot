#!/usr/bin/env bash
# Smoke-tests a built candygoogy-bot image the way docker-compose.yml runs it.
# The bot never reaches Telegram: no network, and a fake but well-formed config.
#
#   .github/scripts/smoke-image.sh IMAGE
#
# Runs on GitHub's runners (docker.yml, release.yml) and on any Linux host with
# rootful Docker. Only on a GitHub-hosted runner does it change the host's ping
# sysctl; anywhere else it never touches the host's settings.
set -euo pipefail

if [ $# -ne 1 ] || [ -z "$1" ]; then
  echo "usage: $0 IMAGE" >&2
  exit 2
fi
IMAGE=$1

fail() { echo "::error::$*"; exit 1; }

# The flags docker-compose.yml runs the bot with.
harden=(--read-only --cap-drop ALL --security-opt no-new-privileges:true
        --tmpfs /tmp:size=16m --tmpfs "/var/lib/candygoogy-bot:size=4m,mode=1777")

work=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/candygoogy-smoke.XXXXXX")
cid=""
cleanup() {
  if [ -n "$cid" ]; then docker rm -f "$cid" >/dev/null 2>&1 || true; fi
  rm -rf "$work"
}
trap cleanup EXIT

# A fake but well-formed config: documentation MAC and IP, a token that has a
# token's shape but isn't one.
cat > "$work/fake.env" <<'EOF'
BOT_TOKEN=123456:TEST-TOKEN-not-a-real-token-0000
AUTHORIZED_USER_IDS=1111
WORKSTATION_MAC=00:00:5e:00:53:01
WORKSTATION_IP=192.0.2.10
SSH_USER=botuser
SSH_KEY_PATH=/nonexistent/id_ed25519
EOF

echo "Runs as the fixed non-root user."
[ "$(docker run --rm --network none "$IMAGE" id -u)" = 10001 ] || fail "not UID 10001"

echo "The package imports with the image's dependencies."
docker run --rm --network none "${harden[@]}" "$IMAGE" \
  python -c "import candygoogy.settings, httpx, telegram, dotenv"

echo "--check-config accepts the fake config, and the commit from the build."
out=$(docker run --rm --network none "${harden[@]}" --env-file "$work/fake.env" \
      "$IMAGE" python bot.py --check-config)
[ "$out" = "OK: the configuration is valid." ] || fail "check-config said: $out"

echo "A bad config: exit 2, names the variable, never echoes the value."
for cmd in "python bot.py --check-config" "python bot.py"; do
  set +e
  # shellcheck disable=SC2086 # $cmd is split on purpose
  out=$(docker run --rm --network none "${harden[@]}" \
        -e BOT_TOKEN=not-a-token-SMOKEMARKER "$IMAGE" $cmd 2>&1); code=$?
  set -e
  [ "$code" -eq 2 ] || { echo "$out"; fail "$cmd: expected exit 2, got $code"; }
  grep -q "^- BOT_TOKEN doesn't look like a bot token" <<<"$out" || fail "$cmd: BOT_TOKEN not named"
  if grep -q SMOKEMARKER <<<"$out"; then fail "$cmd: the error echoes a value"; fi
done

echo "A valid fake config starts and keeps running with no network at all"
echo "(it retries Telegram forever, so it never contacts anything)."
cid=$(docker run -d --network none "${harden[@]}" --env-file "$work/fake.env" "$IMAGE")
sleep 10
running=$(docker inspect -f '{{.State.Running}}' "$cid")
docker logs --tail 20 "$cid" 2>&1 || true
docker rm -f "$cid" >/dev/null
cid=""
[ "$running" = true ] || fail "the bot exited with a valid config"

echo "Unprivileged ping as UID 10001, no capabilities (uses the host's sysctl)."
echo "host ping_group_range: $(cat /proc/sys/net/ipv4/ping_group_range)"
if [ "$(awk '{print $1, $2}' /proc/sys/net/ipv4/ping_group_range)" = "1 0" ]; then
  # Only GitHub's own runners are throwaway VMs. GITHUB_ACTIONS alone is also
  # set on self-hosted runners, which are real hosts: never change those.
  if [ "${GITHUB_ACTIONS:-}" = true ] && [ "${RUNNER_ENVIRONMENT:-}" = github-hosted ]; then
    echo "The runner disallows ICMP sockets; widening it on this throwaway VM."
    sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"
  else
    fail "this host disallows unprivileged ping; see the README's ping_group_range fix"
  fi
fi
docker run --rm --network host "${harden[@]}" "$IMAGE" ping -c 1 -W 2 127.0.0.1
docker run --rm --network none "$IMAGE" sh -c 'test ! -u /usr/bin/ping && test ! -g /usr/bin/ping'

echo "The host's neighbour table is readable (output not printed)."
docker run --rm --network host "${harden[@]}" "$IMAGE" ip neigh show >/dev/null

echo "ssh resolves the user and accepts the options (-G connects to nothing)."
docker run --rm --network none "${harden[@]}" "$IMAGE" \
  ssh -G -o UserKnownHostsFile=/var/lib/candygoogy-bot/known_hosts example.invalid >/dev/null

echo "The root filesystem really is read-only."
if docker run --rm --network none "${harden[@]}" "$IMAGE" touch /app/x 2>/dev/null; then
  fail "the root filesystem is writable"
fi

echo "OK: every smoke test passed for $IMAGE"
