"""Сборка переносимой версии для Windows: dist/soglasie-v<версия>-<коммит>-win64.zip.

    python tools/build_win.py               # страница + Python для Windows + выгрузка истории
    python tools/build_win.py --no-history

Состав архива: start.bat, serve.py, README.txt, python/ (встраиваемый Python для Windows,
win/vendor.json, проверка SHA-256), web/ (tools/build_web.py), data/ (tools/export_history.py),
SHA256SUMS. Рядом с архивом — файл .sha256.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import build_web  # noqa: E402
import export_history  # noqa: E402

FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def python_embed(dst: Path) -> str:
    spec = json.loads((ROOT / "win" / "vendor.json").read_text(encoding="utf-8"))
    cache = build_web.CACHE / "win"
    cache.mkdir(parents=True, exist_ok=True)
    z = cache / Path(spec["url"]).name
    if not z.exists() or build_web.sha256(z) != spec["sha256"]:
        print(f"  скачивание {z.name}", flush=True)
        urllib.request.urlretrieve(spec["url"], z)
        if build_web.sha256(z) != spec["sha256"]:
            z.unlink()
            raise SystemExit("контрольная сумма Python для Windows не совпала")
    with zipfile.ZipFile(z) as f:
        f.extractall(dst)
    return spec["python"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-history", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "dist"))
    args = ap.parse_args(argv)
    out = Path(args.out)
    stage_root = out / "win-stage"
    if stage_root.exists():
        shutil.rmtree(stage_root)

    build_web.main(["--out", str(stage_root / "_web")])
    ver = json.loads((stage_root / "_web" / "version.json").read_text(encoding="utf-8"))
    name = f"soglasie-v{ver['version']}-{ver['commit']}{'-dirty' if ver['dirty'] else ''}-win64"
    stage = stage_root / name
    stage.mkdir(parents=True)
    (stage_root / "_web").rename(stage / "web")

    print("Python для Windows:")
    pyver = python_embed(stage / "python")
    shutil.copy2(ROOT / "win" / "serve.py", stage / "serve.py")
    (stage / "start.bat").write_bytes((ROOT / "win" / "start.bat").read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    readme = (ROOT / "win" / "README.txt").read_text(encoding="utf-8").format(
        version=ver["version"], commit=ver["commit"] + (" (с изменениями)" if ver["dirty"] else ""),
        built=ver["built"], python=pyver)
    (stage / "README.txt").write_bytes("﻿".encode() + readme.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))
    if not args.no_history:
        print("История изменений и журнал циклов:")
        export_history.main(["--out", str(stage / "data")])

    files = sorted(p for p in stage.rglob("*") if p.is_file())
    (stage / "SHA256SUMS").write_text("".join(
        f"{build_web.sha256(p)}  {p.relative_to(stage).as_posix()}\n" for p in files), encoding="utf-8")
    files.append(stage / "SHA256SUMS")

    zpath = out / f"{name}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in files:
            info = zipfile.ZipInfo(f"{name}/{p.relative_to(stage).as_posix()}", FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, p.read_bytes())
    digest = build_web.sha256(zpath)
    (out / f"{name}.zip.sha256").write_text(f"{digest}  {zpath.name}\n", encoding="utf-8")
    print(f"Готово: {zpath} ({zpath.stat().st_size / 2**20:.1f} МБ)\nSHA-256: {digest}")
    if os.environ.get("KEEP_STAGE") != "1":
        shutil.rmtree(stage_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
