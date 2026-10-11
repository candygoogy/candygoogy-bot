# A Windows machine as a target

How to set up a Windows PC so the bot can wake it, shut it down, reboot it and
put it to sleep (`os = "windows"`, or the single machine from `WORKSTATION_*`).

Placeholders used below:

| placeholder | meaning |
|---|---|
| `<bot host IP>` | the LAN address of the machine the bot runs on |
| `<machine IP>` | this PC's address (`WORKSTATION_IP`, or `ip` in the machines file) |
| `<SSH_USER>` | the Windows account the bot logs in as |
| `/etc/candygoogy/ssh/id_ed25519` | the bot's private key (the systemd layout; with Docker it is `ssh/id_ed25519` next to `docker-compose.yml`) |

## 0. Before you start

- Both machines need **fixed IPs** (static, or a DHCP reservation). The `from=`
  restriction below, Wake-on-LAN and the bot's ARP check all depend on them.
- Keep a second way in (sitting at the PC, or another key) while you test.
- The bot runs `shutdown`, PowerShell and `quser`, so `<SSH_USER>` must be an
  administrator.

## 1. OpenSSH Server and the key

In an **elevated** PowerShell on the PC:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service sshd -StartupType Automatic
Start-Service sshd
```

Leave sshd's default shell as `cmd.exe`. The bot's commands are written for
it; a `DefaultShell` of PowerShell under `HKLM:\SOFTWARE\OpenSSH` breaks sleep
and `/status` details.

On the bot's host, make a key without a passphrase (the bot can't type one):

```sh
sudo ssh-keygen -t ed25519 -N "" -C candygoogy-bot -f /etc/candygoogy/ssh/id_ed25519
sudo chown root:candygoogy /etc/candygoogy/ssh/id_ed25519 && sudo chmod 640 /etc/candygoogy/ssh/id_ed25519
sudo cat /etc/candygoogy/ssh/id_ed25519.pub
```

For an administrator, Windows OpenSSH reads keys from
`C:\ProgramData\ssh\administrators_authorized_keys` (not the user's own
`.ssh` folder). Add the `.pub` line there, restricted as in section 2.

If the file already has keys, match the bot's line by fingerprint. On the bot's host:

```sh
sudo ssh-keygen -lf /etc/candygoogy/ssh/id_ed25519.pub
```

On the PC, this prints one fingerprint per line in the file:

```powershell
ssh-keygen -lf C:\ProgramData\ssh\administrators_authorized_keys
```

## 2. Restrict the key

Without restrictions, the key opens a full administrator shell for anyone who
holds a copy. Put these options **in front of** the bot's line, separated from
the key type by one space:

```
from="<bot host IP>",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAA... candygoogy-bot
```

| Option | Effect |
|---|---|
| `from="<bot host IP>"` | The key is refused from any other address. The important one. |
| `no-port-forwarding` | Can't tunnel into your LAN through the PC. |
| `no-agent-forwarding`, `no-X11-forwarding` | Unused features, turned off. |
| `no-pty` | No interactive terminal; the bot only runs single commands. |

Edit in an elevated editor (`notepad C:\ProgramData\ssh\administrators_authorized_keys`).
Leave the rest of the line (key type, key, comment) and other keys untouched.

## 3. Keep the file's permissions

sshd ignores this file unless only Administrators and SYSTEM can access it.
Editing in place keeps the permissions; check them:

```powershell
icacls C:\ProgramData\ssh\administrators_authorized_keys
```

Expected: only `BUILTIN\Administrators:(F)` and `NT AUTHORITY\SYSTEM:(F)`. If
something else appears (e.g. after copying the file in from elsewhere):

```powershell
icacls C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"
```

No sshd restart is needed; the file is read on every connection.

## 4. Test

From the bot's host (should print the Windows version):

```sh
sudo -u candygoogy ssh -i /etc/candygoogy/ssh/id_ed25519 -o BatchMode=yes \
    -o StrictHostKeyChecking=accept-new <SSH_USER>@<machine IP> ver
```

With Docker, the same inside the bot's container (its known_hosts is in the
state volume):

```sh
docker compose run --rm --no-deps bot ssh -i /etc/candygoogy/ssh/id_ed25519 -o BatchMode=yes \
    -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/var/lib/candygoogy-bot/known_hosts \
    <SSH_USER>@<machine IP> ver
```

Then in Telegram: `/status`, and `/shutdown` → **Cancel** (the prompt runs a
harmless `quser` over SSH, so a working prompt proves SSH works).

If it fails with `Permission denied (publickey)`, the `from=` address doesn't
match where the bot connects from. The PC's log shows the address it saw: Event
Viewer → Applications and Services Logs → OpenSSH → Operational.

## 5. Wake-on-LAN checklist

- **BIOS/UEFI:** "Wake on LAN" (or "Power on by PCI-E") on; "ErP/EuP ready"
  (deep power saving when off) **off**, or the network card has no power when
  the PC is off.
- **Device Manager → the network adapter → Power Management:** "Allow this
  device to wake the computer" and "Only allow a magic packet to wake the
  computer".
- **Advanced tab:** "Wake on Magic Packet" on, **"Wake on pattern match" off**.
  Otherwise the bot's own checks (ping, ARP) can wake the PC.
- **Fast Startup:** a Start-menu shutdown with Fast Startup on is a hybrid
  hibernate, and some network cards aren't armed for wake after it. The bot's
  `/shutdown` (`shutdown /s`) is a full shutdown. If wake fails only after a
  Start-menu shutdown, turn Fast Startup off (Control Panel → Power Options →
  "Choose what the power buttons do").
- Use a **wired** connection; Wi-Fi wake is rarely supported.
- Test: shut down with `/shutdown`, wait a minute, then `/wake`.

## Rollback

Delete the option prefix you added in section 2, leaving the line as it was, or
delete the bot's line to revoke the key completely.
