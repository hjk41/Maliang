"""Keep dependency licensing notices beside a redistributable Windows build."""
import argparse
import importlib.metadata
import shutil
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("destination", type=Path)
args = parser.parse_args()
args.destination.mkdir(parents=True, exist_ok=True)
for name in ("PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6", "numpy", "pyserial"):
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
    if not copied:
        raise SystemExit(f"No license notice found for {name}; check upstream distribution before release.")
    print(f"{name}: collected {copied} license notices")
