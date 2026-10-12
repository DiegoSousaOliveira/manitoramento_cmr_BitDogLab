"""Mostra o diário da placa (o que ela escreve pela USB) — Windows, só biblioteca padrão.

    python testes/ler_serial.py            (acha a porta COM da placa sozinho)
    python testes/ler_serial.py COM5       (ou diga qual é)
    python testes/ler_serial.py --segundos 30   (para depois de 30 s)
"""

from __future__ import annotations

import argparse
import ctypes
import msvcrt
import sys
import time
import winreg

SETDTR = 5  # EscapeCommFunction: "terminal pronto" — sem isso a Pico acha que ninguém está ouvindo


def portas_usb() -> list[str]:
    """Portas COM de dispositivos USB-serial (a placa aparece como USBSER)."""
    portas = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as chave:
            i = 0
            while True:
                try:
                    nome, valor, _ = winreg.EnumValue(chave, i)
                except OSError:
                    break
                if "USBSER" in nome.upper():
                    portas.append(str(valor))
                i += 1
    except OSError:
        pass
    return portas


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("porta", nargs="?", help="ex.: COM5 (padrão: a primeira porta USB-serial)")
    parser.add_argument("--segundos", type=float, default=0, help="parar depois de N segundos (0 = nunca)")
    args = parser.parse_args()
    porta = args.porta or next(iter(portas_usb()), None)
    if porta is None:
        print("Nenhuma porta USB-serial encontrada. A placa está ligada na USB (e NÃO em modo BOOTSEL)?")
        return 1
    print(f"Lendo {porta} (Ctrl+C para sair)...", flush=True)
    with open(rf"\\.\{porta}", "rb", buffering=0) as arquivo:
        ctypes.windll.kernel32.EscapeCommFunction(msvcrt.get_osfhandle(arquivo.fileno()), SETDTR)
        fim = time.monotonic() + args.segundos if args.segundos else None
        pendente = b""
        while fim is None or time.monotonic() < fim:
            pedaco = arquivo.read(1)
            if not pedaco:
                continue
            pendente += pedaco
            if pedaco == b"\n":
                texto = pendente.decode("ascii", "replace").rstrip()
                print(f"{time.strftime('%H:%M:%S')} {texto}", flush=True)
                pendente = b""
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(0)
