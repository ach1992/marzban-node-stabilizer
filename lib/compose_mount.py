#!/usr/bin/env python3
"""Fail-closed editor for the Stabilizer's Compose bind mount.

Only a service-level ``volumes:`` sequence using short syntax is mutated. Other
service configuration that happens to mention /code/rest_service.py is ignored.
Long-syntax mounts targeting the same path and ambiguous duplicate target mounts
are rejected.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

TARGET = "/code/rest_service.py"


class VolumeMount(NamedTuple):
    index: int
    source: str
    target: str
    options: str | None


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _service_bounds(lines: list[str], service_name: str) -> tuple[int, int, int]:
    services = [
        (idx, _indent(line))
        for idx, line in enumerate(lines)
        if line.strip() == "services:"
    ]
    if len(services) != 1:
        raise ValueError(f"expected exactly one services: section, found {len(services)}")
    services_idx, services_indent = services[0]

    section_end = len(lines)
    for idx in range(services_idx + 1, len(lines)):
        line = lines[idx]
        if line.strip() and _indent(line) <= services_indent:
            section_end = idx
            break

    expected_service_indent = services_indent + 2
    matches = [
        idx
        for idx in range(services_idx + 1, section_end)
        if _indent(lines[idx]) == expected_service_indent
        and lines[idx].strip() == f"{service_name}:"
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one Compose service {service_name!r}, found {len(matches)}"
        )
    service_idx = matches[0]
    service_indent = expected_service_indent

    service_end = section_end
    for idx in range(service_idx + 1, section_end):
        line = lines[idx]
        if line.strip() and _indent(line) <= service_indent:
            service_end = idx
            break

    return service_idx, service_end, service_indent


def _volumes_bounds(
    lines: list[str], service_name: str
) -> tuple[int, int, int] | None:
    service_idx, service_end, service_indent = _service_bounds(lines, service_name)
    key_indent = service_indent + 2
    matches = [
        idx
        for idx in range(service_idx + 1, service_end)
        if _indent(lines[idx]) == key_indent and lines[idx].strip() == "volumes:"
    ]
    if len(matches) > 1:
        raise ValueError("multiple service-level volumes: keys are unsupported")
    if not matches:
        return None

    volumes_idx = matches[0]
    volumes_indent = key_indent
    volumes_end = service_end
    for idx in range(volumes_idx + 1, service_end):
        line = lines[idx]
        if line.strip() and _indent(line) <= volumes_indent:
            volumes_end = idx
            break
    return volumes_idx, volumes_end, volumes_indent


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _parse_short_mount(line: str, item_indent: int, index: int) -> VolumeMount | None:
    if _indent(line) != item_indent:
        return None
    stripped = line.strip()
    if not stripped.startswith("- "):
        return None
    value = _unquote(stripped[2:].strip())
    if not value or value.startswith(("type:", "source:", "target:")):
        return None

    parts = value.split(":")
    if len(parts) < 2:
        return None
    source = parts[0]
    target = parts[1]
    options = ":".join(parts[2:]) if len(parts) > 2 else None
    return VolumeMount(index=index, source=source, target=target, options=options)


def _target_mounts(lines: list[str], service_name: str) -> list[VolumeMount]:
    bounds = _volumes_bounds(lines, service_name)
    if bounds is None:
        return []
    volumes_idx, volumes_end, volumes_indent = bounds
    item_indent = volumes_indent + 2

    for idx in range(volumes_idx + 1, volumes_end):
        line = lines[idx]
        if _indent(line) <= item_indent:
            continue
        stripped = line.strip()
        if stripped.startswith("target:") and _unquote(stripped.split(":", 1)[1]) == TARGET:
            raise ValueError(
                "existing rest_service.py mount uses unsupported long syntax; refusing partial rewrite"
            )

    mounts: list[VolumeMount] = []
    for idx in range(volumes_idx + 1, volumes_end):
        mount = _parse_short_mount(lines[idx], item_indent, idx)
        if mount is not None and mount.target == TARGET:
            mounts.append(mount)

    if len(mounts) > 1:
        raise ValueError("multiple exact rest_service.py mounts found for selected service")
    return mounts


def service_has_mount(text: str, service_name: str) -> bool:
    return bool(_target_mounts(text.splitlines(), service_name))


def mount_source(text: str, service_name: str) -> str | None:
    mounts = _target_mounts(text.splitlines(), service_name)
    return mounts[0].source if mounts else None


def _remove_target_mount(lines: list[str], service_name: str) -> list[str]:
    mounts = _target_mounts(lines, service_name)
    if not mounts:
        return lines
    idx = mounts[0].index
    return lines[:idx] + lines[idx + 1 :]


def _remove_empty_service_volumes(lines: list[str], service_name: str) -> list[str]:
    bounds = _volumes_bounds(lines, service_name)
    if bounds is None:
        return lines
    volumes_idx, volumes_end, volumes_indent = bounds
    item_indent = volumes_indent + 2

    meaningful = []
    for idx in range(volumes_idx + 1, volumes_end):
        line = lines[idx]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _indent(line) >= item_indent:
            meaningful.append(idx)
    if meaningful:
        return lines
    return lines[:volumes_idx] + lines[volumes_end:]


def remove_mount(text: str, service_name: str) -> str:
    lines = text.splitlines()
    _service_bounds(lines, service_name)
    lines = _remove_target_mount(lines, service_name)
    lines = _remove_empty_service_volumes(lines, service_name)
    return "\n".join(lines) + "\n"


def add_mount(text: str, service_name: str, patch_file: str) -> str:
    if not patch_file.startswith("/"):
        raise ValueError("patch file path must be absolute")
    if any(ch in patch_file for ch in ("\n", "\r", '"', "'", ":", "$")):
        raise ValueError("patch file path contains unsupported characters")

    lines = text.splitlines()
    _service_bounds(lines, service_name)
    mounts = _target_mounts(lines, service_name)
    if mounts and mounts[0].source == patch_file and mounts[0].options == "ro":
        return "\n".join(lines) + "\n"
    if mounts:
        lines = _remove_target_mount(lines, service_name)

    service_idx, service_end, service_indent = _service_bounds(lines, service_name)
    bounds = _volumes_bounds(lines, service_name)
    mount_line = f'- "{patch_file}:{TARGET}:ro"'

    if bounds is None:
        key_indent = " " * (service_indent + 2)
        item_indent = " " * (service_indent + 4)
        lines.insert(service_end, key_indent + "volumes:")
        lines.insert(service_end + 1, item_indent + mount_line)
    else:
        volumes_idx, volumes_end, volumes_indent = bounds
        item_indent = " " * (volumes_indent + 2)
        lines.insert(volumes_end, item_indent + mount_line)

    return "\n".join(lines) + "\n"


def atomic_write(path: Path, text: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        temp_path = Path(handle.name)
    try:
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("add", "remove", "has", "source"))
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--service", required=True)
    parser.add_argument("--patch-file")
    args = parser.parse_args()

    original = args.file.read_text()
    try:
        if args.action == "has":
            return 0 if service_has_mount(original, args.service) else 1
        if args.action == "source":
            source = mount_source(original, args.service)
            if source:
                print(source)
            return 0
        if args.action == "add":
            if not args.patch_file:
                raise ValueError("--patch-file is required for add")
            updated = add_mount(original, args.service, args.patch_file)
        else:
            updated = remove_mount(original, args.service)
    except Exception as exc:
        print(f"[ERROR] Could not safely inspect/edit {args.file}: {exc}", file=sys.stderr)
        return 2

    if updated != original:
        atomic_write(args.file, updated)
        print(f"[OK] compose mount {args.action} completed")
    else:
        print(f"[OK] compose mount {args.action}: no change needed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
