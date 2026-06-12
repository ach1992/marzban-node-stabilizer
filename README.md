# Marzban Node Stabilizer

A one-command Debian/Ubuntu helper script to stabilize Marzban Node startup/restart behavior when using heavy Xray configs.

This version is resumable and idempotent. If the script is interrupted halfway, running the same install command again should recover the server state automatically.

## Install and apply automatically

After uploading this repository to GitHub, run:

```bash
curl -fsSL https://raw.githubusercontent.com/ach1992/marzban-node-stabilizer/main/install.sh | sudo bash
```

This command will:

1. Check that the OS is Debian or Ubuntu.
2. Install missing basic dependencies: `curl`, `ca-certificates`, `python3`.
3. Download `marzban-node-stabilizer` into `/usr/local/sbin/`.
4. Run the patch automatically.
5. Recover automatically if a previous run was interrupted.

## What it does

This script patches `/code/rest_service.py` for the `marzban-node` container and persists the patched file on the host using a Docker Compose bind mount.

Main changes:

- Increases Xray startup/restart wait timeout.
- Tracks the latest Xray start timestamp.
- Ignores restart requests during the startup grace period.
- Creates a backup of the Docker Compose file before editing.
- Supports restore by removing the bind mount.
- Can resume if interrupted halfway.

## Supported OS

- Debian
- Ubuntu

## Requirements

- root access
- Docker
- Docker Compose plugin or legacy `docker-compose`
- Python 3
- A Marzban Node Docker Compose installation

Default assumptions:

```bash
CONTAINER_NAME=marzban-node
SERVICE_NAME=marzban-node
COMPOSE_FILE=/opt/marzban-node/docker-compose.yml
```

## Custom install/apply

If your paths or container names are different:

```bash
curl -fsSL https://raw.githubusercontent.com/ach1992/marzban-node-stabilizer/main/install.sh | sudo env \
  COMPOSE_FILE=/opt/marzban-node/docker-compose.yml \
  CONTAINER_NAME=marzban-node \
  SERVICE_NAME=marzban-node \
  bash
```

Custom timeout values:

```bash
curl -fsSL https://raw.githubusercontent.com/ach1992/marzban-node-stabilizer/main/install.sh | sudo env \
  TIMEOUT_SECONDS=60 \
  RESTART_GRACE_SECONDS=90 \
  bash
```

## Install only without applying

```bash
curl -fsSL https://raw.githubusercontent.com/ach1992/marzban-node-stabilizer/main/install.sh | sudo env AUTO_APPLY=0 bash
```

Then apply manually:

```bash
sudo marzban-node-stabilizer apply
```

## Check status

```bash
sudo marzban-node-stabilizer status
```

## Restore

This removes the bind mount from `docker-compose.yml` and recreates the container.

```bash
sudo marzban-node-stabilizer restore
```

## Uninstall

First restore if needed:

```bash
sudo marzban-node-stabilizer restore
```

Then remove the installed command:

```bash
sudo rm -f /usr/local/sbin/marzban-node-stabilizer
```

## Files created on the server

```text
/opt/marzban-node-patches/rest_service.py
/opt/marzban-node-patches/rest_service.py.original_backup
/opt/marzban-node/docker-compose.yml.bak.YYYYMMDD-HHMMSS
```

## Notes

This is an unofficial patch helper. Use it only if you understand that it modifies the runtime behavior of Marzban Node.

Before running it on production, make sure you have access to the server and can restore your Docker Compose file if needed.
