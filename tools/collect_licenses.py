"""Keep dependency licensing notices beside a redistributable Windows build."""
import argparse
import importlib.metadata
import shutil
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("destination", type=Path)
parser.add_argument("--agent", action="store_true", help="Collect MCP SDK and its installed runtime dependencies")
args = parser.parse_args()
args.destination.mkdir(parents=True, exist_ok=True)
notices = Path(__file__).resolve().parents[1] / "licenses"
names = ["PySide6_Essentials", "shiboken6", "numpy", "pyserial"]
# The frozen import graph includes setuptools/packaging. Also preserve the
# bundler's bootloader notice and distribution exception beside its executable.
names += ["setuptools", "packaging", "PyInstaller"]
def append_runtime_dependencies():
    """Follow installed runtime requirements, respecting platform/extra markers."""
    from packaging.requirements import Requirement
    pending, visited = [('jsonschema', frozenset())], set()
    if args.agent:
        pending.append(('mcp', frozenset()))
    while pending:
        name, extras = pending.pop()
        key = (name.lower().replace('_','-'), extras)
        if key in visited: continue
        visited.add(key)
        dist = importlib.metadata.distribution(name)
        if dist.metadata['Name'] not in names:
            names.append(dist.metadata['Name'])
        for value in dist.requires or []:
            req = Requirement(value)
            if req.marker and not any(req.marker.evaluate({'extra': extra}) for extra in (extras or {''})):
                continue
            pending.append((req.name, frozenset(req.extras)))
append_runtime_dependencies()
for name in names:
    dist = importlib.metadata.distribution(name)
    folder = args.destination / name
    folder.mkdir(exist_ok=True)
    (folder / "METADATA.txt").write_text(dist.read_text("METADATA") or "", encoding="utf-8")
    copied = 0
    for item in dist.files or []:
        if any(word in str(item).lower() for word in ("license", "copying", "copyright", "notice")):
            source = Path(dist.locate_file(item))
            if source.is_file():
                target = folder.joinpath(*[part for part in item.parts if part not in ("..", ".")])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                copied += 1
    if not copied and name == "pyserial":
        shutil.copy2(notices / "pyserial-LICENSE.txt", folder / "LICENSE.txt")
        copied = 1
    if not copied:
        raise SystemExit(f"No license notice found for {name}; check upstream distribution before release.")
    print(f"{name}: collected {copied} license notices")
shutil.copytree(notices / "qt", args.destination / "Qt-licenses", dirs_exist_ok=True)
font_notices = Path(__file__).resolve().parents[1] / "pagewriter/resources/fonts"
licenses = list(font_notices.glob('*-LICENSE.txt'))
if len(licenses) != 9:
    raise SystemExit('The nine bundled font license notices are required.')
(args.destination / 'Fonts').mkdir(exist_ok=True)
for path in licenses:
    shutil.copy2(path, args.destination / 'Fonts' / path.name)
shutil.copy2(font_notices / 'ARPHICPL.TXT', args.destination / 'Fonts' / 'ARPHICPL.TXT')
print('Fonts: collected nine source/derivative license notices')
