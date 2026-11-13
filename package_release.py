"""Package the portable Windows folder, retaining folder-icon metadata."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path


def main() -> None:
    source = Path(sys.argv[1]).resolve()
    target = Path(sys.argv[2]).resolve()
    if source.name != "iBatcher" or not (source / "iBatcher.exe").is_file():
        raise SystemExit("Expected a staged iBatcher folder with iBatcher.exe")
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in [source, *sorted(source.rglob("*"))]:
            relative = path.relative_to(source.parent).as_posix()
            directory = path.is_dir()
            info = zipfile.ZipInfo(relative + ("/" if directory else ""))
            info.create_system = 0  # Windows/DOS attributes
            attributes = 0x10 if directory else 0x20
            if path == source:
                attributes |= 0x01  # Explorer reads desktop.ini in customized folders
            if path.name == "desktop.ini":
                attributes = 0x06  # hidden + system
            if path.name == "iBatcher.ico":
                attributes = 0x02  # hidden
            info.external_attr = attributes << 16
            if not directory:
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
            else:
                archive.writestr(info, b"")
    print(target)


if __name__ == "__main__":
    main()
