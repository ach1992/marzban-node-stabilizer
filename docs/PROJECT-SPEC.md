# Marzban Node Stabilizer — Project Specification

## Purpose

Marzban Node Stabilizer is a small, node-side compatibility and reliability helper for Marzban Node deployments.

Its purpose is to reduce avoidable startup/restart instability while preserving the upstream Marzban Node operating model and keeping recovery straightforward.

The project should remain small. It is not intended to become a fork of Marzban Node or a second control plane.

## Supported boundary

The repository owns only changes that can be delivered from this repository and applied on the Marzban Node host.

The Marzban Panel and upstream Marzban repositories are compatibility references, not mutation targets for this project.

If a future fix genuinely requires a Panel-side change, that work must be handled as a separate, explicitly authorized change in the appropriate repository. Do not hide a cross-repository dependency inside this stabilizer.

## Design principles

### 1. Prefer root-cause fixes over restart loops

Do not add a periodic "restart until it works" watchdog as the default solution.

Before adding self-healing, distinguish the failure being recovered from, such as:

- node container unavailable;
- Xray process unavailable;
- Xray process alive but not ready/usable;
- stale or conflicting session state;
- incompatible or stale patch source;
- invalid Compose/runtime configuration.

Recovery must be bounded, rate-limited where repeated action is possible, and observable.

### 2. Never acknowledge a config change that was not applied

A successful request must not silently discard a materially different Xray configuration.

Deduplication is acceptable only when the effective requested state is equivalent to the state already being started or run.

### 3. Protect lifecycle transitions

Connection, disconnect, start, and restart transitions must not allow stale or overlapping operations to tear down a newer valid state.

Use explicit state/serialization where needed instead of relying on timing alone.

Use monotonic time for elapsed-time and grace-period decisions.

### 4. Stay within the upstream caller contract

Node-side stabilization must not depend on making REST requests block for progressively longer periods.

Readiness should be detected promptly and returned as soon as it is established.

When upstream caller behavior matters, inspect the current upstream implementation at development time rather than copying a version-specific timeout into permanent project assumptions.

### 5. Do not pin obsolete upstream code silently

A persisted bind mount must not cause an old patched `rest_service.py` to mask newer image-provided code indefinitely.

The stabilizer should retain enough source identity to detect upstream drift.

When upstream code changes:

1. obtain the current image-provided source;
2. verify that the patch can still be applied safely;
3. rebuild the patched host copy from that source;
4. fail clearly and recoverably if compatibility cannot be established.

Never keep an old patch active merely because it still compiles.

### 6. Apply and restore must be recoverable

`apply` should be idempotent and safe to repeat after interruption.

Before recreating services after Compose mutation:

- keep a recoverable backup;
- write changes safely;
- validate the resulting Compose configuration;
- avoid concurrent stabilizer operations racing over the same files.

`restore` must remove the stabilizer override and return the container to the image-provided implementation without requiring chat history or manual reverse engineering.

### 7. Diagnostics should explain failure, not add noise

Diagnostics should collect only information that materially shortens incident diagnosis.

Useful evidence includes:

- effective container/service identity;
- container running/restart state;
- Xray process state;
- relevant listener/API readiness;
- effective patch source/hash or equivalent identity;
- detected upstream drift;
- recent restart/session-transition evidence;
- concise relevant logs.

Do not log secrets, private keys, certificates, tokens, or unrelated sensitive configuration.

## Compatibility

The project currently targets:

- Debian and Ubuntu hosts;
- Docker;
- Docker Compose plugin or legacy `docker-compose`;
- Marzban Node deployments where the target runtime file is provided by the container image.

Environment overrides documented in the README are part of the supported interface unless deliberately changed and documented.

Compatibility with upstream Marzban Node must be verified from current source at implementation time. Do not assume a previously supported source layout remains unchanged.

## Engineering workflow

Durable project truth should remain discoverable from the repository and GitHub:

- this document owns project purpose, boundaries, and lasting engineering invariants;
- `README.md` owns installation, usage, and operator-facing entry points;
- GitHub Issues own active unresolved work and acceptance criteria;
- Pull Requests own implementation/review/integration evidence;
- Git history owns exact implementation state.

Do not use documentation as a duplicate backlog or status tracker.

For substantive runtime changes:

1. define or reuse a GitHub Issue with observable acceptance criteria;
2. implement on an isolated branch;
3. add focused regression tests for the failure being fixed;
4. run the narrowest high-signal checks first;
5. validate against a disposable Marzban Node environment when runtime behavior changes;
6. use independent review for high-risk lifecycle/recovery changes before merge;
7. treat rollout to real nodes as a separate operational step from repository integration.

## Validation baseline

At minimum, keep these checks available for relevant changes:

```bash
bash -n install.sh
bash -n bin/marzban-node-stabilizer
python3 -m py_compile lib/patch_rest_service.py lib/compose_mount.py
python3 -m unittest discover -s tests -v
bash tests/test_installer_snapshot.sh
bash tests/test_compose_effective_inputs.sh
```

Use ShellCheck for shell changes. The repository CI should run the same focused validation rather than duplicating multiple equivalent suites.

Behavioral fixes should add focused regression coverage rather than relying only on shell syntax checks.

Patch-transform logic should be tested against representative reviewed upstream source fixtures and for repeat/idempotent application.

Compose-editing behavior should be tested against representative supported layouts and exact mount ownership, and validated before service recreation. The current helper intentionally narrows the entire path from the YAML root to the selected service and its `volumes:` list: the root/service definitions must use literal block mappings with plain keys; service-composition or mount-producing mechanisms the helper does not own (including top-level `include`, YAML merge, `extends`, `volumes_from`, `configs`, `secrets`, `tmpfs`, and `devices`) must fail closed; and the local `volumes:` block supports only single-line short syntax with container targets already expressed as canonical absolute Linux paths. Non-canonical target spellings that Compose/Docker would normalize (including repeated separators and dot/parent segments) must fail closed before ownership comparison. Canonical volume targets that are ancestors of the managed file path must also fail closed because they can own the effective `/code/rest_service.py` content without an exact file-target entry. Unsupported semantic-key forms, mapping/long syntax, or other ambiguous YAML must fail closed before mutation rather than be partially interpreted. Repository tests should include valid Docker Compose fixtures for supported fail-closed cases and should prove effective target canonicalization where ownership depends on consumer semantics, so malformed or semantically irrelevant fixtures cannot create false confidence.

When lifecycle or apply/restore behavior changes, disposable runtime validation must exercise the actual Marzban Node REST service path sufficiently to cover the affected request/concurrency behavior. A container that only sleeps is not sufficient evidence for lifecycle correctness. When relevant, runtime validation should also cover recreated-image identity changes and fail-closed handling of incompatible upstream source.

## Non-goals

This project should not become:

- a fork of Marzban or Marzban Node;
- a replacement health-monitoring platform;
- an unconditional periodic restart daemon;
- a collection of unrelated kernel/network tuning;
- a place to persist Panel-side changes;
- a compatibility layer that silently ignores upstream drift.

## Change discipline

Prefer the smallest change that fixes a demonstrated failure mode.

Do not add retries, watchdogs, dependencies, services, telemetry, or abstractions without evidence that they solve a material reliability or diagnosability problem.

When a new failure is observed in production, capture enough evidence to classify it before broadening stabilization behavior.
