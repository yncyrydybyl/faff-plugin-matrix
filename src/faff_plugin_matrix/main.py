"""
faff-plugin-matrix — sidecar that posts faff session transitions to an
end-to-end encrypted Matrix room.

Architecture
------------
The Rust core's `start_watching` is a blocking iterator that yields filesystem
events. We run it in a daemon thread (same pattern as faff-menubar-mac) and
push notifications onto an asyncio.Queue. The main coroutine consumes the
queue, re-reads the active session via the Workspace, diffs it against the
last seen state, and sends a message via matrix-nio.

Auth model
----------
This plugin is built for Matrix Authentication Service (MAS) homeservers
where the legacy /_matrix/client/v3/login endpoint is disabled. We never call
login(); we use AsyncClient.restore_login() with a (user_id, device_id,
access_token) triple obtained out-of-band — typically via `mas-cli manage
issue-compatibility-token` or by extracting them from an existing Element
session. device_id must be the one MAS issued the token for, otherwise
Megolm key sharing will fail and messages will be undecryptable.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import os
import sys
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from nio import (
    AsyncClient,
    AsyncClientConfig,
    LoginError,
    RoomResolveAliasError,
    RoomSendError,
)

from faff_core import Workspace, start_watching


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Config:
    instance_id: str
    homeserver: str
    user_id: str
    device_id: str
    access_token: str
    room: str
    notify_on: set[str]
    announce_on_startup: bool
    dry_run: bool
    templates: dict[str, str]


def load_config(path: str) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    instance_id = raw.get("id") or "default"
    conn = raw.get("connection") or {}
    opts = raw.get("options") or {}
    templates = (opts.get("templates") or {})

    required = ["homeserver", "user_id", "device_id", "room"]
    missing = [k for k in required if not conn.get(k)]
    if missing:
        raise SystemExit(f"config: connection.{', connection.'.join(missing)} required")

    token = conn.get("access_token") or ""
    token_env = conn.get("access_token_env")
    if token_env:
        token = os.environ.get(token_env) or token
    if not token:
        raise SystemExit(
            "config: provide connection.access_token or set the env var named by "
            "connection.access_token_env"
        )

    return Config(
        instance_id=instance_id,
        homeserver=conn["homeserver"].rstrip("/"),
        user_id=conn["user_id"],
        device_id=conn["device_id"],
        access_token=token,
        room=conn["room"],
        notify_on=set(opts.get("notify_on") or ["start", "stop", "switch"]),
        announce_on_startup=bool(opts.get("announce_on_startup", False)),
        dry_run=bool(opts.get("dry_run", False)),
        templates={
            "start":  templates.get("start",  "[faff] started: {alias} ({start_time})"),
            "stop":   templates.get("stop",   "[faff] stopped: {alias} after {duration}"),
            "switch": templates.get("switch", "[faff] switched: {prev_alias} -> {alias}"),
        },
    )


# ---------------------------------------------------------------------------
# Faff session helpers
# ---------------------------------------------------------------------------

def session_fields(session: Any) -> Optional[dict[str, Any]]:
    """Snapshot the bits of an active session we need for diffing + rendering."""
    if session is None:
        return None
    intent = session.intent
    return {
        "intent_id": intent.intent_id,
        "alias":     getattr(intent, "alias", "") or "",
        "role":      getattr(intent, "role", "") or "",
        "objective": getattr(intent, "objective", "") or "",
        "action":    getattr(intent, "action", "") or "",
        "subject":   getattr(intent, "subject", "") or "",
        "trackers":  ", ".join(intent.trackers or []),
        "start":     getattr(session, "start", None),
    }


def fmt_time(ts: Any) -> str:
    if ts is None:
        return ""
    if hasattr(ts, "strftime"):
        return ts.strftime("%H:%M")
    return str(ts)


def fmt_duration(start: Any, end: Any) -> str:
    if start is None or end is None:
        return ""
    try:
        delta = end - start
        secs = int(delta.total_seconds())
    except Exception:
        return ""
    if secs <= 0:
        return "0m"
    h, rem = divmod(secs, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:  # type: ignore[override]
        return ""


def render(template: str, fields: dict[str, Any]) -> str:
    return template.format_map(_SafeDict(fields))


def diff_and_render(
    prev: Optional[dict[str, Any]],
    curr: Optional[dict[str, Any]],
    now: Any,
    templates: dict[str, str],
) -> tuple[Optional[str], Optional[str]]:
    """Decide what (if any) message to emit for a transition prev -> curr."""
    if prev is None and curr is not None:
        f = dict(curr)
        f["start_time"] = fmt_time(curr["start"])
        return "start", render(templates["start"], f)

    if prev is not None and curr is None:
        f = dict(prev)
        f["start_time"] = fmt_time(prev["start"])
        f["duration"] = fmt_duration(prev["start"], now)
        return "stop", render(templates["stop"], f)

    if prev is not None and curr is not None:
        if prev["intent_id"] != curr["intent_id"]:
            f = dict(curr)
            f["start_time"] = fmt_time(curr["start"])
            f["prev_alias"] = prev["alias"]
            f["prev_duration"] = fmt_duration(prev["start"], now)
            return "switch", render(templates["switch"], f)

    return None, None


# ---------------------------------------------------------------------------
# Matrix sender (E2EE, MAS-compatible)
# ---------------------------------------------------------------------------

class MatrixSender:
    def __init__(self, cfg: Config, store_path: Path):
        self.cfg = cfg
        self.store_path = store_path
        self.store_path.mkdir(parents=True, exist_ok=True)

        client_cfg = AsyncClientConfig(
            store_sync_tokens=True,
            encryption_enabled=True,
        )
        self.client = AsyncClient(
            homeserver=cfg.homeserver,
            user=cfg.user_id,
            device_id=cfg.device_id,
            store_path=str(self.store_path),
            config=client_cfg,
        )
        self.room_id: Optional[str] = None

    async def setup(self) -> None:
        # We bypass /login entirely (MAS does not support it). Instead we
        # restore credentials directly. nio will use the existing store_path
        # for the Olm session DB.
        self.client.restore_login(
            user_id=self.cfg.user_id,
            device_id=self.cfg.device_id,
            access_token=self.cfg.access_token,
        )

        # Initial sync to fetch room state, member lists, and Megolm keys.
        # full_state=True is important on first run so the bot learns about
        # all members of the encrypted room and can share keys with them.
        await self.client.sync(timeout=10000, full_state=True)

        # Resolve the room. Aliases need a directory lookup.
        if self.cfg.room.startswith("#"):
            resolved = await self.client.room_resolve_alias(self.cfg.room)
            if isinstance(resolved, RoomResolveAliasError):
                raise RuntimeError(f"failed to resolve room alias {self.cfg.room}: {resolved.message}")
            self.room_id = resolved.room_id
        else:
            self.room_id = self.cfg.room

        if self.room_id not in self.client.rooms:
            raise RuntimeError(
                f"bot user {self.cfg.user_id} is not a member of {self.room_id}. "
                f"Invite + accept the invite first; this plugin will not auto-join."
            )

    async def send_text(self, body: str) -> None:
        assert self.room_id is not None, "setup() not called"
        # ignore_unverified_devices=True: we do not implement device
        # verification or cross-signing in this sidecar; we trust whoever is
        # in the room. If you want to lock this down, do verification out of
        # band and remove this flag.
        resp = await self.client.room_send(
            room_id=self.room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": body},
            ignore_unverified_devices=True,
        )
        if isinstance(resp, RoomSendError):
            raise RuntimeError(f"matrix send failed: {resp.message}")

    async def background_sync_once(self) -> None:
        try:
            await self.client.sync(timeout=2000)
        except Exception as e:
            print(f"warn: background sync failed: {e}", file=sys.stderr)

    async def close(self) -> None:
        await self.client.close()


# ---------------------------------------------------------------------------
# Watcher thread bridge
# ---------------------------------------------------------------------------

def _watcher_thread(workspace_path: str, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
    try:
        stream = start_watching(workspace_path)
        for event in stream:
            if getattr(event, "event_type", None) == "log_changed":
                asyncio.run_coroutine_threadsafe(queue.put("change"), loop)
    except Exception as e:
        print(f"watcher thread crashed: {e}", file=sys.stderr)
        asyncio.run_coroutine_threadsafe(queue.put(None), loop)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

async def cmd_test(cfg: Config, store_path: Path) -> int:
    sender = MatrixSender(cfg, store_path)
    try:
        await sender.setup()
        print(f"authenticated as {sender.client.user_id} (device {sender.client.device_id})")
        print(f"resolved room: {sender.room_id}")
        if cfg.dry_run:
            print("dry_run set; not posting probe.")
            return 0
        await sender.send_text("[faff] connection test ok")
        print("posted probe message")
        return 0
    finally:
        await sender.close()


async def cmd_now(cfg: Config, store_path: Path) -> int:
    ws = Workspace()
    fields = session_fields(ws.logs.get_log(ws.today()).active_session())
    if fields is None:
        print("no active session")
        return 0
    fields["start_time"] = fmt_time(fields["start"])
    body = render(cfg.templates["start"], fields)

    if cfg.dry_run:
        print(f"[dry_run] {body}")
        return 0

    sender = MatrixSender(cfg, store_path)
    try:
        await sender.setup()
        await sender.send_text(body)
        print(f"posted: {body}")
        return 0
    finally:
        await sender.close()


async def cmd_run(cfg: Config, store_path: Path, workspace_path: str) -> int:
    sender = MatrixSender(cfg, store_path)
    await sender.setup()
    print(f"authenticated as {sender.client.user_id} device {sender.client.device_id}", file=sys.stderr)
    print(f"posting to {sender.room_id}", file=sys.stderr)

    ws = Workspace()
    prev: Optional[dict[str, Any]] = session_fields(ws.logs.get_log(ws.today()).active_session())

    if cfg.announce_on_startup and prev is not None and "start" in cfg.notify_on:
        f = dict(prev)
        f["start_time"] = fmt_time(prev["start"])
        body = render(cfg.templates["start"], f)
        await _emit(sender, body, cfg.dry_run)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    t = threading.Thread(
        target=_watcher_thread, args=(workspace_path, loop, queue), daemon=True,
    )
    t.start()
    print(f"watching {workspace_path} for log changes...", file=sys.stderr)

    try:
        while True:
            item = await queue.get()
            if item is None:
                print("watcher stream ended", file=sys.stderr)
                return 1
            try:
                curr = session_fields(ws.logs.get_log(ws.today()).active_session())
            except Exception as e:
                print(f"warn: failed to read active session: {e}", file=sys.stderr)
                continue
            kind, body = diff_and_render(prev, curr, ws.now(), cfg.templates)
            prev = curr
            if kind and kind in cfg.notify_on and body:
                await _emit(sender, body, cfg.dry_run)
            # Periodic background sync keeps Megolm session keys current as
            # members join/leave the room.
            await sender.background_sync_once()
    finally:
        await sender.close()


async def _emit(sender: MatrixSender, body: str, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry_run] {body}")
        return
    try:
        await sender.send_text(body)
        print(body)
    except Exception as e:
        print(f"matrix post failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        prog="faff-plugin-matrix",
        description="Post the current faff to an end-to-end encrypted Matrix room (MAS-compatible).",
    )
    p.add_argument("--config", "-c", required=True, help="Path to plugin config TOML")
    p.add_argument("--workspace", default=os.path.expanduser("~/.faff"),
                   help="Faff workspace path (default: ~/.faff)")
    p.add_argument("--store-path", default=None,
                   help="E2EE crypto store directory (default: ~/.local/share/faff-plugin-matrix/<id>)")

    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("run",  help="Run the watcher loop (default).")
    sub.add_parser("test", help="Verify credentials and room access, post a probe message.")
    sub.add_parser("now",  help="Post the current active session once and exit.")

    args = p.parse_args()
    cfg = load_config(args.config)

    store_path = (
        Path(args.store_path) if args.store_path
        else Path(os.path.expanduser("~/.local/share/faff-plugin-matrix")) / cfg.instance_id
    )

    cmd = args.cmd or "run"
    try:
        if cmd == "test":
            return asyncio.run(cmd_test(cfg, store_path))
        if cmd == "now":
            return asyncio.run(cmd_now(cfg, store_path))
        return asyncio.run(cmd_run(cfg, store_path, args.workspace))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
