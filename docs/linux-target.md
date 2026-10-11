# A Linux machine as a target (experimental)

> **Experimental.** The Linux commands are tested against recorded outputs, not
> yet on many real machines. Please report what works and what doesn't.

How to set up a Linux machine with systemd so the bot can wake it, power it off,
reboot it and suspend it (`os = "linux"` in the machines file; see
`machines.example.toml`). A container is not a useful target: point the bot at
its host instead.

Placeholders: `<bot host IP>` (the machine the bot runs on), `<machine IP>`,
`<if>` (this machine's wired network interface, e.g. `enp3s0`), `botuser` (the
`ssh_user` the bot logs in as), and `/etc/candygoogy/ssh/<key>` (the bot's key
on its host; with Docker, `ssh/<key>` next to `docker-compose.yml`).

## 1. The SSH user and key

Use a dedicated user with no sudo rights; the polkit rule below is all it needs.

```sh
sudo useradd --create-home --shell /bin/sh botuser
```

On the bot's host, make a key without a passphrase (the bot can't type one):

```sh
sudo ssh-keygen -t ed25519 -N "" -C candygoogy-bot -f /etc/candygoogy/ssh/<key>
sudo chown root:candygoogy /etc/candygoogy/ssh/<key> && sudo chmod 640 /etc/candygoogy/ssh/<key>
sudo cat /etc/candygoogy/ssh/<key>.pub
```

On this machine, add the `.pub` line, restricted, to `~botuser/.ssh/authorized_keys`:

```
from="<bot host IP>",restrict ssh-ed25519 AAAA... candygoogy-bot
```

`from=` refuses the key from any other address; `restrict` turns off port,
agent and X11 forwarding, the terminal and `~/.ssh/rc`. The file and folder must
belong to botuser and not be writable by others:

```sh
sudo install -d -o botuser -g botuser -m 700 ~botuser/.ssh
sudoedit ~botuser/.ssh/authorized_keys    # paste the line
sudo chown botuser:botuser ~botuser/.ssh/authorized_keys && sudo chmod 600 ~botuser/.ssh/authorized_keys
```

On SELinux systems (Fedora, RHEL), also `sudo restorecon -Rv ~botuser/.ssh`.

Set the machine's `ssh_key_path` to `/etc/candygoogy/ssh/<key>` in the machines
file (or leave it out to use `SSH_KEY_PATH`), and `ssh_port` if sshd isn't on 22.

## 2. The power rule

The bot runs `systemctl poweroff|reboot|suspend --no-ask-password` as botuser.
systemd-logind asks polkit, and the rule in
[`deploy/targets/linux-power.rules`](../deploy/targets/linux-power.rules) allows
exactly those three for botuser (instructions inside). In short:

```sh
# edit "botuser" in the file first if your user has another name
sudo install -m 644 linux-power.rules /etc/polkit-1/rules.d/50-candygoogy-power.rules
```

- No polkit daemon (common on headless servers): logind then refuses
  everything. Check `systemctl status polkit`; Debian/Ubuntu: `sudo apt install polkitd`.
- Old polkit 0.105 (Debian 11, Ubuntu 23.04 and older) ignores `.rules` files.
  Use `/etc/polkit-1/localauthority/50-local.d/candygoogy-power.pkla` instead:

  ```ini
  [candygoogy power]
  Identity=unix-user:botuser
  Action=org.freedesktop.login1.power-off;org.freedesktop.login1.power-off-multiple-sessions;org.freedesktop.login1.reboot;org.freedesktop.login1.reboot-multiple-sessions;org.freedesktop.login1.suspend;org.freedesktop.login1.suspend-multiple-sessions
  ResultAny=yes
  ResultInactive=yes
  ResultActive=yes
  ```
- Inhibitor locks still apply: `systemd-inhibit --list` shows what blocks a
  shutdown (e.g. a package upgrade). The bot then reports "not allowed, or blocked".

## 3. Wake-on-LAN

- **BIOS/UEFI:** "Wake on LAN" (or "Power on by PCI-E") on; "ErP/EuP ready" off.
- **The network card:** `sudo ethtool <if>` must show `Supports Wake-on: ...g...`
  and `Wake-on: g`. Set it with `sudo ethtool -s <if> wol g`. Use `g` (magic
  packet) only: the other modes let ordinary traffic, including the bot's own
  checks, wake the machine.
- **Make it stick:** many drivers reset it at boot. One of:
  - NetworkManager: `sudo nmcli connection modify <connection> 802-3-ethernet.wake-on-lan magic`
  - systemd-networkd/udev: `/etc/systemd/network/50-wol.link` with
    `[Match]` `MACAddress=<this machine's MAC>` and `[Link]` `WakeOnLan=magic`
    (a matching `.link` file replaces the distribution's default one for that
    card, so copy its `NamePolicy=`/`MACAddressPolicy=` lines too)
  - ifupdown (Debian): `ethernet-wol g` under the interface in
    `/etc/network/interfaces` (needs `ethtool` installed)
- **From suspend:** the card must also be a wake source:
  `cat /sys/class/net/<if>/device/power/wakeup` should say `enabled`. Machines
  that suspend with `s2idle` (`cat /sys/power/mem_sleep` shows `[s2idle]`) often
  don't wake reliably from the network.
- A suspended Linux machine doesn't answer ARP, so `/status` can't tell
  "asleep" from "off" and says "Off or asleep".
- Use a wired connection. A VM wakes only if its hypervisor supports it.

## 4. Check each command by hand

From the bot's host, as the bot's user (with Docker, run the same `ssh` inside
`docker compose run --rm --no-deps bot`, adding
`-o UserKnownHostsFile=/var/lib/candygoogy-bot/known_hosts`):

```sh
key=/etc/candygoogy/ssh/<key>
s() { sudo -u candygoogy ssh -i "$key" -o BatchMode=yes -o StrictHostKeyChecking=accept-new botuser@<machine IP> "$@"; }
s true && echo "ssh works"
s "pkcheck --action-id org.freedesktop.login1.power-off --process \$\$" && echo "power-off allowed"
s "pkcheck --action-id org.freedesktop.login1.reboot --process \$\$" && echo "reboot allowed"
s "pkcheck --action-id org.freedesktop.login1.suspend --process \$\$" && echo "suspend allowed"
```

The `pkcheck` lines ask polkit only and change nothing. Then in Telegram:
`/status` (details and who is signed in), `/sleep` and `/wake`, and finally
`/shutdown` and `/wake`. If a power command reports "not allowed", the polkit
rule (section 2) isn't in effect for botuser.

## Rollback

Remove the polkit rule (`sudo rm /etc/polkit-1/rules.d/50-candygoogy-power.rules`)
and the bot's line from `~botuser/.ssh/authorized_keys`.
