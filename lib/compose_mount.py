#!/usr/bin/env python3
"""Fail-closed editor for the Stabilizer's Compose bind mount.

This helper intentionally supports only a narrow, fully classified Compose
subset: a block-style service volumes sequence containing single-line
short-syntax scalar entries. Mapping/long syntax, aliases/anchors, flow
collections, interpolation, multiline items, and other layouts the helper
cannot prove safe are rejected before mutation.

The safety goal is stronger than broad YAML compatibility: never create or
remove an exact /code/rest_service.py target unless ownership can be determined
unambiguously from the supported representation.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

TARGET = "/code/rest_service.py"
_MAPPING_ITEM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*:(?:\s|$)")
_SERVICE_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)\s*:(.*)$")
_SERVICE_BLOCK_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*:\s*$")
_UNSUPPORTED_SCALAR_PREFIXES = ("{", "[", "&", "*", "!", "?", "|", ">")
_ROOT_KEYS_AFFECTING_SERVICE_OWNERSHIP = frozenset({"include"})
_SERVICE_KEYS_AFFECTING_MOUNT_OWNERSHIP = frozenset(
    {
        "extends",
        "volumes_from",
        "configs",
        "secrets",
        "tmpfs",
        "devices",
    }
)


class VolumeMount(NamedTuple):
    index: int
    source: str
    target: str
    options: str | None


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _strip_inline_comment(value: str) -> str:
    """Strip a YAML inline comment while respecting simple quoted scalars."""

    quote: str | None = None
    escaped = False
    i = 0
    while i < len(value):
        ch = value[i]

        if quote == '"':
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quote = None
        elif quote == "'":
            if ch == "'":
                if i + 1 < len(value) and value[i + 1] == "'":
                    i += 1
                else:
                    quote = None
        else:
            if ch in ("'", '"'):
                quote = ch
            elif ch == "#" and (i == 0 or value[i - 1].isspace()):
                return value[:i].rstrip()

        i += 1

    if quote is not None:
        raise ValueError("unterminated quoted scalar in Compose volumes section")
    return value.rstrip()


def _unquote_scalar(value: str) -> str:
    value = value.strip()
    if not value:
        return value

    if value[0] in ("'", '"'):
        if len(value) < 2 or value[-1] != value[0]:
            raise ValueError("unsupported partially quoted Compose volume scalar")
        inner = value[1:-1]
        if value[0] == "'":
            return inner.replace("''", "'")
        if "\\" in inner:
            raise ValueError("escaped double-quoted Compose volume scalars are unsupported")
        return inner

    if "'" in value or '"' in value:
        raise ValueError("mixed/unbalanced quoting in Compose volume scalar is unsupported")
    return value


def _service_bounds(lines: list[str], service_name: str) -> tuple[int, int, int]:
    services: list[tuple[int, int]] = []

    # The path from the YAML root to the selected service is intentionally
    # narrow too. Reject semantic/quoted/explicit/merge root keys instead of
    # allowing an alternate effective "services" mapping to coexist with the
    # literal block this helper edits.
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or _indent(line) != 0:
            continue

        normalized = _strip_inline_comment(stripped)
        match = _SERVICE_KEY_RE.fullmatch(normalized)
        if match is None:
            raise ValueError(
                "top-level Compose mapping uses unsupported YAML key syntax"
            )

        key = match.group(1)
        value = match.group(2).strip()
        if key in _ROOT_KEYS_AFFECTING_SERVICE_OWNERSHIP:
            raise ValueError(
                f"top-level Compose key {key!r} can change the effective services model "
                "and is unsupported by the narrow Compose editor"
            )
        if key == "services":
            if value:
                raise ValueError(
                    "services must use a literal block mapping; inline/alias/anchor "
                    "representations are unsupported"
                )
            services.append((idx, 0))

    if len(services) != 1:
        raise ValueError(f"expected exactly one services: section, found {len(services)}")
    services_idx, services_indent = services[0]

    section_end = len(lines)
    for idx in range(services_idx + 1, len(lines)):
        line = lines[idx]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _indent(line) <= services_indent:
            section_end = idx
            break

    expected_service_indent = services_indent + 2
    matches: list[int] = []
    for idx in range(services_idx + 1, section_end):
        line = lines[idx]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _indent(line) != expected_service_indent:
            continue

        normalized = _strip_inline_comment(stripped)
        match = _SERVICE_BLOCK_RE.fullmatch(normalized)
        if match is None:
            raise ValueError(
                "services mapping uses unsupported service key/value syntax; "
                "service definitions must be literal block mappings"
            )
        if match.group(1) == service_name:
            matches.append(idx)

    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one Compose service {service_name!r}, found {len(matches)}"
        )
    service_idx = matches[0]
    service_indent = expected_service_indent

    service_end = section_end
    for idx in range(service_idx + 1, section_end):
        line = lines[idx]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _indent(line) <= service_indent:
            service_end = idx
            break

    return service_idx, service_end, service_indent


def _validate_selected_service_definition(
    lines: list[str], service_name: str
) -> None:
    service_idx, service_end, service_indent = _service_bounds(lines, service_name)
    key_indent = service_indent + 2

    for idx in range(service_idx + 1, service_end):
        line = lines[idx]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _indent(line) != key_indent:
            continue

        normalized = _strip_inline_comment(stripped)
        match = _SERVICE_KEY_RE.fullmatch(normalized)
        if match is None:
            raise ValueError(
                "selected service uses unsupported YAML key/composition syntax; "
                "refusing to infer effective volume ownership"
            )

        key = match.group(1)
        if key in _SERVICE_KEYS_AFFECTING_MOUNT_OWNERSHIP:
            raise ValueError(
                f"selected service key {key!r} can affect effective mount ownership "
                "and is unsupported by the narrow Compose editor"
            )


def _volumes_bounds(
    lines: list[str], service_name: str
) -> tuple[int, int, int] | None:
    _validate_selected_service_definition(lines, service_name)
    service_idx, service_end, service_indent = _service_bounds(lines, service_name)
    key_indent = service_indent + 2

    candidates: list[int] = []
    for idx in range(service_idx + 1, service_end):
        line = lines[idx]
        if _indent(line) != key_indent:
            continue

        stripped = line.strip()
        if not re.match(r"^volumes\s*:", stripped):
            continue

        normalized = _strip_inline_comment(stripped)
        if normalized != "volumes:":
            raise ValueError(
                "inline/flow/aliased service-level volumes are unsupported; "
                "refusing partial Compose rewrite"
            )
        candidates.append(idx)

    if len(candidates) > 1:
        raise ValueError("multiple service-level volumes: keys are unsupported")
    if not candidates:
        return None

    volumes_idx = candidates[0]
    volumes_indent = key_indent
    volumes_end = service_end
    for idx in range(volumes_idx + 1, service_end):
        line = lines[idx]
        if line.strip() and _indent(line) <= volumes_indent:
            volumes_end = idx
            break
    return volumes_idx, volumes_end, volumes_indent


def _validate_canonical_container_target(target: str) -> None:
    """Require a lexical form whose identity already matches runtime identity.

    Compose and Docker canonicalize Linux mount destinations. This editor does
    not normalize source text and then mutate it; it supports only target paths
    that are already canonical so raw-text ownership cannot diverge from the
    effective runtime destination.
    """

    if not target.startswith("/"):
        raise ValueError(
            "non-absolute Compose volume target is unsupported for this Linux deployment boundary"
        )
    if "\\" in target:
        raise ValueError(
            "backslashes in Compose volume targets are unsupported for ownership-safe rewriting"
        )
    if target == "/":
        return

    segments = target.split("/")[1:]
    if any(segment in ("", ".", "..") for segment in segments):
        raise ValueError(
            "non-canonical Compose volume target is unsupported; "
            "remove repeated separators and dot/parent path segments before applying"
        )


def _parse_short_mount_scalar(value: str, index: int) -> VolumeMount | None:
    scalar = _unquote_scalar(value)

    if not scalar:
        raise ValueError("empty Compose volume item is unsupported")
    if scalar.startswith(_UNSUPPORTED_SCALAR_PREFIXES):
        raise ValueError(
            "mapping/flow/anchor/alias/multiline Compose volume syntax is unsupported; "
            "refusing partial rewrite"
        )
    if _MAPPING_ITEM_RE.match(scalar):
        raise ValueError(
            "mapping-style Compose volume syntax is unsupported; refusing partial rewrite"
        )
    if "$" in scalar:
        raise ValueError(
            "interpolated Compose volume syntax is unsupported for ownership-safe rewriting"
        )

    if ":" not in scalar:
        raise ValueError(
            "anonymous/target-only Compose volume entries are unsupported; "
            "effective ownership requires an explicit source and canonical target"
        )

    parts = scalar.rsplit(":", 2)
    source: str
    target: str
    options: str | None

    if len(parts) == 2:
        source, target = parts
        options = None
    else:
        left, middle, right = parts
        if right.startswith("/"):
            source = f"{left}:{middle}"
            target = right
            options = None
        elif middle.startswith("/"):
            source = left
            target = middle
            options = right
        else:
            raise ValueError(
                "ambiguous short-syntax Compose volume target; refusing partial rewrite"
            )

    if not source:
        raise ValueError("empty Compose volume source is unsupported")

    _validate_canonical_container_target(target)

    return VolumeMount(index=index, source=source, target=target, options=options)


def _short_volume_items(lines: list[str], service_name: str) -> list[VolumeMount]:
    bounds = _volumes_bounds(lines, service_name)
    if bounds is None:
        return []

    volumes_idx, volumes_end, volumes_indent = bounds
    item_indent = volumes_indent + 2
    mounts: list[VolumeMount] = []

    for idx in range(volumes_idx + 1, volumes_end):
        line = lines[idx]
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            continue

        indent = _indent(line)
        if indent > item_indent:
            raise ValueError(
                "multiline/mapping Compose volume items are unsupported; "
                "refusing partial rewrite"
            )
        if indent < item_indent:
            raise ValueError(
                "unexpected indentation inside service volumes; refusing partial rewrite"
            )

        if not stripped.startswith("- "):
            raise ValueError(
                "service volumes must use single-line short-syntax sequence items"
            )

        raw_value = _strip_inline_comment(stripped[2:].strip())
        mount = _parse_short_mount_scalar(raw_value, idx)
        if mount is not None:
            mounts.append(mount)

    return mounts


def _target_mounts(lines: list[str], service_name: str) -> list[VolumeMount]:
    exact: list[VolumeMount] = []

    for mount in _short_volume_items(lines, service_name):
        if mount.target == TARGET:
            exact.append(mount)
            continue

        if mount.target == "/" or TARGET.startswith(mount.target + "/"):
            raise ValueError(
                f"Compose volume target {mount.target!r} is an ancestor of {TARGET!r}; "
                "effective file ownership is ambiguous and cannot be rewritten safely"
            )

    if len(exact) > 1:
        raise ValueError("multiple exact rest_service.py mounts found for selected service")
    return exact


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
    _short_volume_items(lines, service_name)
    lines = _remove_target_mount(lines, service_name)
    lines = _remove_empty_service_volumes(lines, service_name)
    return "\n".join(lines) + "\n"


def add_mount(text: str, service_name: str, patch_file: str) -> str:
    if not patch_file.startswith("/"):
        raise ValueError("patch file path must be absolute")
    if any(ch in patch_file for ch in ("\n", "\r", '"', "'", ":", "$", "#")):
        raise ValueError("patch file path contains unsupported characters")

    lines = text.splitlines()
    _service_bounds(lines, service_name)
    _short_volume_items(lines, service_name)
    mounts = _target_mounts(lines, service_name)
    if mounts and mounts[0].source == patch_file and mounts[0].options == "ro":
        return "\n".join(lines) + "\n"
    if mounts:
        lines = _remove_target_mount(lines, service_name)

    _, service_end, service_indent = _service_bounds(lines, service_name)
    bounds = _volumes_bounds(lines, service_name)
    mount_line = f'- "{patch_file}:{TARGET}:ro"'

    if bounds is None:
        key_indent = " " * (service_indent + 2)
        item_indent = " " * (service_indent + 4)
        lines.insert(service_end, key_indent + "volumes:")
        lines.insert(service_end + 1, item_indent + mount_line)
    else:
        _, volumes_end, volumes_indent = bounds
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
