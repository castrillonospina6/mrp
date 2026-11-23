"""Turno de escritura en orden de llegada (para SQLite).

SQLite admite un solo escritor a la vez y, cuando varios esperan, los atiende sin orden: casi todos pasan en
milisegundos, pero alguno puede quedarse esperando varios segundos (el «lag» que se nota a veces en la planta). Como el
servidor es un solo proceso, las escrituras frecuentes de la planta (marcar, reportar un tramo, paros) toman aquí su
turno en orden de llegada: cada una espera solo a las que llegaron antes. Lo demás (lecturas) no espera nunca.
Con PostgreSQL no hace nada.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

from django.conf import settings


class FifoLock:
    """Candado por turnos (como una fila con fichas). Reentrante para el mismo hilo."""

    def __init__(self):
        self._cond = threading.Condition()
        self._next = 0
        self._serving = 0
        self._owner = None
        self._depth = 0

    def acquire(self):
        me = threading.get_ident()
        with self._cond:
            if self._owner == me:
                self._depth += 1
                return
            ticket = self._next
            self._next += 1
            while ticket != self._serving:
                self._cond.wait()
            self._owner, self._depth = me, 1

    def release(self):
        with self._cond:
            self._depth -= 1
            if self._depth == 0:
                self._owner = None
                self._serving += 1
                self._cond.notify_all()


_LOCK = FifoLock()


def _sqlite() -> bool:
    return settings.DATABASES.get("default", {}).get("ENGINE", "").endswith("sqlite3")


@contextmanager
def write_turn():
    if not _sqlite():
        yield
        return
    _LOCK.acquire()
    try:
        yield
    finally:
        _LOCK.release()
