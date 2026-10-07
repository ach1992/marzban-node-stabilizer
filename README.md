# Marzban Node Stabilizer

A small Debian/Ubuntu helper that applies a conservative, recoverable node-side patch for Marzban Node startup/restart instability.

The project is intentionally limited to the Marzban Node host. It does **not** modify the Marzban Panel.

## Install the stable release

The recommended production-facing install path is the latest tagged release. For `v0.2.0`:

```bash
curl -fsSL https://raw.githubusercontent.com/ach1992/marzban-node-stabilizer/v0.2.0/install.sh | sudo env REF=v0.2.0 bash
```

Both the installer URL and `REF` are pinned to the same release. The installer then resolves `REF` once to an immutable commit SHA and downloads the CLI and both Python helpers from that exact snapshot. It validates their syntax, installs them under `/usr/local/`, and runs `apply` unless `AUTO_APPLY=0` is set.

### Install the latest development state

Use `main` only when you intentionally want the newest integrated repository state instead of a tagged release:

```bash
curl -fsSL https://raw.githubusercontent.com/ach1992/marzban-node-stabilizer/main/install.sh | sudo env REF=main bash
```

For normal installs and repeatable recovery, prefer a tagged release.

## What `apply` does

The stabilizer rebuilds its patched `/code/rest_service.py` from the **current container image source on every apply** rather than continuing to patch an old host copy.

The patch is designed to:

- return from Xray startup/restart confirmation as soon as the Xray started signal is observed;
- keep the node-side confirmation window within the current caller request budget;
- deduplicate only same-config restarts during the startup grace period;
- never silently drop a materially different requested Xray config;
- use monotonic time for grace-period calculations;
- serialize lifecycle transitions without holding the lifecycle lock through readiness polling;
- use lifecycle generations so superseded start/restart completions cannot overwrite newer session/core state;
- preserve upstream session-takeover behavior while preventing an older session from tearing down the newer one;
- fail closed if the upstream methods being replaced have changed unexpectedly;
- record the source/image identity used to build the active patch and verify the recreated container image still matches it;
- rebase once onto a different compatible recreated image, otherwise disable the stale mount and fail closed;
- validate Docker Compose before service recreation;
- prevent concurrent `apply`/`restore` operations with a host lock;
- recreate only the configured Marzban Node Compose service.

The patched file is persisted with a Docker Compose bind mount so normal container recreation keeps the stabilization active.

For safety, the Compose editor deliberately supports only a narrow representation it can classify without ambiguity. The root `services` mapping and selected service must use literal block mappings with plain unquoted keys; top-level `include`, YAML merge/service composition, Compose `extends`, `volumes_from`, `configs`, `secrets`, `tmpfs`, `devices`, quoted/explicit semantic keys, and other unclassified service-composition forms are refused before mutation. Within the selected service, `volumes:` must be a block list of single-line short-syntax entries whose container targets are already canonical absolute Linux paths. Targets containing repeated separators, `.` / `..` path segments, trailing separators, or other non-canonical spellings are refused before ownership comparison because Compose/Docker can normalize those spellings to the same runtime destination. Canonical ancestor mounts such as `/code` or `/` are also refused because they can supply the managed `/code/rest_service.py` path even without an exact file-target entry. Anonymous/target-only volume entries are unsupported altogether because their ownership source is implicit and can create the same ambiguity. Mapping/long syntax, flow/alias/anchor layouts, interpolation, anonymous exact `/code/rest_service.py` targets, and other ambiguous volume forms are also refused rather than partially rewritten.

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
curl -fsSL https://raw.githubusercontent.com/ach1992/marzban-node-stabilizer/v0.2.0/install.sh | sudo env \
  REF=v0.2.0 \
  COMPOSE_FILE=/opt/marzban-node/docker-compose.yml \
  CONTAINER_NAME=marzban-node \
  SERVICE_NAME=marzban-node \
  bash
```

Runtime tuning:

```bash
curl -fsSL https://raw.githubusercontent.com/ach1992/marzban-node-stabilizer/v0.2.0/install.sh | sudo env \
  REF=v0.2.0 \
  TIMEOUT_SECONDS=7 \
  RESTART_GRACE_SECONDS=60 \
  bash
```

`TIMEOUT_SECONDS` is the maximum node-side confirmation wait. The current implementation clamps values above `7` seconds, leaving margin inside the current 10-second upstream start/restart request budget for config transformation and core process work. The loop exits earlier as soon as Xray readiness is observed.

`RESTART_GRACE_SECONDS` controls same-config restart deduplication. A different config is never ignored because of the grace period.

`STARTUP_WAIT_SECONDS` controls only the non-fatal post-apply observation window after the service is recreated. Its default is `30` seconds, and the command exits earlier as soon as Xray is detected. It is separate from the `TIMEOUT_SECONDS=7` request-budget protection. Runtime detection reads Linux `/proc` directly, so minimal Marzban Node images do not need `pgrep` or `ss` installed.

## Install only without applying

```bash
curl -fsSL https://raw.githubusercontent.com/ach1992/marzban-node-stabilizer/v0.2.0/install.sh | sudo env REF=v0.2.0 AUTO_APPLY=0 bash
```

Then apply manually:

```bash
sudo marzban-node-stabilizer apply
```

## Upgrade or reinstall

Re-running the installer for a chosen release replaces the installed CLI/helpers from one immutable release snapshot and runs `apply` again by default. `apply` is designed to be repeatable: it rebuilds the patch from the currently running service image source rather than reusing an old patched host copy.

To move to a newer release, change both the installer tag and `REF` to that release. To install files without applying immediately, add `AUTO_APPLY=0`.

## Status

```bash
sudo marzban-node-stabilizer status
```

Shows container state, bind-mount state, patch metadata, Xray process/listeners when available, and recent logs. Listener diagnostics read the effective `SERVICE_PORT` and `XRAY_API_PORT` from the running container environment, falling back to Marzban Node defaults only when those variables are genuinely absent. Explicitly empty/invalid values or an inspection failure are reported as unknown/invalid rather than silently replaced with defaults.

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

Restore removes only a bind mount whose Stabilizer ownership is verified from its path, patch marker, metadata, and patch hash. Foreign `/code/rest_service.py` mounts are refused without mutation. The resulting Compose configuration is validated before recreating only the configured service; rollback recreation is verified and secondary recovery failure is reported explicitly.

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

After recreation, `apply` verifies the actual container image identity against the image used to build the patch. If recreation resolves to a different compatible image, the patch is rebuilt from that image and the service is recreated once more. If the new source is incompatible or the image changes again, stale-mount disablement is attempted and `apply` fails instead of reporting success with an old host copy masking the current image source.

## Development and project scope

The durable project boundary, engineering invariants, validation expectations, and maintenance workflow are documented in [`docs/PROJECT-SPEC.md`](docs/PROJECT-SPEC.md).

Active implementation work belongs in GitHub Issues; Pull Requests carry implementation and review evidence.

## Notes

This is an unofficial compatibility helper that changes Marzban Node runtime behavior. Keep working server access and a recovery path available before production rollout.
