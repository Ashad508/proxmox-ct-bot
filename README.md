# Axo Proxmox Manager

`bot.py` is the complete Discord bot. This edition runs **directly on the Proxmox VE host** and calls `pct`, `pvesh`, `pvesm`, and `pveum` locally. It does not use a Proxmox API token, SSH-to-host feature, tmate, web shell, or arbitrary command endpoint.

For each Discord member, Axo can create:

- one unprivileged LXC container named `Axo-<DiscordUserID>`;
- one exclusive static address from `192.168.100.2` through `192.168.100.254` on `vmbr1`;
- one Proxmox login named `axo-<DiscordUserID>@pve`;
- an ACL only on `/vms/<VMID>` with `VM.Audit`, `VM.Console`, and `VM.PowerMgmt`;
- a separate random CT root/SSH password;
- a private DM containing the web URL, realm, both usernames, and both passwords.

Passwords are generated with Python's cryptographic `secrets` module. They are sent once by DM and are never written to SQLite or the bot log. `!reset-login` replaces both passwords.

## Important security action

The Discord bot token and old Proxmox API token posted in chat must be considered compromised. Reset the Discord token in **Discord Developer Portal -> Bot -> Reset Token**, and revoke the old Proxmox token in **Datacenter -> Permissions -> API Tokens**. This local version does not need a replacement Proxmox API token.

Do not paste either old token into the supplied `.env`.

## Where the bot must run

Install this bundle on Proxmox node `sum-085`, not on an ordinary remote Debian VPS. Direct mode only works where these commands exist:

```bash
pct --version
pvesh --version
pvesm --version
pveum --version
```

Proxmox is Debian-based, so the Python setup is familiar, but this service intentionally runs as root. Anyone who gains control of the bot process or an Axo administrator Discord account could perform destructive host operations. Keep the Discord token private, keep `MAIN_ADMIN_ID` correct, and use a dedicated/test Proxmox node until you have verified the workflow.

## 1. Prepare the Discord application

In the Discord Developer Portal, enable:

- Message Content Intent
- Server Members Intent

Invite the bot with View Channels, Send Messages, Embed Links, Read Message History, and Use External Emojis. Discord Administrator permission is not required.

Keep the intended user's DMs open before running `!create`; Axo refuses to provision if it cannot first confirm that private delivery works.

## 2. Prepare `vmbr1`

Axo expects this network to exist already:

```text
Bridge:  vmbr1
Subnet:  192.168.100.0/24
Gateway: 192.168.100.1
Pool:    192.168.100.2-192.168.100.254
```

Reserve the full pool for Axo. Do not place a DHCP pool, a VM, a physical device, or a manually managed guest on those addresses. Axo serializes allocations, enforces database uniqueness, and scans all cluster LXC configurations before each creation. It deliberately stops if an LXC on `vmbr1` uses DHCP or has an unknown IPv4 address.

The bridge/gateway must already route correctly. Private IPs are not publicly reachable by themselves; use your own routed private network, VPN, or carefully configured NAT/port forwarding. The bot does not modify the host firewall or network interfaces because a wrong automatic rule could disconnect the Proxmox host.

## 3. Download an LXC template

Run as root on `sum-085`:

```bash
pveam update
pveam available --section system
pveam download local <exact-template-filename>
pveam list local
```

Copy the exact `local:vztmpl/...tar.zst` volume ID from `pveam list local` into `CT_TEMPLATES_JSON` in `.env`. The filenames in the supplied configuration are examples and may have been superseded.

Root SSH auto-configuration currently supports Debian/Ubuntu templates using `apt`. With another distribution, set `CONFIGURE_ROOT_SSH=false`; the Proxmox console account will still work.

Root password SSH is enabled because it was requested. Do not expose port 22 directly to the public Internet without strong firewall/rate-limit protection; a private VPN or SSH keys are safer for production.

## 4. Install on the Proxmox host

Upload/extract the folder, then run:

```bash
apt update
apt install -y python3 python3-venv
mkdir -p /opt/axo-proxmox-manager
cp -a /path/to/axo-proxmox-manager/. /opt/axo-proxmox-manager/
cd /opt/axo-proxmox-manager
bash install.sh
nano .env
chmod 600 .env
```

In `.env`:

1. paste the newly reset Discord token into `DISCORD_TOKEN`;
2. confirm `MAIN_ADMIN_ID=1449782213947424861`;
3. replace the template filenames with the exact values installed on your node;
4. adjust the RAM/CPU/disk maximums if required.

