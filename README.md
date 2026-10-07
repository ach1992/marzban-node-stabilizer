# Marzban Node Stabilizer

A small Debian/Ubuntu helper that applies a conservative, recoverable node-side patch for Marzban Node startup/restart instability.

The project is intentionally limited to the Marzban Node host. It does **not** modify the Marzban Panel.

## Install and apply automatically

```bash
curl -fsSL https://raw.githubusercontent.com/ach1992/marzban-node-stabilizer/main/install.sh | sudo bash
```

The installer downloads the CLI and its two small Python helpers, validates their syntax, installs them under `/usr/local/`, and runs `apply` unless `AUTO_APPLY=0` is set.

## What `apply` does

The stabilizer rebuilds its patched `/code/rest_service.py` from the **current container image source on every apply** rather than continuing to patch an old host copy.

The patch is designed to:

- return from Xray startup/restart confirmation as soon as the Xray started signal is observed;
- keep the node-side confirmation window within the current caller request budget;
- deduplicate only same-config restarts during the startup grace period;
- never silently drop a materially different requested Xray config;
- use monotonic time for grace-period calculations;
- serialize lifecycle transitions and reject stale-session disconnects;
- avoid stopping a running Xray core merely because a newer control session connects;
- fail closed if the upstream methods being replaced have changed unexpectedly;
- record the source/image identity used to build the active patch;
- validate Docker Compose before service recreation;
- prevent concurrent `apply`/`restore` operations with a host lock;
- recreate only the configured Marzban Node Compose service.

The patched file is persisted with a Docker Compose bind mount so normal container recreation keeps the stabilization active.

## Supported OS

- Debian
- Ubuntu

## Requirements

- root access
- Docker
- Docker Compose plugin or legacy `docker-compose`
- Python 3
- `flock` (`util-linux`)
- a Marzban Node Docker Compose installation

Default assumptions:

```bash
CONTAINER_NAME=marzban-node
SERVICE_NAME=marzban-node
COMPOSE_FILE=/opt/marzban-node/docker-compose.yml
```

## Custom install/apply

If paths or service names differ:

```bash
curl -fsSL https://raw.githubusercontent.com/ach1992/marzban-node-stabilizer/main/install.sh | sudo env \
  COMPOSE_FILE=/opt/marzban-node/docker-compose.yml \
  CONTAINER_NAME=marzban-node \
  SERVICE_NAME=marzban-node \
  bash
```

Runtime tuning:

```bash
curl -fsSL https://raw.githubusercontent.com/ach1992/marzban-node-stabilizer/main/install.sh | sudo env \
  TIMEOUT_SECONDS=7 \
  RESTART_GRACE_SECONDS=60 \
  bash
```

`TIMEOUT_SECONDS` is the maximum node-side confirmation wait. The current implementation clamps values above `8` seconds to keep the node response inside the current upstream caller budget. The loop exits earlier as soon as Xray readiness is observed.

`RESTART_GRACE_SECONDS` controls same-config restart deduplication. A different config is never ignored because of the grace period.

## Install only without applying

```bash
curl -fsSL https://raw.githubusercontent.com/ach1992/marzban-node-stabilizer/main/install.sh | sudo env AUTO_APPLY=0 bash
```

Then apply manually:

```bash
sudo marzban-node-stabilizer apply
```

## Status

```bash
sudo marzban-node-stabilizer status
```

Shows container state, bind-mount state, patch metadata, Xray process/listeners when available, and recent logs.

## Diagnose intermittent failures

```bash
sudo marzban-node-stabilizer diagnose
```

In addition to normal status, this compares the current image-provided `rest_service.py` identity with the source used for the active patch and shows restart/session-focused recent logs.

This command does not restart the Marzban Node service.

## Restore

```bash
sudo marzban-node-stabilizer restore
```

Restore removes the stabilizer bind mount, validates the resulting Compose configuration, and recreates only the configured service. If recreation fails, it attempts to reinstate the previous Compose state.

## Uninstall

Restore first if the patch is active:

```bash
sudo marzban-node-stabilizer restore
```

Then remove the installed components:

```bash
sudo rm -f /usr/local/sbin/marzban-node-stabilizer
sudo rm -rf /usr/local/lib/marzban-node-stabilizer
```

## Files created on the server

```text
/usr/local/sbin/marzban-node-stabilizer
/usr/local/lib/marzban-node-stabilizer/patch_rest_service.py
/usr/local/lib/marzban-node-stabilizer/compose_mount.py
/opt/marzban-node-patches/rest_service.py
/opt/marzban-node-patches/rest_service.py.original_backup
/opt/marzban-node-patches/metadata.env
/opt/marzban-node/docker-compose.yml.bak.YYYYMMDD-HHMMSS.PID
```

Custom paths change the corresponding entries.

## Upstream compatibility

The source transformer deliberately refuses to replace lifecycle methods whose reviewed upstream shape has changed. This is intentional: an upstream change must be reviewed before this project overwrites it.

If `apply` reports upstream drift/incompatibility, do not bypass the check by forcing an old patched file onto the new image. Review the new upstream source and update the transformer/tests instead.

## Development and project scope

The durable project boundary, engineering invariants, validation expectations, and maintenance workflow are documented in [`docs/PROJECT-SPEC.md`](docs/PROJECT-SPEC.md).

Active implementation work belongs in GitHub Issues; Pull Requests carry implementation and review evidence.

## Notes

This is an unofficial compatibility helper that changes Marzban Node runtime behavior. Keep working server access and a recovery path available before production rollout.
