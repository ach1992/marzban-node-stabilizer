#!/usr/bin/env python3
"""Minimal fail-closed editor for the stabilizer's Compose bind mount.

This intentionally supports the common short-syntax volumes form without adding a
YAML dependency. Unsupported existing long-syntax mounts targeting
/code/rest_service.py are rejected instead of being partially rewritten.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
import tempfile
from pathlib import Path

TARGET = "/code/rest_service.py"


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _service_bounds(lines: list[str], service_name: str) -> tuple[int, int, int]:
    services_idx = None
    services_indent = None
    for idx, line in enumerate(lines):
        if line.strip() == "services:":
            services_idx = idx
            services_indent = _indent(line)
            break
    if services_idx is None:
        raise ValueError("Compose file has no services: section")

    section_end = len(lines)
    for idx in range(services_idx + 1, len(lines)):
        line = lines[idx]
        if line.strip() and _indent(line) <= services_indent:
            section_end = idx
            break

    service_idx = None
    service_indent = None
    for idx in range(services_idx + 1, section_end):
        line = lines[idx]
        if line.strip() == f"{service_name}:":
            service_idx = idx
            service_indent = _indent(line)
            break
    if service_idx is None:
        raise ValueError(f"Compose service not found: {service_name}")

    service_end = section_end
    for idx in range(service_idx + 1, section_end):
        line = lines[idx]
        if line.strip() and _indent(line) <= service_indent:
            service_end = idx
            break

    return service_idx, service_end, service_indent


def _service_target_lines(lines: list[str], service_name: str) -> list[int]:
    service_idx, service_end, _ = _service_bounds(lines, service_name)
    return [idx for idx in range(service_idx + 1, service_end) if TARGET in lines[idx]]


def service_has_mount(text: str, service_name: str) -> bool:
    lines = text.splitlines()
    return bool(_service_target_lines(lines, service_name))


def mount_source(text: str, service_name: str) -> str | None:
    lines = text.splitlines()
    indexes = _service_target_lines(lines, service_name)
    if not indexes:
        return None
    if len(indexes) != 1:
        raise ValueError("multiple rest_service.py mounts found for selected service")

    line = lines[indexes[0]].strip()
    if not line.startswith("- "):
        raise ValueError(
            "existing rest_service.py mount uses unsupported long syntax; refusing partial rewrite"
        )

    value = line[2:].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]

    marker = f":{TARGET}"
    if marker not in value:
        raise ValueError("could not determine rest_service.py bind-mount source")
    return value.split(marker, 1)[0]


def _remove_target_lines(lines: list[str], service_name: str) -> list[str]:
    target_indexes = _service_target_lines(lines, service_name)
    for idx in target_indexes:
        if not lines[idx].strip().startswith("- "):
            raise ValueError(
                "existing rest_service.py mount uses unsupported long syntax; refusing partial rewrite"
            )
    target_set = set(target_indexes)
    return [line for idx, line in enumerate(lines) if idx not in target_set]


def _remove_empty_service_volumes(lines: list[str], service_name: str) -> list[str]:
    service_idx, service_end, _ = _service_bounds(lines, service_name)
    idx = service_idx + 1
    while idx < service_end:
        line = lines[idx]
        if line.strip() != "volumes:":
            idx += 1
            continue

        vol_indent = _indent(line)
        end = idx + 1
        has_item = False
        while end < service_end:
            next_line = lines[end]
            if next_line.strip() and _indent(next_line) <= vol_indent:
                break
            if next_line.strip().startswith("- "):
                has_item = True
            end += 1

        if not has_item:
            return lines[:idx] + lines[end:]
        idx = end
    return lines


def remove_mount(text: str, service_name: str) -> str:
    lines = text.splitlines()
    _service_bounds(lines, service_name)
    lines = _remove_target_lines(lines, service_name)
    lines = _remove_empty_service_volumes(lines, service_name)
    return "\n".join(lines) + "\n"


def add_mount(text: str, service_name: str, patch_file: str) -> str:
    if not patch_file.startswith("/"):
        raise ValueError("patch file path must be absolute")
    if any(ch in patch_file for ch in ("\n", "\r", '"', ":", "$")):
        raise ValueError("patch file path contains unsupported characters")

    lines = text.splitlines()
    _service_bounds(lines, service_name)
    lines = _remove_target_lines(lines, service_name)
    service_idx, service_end, service_indent = _service_bounds(lines, service_name)

    mount_line = f'- "{patch_file}:{TARGET}:ro"'
    volumes_idx = None
    for idx in range(service_idx + 1, service_end):
        if lines[idx].strip() == "volumes:":
            volumes_idx = idx
            break

    if volumes_idx is None:
        key_indent = " " * (service_indent + 2)
        item_indent = " " * (service_indent + 4)
        lines.insert(service_end, key_indent + "volumes:")
        lines.insert(service_end + 1, item_indent + mount_line)
    else:
        vol_indent = _indent(lines[volumes_idx])
        item_indent = " " * (vol_indent + 2)
        insert_at = volumes_idx + 1
        while insert_at < service_end:
            line = lines[insert_at]
            if line.strip() and _indent(line) <= vol_indent:
                break
            insert_at += 1
        lines.insert(insert_at, item_indent + mount_line)

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
