# -*- coding: utf-8 -*-
import threading, collections

class StateStore:
    """ Key-Value thread-safe + rolling log para UI """
    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()
        self._events = collections.deque(maxlen=2000)

    def set(self, key, value):
        with self._lock:
            self._data[key] = value

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def update(self, key, patch: dict):
        with self._lock:
            base = self._data.get(key, {})
            if not isinstance(base, dict):
                base = {}
            if isinstance(patch, dict):
                base.update(patch)
            self._data[key] = base

    def reset(self):
        with self._lock:
            self._data.clear()
            self._events.clear()

    def push_event(self, evt: dict):
        with self._lock:
            self._events.append(evt)

    def tail_events(self, n: int):
        with self._lock:
            return list(self._events)[-n:]
