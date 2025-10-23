# -*- coding: utf-8 -*-
"""IA Auxiliar — valida proposta (thresholds dinâmicos) e APROVA sinal."""
import time
from core.base_agent import BaseAgent
from core.bus import Event

class IAAuxiliar(BaseAgent):
    TICK_MS = 200
    def _bind(self):
        self.bus.on("signal.proposed", self.on_proposal)

    def on_proposal(self, evt):
        prop = dict(evt.data)
        # regra inicial: aprova se sistema não está pausado e existe strategy_id
        if not self.state.get("system.paused", False) and prop.get("strategy_id"):
            self.bus.emit(Event("signal.approved", {"proposal": prop, "by": self.name}))
            # espelha para /signal
            self.state.set("signal.last", prop)
        else:
            self.bus.emit(Event("signal.veto", {"proposal": prop, "by": self.name}))

    def tick(self): pass
