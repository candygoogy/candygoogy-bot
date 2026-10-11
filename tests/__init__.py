"""The tests' fake config. Python imports this package before conftest.py
and the test files, so it is set before any of them imports the bot."""

import os

# Fake config, set before the bot is imported: every variable the settings
# loader reads, so a developer's environment can't leak into a test run (and
# bot.py, which loads .env, isn't imported until a test calls it). 192.0.2.0/24
# is TEST-NET-1: documentation-only and unroutable, so nothing here can reach a
# real machine. The token has a real token's shape but can't be one.
os.environ.update(
    {
        "BOT_TOKEN": "123456:TEST-TOKEN-not-a-real-token-0000",
        "AUTHORIZED_USER_IDS": "1111",
        "WORKSTATION_MAC": "00:00:5e:00:53:01",
        "WORKSTATION_IP": "192.0.2.10",
        "SSH_USER": "tester",
        "SSH_KEY_PATH": "/nonexistent/test_key",
        "HOMELAB_DISKS": "/,/srv/data",
        "BACKUP_DIR": "/nonexistent/backups",  # FakeHomelab points it at tmp_path
        "BACKUP_MAX_AGE_HOURS": "26",
        "DIGEST_TIME": "08:00",
        "WEEKLY_REPORT": "sun 20:00",
        "INTERNET_CHECK_TARGETS": "1.1.1.1:443,9.9.9.9:443",
    }
)
for name in (
    "AUTHORIZED_USER_ID",
    "STATE_DIRECTORY",
    "CANDYGOOGY_IN_CONTAINER",
    "CANDYGOOGY_COMMIT",
    "MACHINES_FILE",
    "SSH_KNOWN_HOSTS",
    "HOMELAB_HOST_ROOT",
    "DOCKER_HOST",
):
    _ = os.environ.pop(name, None)
