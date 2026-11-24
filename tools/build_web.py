"""Сборка веб-версии симулятора в каталог dist/web.

    python tools/build_web.py            # dist/web
    python tools/build_web.py --out DIR

Состав: страница (web/*.html, *.js, *.css), среда Pyodide и numpy (web/vendor.json —
скачиваются один раз в кэш, проверяются по SHA-256), пакет модели из src/agreemodel и
связка web/py/soglasie_web.py в py/app.zip, базовый сценарий, version.json (версия,
коммит, дата сборки). Страница открывается любым статическим веб-сервером, например
`python -m http.server -d dist/web`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
CACHE = Path(os.environ.get("SOGLASIE_CACHE", Path.home() / ".cache" / "soglasie-vendor"))
FIXED_TIME = (2026, 1, 1, 0, 0, 0)  # одинаковые метки в архиве — воспроизводимая сборка


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_vendor(dst: Path) -> dict:
    spec = json.loads((WEB / "vendor.json").read_text(encoding="utf-8"))
    cache = CACHE / spec["pyodide"]
    cache.mkdir(parents=True, exist_ok=True)
    dst.mkdir(parents=True, exist_ok=True)
    for name, digest in spec["files"].items():
        c = cache / name
        if not c.exists() or sha256(c) != digest:
            print(f"  скачивание {name}", flush=True)
            tmp = c.with_suffix(c.suffix + ".part")
            urllib.request.urlretrieve(spec["base_url"] + name, tmp)
            if sha256(tmp) != digest:
                tmp.unlink()
                raise SystemExit(f"контрольная сумма не совпала: {name}")
            tmp.replace(c)
        shutil.copy2(c, dst / name)
    return spec


def git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def app_zip(dst: Path) -> None:
    items: list[tuple[Path, str]] = []
    for p in sorted((ROOT / "src" / "agreemodel").rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
            items.append((p, "agreemodel/" + p.relative_to(ROOT / "src" / "agreemodel").as_posix()))
    items.append((WEB / "py" / "soglasie_web.py", "soglasie_web.py"))
    for p in sorted((ROOT / "scenarios").glob("*.toml")):
        items.append((p, "scenarios/" + p.name))
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for src, arc in items:
            info = zipfile.ZipInfo(arc, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, src.read_bytes())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "dist" / "web"))
    args = ap.parse_args(argv)
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    print("Среда исполнения (Pyodide, numpy):")
    spec = fetch_vendor(out / "pyodide")
    for name in ("index.html", "app.js", "worker.js", "presets.js", "terms.js", "style.css"):
        shutil.copy2(WEB / name, out / name)
    (out / "py").mkdir()
    app_zip(out / "py" / "app.zip")

    init = (ROOT / "src" / "agreemodel" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init)
    version = {
        "version": m.group(1) if m else "?",
        "commit": git("rev-parse", "--short", "HEAD") or "?",
        "commit_date": git("log", "-1", "--format=%cd", "--date=iso-strict"),
        "dirty": bool(git("status", "--porcelain", "--", "src", "web", "scenarios")),
        "built": datetime.now(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M МСК"),
        "pyodide": spec["pyodide"],
    }
    (out / "version.json").write_text(json.dumps(version, ensure_ascii=False, indent=2), encoding="utf-8")

    files = sorted(p for p in out.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    (out / "SHA256SUMS").write_text(
        "".join(f"{sha256(p)}  {p.relative_to(out).as_posix()}\n" for p in files), encoding="utf-8")
    size = sum(p.stat().st_size for p in files) / 2**20
    print(f"Готово: {out} ({len(files)} файлов, {size:.1f} МБ), версия {version['version']}, "
          f"коммит {version['commit']}{' (с изменениями)' if version['dirty'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
