"""Audit Git's publication set or a built wheel/sdist, never local user files."""
import argparse
import ast
import json
import re
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

PRIVATE_PREFIXES = ("pics/", "evidence/", "samples/", "assets/", ".maliang/")
PRIVATE_NAME = re.compile(r"^(?:acceptance.*\.json|migration-.*\.json)$")
PRIVATE_DOC = re.compile(r"^docs/(?:local-|kenjoy-|photo-acceptance-|handoff-20261025)")
HOME_PATH = re.compile(rb"[A-Za-z]:[\\/]+Users[\\/]+(?!Public(?:[\\/]|$))[^\\/\s\"']+", re.I)


def material_dependencies(data):
    """Reject literal machine paths and access to private research asset trees."""
    problems = set()
    for node in ast.walk(ast.parse(data)):
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            is_path = (isinstance(func, ast.Name) and func.id == 'Path') or (
                isinstance(func, ast.Attribute) and func.attr in ('Path', 'joinpath'))
            if is_path:
                for value in node.args:
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        path = value.value.replace('\\', '/')
                        if (path.startswith('/') or re.match(r'^[a-zA-Z]:/', path)
                                or 'gcodefonts' in path.casefold()
                                or any(part in {'pics', 'evidence'} for part in path.split('/'))):
                            problems.add('hardcoded external material path')
        if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)
                and any(part in {'pics', 'evidence'} for part in node.right.value.replace('\\', '/').split('/'))):
            problems.add('private material directory dependency')
    return sorted(problems)


def check(name, data):
    name = name.replace("\\", "/")
    errors = []
    if name.startswith(PRIVATE_PREFIXES) or name.startswith("pip-") or PRIVATE_NAME.match(name) or PRIVATE_DOC.match(name):
        errors.append("local-only material")
    if name.endswith((".gfont", ".lnk", ".pem", ".key", ".pfx")) or PurePosixPath(name).name.startswith(".env"):
        errors.append("font archive, shortcut or credential file")
    if name.endswith((".py", ".md", ".json", ".toml", ".pagewriter", ".yml", ".cmd", ".spec")):
        if HOME_PATH.search(data):
            errors.append("private Windows home path")
    if name.endswith('.py') and name.startswith(('pagewriter/', 'tests/', 'tools/')):
        errors.extend(material_dependencies(data))
    if name.endswith(".pagewriter"):
        project = json.loads(data.decode("utf-8-sig"))["project"]
        for block in project.get("blocks", []):
            if any(not family.startswith("Maliang Synthetic") for family in block.get("stroke_fonts", {})):
                errors.append("non-synthetic single-line font cache")
    return [f"{name}: {message}" for message in errors]


def entries(archive):
    if archive:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as handle:
                for name in handle.namelist():
                    if not name.endswith("/"):
                        yield name, handle.read(name)
        else:
            with tarfile.open(archive) as handle:
                for member in handle.getmembers():
                    if member.isfile():
                        # Source archives have a versioned top-level directory.
                        yield member.name.split("/", 1)[-1], handle.extractfile(member).read()
    else:
        # Audit tracked and unignored new files, including changes not staged
        # yet. An intentional working-tree deletion has no bytes to publish.
        names = subprocess.check_output(["git", "ls-files", "--cached", "--others",
                                         "--exclude-standard", "-z"]).decode().split("\0")
        for name in sorted(set(filter(None, names))):
            path = Path(name)
            if path.is_file():
                yield name, path.read_bytes()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", nargs="*", type=Path)
    args = parser.parse_args()
    errors, count = [], 0
    for archive in args.archives or [None]:
        for name, data in entries(archive):
            count += 1
            errors.extend(check(name, data))
    if count == 0:
        raise SystemExit("No files audited; stage files with git add first.")
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"Publication audit passed: {count} files; no private assets, font caches or home paths.")


if __name__ == "__main__":
    main()
