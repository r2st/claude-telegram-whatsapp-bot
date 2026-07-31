"""Standalone QR code generator for terminal display.

Uses the optional ``qrcode`` dependency. When it isn't installed we just
print the URL with an install hint — we used to ship a hand-rolled Reed–Solomon
encoder here, but that's ~200 lines of crypto-adjacent code maintained for a
single nice-to-have feature, so it was removed.
"""
from __future__ import annotations

import socket


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def print_web_qr(port: str, host: str | None = None) -> None:
    """Print a scannable QR code for the web chat.

    ``host`` is the address the code should advertise; it defaults to this
    machine's LAN IP. Only call this when the server is actually reachable
    there. The QR used to encode the LAN address unconditionally even though
    the web chat binds to ``127.0.0.1`` by default, so scanning it from a phone
    timed out — an invitation the configuration could not honour, which reads
    as a broken product rather than a disabled feature.
    """
    ip = host or _get_local_ip()
    url = f"http://{ip}:{port}"

    try:
        import qrcode  # type: ignore
    except ImportError:
        print(f"\n  Open on your phone: {url}")
        print("  (Install 'qrcode' for a scannable QR: pip install qrcode)")
        return

    qr = qrcode.QRCode(
        box_size=1, border=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
    )
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()

    print("\n  ── Scan to open on your phone ──\n")
    _render_qr_terminal(matrix)
    print(f"\n  {url}")


def _render_qr_terminal(matrix: list[list[bool]]) -> None:
    rows = len(matrix)
    for y in range(0, rows, 2):
        line = "  "
        for x in range(len(matrix[0])):
            top = matrix[y][x]
            bot = matrix[y + 1][x] if y + 1 < rows else False
            if top and bot:
                line += "█"
            elif top and not bot:
                line += "▀"
            elif not top and bot:
                line += "▄"
            else:
                line += " "
        print(line)
