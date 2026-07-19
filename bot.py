#!/usr/bin/env python3
"""Axo Proxmox Manager - one-file, local Proxmox VE Discord bot.

Run this file directly on the target Proxmox node as a tightly protected root
systemd service. It uses pct, pvesh, pvesm, and pveum locally; no Proxmox API
token is used. Passwords are generated with the secrets module, sent only by DM,
and never stored in SQLite or logs.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import string
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import discord
from discord.ext import commands
from dotenv import load_dotenv


BOT_NAME = "Axo Proxmox Manager"
BOT_VERSION = "2.0.0-local"
HOSTNAME_PREFIX = "Axo"
MANAGED_TAG = "axo-managed"
MENTION_RE = re.compile(r"^<@!?(\d+)>$")
SNAPSHOT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required setting: {name}")
    return value


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def parse_templates(raw: str) -> dict[str, str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("CT_TEMPLATES_JSON must be valid JSON") from exc
    if not isinstance(value, dict) or not value:
        raise ValueError("CT_TEMPLATES_JSON must be a non-empty JSON object")
    templates = {str(key).strip().lower(): str(item).strip() for key, item in value.items()}
    if any(not key or not item for key, item in templates.items()):
        raise ValueError("Template names and volume IDs cannot be empty")
    return templates


@dataclass(frozen=True, slots=True)
class Config:
    discord_token: str
    prefix: str
    main_admin_id: int
    admin_ids: frozenset[int]
    node: str
    web_url: str
    storage: str
    templates: Mapping[str, str]
    default_template: str
    bridge: str
    subnet: ipaddress.IPv4Network
    gateway: ipaddress.IPv4Address
    first_ip: ipaddress.IPv4Address
    last_ip: ipaddress.IPv4Address
    vmid_start: int
    vmid_end: int
    database_path: Path
    features: str
    unprivileged: bool
    onboot: bool
    nameserver: str | None
    configure_ssh: bool
    pve_realm: str
    pve_user_prefix: str
    pve_role: str
    pve_role_privileges: str
    password_length: int
    max_ram_gb: int
    max_cores: int
    max_disk_gb: int

    @classmethod
    def load(cls) -> "Config":
        load_dotenv(Path(__file__).with_name(".env"))
        templates = parse_templates(required("CT_TEMPLATES_JSON"))
        default_template = os.getenv("DEFAULT_TEMPLATE", next(iter(templates))).strip().lower()
        if default_template not in templates:
            raise ValueError("DEFAULT_TEMPLATE must exist in CT_TEMPLATES_JSON")

        subnet = ipaddress.ip_network(os.getenv("CT_SUBNET", "192.168.100.0/24"), strict=True)
        gateway = ipaddress.ip_address(os.getenv("CT_GATEWAY", "192.168.100.1"))
        first_ip = ipaddress.ip_address(os.getenv("CT_FIRST_IP", "192.168.100.2"))
        last_ip = ipaddress.ip_address(os.getenv("CT_LAST_IP", "192.168.100.254"))
        if not isinstance(subnet, ipaddress.IPv4Network) or not all(
            isinstance(value, ipaddress.IPv4Address)
            for value in (gateway, first_ip, last_ip)
        ):
            raise ValueError("Only IPv4 networks are supported")
        if any(value not in subnet for value in (gateway, first_ip, last_ip)):
            raise ValueError("Gateway and allocation range must be inside CT_SUBNET")
        if int(first_ip) > int(last_ip):
            raise ValueError("CT_FIRST_IP cannot be greater than CT_LAST_IP")
        if any(value in {subnet.network_address, subnet.broadcast_address, gateway}
               for value in (first_ip, last_ip)):
            raise ValueError("IP range endpoints cannot be the network, gateway, or broadcast")

        main_admin_id = int(required("MAIN_ADMIN_ID"))
        try:
            admins = {
                int(value.strip()) for value in os.getenv("ADMIN_IDS", "").split(",")
                if value.strip()
            }
        except ValueError as exc:
            raise ValueError("ADMIN_IDS must be comma-separated Discord IDs") from exc
        admins.add(main_admin_id)

        vmid_start = env_int("PROXMOX_VMID_START", 1000)
        vmid_end = env_int("PROXMOX_VMID_END", 999999)
        if vmid_start < 100 or vmid_start > vmid_end:
            raise ValueError("Invalid Proxmox VMID range")

        pve_realm = os.getenv("PVE_REALM", "pve").strip().lower()
        pve_user_prefix = os.getenv("PVE_USER_PREFIX", "axo").strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,15}", pve_user_prefix):
            raise ValueError("PVE_USER_PREFIX must use 2-16 lowercase letters/numbers/_/-")
        password_length = env_int("PASSWORD_LENGTH", 24)
        if password_length < 16 or password_length > 64:
            raise ValueError("PASSWORD_LENGTH must be between 16 and 64")

        return cls(
            discord_token=required("DISCORD_TOKEN"),
            prefix=os.getenv("PREFIX", "!").strip() or "!",
            main_admin_id=main_admin_id,
            admin_ids=frozenset(admins),
            node=(os.getenv("PROXMOX_NODE") or os.getenv("HOSTNAME", "pve")).strip(),
            web_url=os.getenv("PROXMOX_WEB_URL", "https://usa-1.axonetwork.fun/").strip().rstrip("/") + "/",
            storage=os.getenv("PROXMOX_STORAGE", "local-lvm").strip(),
            templates=templates,
            default_template=default_template,
            bridge=os.getenv("CT_BRIDGE", "vmbr1").strip(),
            subnet=subnet,
            gateway=gateway,
            first_ip=first_ip,
            last_ip=last_ip,
            vmid_start=vmid_start,
            vmid_end=vmid_end,
            database_path=Path(os.getenv("DATABASE_PATH", "data/axo-manager.db")),
            features=os.getenv("CT_FEATURES", "nesting=1,keyctl=1").strip(),
            unprivileged=env_bool("CT_UNPRIVILEGED", True),
            onboot=env_bool("CT_ONBOOT", False),
            nameserver=os.getenv("CT_NAMESERVER", "1.1.1.1").strip() or None,
            configure_ssh=env_bool("CONFIGURE_ROOT_SSH", True),
            pve_realm=pve_realm,
            pve_user_prefix=pve_user_prefix,
            pve_role=os.getenv("PVE_CT_USER_ROLE", "AxoCTUser").strip(),
            pve_role_privileges=os.getenv(
                "PVE_CT_USER_PRIVILEGES", "VM.Audit VM.Console VM.PowerMgmt"
            ).strip(),
            password_length=password_length,
            max_ram_gb=env_int("MAX_RAM_GB", 64),
            max_cores=env_int("MAX_CORES", 32),
            max_disk_gb=env_int("MAX_DISK_GB", 1000),
        )


@dataclass(frozen=True, slots=True)
class CTRecord:
    user_id: str
    guild_id: str
    vmid: int
    hostname: str
    ip_address: str
    pve_username: str
    template_key: str
    ram_mb: int
    cores: int
    disk_gb: int
    status: str
    suspended: bool
    ssh_ready: bool
    created_at: str
    updated_at: str

    @property
    def ram_gb(self) -> int:
        return self.ram_mb // 1024


@dataclass(frozen=True, slots=True)
class LoginCredentials:
    pve_username: str
    pve_password: str
    root_password: str
    ssh_ready: bool


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self, bootstrap_admins: Sequence[int]) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS admins (
                    user_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS containers (
                    user_id TEXT PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    vmid INTEGER NOT NULL UNIQUE,
                    hostname TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    ip_address TEXT NOT NULL UNIQUE,
                    pve_username TEXT,
                    template_key TEXT NOT NULL,
                    ram_mb INTEGER NOT NULL CHECK (ram_mb > 0),
                    cores INTEGER NOT NULL CHECK (cores > 0),
                    disk_gb INTEGER NOT NULL CHECK (disk_gb > 0),
                    status TEXT NOT NULL,
                    suspended INTEGER NOT NULL DEFAULT 0 CHECK (suspended IN (0, 1)),
                    ssh_ready INTEGER NOT NULL DEFAULT 0 CHECK (ssh_ready IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shared_access (
                    owner_id TEXT NOT NULL,
                    guest_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (owner_id, guest_id),
                    FOREIGN KEY (owner_id) REFERENCES containers(user_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_user_id TEXT,
                    vmid INTEGER,
                    details TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(containers)")
            }
            if "pve_username" not in columns:
                connection.execute("ALTER TABLE containers ADD COLUMN pve_username TEXT")
            if "ssh_ready" not in columns:
                connection.execute(
                    "ALTER TABLE containers ADD COLUMN ssh_ready INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS containers_pve_username_uq
                   ON containers(pve_username) WHERE pve_username IS NOT NULL"""
            )
            now = utc_now()
            connection.executemany(
                "INSERT OR IGNORE INTO admins (user_id, created_at) VALUES (?, ?)",
                ((str(user_id), now) for user_id in bootstrap_admins),
            )
            connection.commit()

    @staticmethod
    def record(row: sqlite3.Row | None) -> CTRecord | None:
        if row is None:
            return None
        return CTRecord(
            user_id=row["user_id"], guild_id=row["guild_id"], vmid=row["vmid"],
            hostname=row["hostname"], ip_address=row["ip_address"],
            pve_username=row["pve_username"] or "", template_key=row["template_key"],
            ram_mb=row["ram_mb"], cores=row["cores"], disk_gb=row["disk_gb"],
            status=row["status"], suspended=bool(row["suspended"]),
            ssh_ready=bool(row["ssh_ready"]), created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_user(self, user_id: str | int) -> CTRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM containers WHERE user_id = ?", (str(user_id),)
            ).fetchone()
        return self.record(row)

    def get_vmid(self, vmid: int) -> CTRecord | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM containers WHERE vmid = ?", (vmid,)).fetchone()
        return self.record(row)

    def get_hostname(self, hostname: str) -> CTRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM containers WHERE hostname = ? COLLATE NOCASE", (hostname,)
            ).fetchone()
        return self.record(row)

    def list_cts(self) -> list[CTRecord]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM containers ORDER BY vmid").fetchall()
        return [item for row in rows if (item := self.record(row))]

    def reserve(self, *, user_id: str, guild_id: str, vmid: int, hostname: str,
                ip_address: str, pve_username: str, template_key: str,
                ram_mb: int, cores: int, disk_gb: int) -> CTRecord:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO containers
                   (user_id, guild_id, vmid, hostname, ip_address, pve_username,
                    template_key, ram_mb, cores, disk_gb, status, suspended,
                    ssh_ready, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'allocating', 0, 0, ?, ?)""",
                (user_id, guild_id, vmid, hostname, ip_address, pve_username,
                 template_key, ram_mb, cores, disk_gb, now, now),
            )
            connection.commit()
        item = self.get_user(user_id)
        assert item
        return item

    def update(self, user_id: str | int, **changes: Any) -> CTRecord:
        allowed = {
            "pve_username", "template_key", "ram_mb", "cores", "disk_gb",
            "status", "suspended", "ssh_ready",
        }
        if set(changes) - allowed:
            raise ValueError("Unsupported database update")
        if not changes:
            item = self.get_user(user_id)
            if not item:
                raise KeyError(str(user_id))
            return item
        for boolean in ("suspended", "ssh_ready"):
            if boolean in changes:
                changes[boolean] = int(bool(changes[boolean]))
        changes["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in changes)
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE containers SET {assignments} WHERE user_id = ?",
                (*changes.values(), str(user_id)),
            )
            if cursor.rowcount != 1:
                raise KeyError(str(user_id))
            connection.commit()
        item = self.get_user(user_id)
        assert item
        return item

    def delete(self, user_id: str | int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM containers WHERE user_id = ?", (str(user_id),))
            connection.commit()

    def is_admin(self, user_id: str | int) -> bool:
        with self.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM admins WHERE user_id = ?", (str(user_id),)
            ).fetchone() is not None

    def add_admin(self, user_id: str | int) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO admins (user_id, created_at) VALUES (?, ?)",
                (str(user_id), utc_now()),
            )
            connection.commit()

    def remove_admin(self, user_id: str | int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM admins WHERE user_id = ?", (str(user_id),))
            connection.commit()

    def list_admins(self) -> list[str]:
        with self.connect() as connection:
            return [row["user_id"] for row in connection.execute(
                "SELECT user_id FROM admins ORDER BY user_id"
            ).fetchall()]

    def share(self, owner_id: str | int, guest_id: str | int) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO shared_access (owner_id, guest_id, created_at) VALUES (?, ?, ?)",
                (str(owner_id), str(guest_id), utc_now()),
            )
            connection.commit()

    def unshare(self, owner_id: str | int, guest_id: str | int) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM shared_access WHERE owner_id = ? AND guest_id = ?",
                (str(owner_id), str(guest_id)),
            )
            connection.commit()

    def has_share(self, owner_id: str | int, guest_id: str | int) -> bool:
        with self.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM shared_access WHERE owner_id = ? AND guest_id = ?",
                (str(owner_id), str(guest_id)),
            ).fetchone() is not None

    def audit(self, actor_id: str | int, action: str, item: CTRecord | None = None,
              details: str = "") -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO audit_log
                   (actor_id, action, target_user_id, vmid, details, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (str(actor_id), action, item.user_id if item else None,
                 item.vmid if item else None, details[:2000], utc_now()),
            )
            connection.commit()

    def recent_audit(self, limit: int = 15) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
                (min(max(limit, 1), 50),),
            ).fetchall()


class AxoError(RuntimeError):
    pass


class LocalProxmox:
    """Safe argv-only wrappers for Proxmox CLI tools; no shell interpolation."""

    REQUIRED_BINARIES = ("pct", "pvesh", "pvesm", "pveum")

    def __init__(self, config: Config):
        self.config = config
        self.lock = threading.RLock()
        self.logger = logging.getLogger("axo.proxmox")

    def run(self, args: Sequence[str], *, input_text: str | None = None,
            timeout: int = 600, check: bool = True) -> str:
        command = [str(value) for value in args]
        self.logger.info("Running local Proxmox command: %s %s", command[0], command[1] if len(command) > 1 else "")
        try:
            result = subprocess.run(
                command, input=input_text, capture_output=True, text=True,
                timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AxoError(f"{command[0]} {command[1] if len(command) > 1 else ''} timed out") from exc
        output = (result.stdout or "").strip()
        error = (result.stderr or "").strip()
        if check and result.returncode != 0:
            raise AxoError(error or output or f"Command exited with code {result.returncode}")
        return output

    def json(self, args: Sequence[str], *, timeout: int = 120) -> Any:
        output = self.run([*args, "--output-format", "json"], timeout=timeout)
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise AxoError(f"Invalid JSON from {args[0]} {args[1] if len(args) > 1 else ''}") from exc

    def preflight(self) -> dict[str, Any]:
        if os.geteuid() != 0:
            raise AxoError("Local mode must run as root on the Proxmox node")
        missing = [name for name in self.REQUIRED_BINARIES if not shutil.which(name)]
        if missing:
            raise AxoError(f"Missing Proxmox commands: {', '.join(missing)}")
        node = self.json(["pvesh", "get", f"/nodes/{self.config.node}/status"])
        bridge = self.json([
            "pvesh", "get", f"/nodes/{self.config.node}/network/{self.config.bridge}"
        ])
        storage = self.json(["pvesm", "status", "--storage", self.config.storage])
        template_volumes: set[str] = set()
        for template_storage in {
            value.split(":", 1)[0] for value in self.config.templates.values()
        }:
            for item in self.json([
                "pvesm", "list", template_storage, "--content", "vztmpl"
            ]):
                if item.get("volid"):
                    template_volumes.add(str(item["volid"]))
        _, _, live_ips = self.live_inventory()
        return {
            "node": node,
            "bridge": bridge,
            "storage": storage,
            "live_ips": live_ips,
            "missing_templates": {
                key: value for key, value in self.config.templates.items()
                if value not in template_volumes
            },
        }

    def cluster_lxcs(self) -> list[dict[str, Any]]:
        resources = self.json(["pvesh", "get", "/cluster/resources", "--type", "vm"])
        return [item for item in resources if item.get("type") == "lxc"]

    def live_inventory(self) -> tuple[set[int], set[str], set[str]]:
        vmids: set[int] = set()
        hostnames: set[str] = set()
        addresses: set[str] = set()
        for item in self.cluster_lxcs():
            vmid = int(item["vmid"])
            node = str(item.get("node") or self.config.node)
            vmids.add(vmid)
            if item.get("name"):
                hostnames.add(str(item["name"]).lower())
            config = self.json([
                "pvesh", "get", f"/nodes/{node}/lxc/{vmid}/config"
            ])
            if config.get("hostname"):
                hostnames.add(str(config["hostname"]).lower())
            for key, raw in config.items():
                if not str(key).startswith("net") or not isinstance(raw, str):
                    continue
                values = {}
                for pair in raw.split(","):
                    name, separator, value = pair.partition("=")
                    if separator:
                        values[name.strip()] = value.strip()
                if values.get("bridge") != self.config.bridge:
                    continue
                configured_ip = values.get("ip", "")
                if configured_ip.lower() in {"", "dhcp", "manual"}:
                    raise AxoError(
                        f"CT {vmid} has DHCP/unknown IPv4 on {self.config.bridge}; "
                        "unique allocation cannot be proven"
                    )
                address = configured_ip.split("/", 1)[0]
                try:
                    parsed = ipaddress.ip_address(address)
                except ValueError:
                    continue
                if isinstance(parsed, ipaddress.IPv4Address):
                    addresses.add(str(parsed))
        return vmids, hostnames, addresses

    def exists(self, vmid: int) -> bool:
        return any(int(item["vmid"]) == vmid for item in self.cluster_lxcs())

    def status_for(self, vmid: int) -> dict[str, Any]:
        return self.json([
            "pvesh", "get", f"/nodes/{self.config.node}/lxc/{vmid}/status/current"
        ])

    def config_for(self, vmid: int) -> dict[str, Any]:
        return self.json([
            "pvesh", "get", f"/nodes/{self.config.node}/lxc/{vmid}/config"
        ])

    def node_status(self) -> dict[str, Any]:
        return self.json(["pvesh", "get", f"/nodes/{self.config.node}/status"])

    def ensure_role(self) -> None:
        roles = self.json(["pveum", "role", "list"])
        existing = next(
            (item for item in roles if item.get("roleid") == self.config.pve_role), None
        )
        required_privs = set(self.config.pve_role_privileges.split())
        if existing:
            actual = set(str(existing.get("privs", "")).split())
            if actual != required_privs:
                differences = []
                if required_privs - actual:
                    differences.append(
                        "missing " + ", ".join(sorted(required_privs - actual))
                    )
                if actual - required_privs:
                    differences.append(
                        "unexpected " + ", ".join(sorted(actual - required_privs))
                    )
                raise AxoError(
                    f"Existing role {self.config.pve_role} does not exactly match the "
                    f"restricted policy ({'; '.join(differences)})"
                )
            return
        self.run([
            "pveum", "role", "add", self.config.pve_role,
            "--privs", self.config.pve_role_privileges,
        ])

    def pve_user_exists(self, username: str) -> bool:
        return any(
            item.get("userid") == username
            for item in self.json(["pveum", "user", "list"])
        )

    def create_pve_user(self, item: CTRecord, password: str) -> None:
        if self.pve_user_exists(item.pve_username):
            raise AxoError(f"Proxmox user {item.pve_username} already exists")
        self.ensure_role()
        self.run([
            "pveum", "user", "add", item.pve_username,
            "--password", password,
            "--enable", "1",
            "--comment", f"Managed by {BOT_NAME}; Discord user {item.user_id}",
        ])
        try:
            self.run([
                "pveum", "acl", "modify", f"/vms/{item.vmid}",
                "--users", item.pve_username,
                "--roles", self.config.pve_role,
            ])
        except Exception:
            self.run(["pveum", "user", "delete", item.pve_username], check=False)
            raise

    def set_access_user(self, item: CTRecord, password: str) -> None:
        self.ensure_role()
        if self.pve_user_exists(item.pve_username):
            self.reset_pve_password(item.pve_username, password)
        else:
            self.run([
                "pveum", "user", "add", item.pve_username,
                "--password", password,
                "--enable", "1",
                "--comment", f"Managed by {BOT_NAME}; Discord user {item.user_id}",
            ])
        self.run([
            "pveum", "acl", "modify", f"/vms/{item.vmid}",
            "--users", item.pve_username,
            "--roles", self.config.pve_role,
        ])

    def delete_pve_user(self, username: str) -> None:
        if username and self.pve_user_exists(username):
            self.run(["pveum", "user", "delete", username])

    def reset_pve_password(self, username: str, password: str) -> None:
        # `pveum passwd` prompts interactively. The user-modify endpoint accepts
        # a password argument and therefore works safely under systemd.
        self.run(["pveum", "user", "modify", username, "--password", password])

    def reset_root_password(self, vmid: int, password: str) -> None:
        status = self.run(["pct", "status", str(vmid)], timeout=30).lower()
        was_running = "running" in status
        if not was_running:
            self.run(["pct", "start", str(vmid)], timeout=120)
        try:
            self.wait_until_exec_ready(vmid)
            self.configure_root_password(vmid, password)
        finally:
            if not was_running:
                self.run(["pct", "stop", str(vmid)], timeout=120, check=False)

    def configure_root_password(self, vmid: int, password: str) -> None:
        self.run(
            ["pct", "exec", str(vmid), "--", "chpasswd"],
            input_text=f"root:{password}\n", timeout=60,
        )

    def configure_ssh(self, vmid: int) -> bool:
        if not self.config.configure_ssh:
            return False
        script = """
