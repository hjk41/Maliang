"""Keep dependency licensing notices beside a redistributable Windows build."""
import argparse
import importlib.metadata
import shutil
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("destination", type=Path)
args = parser.parse_args()
args.destination.mkdir(parents=True, exist_ok=True)
notices = Path(__file__).resolve().parents[1] / "licenses"
for name in ("PySide6_Essentials", "shiboken6", "numpy", "pyserial"):
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
