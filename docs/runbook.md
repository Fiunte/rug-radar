# Runbook (laptop = collector, desktop = everything else)

Steps the user runs by hand. Every `sudo` line says what it changes. Machine facts as of
2026-11-17: laptop Ubuntu 22.04 (jammy), Wi-Fi `wlp2s0`, address 192.168.1.7 from DHCP, router
192.168.1.1; the Wi-Fi profile is system-wide with a stored password, so it connects at boot
without anyone logging in.

## 1. Free disk space on the laptop (target: 20 GB free)

Status 2026-11-17 12:10 UTC: pip and JetBrains caches deleted by Claude (6.0 -> 11.5 GB free).
Chrome and VS Code were running, so their caches were left. Still to do: the three `sudo` rows and
the snap block (user), and the Chrome and VS Code caches once both apps are closed.

Safe: these are caches and old package versions. Programs rebuild them when needed, and nothing in
the project uses them. About 19.7 GB in total:

| What | Size | Command |
| --- | --- | --- |
| pip download cache | 4.0 GB | `rm -rf ~/.cache/pip` |
| Chrome cache (not profile, history or passwords) | 3.2 GB | close Chrome, then `rm -rf ~/.cache/google-chrome` |
| VS Code C++ IntelliSense cache | 2.0 GB | `rm -rf ~/.cache/vscode-cpptools` |
| JetBrains caches | 0.8 GB | `rm -rf ~/.cache/JetBrains` |
| Old snap revisions (disabled) | 5.3 GB | see below |
| System journal beyond 500 MB | ~3.4 GB | `sudo journalctl --vacuum-size=500M` (deletes old system log files) |
| Downloaded .deb packages | 1.0 GB | `sudo apt clean` (deletes cached installers; installed software is untouched) |

Old snap revisions (snap keeps the previous version of every snap, and these are the disabled ones):

```bash
sudo snap set system refresh.retain=2      # keep at most 2 revisions from now on
snap list --all | awk '/disabled/{print $1, $3}' | while read -r name rev; do
  sudo snap remove "$name" --revision="$rev"     # removes only that old, disabled revision
done
```

Your call; I cannot tell whether you still need these:
- `~/.docker` 22 GB: the Docker Desktop VM disk, with all images and containers. If you don't use
  Docker, open Docker Desktop -> Troubleshoot -> "Clean / Purge data". If you do, start it and run
  `docker system prune -a`, which removes unused images only.
- `~/Downloads` 25 GB (`Yelp-JSON.zip` 4.1 GB, `SEED-Ubuntu20.04.zip` 4.1 GB,
  `idea-IU-243.25659.59` 4.0 GB, Jackbox `.rar` plus its extracted folder 3.3 GB), `~/Desktop` 39 GB,
  `~/.steam` 16 GB.
- After the reboot in step 2, `sudo apt autoremove --purge` removes the old kernel
  6.8.0-138 (~0.3 GB).

Never delete anything under `~/rug-radar/data/`. It is the only copy until the backup in step 4
runs. Check with `df -h /` and `make status`.

## 2. Reboot test (do it once, after step 3, so you can check from the desktop)

1. On the laptop: `make status`, and note the uptime line.
2. `systemctl reboot`. If it asks for a password, use `sudo reboot`; both restart the machine.
   It will boot the newer kernel 6.8.0-149 that is already installed.
3. **Do not log in on the laptop.** Wait 3 minutes. Lingering is on, so the collector must
   start at boot without a login.
4. From the desktop: `ssh rugwatch-laptop 'uptime -s; systemctl --user show rugwatch-collector -p ActiveEnterTimestamp; cd rug-radar && make status'`.
   - Pass: `ActiveEnterTimestamp` is within about 1 minute of the boot time `uptime -s` prints, the
     service is `active`, the "last launch event age" is under a minute, and
     "service stops 10d" counts the clean stop at shutdown.
   - Fail: paste the output into a session.
