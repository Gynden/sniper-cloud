# -*- coding: utf-8 -*-
import time
from core.bus import Event

class BaseAgent:
    TICK_MS = 400
    def __init__(self, name, bus, state):
        self.name = name
        self.bus = bus
        self.state = state
        self._last_tick = 0
        self._bind()

    def _bind(self): pass

    def status(self):
        return {"paused": self.state.get(f"{self.name}.paused", False), "last_tick": self._last_tick}

    def pause(self): self.state.set(f"{self.name}.paused", True)
    def resume(self): self.state.set(f"{self.name}.paused", False)

    def run(self):
        while True:
            if self.state.get("system.paused", False) or self.state.get(f"{self.name}.paused", False):
                time.sleep(0.2); continue
            self._last_tick = time.time()
            try:
                self.tick()
            except Exception as e:
                self.state.push_event({"agent": self.name, "error": str(e), "ts": time.time()})
            time.sleep(self.TICK_MS/1000.0)

    def tick(self): pass
