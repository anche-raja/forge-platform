"""Launch the local UI. Loopback only — there is no ``--host`` by design."""

import socket
import threading
import webbrowser

HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def probe_port(port: int, *, strict: bool, host: str = HOST, tries: int = 50) -> int:
    """The first free port from ``port`` upward; with ``strict`` only ``port`` itself will do."""
    for candidate in range(port, port + (1 if strict else tries)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, candidate))
            except OSError:
                continue
            return candidate
    raise OSError(f"port {port} is in use" if strict else f"no free port in {port}–{port + tries - 1}")


def serve(port: int = DEFAULT_PORT, open_browser: bool = True, *, strict_port: bool = False) -> int:
    import uvicorn

    from forge.ui.app import create_app

    chosen = probe_port(port, strict=strict_port)
    url = f"http://{HOST}:{chosen}/"
    print(f"FORGE UI: {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    server = uvicorn.Server(uvicorn.Config(create_app(), host=HOST, port=chosen, log_level="warning"))
    server.run()
    return 0
