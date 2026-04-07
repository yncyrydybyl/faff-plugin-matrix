# faff-plugin-matrix
A faffage plugin for posting the current faff into an end-to-end encrypted Matrix room.

Designed for [Matrix Authentication Service](https://element-hq.github.io/matrix-authentication-service/) homeservers — restores its session from a `(user_id, device_id, access_token)` triple and never calls the legacy `/login` endpoint.

## Installation

Requires `libolm` (Fedora: `dnf install libolm-devel`, Debian: `apt install libolm-dev`).

```sh
pip install -e .
```

## Configuration

Copy `config.template.toml` somewhere and fill it in. Get the
`(user_id, device_id, access_token)` triple from `mas-cli manage
issue-compatibility-token <username> [device_id]` on the homeserver, or
from an existing Element session (Settings → Help & About → Advanced).
The bot must already be a member of the target room — this plugin will
not auto-join.

The token is best kept out of dotfiles via `access_token_env`:

```sh
export FAFF_MATRIX_TOKEN="mct_..."
```

## Usage

```sh
# Verify credentials, resolve room, post a probe.
faff-plugin-matrix -c ~/.config/faff/plugin-matrix.toml test

# Post the current active session once and exit.
faff-plugin-matrix -c ~/.config/faff/plugin-matrix.toml now

# Run the watcher loop. Posts on every start / stop / switch.
faff-plugin-matrix -c ~/.config/faff/plugin-matrix.toml run
```

The watcher uses `faff_core.start_watching` and announces transitions with templated messages from `[options.templates]` in the config.

## Notes

- Encryption store lives at `~/.local/share/faff-plugin-matrix/<id>/`. Don't delete it.
- Sends use `ignore_unverified_devices=True` — keys are shared with all devices in the room without manual verification.
