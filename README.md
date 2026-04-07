# faff-plugin-matrix

A faff sidecar that announces the **current faff** (active session start /
stop / switch) into a Matrix room. Designed for **end-to-end encrypted**
rooms on **Matrix Authentication Service (MAS)** homeservers such as
`matrix.datanauten.de`.

It is modelled on `faff-menubar-mac`: it spawns the Rust core's
`start_watching` iterator in a background thread, diffs the active session on
every `log_changed` event, and posts a formatted message via
[matrix-nio](https://github.com/matrix-nio/matrix-nio) with E2EE enabled.

## How it differs from the other plugins

The existing `faff-plugins/*` modules subclass `PlanSource` / `Audience` —
those interfaces are for pulling planned work and pushing compiled timesheets
on demand. "Broadcast the current task whenever it changes" doesn't fit
either, so this is a **standalone sidecar process**, not an in-process
plugin. Same shape as `faff-menubar-mac`.

## Requirements

- Python 3.11+
- **libolm** development headers (matrix-nio's E2EE backend pulls in
  `python-olm` which links against `libolm`):
  - Fedora / RHEL:   `sudo dnf install libolm-devel`
  - Debian / Ubuntu: `sudo apt install libolm-dev`
  - macOS (brew):    `brew install libolm`
- A Matrix account on the target homeserver, already a member of the room
  you want to post into. **The bot will not auto-join.**

## Install

```sh
pip install -e .
```

This pulls `faff-core` and `matrix-nio[e2e]`.

## Getting credentials under MAS

MAS homeservers (e.g. `matrix.datanauten.de`) **disable the legacy
`/_matrix/client/v3/login` password endpoint**. Therefore this plugin never
calls `login()`. You must obtain three things and put them in the config:

- `user_id`     — e.g. `@yourbot:datanauten.de`
- `device_id`   — the device the access token was issued for
- `access_token` — a valid Matrix C-S API access token

The `device_id` and `access_token` **must be a matched pair**. If you mix a
token from one device with a different device id, Megolm key sharing will
silently fail and the bot's messages will be undecryptable for everyone.

There are two practical ways to get this triple:

### Option A — `mas-cli` (server admin)

If you administer the homeserver:

```sh
mas-cli manage issue-compatibility-token <username>
```

prints a `device_id` and an `access_token` for that user. Combine with the
known `user_id`.

### Option B — Element session (no admin needed)

1. Log in as the bot user in Element Web or Element X.
2. In Element Web: **Settings → Help & About → Advanced**. Reveal
   `Access Token`, `Device ID`, and your full `User ID`.
3. Copy all three into the config below.

Treat the access token like a password — it grants full account access.

## Configure

Copy `config.template.toml` to a real path and fill in the values:

```toml
id = "personal"
plugin = "faff-plugin-matrix"

[connection]
homeserver = "https://matrix.datanauten.de"
user_id    = "@yourbot:datanauten.de"
device_id  = "ABCDEFGHIJ"
# Prefer the env var form over committing the token to disk:
access_token_env = "FAFF_MATRIX_TOKEN"

room = "#faff-status:datanauten.de"

[options]
notify_on = ["start", "stop", "switch"]
```

Then export the token before running:

```sh
export FAFF_MATRIX_TOKEN="syt_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

## Use

```sh
# 1. Verify everything works end-to-end and post a probe message.
faff-plugin-matrix -c ~/.config/faff/plugin-matrix.toml test

# 2. Post the current active session once and exit (handy for cron / hooks).
faff-plugin-matrix -c ~/.config/faff/plugin-matrix.toml now

# 3. Run the watcher loop. Posts on every start / stop / switch.
faff-plugin-matrix -c ~/.config/faff/plugin-matrix.toml run
```

## E2EE notes

- The encryption store lives at `~/.local/share/faff-plugin-matrix/<id>/`.
  **Do not delete it** — it holds the Olm session keys for this device. If
  you nuke it, you have to re-issue the device + token.
- The bot **does not verify devices**. It sends with
  `ignore_unverified_devices=True`, which means any device currently in the
  room receives the message keys. This is the correct trade-off for a
  posting-only bot, but worth knowing.
- On first run, the bot does a `full_state=True` sync so it learns the
  membership of the encrypted room and can immediately share keys. After
  that, every event handled by the watcher also triggers a short background
  sync to keep up with member changes.

## Run as a systemd user service

`~/.config/systemd/user/faff-plugin-matrix.service`:

```ini
[Unit]
Description=faff matrix sidecar
After=default.target

[Service]
Type=simple
Environment=FAFF_MATRIX_TOKEN=syt_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
ExecStart=%h/.local/bin/faff-plugin-matrix -c %h/.config/faff/plugin-matrix.toml run
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now faff-plugin-matrix
journalctl --user -u faff-plugin-matrix -f
```

## Limitations / not yet

- No interactive `login` subcommand. MAS requires the OAuth2 device-code
  flow, which needs a registered client_id at the homeserver. Out of scope
  for now — get a token via Option A or B above.
- No device verification / cross-signing.
- No reply threading or rich (HTML) formatting — plain `m.text` only.
