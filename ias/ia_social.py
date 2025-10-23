# -*- coding: utf-8 -*-
"""IA Social — mensagens para UI/usuários."""
import time
from core.base_agent import BaseAgent

class IASocial(BaseAgent):
    TICK_MS = 600
    def _bind(self):
        self.bus.on("signal.approved", self.on_signal)
        self.bus.on("system.paused", self.on_pause)
        self.bus.on("system.resumed", self.on_resume)

    def on_signal(self, evt):
        p = evt.data.get("proposal", {})
        msg = f"🚀 Entrada: {p.get('suggest')} | Strat {p.get('strategy_id')}"
        self.state.push_event({"agent": self.name, "msg": msg, "ts": time.time()})
        self.state.set("social.last_msg", msg)

    def on_pause(self, evt):
        self.state.push_event({"agent": self.name, "msg": "⏸️ Sistema pausado por segurança.", "ts": time.time()})

    def on_resume(self, evt):
        self.state.push_event({"agent": self.name, "msg": "▶️ Sistema retomado.", "ts": time.time()})

    def tick(self): pass