5. No desktop yet? Log in on the laptop and run the same commands. The start time must be earlier
   than your login.

## 3. SSH server and a fixed address on the laptop

```bash
sudo apt install -y openssh-server        # installs the SSH server; it starts and starts at boot
sudo ufw status                           # if it says "Status: active", also run:
sudo ufw allow from 192.168.1.0/24 to any port 22 proto tcp   # SSH from the home network only
```

Fixed address. Option A is recommended because it changes nothing on the laptop:
- **A. Router reservation.** Get the Wi-Fi MAC with `cat /sys/class/net/wlp2s0/address`. In the
  router's web page (http://192.168.1.1; LAN / DHCP settings, usually "static lease" or
  "reservation"), reserve 192.168.1.7 for that MAC.
- **B. Static address on the laptop** (only if the router can't reserve). Pick an address outside
  the router's DHCP range, e.g. 192.168.1.200. Do this at the laptop, not over SSH, because the
  connection drops:
  `sudo nmcli con mod "NOS-F8E6-5 1" ipv4.method manual ipv4.addresses 192.168.1.200/24 ipv4.gateway 192.168.1.1 ipv4.dns 192.168.1.1`,
  then `sudo nmcli con up "NOS-F8E6-5 1"`. This sets the Wi-Fi profile to that fixed
  address. Use the new address in the desktop's `~/.ssh/config`.

After the desktop key works (step 4.3), turn off password logins:

```bash
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\n' | sudo tee /etc/ssh/sshd_config.d/10-rugwatch.conf
sudo systemctl restart ssh                # reloads the SSH server with key-only logins
```

Never forward port 22 on the router. The laptop is reachable from the home network only.

## 4. Desktop (Windows 10 + WSL)

### 4.1 WSL and base tools
In PowerShell, run `wsl -l -v`. You want an Ubuntu distribution with VERSION 2; note its exact name.
Optional: create `%UserProfile%\.wslconfig` containing `[wsl2]` and `memory=12GB` (WSL gets 8 GB by
default; parsing will want more). Then run `wsl --shutdown` once.

In the Ubuntu terminal:

```bash
sudo apt update && sudo apt install -y git make rsync openssh-client curl   # base tools
curl -LsSf https://astral.sh/uv/install.sh | sh                              # uv (fetches Python 3.12 itself)
```

Open a new terminal afterwards.

### 4.2 GitHub access and the repo
Keep the repo in the Linux home (`~/rug-radar`), not under `/mnt/c`, which is slow for many small
files.

```bash
ssh-keygen -t ed25519 -C "desktop-wsl github"            # Enter for the default path; passphrase optional
cat ~/.ssh/id_ed25519.pub                                 # paste into GitHub -> Settings -> SSH and GPG keys
git clone git@github.com:Fiunte/rug-radar.git ~/rug-radar
cd ~/rug-radar && make setup && make test
```

The desktop needs no API keys. `make setup` creates an `.env` from the example; leave it empty.

### 4.3 SSH key for the laptop and the first backup

```bash
ssh-keygen -t ed25519 -f ~/.ssh/rugwatch_laptop -N "" -C "desktop backup"   # no passphrase: the weekly task runs unattended
cat >> ~/.ssh/config <<'EOF'
Host rugwatch-laptop
    HostName 192.168.1.7
    User pedro
    IdentityFile ~/.ssh/rugwatch_laptop
    IdentitiesOnly yes
EOF
ssh-copy-id -i ~/.ssh/rugwatch_laptop.pub rugwatch-laptop   # asks for the laptop password once
ssh rugwatch-laptop 'cd rug-radar && make status'           # must work without a password now
cd ~/rug-radar && make backup                               # first full pull of data/
```

Optional hardening on the laptop: in `~/.ssh/authorized_keys`, put `from="192.168.1.0/24" ` in
front of the `desktop backup` key line. Then disable password logins (end of step 3).

`make backup` takes a consistent copy of the live state database on the laptop, then pulls
`data/raw/`. Raw files are append-only, so later runs copy only new bytes. It never deletes
anything on either side.

### 4.4 Weekly backup (Windows Task Scheduler)
Task Scheduler -> Create Basic Task -> "rugwatch backup" -> Weekly, Sunday 12:00 -> Start a program:
- Program: `wsl.exe`
- Arguments: `-d Ubuntu -- bash -lc "cd ~/rug-radar && make backup >> ~/rugwatch-backup.log 2>&1"`
  (replace `Ubuntu` with the name from 4.1)

Then open the task's Properties -> Settings and tick "Run task as soon as possible after a
scheduled start is missed". Check `~/rugwatch-backup.log` after the first run.

### 4.5 Claude Code on the desktop
In the Ubuntu (WSL) terminal, not PowerShell:

```bash
curl -fsSL https://claude.ai/install.sh | bash   # official native installer
```

Open a new terminal, run `claude --version`, then `cd ~/rug-radar && claude` and log in through the
browser. The repo's `CLAUDE.md` tells every session which machine it is on and how to work.
Before working on a machine, run `git pull --ff-only` there, because both machines push to the same repo.

## 5. RPC fallback check (laptop)
- Alchemy: enable Solana Mainnet for the app in the Alchemy dashboard. The collector retries it
  hourly, so no restart is needed.
- FluxRPC: copy the dashboard's **"RPC"** value (the whole URL, starting with `https://`) into `.env`
  as `FLUXRPC_RPC_URL=<that value>`. Leave `FLUXRPC_API_KEY` as it is; the URL takes precedence.

Then run `make check-env`; it prints names and `ok`/errors only, never values. If both rpc lines
say `ok`, run `systemctl --user restart rugwatch-collector` so the collector loads the new
variable. If FluxRPC still fails, say so and it will be removed (decision D-20).

## 6. Letting Claude push without asking
In Claude Code, run `/permissions` -> Allow -> add the rule `Bash(git push origin main)`. Force pushes
stay forbidden by `CLAUDE.md`.

## 7. Phone alerts, daily summary and reminders (decision D-35)
A systemd timer runs the health checks every 10 minutes (`make install-service` installs it). It
sends a notification when a check has failed twice in a row, one "Recovered" message when it has
passed twice in a row, and nothing in between. A daily summary arrives at 12:30 Lisbon time. If it
doesn't arrive, the alerting itself is down: say so to Claude. Reminders come from the
"Checkpoints and reminders" table in `docs/status.md`.

Setup (once):
1. Install the **ntfy** app (Google Play / F-Droid / App Store).
2. In a terminal on the laptop (a normal terminal, so the topic name stays out of the chat):
   `cd ~/rug-radar && t=$(openssl rand -hex 12) && printf 'NTFY_TOPIC=%s\n' "$t" >> .env && echo "$t"`
   It prints the topic name. Anyone who knows the name can read the alerts, so keep it private.
3. In the ntfy app: **+** -> topic = the printed name, server `ntfy.sh` (default) -> Subscribe.
4. healthchecks.io (free): sign up, **Add Check**, name it `heartbeat`, Period **10 minutes**, Grace
   **20 minutes**. Under **Integrations** add **ntfy** (server `https://ntfy.sh`, the same topic,
   priority high), or keep the e-mail integration. Copy the check's ping URL
   (`https://hc-ping.com/...`) into `.env` as `HEALTHCHECKS_PING_URL=<that URL>`.
5. Tell Claude "alerts are set up". Claude runs `make notify-test`, which sends a test notification
   and the first ping. `make health` shows every check now.

The laptop can't report its own outage: if it is off, asleep or offline, healthchecks.io notices
that the pings have stopped and alerts within about 30 minutes.

### Health checks

Normal ranges are from 2026-11-17/18. Thresholds are rechecked at day 7.

| Check | Alert when | Normal | A failure means |
| --- | --- | --- | --- |
| collector service | not `active` | active | The collector is down. systemd restarts it within 5 s, so a lasting failure is a crash loop or a config error. |
| collector heartbeat | older than 300 s | under 70 s | Running but stuck. The systemd watchdog should kill and restart it. |
| last launch seen | older than 900 s | seconds | Both launch streams are silent. The stall watchdog restarts after 600 s; this means it didn't help. |
| launches per hour | under 700 | 1,323-2,931 | Launches are being missed, or pump.fun activity collapsed. |
| create-log stream share | under 90% of launches | 99.8% | The public-RPC stream is connected but delivering little. |
| PumpPortal share | under 10% of launches | 20-26% | PumpPortal is degraded. It is also the only source of migrations. |
| launch coverage | under 95%, or no check in 26 h | 99.6-99.8% | The daily check against the chain failed or didn't run. |
| Helius credit projection | over 850,000/30 d, last throttle step, or hard stop | 715k-815k, throttle 0-2 | The free credit budget is at risk. |
| RPC error rate | over 5% of calls in the current + previous hour | ~0% | Helius/Alchemy are failing (key, outage, rate limit). |
| public RPC rate limiting | any in 24 h | 0 | The free endpoint is limiting us. The FluxRPC fallback should take over. |
| create-log stream endpoint | `fluxrpc` | `publicrpc` | The fallback is in use. That is fine for hours, but it uses FluxRPC bandwidth. |
| FluxRPC bandwidth | 80% of 10 GB this month | 0% | The fallback stops at 90%. |
| clock | not NTP-synced or offset over 2 s | under 10 ms | Timestamps are unreliable (HANDOFF risk 11). |
| free disk | under 10 GB | 26 GB | The collector stops at 0.5 GB. About 0.1 GB of raw data a day. |
| collector crashes | any in 24 h | 0 | A crash, watchdog kill or disk-full stop. Deploy restarts exit cleanly and don't count. |
| stall restarts | 3 or more in 24 h | 0 | One is the watchdog doing its job; repeated ones mean a source is broken. |
| collector step errors | 5 or more in 24 h | 0 | Internal exceptions in polling steps. |

## 8. Talking to Claude from the phone (Remote Control, decision D-36)
The laptop runs a Claude Code Remote Control server for this repo (`claude-remote` systemd user unit,
`make install-remote`). It starts at boot without a login. If it exits (for example after about
10 minutes offline), systemd restarts it after 30 s, and the restarted server brings back its
sessions (up to about 4 hours later; after that it starts a fresh one).

Once only, accept Remote Control in its terminal:
1. `tmux -L claude-rc attach -t rc`
2. Press `y` at `Enable Remote Control? (y/n)`. It shows the session URL; spacebar shows a QR code.
3. Detach with Ctrl+b, then d. The server keeps running.

Connect from the phone:
1. Install the Claude app and sign in with the same account as Claude Code on the laptop.
2. Tap **Code** in the navigation. The session `rug-radar` has a computer icon with a green dot
   while it is online. Tap it and type as here, for example "run the day-3 checkpoint". Scanning
   the QR code from step 2 above also opens it.
3. If the app asks you to verify the device (Trusted Devices), do it once.
4. Optional: in Claude Code run `/config` and turn on **Push when actions required**, so the phone
   is notified when Claude waits for a permission.

Notes:
- Claude runs on the laptop; the app only shows the session. While the laptop is off, the session
  is offline.
- A Remote Control transcript is stored on Anthropic's servers (that is how it syncs). Never paste
  keys into it, same as here.
- Phone sessions and terminal sessions share the repo folder; use one at a time.
- Check it: `systemctl --user status claude-remote`. Turn it off:
  `systemctl --user disable --now claude-remote`.
