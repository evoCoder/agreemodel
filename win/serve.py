"""Локальный сервер для страницы симулятора «Согласие» (переносимая версия).

Запуск: start.bat (Windows) или `python serve.py` (любая ОС с Python 3.8+).
Страница открывается в браузере по умолчанию по адресу http://127.0.0.1:<порт>/.
Сервер слушает только этот компьютер, в интернет не обращается; расчёт идёт в браузере.

Параметры: --port N (по умолчанию первый свободный от 8765), --no-browser, --root КАТАЛОГ.
"""

from __future__ import annotations

import argparse
import errno
import http.server
import socket
import sys
import threading
import webbrowser
from functools import partial
from pathlib import Path

# Типы задаются явно: на Windows модуль mimetypes читает реестр, где .js бывает text/plain,
# и тогда браузер отказывается загружать модули страницы.
TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".wasm": "application/wasm",
    ".zip": "application/zip",
    ".whl": "application/zip",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".pdf": "application/pdf",
}


class Handler(http.server.SimpleHTTPRequestHandler):
    def guess_type(self, path):  # noqa: D401
        return TYPES.get(Path(str(path)).suffix.lower(), "application/octet-stream")

    def end_headers(self):
        # no-store: после обновления архива браузер не покажет старую страницу из кэша
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, fmt, *args):  # без журнала каждого запроса
        if args and str(args[1])[:1] in "45":
            sys.stderr.write("  ошибка запроса: %s\n" % (fmt % args))


class Server(http.server.ThreadingHTTPServer):
    # На Windows SO_REUSEADDR позволяет второму процессу занять тот же порт — запрещаем,
    # чтобы два запущенных экземпляра не делили один адрес.
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def bind(root: Path, port: int | None) -> http.server.ThreadingHTTPServer:
    handler = partial(Handler, directory=str(root))
    ports = [port] if port else list(range(8765, 8786)) + [0]
    last = None
    for p in ports:
        try:
            return Server(("127.0.0.1", p), handler)
        except OSError as e:  # порт занят — следующий
            last = e
            if e.errno not in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1), 10013, 10048):
                raise
    raise SystemExit(f"Не удалось открыть порт: {last}")


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--root", default=str(here / "web"))
    args = ap.parse_args(argv)
    root = Path(args.root)
    if not (root / "index.html").is_file():
        print(f"Не найдена страница: {root / 'index.html'}. Распакуйте архив целиком.")
        return 1
    srv = bind(root, args.port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print("Симулятор «Согласие» — переносимая версия")
    print(f"  Страница: {url}")
    print("  Если браузер не открылся сам — скопируйте адрес в Microsoft Edge, Chrome или Firefox.")
    print("  Не закрывайте это окно, пока работаете со страницей. Завершение — Ctrl+C или закрыть окно.")
    sys.stdout.flush()
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    print("Сервер остановлен.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
