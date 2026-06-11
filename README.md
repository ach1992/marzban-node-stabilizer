# marzban-node-stabilizer

A one-command Debian/Ubuntu helper script to stabilize Marzban Node startup/restart behavior when using heavy Xray configs.

## Install and apply automatically

After uploading this repository to GitHub, run:

```bash
curl -fsSL https://raw.githubusercontent.com/ach1992/marzban-node-stabilizer/main/install.sh | sudo bash
```

This one command will install the script and run the patch automatically.

## What it does

This script patches `/code/rest_service.py` inside the running `marzban-node` container and then persists the patched file on the host using a Docker Compose bind mount.

Main changes:

- Increases Xray startup/restart wait timeout.
- Tracks the latest Xray start timestamp.
- Ignores restart requests during the startup grace period.
- Creates a backup of the Docker Compose file before editing.
- Supports restore by removing the bind mount.

## Supported OS

- Debian
- Ubuntu

## Requirements

- root access
- Docker
- Docker Compose plugin or legacy `docker-compose`
- Python 3
- A running Marzban Node container

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
