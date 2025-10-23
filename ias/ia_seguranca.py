# -*- coding: utf-8 -*-
"""IA de Segurança — pausa se 3 losses seguidos (placeholder)."""
import time
from core.base_agent import BaseAgent
from core.bus import Event

class IASeguranca(BaseAgent):
    TICK_MS = 400
    def _bind(self):
        self.loss_streak = 0
        self.bus.on("signal.result", self.on_result)

    def on_result(self, evt):
        r = evt.data.get("result")
        if r == "loss": self.loss_streak += 1
        if r == "win": self.loss_streak = 0
        if self.loss_streak >= 3:
            self.state.set("system.paused", True)
            self.state.set("security.unpause_at", time.time()+5*60)
            self.bus.emit(Event("system.paused", {"by": self.name}))
            self.loss_streak = 0

    def tick(self):
        unpause_at = self.state.get("security.unpause_at")
        if unpause_at and time.time()>=unpause_at:
            self.state.set("system.paused", False)
            self.state.set("security.unpause_at", None)
            self.bus.emit(Event("system.resumed", {"by": self.name}))
