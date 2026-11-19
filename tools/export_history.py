"""Выгрузка истории изменений и данных циклов разработки (прил. 10, п. 2).

    python tools/export_history.py --out dist/history

Состав выгрузки:
  history.csv        — все коммиты: хеш, даты автора и фиксации, автор, ветки, изменённые файлы, строки +/−;
  history.txt        — то же подробно (git log --stat);
  agreemodel.bundle  — полная копия репозитория с историей (проверка: git clone agreemodel.bundle);
  journal/           — журнал циклов: исходные данные, показатели, временные ряды;
  scenarios/         — исходные данные моделирования;
  README.txt, SHA256SUMS.
Проверяется, что все коммиты сделаны в период приёма заявок (не ранее 04.11.2026 12:00 МСК).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PERIOD_START = datetime.fromisoformat("2026-11-04T12:00:00+03:00")
SEP = "\x1f"
MSK = timezone(timedelta(hours=3), "МСК")


def msk(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(MSK).strftime("%d.%m.%Y %H:%M:%S")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "dist" / "history"))
    ap.add_argument("--no-bundle", action="store_true", help="без полной копии репозитория")
    args = ap.parse_args(argv)
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    shallow = git("rev-parse", "--is-shallow-repository").strip() == "true"
    refs = [r for r in git("for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes", "refs/tags").split()
            if not r.endswith("/HEAD")]
    fmt = SEP.join(["%H", "%h", "%aI", "%cI", "%an", "%s"])
    rows, early = [], []
    for line in git("log", *refs, "--date-order", f"--format={fmt}").splitlines():
        full, short, adate, cdate, author, subject = line.split(SEP)
        stat = git("show", "--numstat", "--format=", full).splitlines()
        plus = sum(int(s.split("\t")[0]) for s in stat if s.split("\t")[0].isdigit())
        minus = sum(int(s.split("\t")[1]) for s in stat if s.split("\t")[1:2] and s.split("\t")[1].isdigit())
        branches = git("for-each-ref", "--contains", full, "--format=%(refname:short)", *refs).split()
        rows.append([short, full, msk(adate), msk(cdate), author, " ".join(sorted(set(branches))), len(stat), plus, minus, subject])
        if min(datetime.fromisoformat(adate), datetime.fromisoformat(cdate)) < PERIOD_START:
            early.append(short)
    with open(out / "history.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["коммит", "хеш полностью", "дата автора, МСК", "дата фиксации, МСК", "автор", "ветки",
                    "файлов", "строк добавлено", "строк удалено", "описание"])
        w.writerows(rows)
    (out / "history.txt").write_text(git("log", *refs, "--date-order", "--stat", "--date=iso"), encoding="utf-8")

    if not args.no_bundle:
        if shallow:
            print("Внимание: репозиторий неполный (shallow) — копия репозитория не создана; выполните git fetch --unshallow")
        else:
            subprocess.check_call(["git", "bundle", "create", str(out / "agreemodel.bundle"), *refs], cwd=ROOT,
                                  stderr=subprocess.DEVNULL)
            subprocess.check_call(["git", "bundle", "verify", str(out / "agreemodel.bundle")], cwd=ROOT,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for d in ("journal", "scenarios"):
        if (ROOT / d).is_dir():
            shutil.copytree(ROOT / d, out / d)

    head = git("rev-parse", "--short", "HEAD").strip()
    first, last = (rows[-1][2] + " МСК", rows[0][2] + " МСК") if rows else ("—", "—")
    (out / "README.txt").write_text("\r\n".join([
        "Симулятор «Согласие» — история изменений и данные циклов разработки (прил. 10, п. 2)",
        "",
        f"Выгрузка из репозитория evoCoder/agreemodel, состояние {head}, {datetime.now().astimezone():%d.%m.%Y %H:%M}.",
        f"Коммитов: {len(rows)}; первый — {first}, последний — {last}.",
        f"Ветки и метки: {', '.join(r.split('/', 2)[-1] for r in refs)}.",
        "Начало периода приёма заявок: 04.11.2026 12:00 МСК. " + (
            "Все коммиты сделаны в этом периоде." if not early else f"РАНЬШЕ ПЕРИОДА: {', '.join(early)}"),
        "",
        "history.csv        — перечень коммитов (открывается в Excel; разделитель «;»)",
        "history.txt        — подробная история с изменёнными файлами",
        "agreemodel.bundle  — полная копия репозитория: git clone agreemodel.bundle agreemodel",
        "journal/           — журнал циклов: README.md, по каждому циклу scenario.json, summary.json, series.csv",
        "scenarios/         — исходные данные моделирования (сценарии .toml)",
        "SHA256SUMS         — контрольные суммы файлов",
        "",
    ]), encoding="utf-8-sig")
    files = sorted(p for p in out.rglob("*") if p.is_file())
    (out / "SHA256SUMS").write_text("".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(out).as_posix()}\n" for p in files),
        encoding="utf-8")
    print(f"Готово: {out}; коммитов {len(rows)}" + (f"; раньше периода: {early}" if early else "; все в периоде"))
    return 1 if early else 0


if __name__ == "__main__":
    sys.exit(main())
