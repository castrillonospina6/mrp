"""Prueba a qué puertos TCP de un equipo se puede conectar desde este PC.

    python probar_puertos.py 192.168.170.108                 # los 65 535 puertos
    python probar_puertos.py 192.168.170.108 1-10510         # un rango
    python probar_puertos.py 192.168.170.108 80,443,8508     # puertos sueltos (se pueden mezclar con rangos)
    python probar_puertos.py 192.168.170.108 --todos         # además lista cada puerto cerrado o bloqueado
    python probar_puertos.py 192.168.170.108 --espera 1.5 --hilos 300 --csv resultado.csv

Cada puerto queda en uno de tres estados:
  ABIERTO    se conectó: hay un programa escuchando y nada lo bloquea.
  CERRADO    el equipo respondió «no hay nada aquí»: el camino está libre, pero ningún programa usa ese puerto.
  BLOQUEADO  no hubo respuesta en el tiempo de espera: un firewall descarta la conexión (o el equipo está apagado).
"""
import argparse
import csv
import errno
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

REFUSED = {errno.ECONNREFUSED, getattr(errno, "WSAECONNREFUSED", 10061), 10061}


def parse_ports(text: str) -> list[int]:
    ports: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
            ports.update(range(min(a, b), max(a, b) + 1))
        else:
            ports.add(int(part))
    bad = [p for p in ports if not 1 <= p <= 65535]
    if bad:
        raise SystemExit(f"Puertos fuera de rango (1–65535): {bad[:5]}")
    return sorted(ports)


def probe(host: str, port: int, wait: float) -> tuple[int, str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(wait)
        try:
            code = s.connect_ex((host, port))
        except (socket.timeout, TimeoutError):
            return port, "BLOQUEADO"
        except OSError:
            return port, "BLOQUEADO"
    if code == 0:
        return port, "ABIERTO"
    if code in REFUSED:
        return port, "CERRADO"
    return port, "BLOQUEADO"


def service(port: int) -> str:
    try:
        return socket.getservbyport(port, "tcp")
    except OSError:
        return ""


def main() -> None:
    ap = argparse.ArgumentParser(description="Prueba los puertos TCP de un equipo.")
    ap.add_argument("ip", help="IP o nombre del equipo, p. ej. 192.168.170.108")
    ap.add_argument("puertos", nargs="?", default="1-65535", help="rango o lista: 1-65535 (por defecto), 80,443,8000-9000")
    ap.add_argument("--espera", type=float, default=1.0, help="segundos a esperar por puerto (por defecto 1)")
    ap.add_argument("--hilos", type=int, default=400, help="puertos probados a la vez (por defecto 400)")
    ap.add_argument("--todos", action="store_true", help="mostrar también cada puerto cerrado o bloqueado")
    ap.add_argument("--csv", help="guardar el resultado de todos los puertos en este archivo")
    args = ap.parse_args()

    try:
        host = socket.gethostbyname(args.ip)
    except OSError:
        raise SystemExit(f"No se encuentra el equipo «{args.ip}».")
    ports = parse_ports(args.puertos)
    print(f"Probando {len(ports)} puertos TCP de {host} (espera {args.espera}s, {args.hilos} a la vez)...", flush=True)

    results: dict[int, str] = {}
    start = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=args.hilos) as pool:
        futures = [pool.submit(probe, host, p, args.espera) for p in ports]
        try:
            for f in as_completed(futures):
                port, state = f.result()
                results[port] = state
                done += 1
                if state == "ABIERTO":
                    name = service(port)
                    print(f"\rPuerto {port:5d} : ABIERTO {('(' + name + ')') if name else ''}".ljust(60), flush=True)
                elif args.todos:
                    print(f"\rPuerto {port:5d} : {state}".ljust(60), flush=True)
                if done % 500 == 0 or done == len(ports):
                    print(f"\r  avance: {done}/{len(ports)} ({done * 100 // len(ports)}%)", end="", flush=True, file=sys.stderr)
        except KeyboardInterrupt:
            print("\nDetenido. Resultado parcial:")
            for f in futures:
                f.cancel()
    print(file=sys.stderr)

    by_state = {s: sorted(p for p, st in results.items() if st == s) for s in ("ABIERTO", "CERRADO", "BLOQUEADO")}
    print(f"\nResultado para {host} en {time.time() - start:.0f} s ({len(results)} puertos probados):")
    print(f"  ABIERTOS  : {len(by_state['ABIERTO']):5d}  {', '.join(map(str, by_state['ABIERTO'])) or '-'}")
    print(f"  CERRADOS  : {len(by_state['CERRADO']):5d}  (el equipo responde, pero nada escucha ahí)")
    print(f"  BLOQUEADOS: {len(by_state['BLOQUEADO']):5d}  (sin respuesta: firewall o equipo apagado)")
    if by_state["BLOQUEADO"] and not by_state["ABIERTO"] and not by_state["CERRADO"]:
        print("  Ningún puerto respondió: revisa que el equipo esté encendido y en la misma red.")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["puerto", "estado", "servicio"])
            for p in sorted(results):
                w.writerow([p, results[p], service(p) if results[p] == "ABIERTO" else ""])
        print(f"  Guardado en {args.csv}")


if __name__ == "__main__":
    main()