set -eu
if ! command -v sshd >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y openssh-server
  else
    exit 20
  fi
fi
mkdir -p /etc/ssh/sshd_config.d
printf '%s\n' 'PasswordAuthentication yes' 'PermitRootLogin yes' > /etc/ssh/sshd_config.d/99-axo-login.conf
systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || service ssh restart
""".strip()
        try:
            self.run(["pct", "exec", str(vmid), "--", "bash", "-lc", script], timeout=300)
            return True
        except Exception as exc:
            self.logger.warning("SSH setup failed for CT %s: %s", vmid, exc)
            return False

    def wait_until_exec_ready(self, vmid: int, timeout: int = 45) -> None:
        deadline = time.monotonic() + timeout
        last_error = "container not ready"
        while time.monotonic() < deadline:
            try:
                self.run(["pct", "exec", str(vmid), "--", "true"], timeout=10)
                return
            except Exception as exc:
                last_error = str(exc)
                time.sleep(2)
        raise AxoError(f"CT started but guest execution was not ready: {last_error}")

    def create(self, item: CTRecord, root_password: str, pve_password: str,
               *, create_access_user: bool = True) -> bool:
        template = self.config.templates[item.template_key]
        net0 = (
            f"name=eth0,bridge={self.config.bridge},"
            f"ip={item.ip_address}/{self.config.subnet.prefixlen},"
            f"gw={self.config.gateway},firewall=1,type=veth"
        )
        command = [
            "pct", "create", str(item.vmid), template,
            "--hostname", item.hostname,
            "--cores", str(item.cores),
            "--memory", str(item.ram_mb),
            "--swap", str(min(item.ram_mb, 1024)),
            "--rootfs", f"{self.config.storage}:{item.disk_gb}",
            "--net0", net0,
            "--unprivileged", "1" if self.config.unprivileged else "0",
            "--onboot", "1" if self.config.onboot else "0",
            "--start", "1",
            "--tags", MANAGED_TAG,
            "--description", f"Managed by {BOT_NAME}; Discord user {item.user_id}",
        ]
        if self.config.features:
            command.extend(["--features", self.config.features])
        if self.config.nameserver:
            command.extend(["--nameserver", self.config.nameserver])
        created_user = False
        try:
            self.run(command, timeout=900)
            self.wait_until_exec_ready(item.vmid)
            self.configure_root_password(item.vmid, root_password)
            ssh_ready = self.configure_ssh(item.vmid)
            if create_access_user:
                self.create_pve_user(item, pve_password)
                created_user = True
            return ssh_ready
        except Exception:
            if created_user:
                self.delete_pve_user(item.pve_username)
            if self.exists(item.vmid):
                self.run(["pct", "stop", str(item.vmid)], check=False, timeout=60)
                self.run(["pct", "destroy", str(item.vmid), "--purge", "1", "--force", "1"], check=False)
            raise

    def power(self, vmid: int, action: str) -> None:
        if action not in {"start", "stop", "shutdown", "reboot"}:
            raise AxoError("Unsupported power action")
        command = ["pct", action, str(vmid)]
        if action in {"shutdown", "reboot"}:
            command.extend(["--timeout", "60"])
        self.run(command, timeout=120)

    def destroy(self, vmid: int) -> None:
        if not self.exists(vmid):
            return
        status = self.status_for(vmid)
        if status.get("status") == "running":
            self.run(["pct", "stop", str(vmid)], timeout=90)
        self.run([
            "pct", "destroy", str(vmid), "--purge", "1",
            "--destroy-unreferenced-disks", "1", "--force", "1",
        ], timeout=600)

    def resize(self, item: CTRecord, ram_mb: int | None, cores: int | None,
               disk_gb: int | None) -> None:
        changes = ["pct", "set", str(item.vmid)]
        if ram_mb is not None:
            changes.extend(["--memory", str(ram_mb), "--swap", str(min(ram_mb, 1024))])
        if cores is not None:
            changes.extend(["--cores", str(cores)])
        if len(changes) > 3:
            self.run(changes)
        if disk_gb is not None and disk_gb > item.disk_gb:
            self.run(["pct", "resize", str(item.vmid), "rootfs", f"{disk_gb}G"])

    def snapshots(self, vmid: int) -> list[dict[str, Any]]:
        return self.json(["pct", "listsnapshot", str(vmid)])

    def snapshot(self, vmid: int, name: str, actor_id: int) -> None:
        self.run([
            "pct", "snapshot", str(vmid), name,
            "--description", f"Created through Discord by {actor_id}",
        ], timeout=600)

    def rollback(self, vmid: int, name: str) -> None:
        self.run(["pct", "rollback", str(vmid), name], timeout=900)


class CTService:
    def __init__(self, config: Config, database: Database, proxmox: LocalProxmox):
        self.config = config
        self.db = database
        self.proxmox = proxmox
        self.allocation_lock = asyncio.Lock()
        self.destructive_lock = asyncio.Lock()

    async def call(self, function: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(function, *args, **kwargs)

    def generate_password(self) -> str:
        alphabet = string.ascii_letters + string.digits + "!@#%_-"
        while True:
            password = "".join(secrets.choice(alphabet) for _ in range(self.config.password_length))
            if (any(value.islower() for value in password)
                    and any(value.isupper() for value in password)
                    and any(value.isdigit() for value in password)
                    and any(value in "!@#%_-" for value in password)):
                return password

    def hostname_for(self, user_id: str | int) -> str:
        return f"{HOSTNAME_PREFIX}-{user_id}"

    def pve_username_for(self, user_id: str | int) -> str:
        return f"{self.config.pve_user_prefix}-{user_id}@{self.config.pve_realm}"

    def validate_resources(self, ram_gb: int, cores: int, disk_gb: int) -> None:
        if not 1 <= ram_gb <= self.config.max_ram_gb:
            raise AxoError(f"RAM must be 1-{self.config.max_ram_gb} GB")
        if not 1 <= cores <= self.config.max_cores:
            raise AxoError(f"CPU cores must be 1-{self.config.max_cores}")
        if not 1 <= disk_gb <= self.config.max_disk_gb:
            raise AxoError(f"Disk must be 1-{self.config.max_disk_gb} GB")

    def choose_ip(self, used: set[str]) -> str:
        for integer in range(int(self.config.first_ip), int(self.config.last_ip) + 1):
            candidate = str(ipaddress.ip_address(integer))
            if candidate not in used:
                return candidate
        raise AxoError("The CT IPv4 pool is exhausted")

    def choose_vmid(self, used: set[int]) -> int:
        for vmid in range(self.config.vmid_start, self.config.vmid_end + 1):
            if vmid not in used:
                return vmid
        raise AxoError("The configured VMID range is exhausted")

    async def create_ct(self, *, actor_id: int, guild_id: int, user_id: int,
                        ram_gb: int, cores: int, disk_gb: int,
                        template_key: str) -> tuple[CTRecord, LoginCredentials]:
        template_key = template_key.lower()
        self.validate_resources(ram_gb, cores, disk_gb)
        if template_key not in self.config.templates:
            raise AxoError(f"Unknown template `{template_key}`")
        async with self.allocation_lock:
            existing = await self.call(self.db.get_user, user_id)
            if existing:
                raise AxoError(
                    f"<@{user_id}> already owns `{existing.hostname}` (VMID {existing.vmid})"
                )
            live_vmids, live_hostnames, live_ips = await self.call(self.proxmox.live_inventory)
            records = await self.call(self.db.list_cts)
            hostname = self.hostname_for(user_id)
            pve_username = self.pve_username_for(user_id)
            if hostname.lower() in live_hostnames:
                raise AxoError(f"A live CT already uses `{hostname}`")
            if await self.call(self.proxmox.pve_user_exists, pve_username):
                raise AxoError(
                    f"Proxmox login `{pve_username}` already exists; resolve it before creation"
                )
            vmid = self.choose_vmid(live_vmids | {item.vmid for item in records})
            address = self.choose_ip(live_ips | {item.ip_address for item in records})
            try:
                item = await self.call(
                    self.db.reserve,
                    user_id=str(user_id), guild_id=str(guild_id), vmid=vmid,
                    hostname=hostname, ip_address=address, pve_username=pve_username,
                    template_key=template_key, ram_mb=ram_gb * 1024,
                    cores=cores, disk_gb=disk_gb,
                )
            except sqlite3.IntegrityError as exc:
                raise AxoError("An allocation changed concurrently; try again") from exc

            pve_password = self.generate_password()
            root_password = self.generate_password()
            try:
                ssh_ready = await self.call(
                    self.proxmox.create, item, root_password, pve_password
                )
            except Exception:
                still_exists = True
                try:
                    still_exists = await self.call(self.proxmox.exists, item.vmid)
                except Exception:
                    pass
                if still_exists:
                    await self.call(self.db.update, item.user_id, status="creation-error")
                else:
                    await self.call(self.db.delete, item.user_id)
                raise
            item = await self.call(
                self.db.update, item.user_id, status="running", ssh_ready=ssh_ready
            )
            await self.call(self.db.audit, actor_id, "create", item, f"template={template_key}")
            return item, LoginCredentials(
                pve_username=pve_username, pve_password=pve_password,
                root_password=root_password, ssh_ready=ssh_ready,
            )

    async def get_record(self, target: str | int | None, default_user_id: int) -> CTRecord:
        if target is None or not str(target).strip():
            item = await self.call(self.db.get_user, default_user_id)
        else:
            raw = str(target).strip()
            mention = MENTION_RE.match(raw)
            if mention:
                item = await self.call(self.db.get_user, mention.group(1))
            elif raw.isdigit() and int(raw) > self.config.vmid_end:
                item = await self.call(self.db.get_user, raw)
            elif raw.isdigit():
                item = await self.call(self.db.get_vmid, int(raw))
                if not item:
                    item = await self.call(self.db.get_user, raw)
            else:
                item = await self.call(self.db.get_hostname, raw)
        if not item:
            raise AxoError("No managed CT matches that user, hostname, or VMID")
        return item

    async def live_status(self, item: CTRecord) -> dict[str, Any]:
        try:
            status = await self.call(self.proxmox.status_for, item.vmid)
        except Exception as exc:
            await self.call(self.db.update, item.user_id, status="unreachable")
            raise AxoError(f"Could not read CT {item.vmid}: {exc}") from exc
        live = str(status.get("status", "unknown"))
        if live != item.status and not item.suspended:
            await self.call(self.db.update, item.user_id, status=live)
        return status

    async def power(self, actor_id: int, item: CTRecord, action: str,
                    admin_override: bool = False) -> CTRecord:
        if item.suspended and not admin_override:
            raise AxoError("This CT is suspended")
        await self.call(self.proxmox.power, item.vmid, action)
        status = "running" if action in {"start", "reboot"} else "stopped"
        item = await self.call(self.db.update, item.user_id, status=status)
        await self.call(self.db.audit, actor_id, f"power:{action}", item)
        return item

    async def delete_ct(self, actor_id: int, item: CTRecord, reason: str) -> None:
        async with self.destructive_lock:
            await self.call(self.proxmox.destroy, item.vmid)
            user_cleanup_error = None
            try:
                await self.call(self.proxmox.delete_pve_user, item.pve_username)
            except Exception as exc:
                user_cleanup_error = str(exc)
            await self.call(self.db.audit, actor_id, "delete", item, reason)
            await self.call(self.db.delete, item.user_id)
            if user_cleanup_error:
                raise AxoError(
                    f"CT deleted, but Proxmox user cleanup failed: {user_cleanup_error}"
                )

    async def reinstall(self, actor_id: int, item: CTRecord,
                        template_key: str) -> tuple[CTRecord, LoginCredentials]:
        template_key = template_key.lower()
        if template_key not in self.config.templates:
            raise AxoError(f"Unknown template `{template_key}`")
        if item.suspended:
            raise AxoError("A suspended CT cannot be reinstalled")
        pve_password = self.generate_password()
        root_password = self.generate_password()
        async with self.destructive_lock:
            await self.call(self.db.update, item.user_id, status="reinstalling")
            await self.call(self.proxmox.destroy, item.vmid)
            changed = await self.call(
                self.db.update, item.user_id, template_key=template_key, status="allocating"
            )
            try:
                ssh_ready = await self.call(
                    self.proxmox.create, changed, root_password, pve_password,
                    create_access_user=False,
                )
                await self.call(self.proxmox.set_access_user, changed, pve_password)
            except Exception:
                await self.call(self.db.update, item.user_id, status="reinstall-error")
                raise
            changed = await self.call(
                self.db.update, item.user_id, status="running", ssh_ready=ssh_ready
            )
            await self.call(self.db.audit, actor_id, "reinstall", changed, template_key)
            return changed, LoginCredentials(
                pve_username=changed.pve_username, pve_password=pve_password,
                root_password=root_password, ssh_ready=ssh_ready,
            )

    async def reset_credentials(self, actor_id: int,
                                item: CTRecord) -> LoginCredentials:
        pve_password = self.generate_password()
        root_password = self.generate_password()
        await self.call(self.proxmox.set_access_user, item, pve_password)
        await self.call(self.proxmox.reset_root_password, item.vmid, root_password)
        ssh_ready = item.ssh_ready
        if self.config.configure_ssh and not ssh_ready:
            status = await self.live_status(item)
            if status.get("status") != "running":
                await self.call(self.proxmox.power, item.vmid, "start")
            await self.call(self.proxmox.wait_until_exec_ready, item.vmid)
            ssh_ready = await self.call(self.proxmox.configure_ssh, item.vmid)
            item = await self.call(self.db.update, item.user_id, ssh_ready=ssh_ready)
        await self.call(self.db.audit, actor_id, "credentials:reset", item)
        return LoginCredentials(
            pve_username=item.pve_username, pve_password=pve_password,
            root_password=root_password, ssh_ready=ssh_ready,
        )

    async def resize(self, actor_id: int, item: CTRecord, ram_gb: int | None,
                     cores: int | None, disk_gb: int | None) -> CTRecord:
        new_ram = ram_gb if ram_gb is not None else item.ram_gb
        new_cores = cores if cores is not None else item.cores
        new_disk = disk_gb if disk_gb is not None else item.disk_gb
        self.validate_resources(new_ram, new_cores, new_disk)
        if disk_gb is not None and disk_gb < item.disk_gb:
            raise AxoError("CT root disks cannot be shrunk")
        await self.call(
            self.proxmox.resize, item,
            ram_gb * 1024 if ram_gb is not None else None, cores, disk_gb,
        )
        item = await self.call(
            self.db.update, item.user_id, ram_mb=new_ram * 1024,
            cores=new_cores, disk_gb=new_disk,
        )
        await self.call(self.db.audit, actor_id, "resize", item)
        return item

    async def suspend(self, actor_id: int, item: CTRecord, reason: str) -> CTRecord:
        status = await self.live_status(item)
        if status.get("status") == "running":
            await self.call(self.proxmox.power, item.vmid, "stop")
        item = await self.call(
            self.db.update, item.user_id, suspended=True, status="stopped"
        )
        await self.call(self.db.audit, actor_id, "suspend", item, reason)
        return item

    async def unsuspend(self, actor_id: int, item: CTRecord) -> CTRecord:
        await self.call(self.proxmox.power, item.vmid, "start")
        item = await self.call(
            self.db.update, item.user_id, suspended=False, status="running"
        )
        await self.call(self.db.audit, actor_id, "unsuspend", item)
        return item

    async def create_snapshot(self, actor_id: int, item: CTRecord, name: str) -> None:
        if not SNAPSHOT_RE.fullmatch(name):
            raise AxoError("Snapshot name must start with a letter and use up to 40 letters/numbers/_/-")
        await self.call(self.proxmox.snapshot, item.vmid, name, actor_id)
        await self.call(self.db.audit, actor_id, "snapshot", item, name)

    async def restore_snapshot(self, actor_id: int, item: CTRecord, name: str) -> None:
        snapshots = await self.call(self.proxmox.snapshots, item.vmid)
        if name not in {value.get("name") for value in snapshots}:
            raise AxoError(f"Snapshot `{name}` does not exist")
        status = await self.live_status(item)
        running = status.get("status") == "running"
        if running:
            await self.call(self.proxmox.power, item.vmid, "stop")
        await self.call(self.proxmox.rollback, item.vmid, name)
        if running:
            await self.call(self.proxmox.power, item.vmid, "start")
        await self.call(self.db.audit, actor_id, "snapshot:restore", item, name)

    async def reconcile(self, actor_id: int) -> tuple[int, int]:
        live = {int(value["vmid"]): value for value in await self.call(self.proxmox.cluster_lxcs)}
        updated = missing = 0
        for item in await self.call(self.db.list_cts):
            status = str(live[item.vmid].get("status", "unknown")) if item.vmid in live else "missing"
            if status == "missing":
                missing += 1
            if status != item.status and not item.suspended:
                await self.call(self.db.update, item.user_id, status=status)
                updated += 1
        await self.call(self.db.audit, actor_id, "sync", details=f"updated={updated},missing={missing}")
        return updated, missing

    async def stop_all(self, actor_id: int) -> tuple[int, list[str]]:
        stopped = 0
        errors: list[str] = []
        for item in await self.call(self.db.list_cts):
            try:
                status = await self.live_status(item)
                if status.get("status") == "running":
                    await self.call(self.proxmox.power, item.vmid, "stop")
                    await self.call(self.db.update, item.user_id, status="stopped")
                    stopped += 1
            except Exception as exc:
                errors.append(f"{item.vmid}: {exc}")
        await self.call(self.db.audit, actor_id, "stop-all", details=f"stopped={stopped}")
        return stopped, errors

    async def forget_missing(self, actor_id: int, item: CTRecord) -> None:
        if await self.call(self.proxmox.exists, item.vmid):
            raise AxoError("The CT still exists; use delete-ct")
        await self.call(self.proxmox.delete_pve_user, item.pve_username)
        await self.call(self.db.audit, actor_id, "forget-missing", item)
        await self.call(self.db.delete, item.user_id)


def trim(value: Any, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def human_bytes(value: int | float | None) -> str:
    size = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def human_duration(value: int | float | None) -> str:
    total = int(value or 0)
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def embed(title: str, description: str = "", color: int = 0x5865F2) -> discord.Embed:
    value = discord.Embed(
        title=trim(f"{BOT_NAME} • {title}", 256),
        description=trim(description, 4096),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    value.set_footer(text=f"{BOT_NAME} {BOT_VERSION}")
    return value


def success(title: str, description: str = "") -> discord.Embed:
    return embed(title, description, 0x2ECC71)


def failure(title: str, description: str = "") -> discord.Embed:
    return embed(title, description, 0xE74C3C)


def warning(title: str, description: str = "") -> discord.Embed:
    return embed(title, description, 0xF39C12)


async def ct_information(service: CTService, item: CTRecord) -> discord.Embed:
    status = await service.live_status(item)
    state = str(status.get("status", "unknown")).upper()
    if item.suspended:
        state += " • SUSPENDED"
    value = embed(
        f"CT {item.hostname}", f"Owner: <@{item.user_id}>",
        0x2ECC71 if status.get("status") == "running" and not item.suspended else 0xF39C12,
    )
    value.add_field(name="VMID", value=str(item.vmid), inline=True)
    value.add_field(name="Status", value=state, inline=True)
    value.add_field(name="Private IP", value=f"`{item.ip_address}`", inline=True)
    value.add_field(
        name="Resources",
        value=f"{item.ram_gb} GB RAM\n{item.cores} CPU cores\n{item.disk_gb} GB disk",
        inline=True,
    )
    cpu = float(status.get("cpu", 0)) * 100
    memory = int(status.get("mem", 0))
    max_memory = int(status.get("maxmem", 0))
    memory_pct = memory / max_memory * 100 if max_memory else 0
    value.add_field(
        name="Live usage",
        value=(
            f"CPU: {cpu:.1f}%\n"
            f"RAM: {human_bytes(memory)} / {human_bytes(max_memory)} ({memory_pct:.1f}%)\n"
            f"Uptime: {human_duration(status.get('uptime'))}"
        ),
        inline=True,
    )
    value.add_field(
        name="Access",
        value=(
            f"Proxmox login: `{item.pve_username}`\n"
            f"Web panel: {service.config.web_url}\n"
            f"SSH: `ssh root@{item.ip_address}`\n"
            f"SSH configured: **{'yes' if item.ssh_ready else 'no'}**"
        ),
        inline=False,
    )
    value.add_field(
        name="Network",
        value=(
            f"Bridge `{service.config.bridge}` • "
            f"`{item.ip_address}/{service.config.subnet.prefixlen}` • "
            f"gateway `{service.config.gateway}`"
        ),
        inline=False,
    )
    return value


def credentials_embed(config: Config, item: CTRecord,
                      credentials: LoginCredentials) -> discord.Embed:
    value = success(
        "Your CT login details",
        "These passwords are shown only in this private message. Store them safely and "
        "change them after first login.",
    )
    value.add_field(
        name="Proxmox web panel",
        value=(
            f"URL: {config.web_url}\n"
            f"Realm: **Proxmox VE authentication server**\n"
            f"Username: `{credentials.pve_username}`\n"
            f"Password: ||`{credentials.pve_password}`||"
        ),
        inline=False,
    )
    value.add_field(
        name="Your assigned CT",
        value=(
            f"Hostname: `{item.hostname}`\nVMID: `{item.vmid}`\n"
            f"Private IP: `{item.ip_address}`\n"
            f"Console: log into Proxmox, select **{item.vmid}**, then open **Console**"
        ),
        inline=False,
    )
    value.add_field(
        name="Root / SSH login",
        value=(
            f"Username: `root`\nPassword: ||`{credentials.root_password}`||\n"
            f"Command: `ssh root@{item.ip_address}`\n"
            f"SSH status: **{'ready' if credentials.ssh_ready else 'not configured; use the Proxmox console'}**"
        ),
        inline=False,
    )
    value.add_field(
        name="Important",
        value=(
            "The CT address is private. SSH works only from a routed private network, VPN, "
            "or an explicitly configured NAT/port-forward. If Cloudflare Access protects the "
            "web URL, you also need permission in Cloudflare Access."
        ),
        inline=False,
    )
    return value


def admin_only() -> Any:
    async def predicate(ctx: commands.Context[Any]) -> bool:
        if ctx.bot.database.is_admin(ctx.author.id):  # type: ignore[attr-defined]
            return True
        raise commands.CheckFailure("This command requires Axo administrator access")
    return commands.check(predicate)


def main_admin_only() -> Any:
    async def predicate(ctx: commands.Context[Any]) -> bool:
        if ctx.author.id == ctx.bot.config.main_admin_id:  # type: ignore[attr-defined]
            return True
        raise commands.CheckFailure("Only MAIN_ADMIN_ID may use this command")
    return commands.check(predicate)


class AxoBot(commands.Bot):
    def __init__(self, config: Config, database: Database, service: CTService):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix=config.prefix, intents=intents, help_command=None)
        self.config = config
        self.database = database
        self.service = service


class RestrictedView(discord.ui.View):
    def __init__(self, actor_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.actor_id = actor_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message(
                embed=failure("Access denied", "This panel belongs to another user"),
                ephemeral=True,
            )
            return False
        return True


class ManageView(RestrictedView):
    def __init__(self, bot: AxoBot, actor_id: int, owner_id: str, admin_override: bool):
        super().__init__(actor_id)
        self.bot = bot
        self.owner_id = owner_id
        self.admin_override = admin_override

    async def power_action(self, interaction: discord.Interaction, action: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            item = await self.bot.service.get_record(self.owner_id, self.actor_id)
            await self.bot.service.power(
                self.actor_id, item, action, admin_override=self.admin_override
            )
            await interaction.followup.send(
                embed=success("Power action complete", f"`{item.hostname}`: **{action}** completed"),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(
                embed=failure("Power action failed", trim(exc, 1500)), ephemeral=True
            )

    @discord.ui.button(label="Start", emoji="▶️", style=discord.ButtonStyle.success)
    async def start(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await self.power_action(interaction, "start")

    @discord.ui.button(label="Shutdown", emoji="⏹️", style=discord.ButtonStyle.secondary)
    async def shutdown(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await self.power_action(interaction, "shutdown")

    @discord.ui.button(label="Force stop", emoji="🛑", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await self.power_action(interaction, "stop")

    @discord.ui.button(label="Reboot", emoji="🔄", style=discord.ButtonStyle.primary)
    async def reboot(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await self.power_action(interaction, "reboot")

    @discord.ui.button(label="Stats", emoji="📊", style=discord.ButtonStyle.secondary)
    async def stats(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            item = await self.bot.service.get_record(self.owner_id, self.actor_id)
            await interaction.followup.send(
                embed=await ct_information(self.bot.service, item), ephemeral=True
            )
        except Exception as exc:
            await interaction.followup.send(
                embed=failure("Stats failed", trim(exc, 1500)), ephemeral=True
            )


class DeleteView(RestrictedView):
    def __init__(self, bot: AxoBot, actor_id: int, item: CTRecord, reason: str):
        super().__init__(actor_id, 60)
        self.bot = bot
        self.item = item
        self.reason = reason

    @discord.ui.button(label="Permanently delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await interaction.response.defer(thinking=True)
        try:
            await self.bot.service.delete_ct(self.actor_id, self.item, self.reason)
            await interaction.edit_original_response(
                embed=success(
                    "CT and login deleted",
                    f"Deleted `{self.item.hostname}`, VMID {self.item.vmid}, and "
                    f"Proxmox user `{self.item.pve_username}`",
                ), view=None,
            )
        except Exception as exc:
            await interaction.edit_original_response(
                embed=failure("Deletion problem", trim(exc, 1500)), view=None
            )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=embed("Cancelled", "Nothing was deleted"), view=None
        )


class ReinstallView(RestrictedView):
    def __init__(self, bot: AxoBot, actor_id: int, item: CTRecord, template: str):
        super().__init__(actor_id, 60)
        self.bot = bot
        self.item = item
        self.template = template

    @discord.ui.button(label="Erase and reinstall", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await interaction.response.defer(thinking=True)
        try:
            item, credentials = await self.bot.service.reinstall(
                self.actor_id, self.item, self.template
            )
            owner = await self.bot.fetch_user(int(item.user_id))
            delivered = True
            try:
                await owner.send(embed=credentials_embed(self.bot.config, item, credentials))
            except discord.Forbidden:
                delivered = False
            description = f"`{item.hostname}` was rebuilt and new credentials were sent by DM"
            if not delivered:
                description = (
                    f"`{item.hostname}` was rebuilt, but the owner's DMs are closed. "
                    f"Enable DMs and run `{self.bot.config.prefix}reset-login`."
                )
            await interaction.edit_original_response(
                embed=success("Reinstall complete", description), view=None
            )
        except Exception as exc:
            await interaction.edit_original_response(
                embed=failure("Reinstall failed", trim(exc, 1500)), view=None
            )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=embed("Cancelled", "The CT was not reinstalled"), view=None
        )


class RestoreView(RestrictedView):
    def __init__(self, bot: AxoBot, actor_id: int, item: CTRecord, snapshot: str):
        super().__init__(actor_id, 60)
        self.bot = bot
        self.item = item
        self.snapshot = snapshot

    @discord.ui.button(label="Restore snapshot", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await interaction.response.defer(thinking=True)
        try:
            await self.bot.service.restore_snapshot(self.actor_id, self.item, self.snapshot)
            await interaction.edit_original_response(
                embed=success("Snapshot restored", f"Restored `{self.snapshot}`"), view=None
            )
        except Exception as exc:
            await interaction.edit_original_response(
                embed=failure("Restore failed", trim(exc, 1500)), view=None
            )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        self.stop()
        await interaction.response.edit_message(embed=embed("Cancelled"), view=None)


class StopAllView(RestrictedView):
    def __init__(self, bot: AxoBot, actor_id: int):
        super().__init__(actor_id, 60)
        self.bot = bot

    @discord.ui.button(label="Stop every managed CT", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        await interaction.response.defer(thinking=True)
        count, errors = await self.bot.service.stop_all(self.actor_id)
        description = f"Stopped **{count}** CT(s)"
        if errors:
            description += "\n" + "\n".join(errors[:8])
        await interaction.edit_original_response(
            embed=success("Stop-all complete", description), view=None
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button[Any]) -> None:
        self.stop()
        await interaction.response.edit_message(embed=embed("Cancelled"), view=None)


def register_commands(bot: AxoBot) -> None:
    async def authorized(ctx: commands.Context[Any], target: str | None = None,
                         allow_shared: bool = True) -> CTRecord:
        item = await bot.service.get_record(target, ctx.author.id)
        if item.user_id == str(ctx.author.id) or bot.database.is_admin(ctx.author.id):
            return item
        if allow_shared and bot.database.has_share(item.user_id, ctx.author.id):
            return item
        raise AxoError("You do not own this CT and it has not been shared with you")

    def help_value(ctx: commands.Context[Any]) -> discord.Embed:
        p = bot.config.prefix
        value = embed(
            "Command help",
            "Local Proxmox mode: every user gets one CT, a restricted Proxmox-web account, "
            "console permission, and private root/SSH credentials by DM.",
        )
        value.add_field(
            name="User commands",
            value=(
                f"`{p}myct` / `{p}myvps` — CT details\n"
                f"`{p}manage` — power-control buttons\n"
                f"`{p}ctinfo [target]` / `{p}ct-stats [target]`\n"
                f"`{p}reinstall [template]` — erase and rebuild\n"
                f"`{p}reset-login` — generate and DM new passwords\n"
                f"`{p}templates` — OS templates\n"
                f"`{p}share-user @user`, `{p}share-ruser @user`, `{p}manage-shared @owner`"
            ), inline=False,
        )
        if bot.database.is_admin(ctx.author.id):
            value.add_field(
                name="Admin CT commands",
                value=(
                    f"`{p}create <ramGB> <cores> <diskGB> @user [template]`\n"
                    f"`{p}delete-ct <target> [reason]`\n"
                    f"`{p}resize-ct <target> <ramGB|-> <cores|-> <diskGB|->`\n"
                    f"`{p}suspend-vps <target> [reason]`, `{p}unsuspend-vps <target>`\n"
                    f"`{p}reinstall-ct <target> <template>`, `{p}reset-login <target>`\n"
                    f"`{p}ct-list`, `{p}lxc-list`, `{p}ip-pool`, `{p}sync-cts`\n"
                    f"`{p}snapshot <target> [name]`, `{p}list-snapshots <target>`\n"
                    f"`{p}restore-snapshot <target> <name>`, `{p}serverstats`\n"
                    f"`{p}stop-vps-all`, `{p}audit-log`, `{p}backup-db`"
                ), inline=False,
            )
        return value

    @bot.event
    async def on_ready() -> None:
        logging.getLogger("axo").info("Connected to Discord as %s", bot.user)
        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="Axo Proxmox CTs"
            )
        )

    @bot.event
    async def on_command_error(ctx: commands.Context[Any], error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.CommandInvokeError):
            error = error.original  # type: ignore[assignment]
        if isinstance(error, commands.MissingRequiredArgument):
            description = f"Missing `{error.param.name}`. Use `{bot.config.prefix}help`."
        elif isinstance(error, (commands.BadArgument, commands.MemberNotFound)):
            description = f"Invalid argument: {error}"
        elif isinstance(error, commands.CheckFailure):
            description = str(error)
        else:
            logging.getLogger("axo").error(
                "Command failed", exc_info=(type(error), error, error.__traceback__)
            )
            description = trim(error, 1500)
        await ctx.send(embed=failure("Command failed", description))

    @bot.command(name="help", aliases=["commands", "quickhelp"])
    async def help_command(ctx: commands.Context[Any]) -> None:
        await ctx.send(embed=help_value(ctx))

    @bot.command(name="ping")
    async def ping(ctx: commands.Context[Any]) -> None:
        await ctx.send(embed=success("Pong", f"Discord latency: {bot.latency * 1000:.0f} ms"))

    @bot.command(name="about")
    async def about(ctx: commands.Context[Any]) -> None:
        value = embed(
            "About",
            "Runs directly on the Proxmox node with local CLI tools. No Proxmox API token, "
            "tmate, web shell, or remote command endpoint is used.",
        )
        value.add_field(name="Node", value=f"`{bot.config.node}`", inline=True)
        value.add_field(name="Bridge", value=f"`{bot.config.bridge}`", inline=True)
        value.add_field(name="Web login", value=bot.config.web_url, inline=False)
        await ctx.send(embed=value)

    @bot.command(name="templates", aliases=["os-list"])
    async def templates(ctx: commands.Context[Any]) -> None:
        lines = [
            f"• `{key}`{' **(default)**' if key == bot.config.default_template else ''} — `{volume}`"
            for key, volume in bot.config.templates.items()
        ]
        await ctx.send(embed=embed("CT templates", "\n".join(lines)))

    @bot.command(name="myct", aliases=["myvps"])
    @commands.guild_only()
    async def myct(ctx: commands.Context[Any]) -> None:
        item = await bot.service.get_record(None, ctx.author.id)
        await ctx.send(embed=await ct_information(bot.service, item))

    @bot.command(name="ctinfo", aliases=["vpsinfo", "info"])
    @commands.guild_only()
    async def ctinfo(ctx: commands.Context[Any], target: str = None) -> None:
        item = await authorized(ctx, target)
        await ctx.send(embed=await ct_information(bot.service, item))

    @bot.command(name="ct-stats", aliases=["vps-stats", "status"])
    @commands.guild_only()
    async def ct_stats(ctx: commands.Context[Any], target: str = None) -> None:
        item = await authorized(ctx, target)
        await ctx.send(embed=await ct_information(bot.service, item))

    @bot.command(name="manage")
    @commands.guild_only()
    async def manage(ctx: commands.Context[Any], user: discord.Member = None) -> None:
        target_id = user.id if user else ctx.author.id
        is_admin = bot.database.is_admin(ctx.author.id)
        if user and user.id != ctx.author.id and not is_admin:
            raise AxoError("Only an admin can open another user's panel")
        item = await bot.service.get_record(str(target_id), ctx.author.id)
        await ctx.send(
            embed=await ct_information(bot.service, item),
            view=ManageView(bot, ctx.author.id, item.user_id, is_admin),
        )

    @bot.command(name="manage-shared")
    @commands.guild_only()
    async def manage_shared(ctx: commands.Context[Any], owner: discord.Member) -> None:
        item = await bot.service.get_record(str(owner.id), ctx.author.id)
        if not bot.database.has_share(owner.id, ctx.author.id):
            raise AxoError("That user has not shared their CT with you")
        await ctx.send(
            embed=await ct_information(bot.service, item),
            view=ManageView(bot, ctx.author.id, item.user_id, False),
        )

    @bot.command(name="share-user")
    @commands.guild_only()
    async def share_user(ctx: commands.Context[Any], user: discord.Member) -> None:
        if user.bot or user.id == ctx.author.id:
            raise AxoError("Choose another human Discord member")
        item = await bot.service.get_record(None, ctx.author.id)
        await bot.service.call(bot.database.share, ctx.author.id, user.id)
        await bot.service.call(bot.database.audit, ctx.author.id, "share:add", item, str(user.id))
        await ctx.send(embed=success("Bot access shared", f"{user.mention} can use Axo power controls"))

    @bot.command(name="share-ruser")
    @commands.guild_only()
    async def unshare_user(ctx: commands.Context[Any], user: discord.Member) -> None:
        item = await bot.service.get_record(None, ctx.author.id)
        await bot.service.call(bot.database.unshare, ctx.author.id, user.id)
        await bot.service.call(bot.database.audit, ctx.author.id, "share:remove", item, str(user.id))
        await ctx.send(embed=success("Bot access revoked", f"Removed {user.mention}"))

    @bot.command(name="credentials", aliases=["login-details"])
    @commands.guild_only()
    async def credentials(ctx: commands.Context[Any]) -> None:
        await bot.service.get_record(None, ctx.author.id)
        await ctx.send(
            embed=embed(
                "Credentials are not stored",
                f"For security, Axo cannot retrieve old passwords. Run `{bot.config.prefix}reset-login` "
                "to generate and privately DM new Proxmox and CT-root passwords.",
            )
        )

    @bot.command(name="reset-login")
    @commands.guild_only()
    async def reset_login(ctx: commands.Context[Any], target: str = None) -> None:
        item = await bot.service.get_record(target, ctx.author.id)
        if item.user_id != str(ctx.author.id) and not bot.database.is_admin(ctx.author.id):
            raise AxoError("Only the owner or an admin can reset these credentials")
        owner = await bot.fetch_user(int(item.user_id))
        try:
            await owner.send(
                embed=embed("Credential reset started", "Axo is generating new private login details…")
            )
        except discord.Forbidden as exc:
            raise AxoError("The owner's DMs are closed; no passwords were changed") from exc
        progress = await ctx.send(embed=embed("Resetting credentials", f"Updating `{item.hostname}`…"))
        try:
            credentials_value = await bot.service.reset_credentials(ctx.author.id, item)
            await owner.send(embed=credentials_embed(bot.config, item, credentials_value))
            await progress.edit(embed=success("Credentials reset", "New details were sent privately to the owner"))
        except Exception as exc:
            await progress.edit(embed=failure("Credential reset failed", trim(exc, 1500)))

    @bot.command(name="reinstall")
    @commands.guild_only()
    async def reinstall_self(ctx: commands.Context[Any], template: str = None) -> None:
        item = await bot.service.get_record(None, ctx.author.id)
        template = (template or bot.config.default_template).lower()
        if template not in bot.config.templates:
            raise AxoError(f"Unknown template `{template}`")
        await ctx.send(
            embed=warning(
                "Permanent reinstall",
                f"This erases all data in `{item.hostname}` and generates new login passwords",
            ),
            view=ReinstallView(bot, ctx.author.id, item, template),
        )

    @bot.command(name="create")
    @commands.guild_only()
    @admin_only()
    async def create_ct(ctx: commands.Context[Any], ram_gb: int, cores: int,
                        disk_gb: int, user: discord.Member, template: str = None) -> None:
        if user.bot:
            raise AxoError("A CT cannot be assigned to a bot account")
        template = (template or bot.config.default_template).lower()
        try:
            await user.send(
                embed=embed(
                    "CT provisioning started",
                    f"An administrator is creating your `{HOSTNAME_PREFIX}-{user.id}` container. "
                    "Final login details will arrive here only.",
                )
            )
        except discord.Forbidden as exc:
            raise AxoError("The user's DMs are closed. No CT was created.") from exc
        progress = await ctx.send(
            embed=embed("Creating CT", f"Provisioning for {user.mention} with `{template}`…")
        )
        try:
            item, login = await bot.service.create_ct(
                actor_id=ctx.author.id, guild_id=ctx.guild.id, user_id=user.id,
                ram_gb=ram_gb, cores=cores, disk_gb=disk_gb, template_key=template,
            )
        except Exception as exc:
            await progress.edit(embed=failure("Creation failed", trim(exc, 1500)))
            return
        try:
            await user.send(embed=credentials_embed(bot.config, item, login))
            result = success("CT created", f"Created `{item.hostname}` and sent login details by DM")
        except (discord.Forbidden, discord.HTTPException):
            result = warning(
                "CT created; DM delivery failed",
                f"`{item.hostname}` exists, but its one-time passwords could not be delivered. "
                f"Have the owner enable DMs, then run `{bot.config.prefix}reset-login {item.vmid}`.",
            )
        result.add_field(name="VMID", value=str(item.vmid), inline=True)
        result.add_field(name="IP", value=f"`{item.ip_address}`", inline=True)
        result.add_field(name="Proxmox user", value=f"`{item.pve_username}`", inline=True)
        await progress.edit(embed=result)

    @bot.command(name="delete-ct", aliases=["delete-vps"])
    @commands.guild_only()
    @admin_only()
    async def delete_ct(ctx: commands.Context[Any], target: str,
                        *, reason: str = "Administrator action") -> None:
        item = await bot.service.get_record(target, ctx.author.id)
        await ctx.send(
            embed=warning(
                "Permanent deletion",
                f"Delete `{item.hostname}`, all data/snapshots, and Proxmox login "
                f"`{item.pve_username}`?\nReason: {trim(reason, 500)}",
            ),
            view=DeleteView(bot, ctx.author.id, item, reason),
        )

    @bot.command(name="reinstall-ct")
    @commands.guild_only()
    @admin_only()
    async def reinstall_ct(ctx: commands.Context[Any], target: str, template: str) -> None:
        item = await bot.service.get_record(target, ctx.author.id)
        template = template.lower()
        if template not in bot.config.templates:
            raise AxoError(f"Unknown template `{template}`")
        await ctx.send(
            embed=warning(
                "Permanent reinstall",
                f"Erase `{item.hostname}`, install `{template}`, and reset both passwords?",
            ),
            view=ReinstallView(bot, ctx.author.id, item, template),
        )

    @bot.command(name="resize-ct", aliases=["resize-vps", "add-resources"])
    @commands.guild_only()
    @admin_only()
    async def resize_ct(ctx: commands.Context[Any], target: str, ram_gb: str,
                        cores: str, disk_gb: str) -> None:
        def optional(value: str, label: str) -> int | None:
            if value == "-":
                return None
            try:
                return int(value)
            except ValueError as exc:
                raise AxoError(f"{label} must be an integer or `-`") from exc
        ram = optional(ram_gb, "RAM")
        cpu = optional(cores, "CPU")
        disk = optional(disk_gb, "disk")
        if ram is None and cpu is None and disk is None:
            raise AxoError("At least one resource must change")
        item = await bot.service.get_record(target, ctx.author.id)
        changed = await bot.service.resize(ctx.author.id, item, ram, cpu, disk)
        await ctx.send(
            embed=success(
                "CT resized",
                f"`{changed.hostname}`: {changed.ram_gb} GB RAM, "
                f"{changed.cores} cores, {changed.disk_gb} GB disk",
            )
        )

    @bot.command(name="suspend-vps", aliases=["suspend-ct"])
    @commands.guild_only()
    @admin_only()
    async def suspend_ct(ctx: commands.Context[Any], target: str,
                         *, reason: str = "Administrator action") -> None:
        item = await bot.service.get_record(target, ctx.author.id)
        item = await bot.service.suspend(ctx.author.id, item, reason)
        await ctx.send(embed=warning("CT suspended", f"`{item.hostname}` stopped. {trim(reason, 800)}"))
        try:
            owner = await bot.fetch_user(int(item.user_id))
            await owner.send(embed=warning("Your CT was suspended", trim(reason, 1000)))
        except (discord.Forbidden, discord.NotFound):
            pass

    @bot.command(name="unsuspend-vps", aliases=["unsuspend-ct"])
    @commands.guild_only()
    @admin_only()
    async def unsuspend_ct(ctx: commands.Context[Any], target: str) -> None:
        item = await bot.service.get_record(target, ctx.author.id)
        if not item.suspended:
            raise AxoError("That CT is not suspended")
        item = await bot.service.unsuspend(ctx.author.id, item)
        await ctx.send(embed=success("CT unsuspended", f"`{item.hostname}` is running"))

    @bot.command(name="restart-vps", aliases=["restart-ct"])
    @commands.guild_only()
    @admin_only()
    async def restart_ct(ctx: commands.Context[Any], target: str) -> None:
        item = await bot.service.get_record(target, ctx.author.id)
        await bot.service.power(ctx.author.id, item, "reboot", admin_override=True)
        await ctx.send(embed=success("CT rebooted", f"`{item.hostname}` reboot completed"))

    @bot.command(name="ct-list", aliases=["list-all", "vps-list"])
    @commands.guild_only()
    @admin_only()
    async def ct_list(ctx: commands.Context[Any]) -> None:
        records = await bot.service.call(bot.database.list_cts)
        if not records:
            await ctx.send(embed=embed("Managed CTs", "No registered CTs"))
            return
        lines = [
            f"`{item.vmid}` • `{item.hostname}` • <@{item.user_id}> • `{item.ip_address}` • "
            f"`{item.pve_username}` • **{'SUSPENDED' if item.suspended else item.status.upper()}**"
            for item in records
        ]
        for offset in range(0, len(lines), 18):
            await ctx.send(embed=embed("Managed CTs", "\n".join(lines[offset:offset + 18])))

    @bot.command(name="lxc-list")
    @commands.guild_only()
    @admin_only()
    async def lxc_list(ctx: commands.Context[Any]) -> None:
        resources = await bot.service.call(bot.service.proxmox.cluster_lxcs)
        lines = [
            f"`{item.get('vmid')}` • `{item.get('name', 'unnamed')}` • "
            f"node `{item.get('node')}` • **{str(item.get('status', 'unknown')).upper()}**"
            for item in sorted(resources, key=lambda value: int(value["vmid"]))
        ]
        if not lines:
            lines = ["No LXC containers found"]
        for offset in range(0, len(lines), 22):
            await ctx.send(embed=embed("All Proxmox CTs", "\n".join(lines[offset:offset + 22])))

    @bot.command(name="ip-pool", aliases=["network-audit"])
    @commands.guild_only()
    @admin_only()
    async def ip_pool(ctx: commands.Context[Any]) -> None:
        _, _, live_ips = await bot.service.call(bot.service.proxmox.live_inventory)
        records = await bot.service.call(bot.database.list_cts)
        used = live_ips | {item.ip_address for item in records}
        total = int(bot.config.last_ip) - int(bot.config.first_ip) + 1
        used_count = sum(
            str(ipaddress.ip_address(value)) in used
            for value in range(int(bot.config.first_ip), int(bot.config.last_ip) + 1)
        )
        try:
            next_ip = bot.service.choose_ip(used)
        except AxoError:
            next_ip = "none"
        value = embed("Private IP pool")
        value.add_field(name="Bridge", value=f"`{bot.config.bridge}`", inline=True)
        value.add_field(name="Gateway", value=f"`{bot.config.gateway}`", inline=True)
        value.add_field(name="Used", value=f"{used_count}/{total}", inline=True)
        value.add_field(name="Next safe IP", value=f"`{next_ip}`", inline=True)
        value.add_field(
            name="Protection",
            value="SQLite uniqueness + cluster-wide live configuration scan + serialized creation",
            inline=False,
        )
        await ctx.send(embed=value)

    @bot.command(name="sync-cts", aliases=["repair-db"])
    @commands.guild_only()
    @admin_only()
    async def sync_cts(ctx: commands.Context[Any]) -> None:
        updated, missing = await bot.service.reconcile(ctx.author.id)
        await ctx.send(
            embed=success("Registry synchronized", f"Updated {updated}; missing in Proxmox: {missing}")
        )

    @bot.command(name="forget-missing")
    @commands.guild_only()
    @main_admin_only()
    async def forget_missing(ctx: commands.Context[Any], target: str) -> None:
        item = await bot.service.get_record(target, ctx.author.id)
        await bot.service.forget_missing(ctx.author.id, item)
        await ctx.send(embed=success("Stale record removed", "Released VMID, IP, and Proxmox user"))

    @bot.command(name="vps-network", aliases=["ct-network"])
    @commands.guild_only()
    @admin_only()
    async def ct_network(ctx: commands.Context[Any], target: str) -> None:
        item = await bot.service.get_record(target, ctx.author.id)
        config = await bot.service.call(bot.service.proxmox.config_for, item.vmid)
        value = embed(f"Network • {item.hostname}")
        for key, raw in config.items():
            if str(key).startswith("net"):
                value.add_field(name=str(key), value=f"`{trim(raw, 1000)}`", inline=False)
        value.add_field(
            name="Safety",
            value="Discord network mutation is disabled so exclusive IP allocation cannot be bypassed",
            inline=False,
        )
        await ctx.send(embed=value)

    @bot.command(name="snapshot")
    @commands.guild_only()
    @admin_only()
    async def snapshot(ctx: commands.Context[Any], target: str, name: str = None) -> None:
        item = await bot.service.get_record(target, ctx.author.id)
        name = name or datetime.now(timezone.utc).strftime("snap-%Y%m%d-%H%M%S")
        await bot.service.create_snapshot(ctx.author.id, item, name)
        await ctx.send(embed=success("Snapshot created", f"`{name}` for `{item.hostname}`"))

    @bot.command(name="list-snapshots")
    @commands.guild_only()
    @admin_only()
    async def list_snapshots(ctx: commands.Context[Any], target: str) -> None:
        item = await bot.service.get_record(target, ctx.author.id)
        snapshots = await bot.service.call(bot.service.proxmox.snapshots, item.vmid)
        lines = [f"• `{value.get('name')}`" for value in snapshots if value.get("name") != "current"]
        await ctx.send(embed=embed(f"Snapshots • {item.hostname}", "\n".join(lines) or "None"))

    @bot.command(name="restore-snapshot")
    @commands.guild_only()
    @admin_only()
    async def restore_snapshot(ctx: commands.Context[Any], target: str, name: str) -> None:
        item = await bot.service.get_record(target, ctx.author.id)
        await ctx.send(
            embed=warning("Restore snapshot", f"Roll `{item.hostname}` back to `{name}`?"),
            view=RestoreView(bot, ctx.author.id, item, name),
        )

    @bot.command(name="serverstats", aliases=["node-check", "uptime"])
    @commands.guild_only()
    async def serverstats(ctx: commands.Context[Any]) -> None:
        status = await bot.service.call(bot.service.proxmox.node_status)
        resources = await bot.service.call(bot.service.proxmox.cluster_lxcs)
        memory = status.get("memory", {})
        root = status.get("rootfs", {})
        value = embed(f"Node {bot.config.node}")
        value.add_field(name="Uptime", value=human_duration(status.get("uptime")), inline=True)
        value.add_field(name="CPU", value=f"{float(status.get('cpu', 0)) * 100:.1f}%", inline=True)
        value.add_field(
            name="RAM",
            value=f"{human_bytes(memory.get('used'))} / {human_bytes(memory.get('total'))}",
            inline=True,
        )
        value.add_field(
            name="Root storage",
            value=f"{human_bytes(root.get('used'))} / {human_bytes(root.get('total'))}",
            inline=True,
        )
        value.add_field(name="Cluster CTs", value=str(len(resources)), inline=True)
        await ctx.send(embed=value)

    @bot.command(name="stop-vps-all", aliases=["stop-ct-all"])
    @commands.guild_only()
    @admin_only()
    async def stop_all(ctx: commands.Context[Any]) -> None:
        await ctx.send(
            embed=warning("Stop every managed CT?", "Unmanaged Proxmox guests are untouched"),
            view=StopAllView(bot, ctx.author.id),
        )

    @bot.command(name="audit-log")
    @commands.guild_only()
    @admin_only()
    async def audit_log(ctx: commands.Context[Any], count: int = 15) -> None:
        rows = await bot.service.call(bot.database.recent_audit, count)
        lines = []
        for row in rows:
            timestamp = int(datetime.fromisoformat(row["created_at"]).timestamp())
            target = f" VMID {row['vmid']}" if row["vmid"] is not None else ""
            lines.append(f"<t:{timestamp}:R> • `{row['action']}` by <@{row['actor_id']}>{target}")
        await ctx.send(embed=embed("Audit log", "\n".join(lines) or "No events"))

    @bot.command(name="backup-db")
    @commands.guild_only()
    @admin_only()
    async def backup_db(ctx: commands.Context[Any]) -> None:
        directory = bot.database.path.parent / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"axo-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.db"
        def perform() -> None:
            with bot.database.connect() as source, sqlite3.connect(target) as destination:
                source.backup(destination)
        await bot.service.call(perform)
        await bot.service.call(bot.database.audit, ctx.author.id, "backup", details=str(target))
        await ctx.send(embed=success("Database backed up", f"`{target}`"))

    @bot.command(name="admin-add")
    @commands.guild_only()
    @main_admin_only()
    async def admin_add(ctx: commands.Context[Any], user: discord.Member) -> None:
        await bot.service.call(bot.database.add_admin, user.id)
        await ctx.send(embed=success("Administrator added", user.mention))

    @bot.command(name="admin-remove")
    @commands.guild_only()
    @main_admin_only()
    async def admin_remove(ctx: commands.Context[Any], user: discord.Member) -> None:
        if user.id == bot.config.main_admin_id:
            raise AxoError("MAIN_ADMIN_ID cannot be removed")
        await bot.service.call(bot.database.remove_admin, user.id)
        await ctx.send(embed=success("Administrator removed", user.mention))

    @bot.command(name="admin-list")
    @commands.guild_only()
    @admin_only()
    async def admin_list(ctx: commands.Context[Any]) -> None:
        users = await bot.service.call(bot.database.list_admins)
        await ctx.send(embed=embed("Administrators", "\n".join(f"• <@{value}>" for value in users)))

    @bot.command(name="set-status")
    @commands.guild_only()
    @admin_only()
    async def set_status(ctx: commands.Context[Any], activity_type: str, *, text: str) -> None:
        types = {
            "playing": discord.ActivityType.playing,
            "watching": discord.ActivityType.watching,
            "listening": discord.ActivityType.listening,
            "competing": discord.ActivityType.competing,
        }
        if activity_type.lower() not in types:
            raise AxoError("Type must be playing, watching, listening, or competing")
        await bot.change_presence(
            activity=discord.Activity(type=types[activity_type.lower()], name=trim(text, 128))
        )
        await ctx.send(embed=success("Bot status updated"))

    @bot.command(name="ports")
    @commands.guild_only()
    async def ports(ctx: commands.Context[Any]) -> None:
        await ctx.send(
            embed=embed(
                "Private networking",
                "Axo supplies a private static IP. Public SSH requires your VPN/router/NAT policy; "
                "the bot does not guess firewall interfaces or expose ports automatically.",
            )
        )

    @bot.command(name="clone-vps")
    @commands.guild_only()
    @admin_only()
    async def clone_disabled(ctx: commands.Context[Any], *_: str) -> None:
        await ctx.send(
            embed=warning(
                "Cloning disabled",
                "Cloning violates one CT, one VMID, one IP, and one Proxmox account per Discord user",
            )
        )


def configure_logging() -> None:
    log_directory = Path(__file__).with_name("logs")
    log_directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_directory / "axo-manager.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def main() -> None:
    configure_logging()
    try:
        config = Config.load()
    except Exception as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    database = Database(Path(__file__).parent / config.database_path)
    database.initialize(config.admin_ids)
    proxmox = LocalProxmox(config)
    if "--check" in sys.argv:
        print("Configuration: OK")
        print(f"Mode: local Proxmox CLI on node {config.node}")
        print(f"Web login URL sent to users: {config.web_url}")
        try:
            result = proxmox.preflight()
        except Exception as exc:
            raise SystemExit(f"Preflight failed: {exc}") from exc
        print(f"Node {config.node}: OK")
        print(f"Bridge {config.bridge}: OK")
        print(f"Root storage {config.storage}: OK")
        print(f"Configured live IPv4 addresses on {config.bridge}: {len(result['live_ips'])}")
        if result["missing_templates"]:
            print("Missing templates:")
            for key, volume in result["missing_templates"].items():
                print(f"  {key}: {volume}")
            raise SystemExit(2)
        proxmox.ensure_role()
        print(f"Restricted role {config.pve_role}: OK")
        print("Preflight passed. No CT or user account was created; the restricted role may have been created.")
        return
    service = CTService(config, database, proxmox)
    bot = AxoBot(config, database, service)
    register_commands(bot)
    bot.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
