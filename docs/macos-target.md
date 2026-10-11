# A Mac as a target (experimental)

> **Experimental.** The macOS commands are tested against recorded outputs, not
> yet on many real Macs. Please report what works and what doesn't.

How to set up a Mac so the bot can wake it from sleep, shut it down, restart it
and put it to sleep (`os = "macos"` in the machines file; see
`machines.example.toml`). **A Mac generally can't be woken from a full shutdown**,
only from sleep; the bot says so when you `/wake` a Mac it shut down.

Placeholders: `<bot host IP>` (the machine the bot runs on), `<machine IP>`,
`botuser` (the `ssh_user` the bot logs in as), and `/etc/candygoogy/ssh/<key>`
(the bot's key on its host; with Docker, `ssh/<key>` next to `docker-compose.yml`).

## 1. The SSH user and key

- Create a standard (not administrator) user for the bot in System Settings →
  Users & Groups.
- Turn on **Remote Login**: System Settings → General → Sharing → Remote Login,
  and set "Allow access for" to **only** that user. Leave "Allow full disk
  access for remote users" **off**; the bot needs none.

On the bot's host, make a key without a passphrase (the bot can't type one):

```sh
sudo ssh-keygen -t ed25519 -N "" -C candygoogy-bot -f /etc/candygoogy/ssh/<key>
sudo chown root:candygoogy /etc/candygoogy/ssh/<key> && sudo chmod 640 /etc/candygoogy/ssh/<key>
sudo cat /etc/candygoogy/ssh/<key>.pub
```

On the Mac, as botuser (or as an admin, then fix the owner), add the `.pub`
line, restricted, to `/Users/botuser/.ssh/authorized_keys`:

```
from="<bot host IP>",restrict ssh-ed25519 AAAA... candygoogy-bot
```

`from=` refuses the key from any other address; `restrict` turns off port,
agent and X11 forwarding, the terminal and `~/.ssh/rc`. Permissions:
`chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys`, both owned by botuser.

Set the machine's `ssh_key_path` to `/etc/candygoogy/ssh/<key>` in the machines
file (or leave it out to use `SSH_KEY_PATH`).

## 2. The power rule

The bot runs `sudo -n /sbin/shutdown -h now`, `sudo -n /sbin/shutdown -r now`
and `sudo -n /usr/bin/pmset sleepnow`. The sudoers rule in
[`deploy/targets/macos-sudoers`](../deploy/targets/macos-sudoers) allows exactly
those three for botuser, without a password (instructions inside). In short, as
an admin on the Mac:

```sh
sudo visudo -f /etc/sudoers.d/candygoogy   # paste the rule line, with your user name
sudo chmod 440 /etc/sudoers.d/candygoogy
```

Always edit with `visudo`: it refuses to save a broken file, which could
otherwise lock you out of sudo.

**FileVault:** after a restart, a Mac with FileVault waits at the unlock screen
until someone types a password, and SSH isn't reachable until then. The bot
then reports that the Mac didn't come back.

## 3. Wake-on-LAN (from sleep)

- Turn on **"Wake for network access"** (System Settings → Battery or Energy →
  Options; older versions: "Wake for Ethernet network access"). Check with
  `pmset -g | grep womp` (`womp 1`); set it with `sudo pmset -a womp 1`.
  Laptops honour it only on power.
- Use **Ethernet**. Wi-Fi wake needs a supporting access point and is unreliable.
- **Bonjour Sleep Proxy:** with an Apple TV or HomePod on the network, the Mac
  can be woken by traffic to its SSH port, including the bot's check of port 22.
  After a `/sleep`, the bot's background on-time check therefore uses ping only;
  a `/status` you ask for still tries port 22 and may wake the Mac. The Mac also
  wakes briefly now and then on its own, which the on-time figures may show.
- The firewall's **stealth mode** drops ping. Then only port 22 shows the Mac is
  awake: allow ping, or accept that a check may wake a sleeping Mac.
- A sleep proxy may answer ARP for the sleeping Mac with its own MAC. For a Mac,
  `/status` then says "Maybe asleep" rather than "Possible IP conflict".

## 4. Check each command by hand

From the bot's host, as the bot's user (with Docker, run the same `ssh` inside
`docker compose run --rm --no-deps bot`, adding
`-o UserKnownHostsFile=/var/lib/candygoogy-bot/known_hosts`):

```sh
key=/etc/candygoogy/ssh/<key>
s() { sudo -u candygoogy ssh -i "$key" -o BatchMode=yes -o StrictHostKeyChecking=accept-new botuser@<machine IP> "$@"; }
s true && echo "ssh works"
s sudo -n -l
```

`sudo -n -l` lists the rules and runs nothing. It must show the three commands
and must not say "a password is required". Then in Telegram: `/status` (details
and who is signed in), `/sleep` and `/wake`, then `/reboot`. Use `/shutdown`
only when someone can switch the Mac back on by hand.

## Rollback

`sudo rm /etc/sudoers.d/candygoogy`, remove the bot's line from
`~botuser/.ssh/authorized_keys`, and turn Remote Login off if nothing else needs it.
