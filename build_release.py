"""Build the Windows release from the current source directory."""

import ast
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def main():
    root = Path(__file__).resolve().parent
    source = ast.parse((root / "app.py").read_text(encoding="utf-8"))
    version = next(
        ast.literal_eval(node.value)
        for node in source.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)
    )
    if importlib.util.find_spec("PyInstaller") is None:
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"], check=True)

    dist = root / "dist"
    dist.mkdir(exist_ok=True)
    filename = f"iBatcher-{version}-win64.zip"
    with TemporaryDirectory(dir=dist) as temp:
        stage = Path(temp)
        subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--distpath", str(stage),
             "--workpath", str(stage / "build"), str(root / "iBatcher.spec")],
            cwd=root,
            check=True,
        )
        app = stage / "iBatcher"
        for name in ("iBatcher.ico", "desktop.ini"):
            shutil.copy2(root / name, app / name)
        archive = stage / filename
        subprocess.run(
            [sys.executable, str(root / "package_release.py"), str(app), str(archive)],
            check=True,
        )
        archive.replace(dist / filename)
    print(f"Release ready: {dist / filename}")


if __name__ == "__main__":
    main()
