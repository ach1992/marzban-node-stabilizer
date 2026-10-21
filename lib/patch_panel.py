#!/usr/bin/env python3
"""Transform reviewed Panel code shapes; runtime payload is AGPL (panel/LICENSE.Marzban)."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path

SPEC_PATH = Path(__file__).with_name("panel") / "patch-spec.json"


def load_spec():
    return json.loads(SPEC_PATH.read_text())


def structural_hash(nodes):
    tree = ast.Module(body=nodes, type_ignores=[])
    return hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()


def source_span(node):
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]) - 1
    return start, node.end_lineno


def locate_rule(nodes, rule):
    if rule["kind"] == "tail":
        selected = [(i, n) for i, n in enumerate(nodes)
                    if isinstance(n, ast.Global) and "_connecting_nodes" in n.names]
        if len(selected) != 1:
            raise ValueError("Expected one reviewed node-operation tail")
        index, node = selected[0]
        return nodes[index:], source_span(node)[0], nodes[-1].end_lineno
    if rule["kind"] == "main":
        selected = [n for n in nodes if isinstance(n, ast.If) and
                    isinstance(n.test, ast.Compare) and
                    isinstance(n.test.left, ast.Name) and n.test.left.id == "__name__"]
    else:
        selected = [n for n in nodes if isinstance(n, (ast.ClassDef, ast.FunctionDef))
                    and n.name == rule["name"]]
    if len(selected) != 1:
        raise ValueError("Expected one unambiguous reviewed block: " + str(rule.get("name", rule["kind"])))
    node = selected[0]
    a, b = source_span(node)
    return [node], a, b


def patch_source(source, relative_path, spec=None):
    spec = spec or load_spec()
    if spec["marker"] in source:
        raise ValueError("Input is already patched; obtain clean source from the selected image")
    entry = spec["files"].get(relative_path)
    if entry is None:
        raise ValueError("Unknown Panel runtime path: " + relative_path)
    tree = ast.parse(source)
    edits = []
    for rule in entry["rules"]:
        nodes, a, b = locate_rule(tree.body, rule)
        actual = structural_hash(nodes)
        if actual not in rule["before"]:
            raise ValueError(f"Unreviewed Panel source shape: {relative_path} {rule.get('name', rule['kind'])} sha256={actual}")
        edits.append((a, b, rule["replacement"].rstrip() + "\n"))
    # Add required imports/state only if absent; preserve unrelated source bytes.
    existing = {structural_hash([n]) for n in tree.body}
    additions = []
    for addition in entry["imports"]:
        node = ast.parse(addition).body
        if structural_hash(node) not in existing:
            if any(isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_core_config_lock"
                   for t in n.targets) for n in node) and any(
                   isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_core_config_lock"
                   for t in n.targets) for n in tree.body):
                raise ValueError("Foreign _core_config_lock already exists")
            additions.append(addition.rstrip() + "\n")
    import_end = max((n.end_lineno for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))), default=0)
    if additions:
        edits.append((import_end, import_end, "\n" + "".join(additions) + "\n"))
    lines = source.splitlines(keepends=True)
    for a, b, replacement in sorted(edits, reverse=True):
        lines[a:b] = [replacement]
    result = spec["marker"] + "\n" + "".join(lines)
    compile(result, relative_path, "exec")
    return result


def build_tree(source_dir, output_dir):
    spec = load_spec()
    generated = {path: patch_source((source_dir / path).read_text(), path, spec)
                 for path in spec["files"]}
    # Validate every source before publishing any output file.
    for path, source in generated.items():
        dest = output_dir / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(source)
    return {path: hashlib.sha256(text.encode()).hexdigest() for path, text in generated.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_tree(args.source, args.output)))


if __name__ == "__main__":
    main()
