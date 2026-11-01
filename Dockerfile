# syntax=docker/dockerfile:1
#
# candygoogy-bot container image. Linux hosts with rootful Docker Engine only:
# the bot needs the host's network (Wake-on-LAN broadcast, ARP table).
#
# Released images are ghcr.io/candygoogy/candygoogy-bot, for amd64 and arm64
# (.github/workflows/release.yml). To build it yourself:
#   docker build -t candygoogy-bot:local \
#     --build-arg CANDYGOOGY_COMMIT="$(git rev-parse HEAD)" .
#
# The base image is pinned by its multi-arch INDEX digest (amd64, arm64, ...),
# and its Python matches the newest version CI tests (.github/workflows/ci.yml).
# To refresh the digest:
#   docker buildx imagetools inspect python:3.14-slim-trixie
#   (copy the top-level "Digest: sha256:..." line)
# then replace it below, rebuild, and let CI run.
FROM python:3.15-slim-trixie@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913

# The commit this image is built from (40 or at least 7 of 0-9 a-f), shown by
# /about since the image has no git checkout. Empty: /about says it's unknown.
ARG CANDYGOOGY_COMMIT=
ARG BUILD_DATE=
# The release (__version__ in candygoogy/__init__.py), for the label below;
# release.yml sets it. /about reads __version__ itself.
ARG VERSION=
ARG BOT_UID=10001
ARG BOT_GID=10001

LABEL org.opencontainers.image.title="candygoogy-bot" \
      org.opencontainers.image.description="Telegram bot: Wake-on-LAN, power control and status for machines on your LAN" \
      org.opencontainers.image.source="https://github.com/candygoogy/candygoogy-bot" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision="${CANDYGOOGY_COMMIT}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.base.name="docker.io/library/python:3.14-slim-trixie"

# Every name here is one the bot reads (tests/test_docker.py checks that), or
# Python's or pip's own.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    CANDYGOOGY_IN_CONTAINER=1 \
    CANDYGOOGY_COMMIT=${CANDYGOOGY_COMMIT} \
    STATE_DIRECTORY=/var/lib/candygoogy-bot \
    SSH_KNOWN_HOSTS=/var/lib/candygoogy-bot/known_hosts

# The tools the bot runs: ping (is it awake?), ip (ARP state), ssh (power
# actions, details) and tini (PID 1). The magic packet is sent from Python.
# Debian stable only ships fixes, so versions aren't pinned; the base digest
# pins the rest.
# Then: ping must work with NO capabilities (ICMP datagram sockets, see
# net.ipv4.ping_group_range). A setuid bit or a file capability would make it
# fail under cap_drop ALL, so the build fails if either is there.
# hadolint ignore=DL3008
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends iputils-ping iproute2 openssh-client tini; \
    rm -rf /var/lib/apt/lists/*; \
    python -c "import os, stat; p = os.path.realpath('/usr/bin/ping'); \
assert not os.stat(p).st_mode & (stat.S_ISUID | stat.S_ISGID), 'ping is setuid or setgid'; \
assert 'security.capability' not in os.listxattr(p), 'ping has file capabilities'"; \
    test -x /usr/bin/tini

# A fixed, non-root user that exists in /etc/passwd (ssh refuses to run for a
# UID without one), and the state directory a named volume inherits.
RUN set -eux; \
    groupadd --gid "${BOT_GID}" candygoogy; \
    useradd --uid "${BOT_UID}" --gid "${BOT_GID}" --home-dir /nonexistent \
        --no-create-home --shell /usr/sbin/nologin candygoogy; \
    install -d -o "${BOT_UID}" -g "${BOT_GID}" -m 0700 /var/lib/candygoogy-bot

WORKDIR /app

# Hash-locked, binary-only, nothing resolved beyond the lock.
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes --no-deps --only-binary=:all: \
        -r requirements.txt \
 && python -m pip check

COPY LICENSE bot.py ./
COPY candygoogy/ ./candygoogy/
# The root filesystem is read-only at runtime: compile now. COPY keeps the
# build context's file modes, and a checkout made with umask 077 would leave
# /app unreadable for the bot's user, so normalise them: owned by root,
# readable by everyone, writable by no one else.
RUN python -m compileall -q /app \
 && chown -R root:root /app \
 && chmod -R u=rwX,go=rX /app

USER ${BOT_UID}:${BOT_GID}
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "bot.py"]
