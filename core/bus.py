# -*- coding: utf-8 -*-
import threading, time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

@dataclass
class Event:
    type: str
    data: Any
    ts: float = field(default_factory=lambda: time.time())

class EventBus:
    """ Barramento simples pub/sub thread-safe. """
    def __init__(self):
        self.subscribers: Dict[str, List[Callable[[Event], None]]] = {}
        self.lock = threading.Lock()

    def on(self, event_type: str, callback: Callable[[Event], None]):
        with self.lock:
            self.subscribers.setdefault(event_type, []).append(callback)

    def emit(self, event: Event):
        with self.lock:
            callbacks = list(self.subscribers.get(event.type, []))
        for cb in callbacks:
            try:
                cb(event)
            except Exception as e:
                print(f"[BUS] erro em callback {cb}: {e}")