There is no Proxmox host, API user, API token, or Cloudflare service-token setting in local mode.

## 5. Validate before connecting Discord

Run as root:

```bash
cd /opt/axo-proxmox-manager
.venv/bin/python bot.py --check
```

This verifies root execution, the node, `vmbr1`, storage, template volumes, and the live LXC IP inventory. It also creates the restricted `AxoCTUser` role if it is missing. It does not create a CT or member account.

If it reports a missing template, correct `CT_TEMPLATES_JSON`. If it reports DHCP/unknown IP usage on `vmbr1`, fix that guest before provisioning.

## 6. Test and enable systemd

First run it interactively:

```bash
cd /opt/axo-proxmox-manager
.venv/bin/python bot.py
```

After the Discord bot connects, press Ctrl+C and install the included service:

```bash
cp axo-proxmox-manager.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now axo-proxmox-manager
systemctl status axo-proxmox-manager --no-pager
journalctl -u axo-proxmox-manager -f
```

The systemd unit runs as root because local CT creation and Proxmox user/ACL management require root. It uses a private temporary directory, a restrictive umask, and read-only system paths where compatible with Proxmox operations.

## 7. First creation

From the Discord account in `MAIN_ADMIN_ID`:

```text
!ping
!templates
!create 2 2 20 @Member debian12
```

This example creates 2 GB RAM, 2 CPU cores, and a 20 GB root disk. Axo DMs the member:

- `https://usa-1.axonetwork.fun/`;
- realm `Proxmox VE authentication server`;
- their `axo-<DiscordID>@pve` login and random password;
- their `Axo-<DiscordID>` hostname, VMID, and private IP;
- the separate root/SSH password and `ssh root@<private-IP>` command.

The Proxmox login can see only its assigned VMID and can view details, open Console, and use power controls. It cannot create/delete guests, change resources/networking, browse other guests, or administer the node.

## Cloudflare tunnel note

`PROXMOX_WEB_URL` is the login URL that Axo sends; local provisioning does not travel through Cloudflare. If Cloudflare Access protects that hostname, each member still needs to satisfy your Cloudflare Access policy. A Proxmox username/password does not bypass Cloudflare Access. Ensure the tunnel supports the Proxmox web UI's WebSocket console traffic.

## Commands

Member commands:

```text
!myct / !myvps                 CT details and live usage
!manage                        private power-control panel
!reinstall [template]          destructive self-reinstall with confirmation
!reset-login                   reset both private passwords
!credentials                   explains how to reset lost credentials
!share-user @Member            share Discord power/stats access
!share-ruser @Member           revoke that Discord sharing
!manage-shared @Owner          use a shared control panel
!templates, !ports, !serverstats
```

Administrator commands:

```text
!create <ramGB> <cores> <diskGB> @Member [template]
!delete-ct <target> [reason]
!reinstall-ct <target> <template>
!reset-login <target>
!resize-ct <target> <ramGB|-> <cores|-> <diskGB|->
!suspend-ct <target> [reason]
!unsuspend-ct <target>
!restart-ct <target>
!ct-list, !lxc-list, !ip-pool, !sync-cts
!snapshot <target> [name]
!list-snapshots <target>
!restore-snapshot <target> <name>
!stop-ct-all, !audit-log, !backup-db
!admin-add @Member, !admin-remove @Member, !admin-list
```

`target` may be a Discord mention/ID, VMID, or hostname. Destructive delete, reinstall, restore, and stop-all actions use confirmation buttons.

## Files and recovery

- Database: `data/axo-manager.db`
- Application log: `logs/axo-manager.log`
- On-demand backups: `data/backups/`
- Configuration: `.env`

Back up the SQLite database and `.env` with root-only permissions. The database maps Discord users to VMIDs/IPs but intentionally contains no generated passwords. If a user loses a password, run `!reset-login`; do not try to recover the old value.

Useful diagnostics:

```bash
systemctl status axo-proxmox-manager --no-pager
journalctl -u axo-proxmox-manager -n 100 --no-pager
tail -n 100 /opt/axo-proxmox-manager/logs/axo-manager.log
cd /opt/axo-proxmox-manager && .venv/bin/python bot.py --check
```

Official references: [Proxmox `pct` manual](https://pve.proxmox.com/pve-docs/pct.1.html) and [Proxmox VE Administration Guide](https://pve.proxmox.com/pve-docs/pve-admin-guide.pdf).
