"""Adquisición cancelable y cola de trabajo; no se llama a Tk desde otros hilos."""
from dataclasses import asdict
import queue
import threading
import time

from .models import Batch, utc_now


class AcquisitionCancelled(RuntimeError):
    pass


def acquire(reader, settings, cancel=None, progress=None):
    settings.validate()
    cancel = cancel or threading.Event()
    started = utc_now()
    start = time.monotonic()

    def wait(seconds):
        if cancel.wait(seconds):
            raise AcquisitionCancelled("Adquisición cancelada; lote parcial descartado.")

    wait(settings.wait_s)
    values, times, elapsed = [], [], []
    for index in range(settings.n):
        wait(0)
        value = reader.read_voltage(settings.channel)
        values.append(value)
        times.append(utc_now())
        elapsed.append(time.monotonic()-start)
        if progress:
            progress(index+1, settings.n, value)
        if index+1 < settings.n:
            # Intervalo mínimo entre el final de una consulta y el inicio de otra.
            wait(settings.interval_s)
    wait(0)
    return Batch(values, times, elapsed, asdict(settings), started, utc_now())


class Worker:
    """Una sola operación instrumental en curso; resultados por cola."""
    def __init__(self):
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.thread = None
        self._busy = False

    @property
    def busy(self):
        return self._busy

    def submit(self, operation, function):
        if self.busy:
            raise RuntimeError("Espere a que termine la operación actual.")
        self.cancel.clear()
        self._busy = True

        def run():
            try:
                result = function()
                event = ("done", operation, result)
            except Exception as exc:
                event = ("error", operation, exc)
            self._busy = False
            self.events.put(event)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def progress(self, index, total, value):
        self.events.put(("progress", "acquire", (index, total, value)))
