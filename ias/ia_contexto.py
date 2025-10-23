# -*- coding: utf-8 -*-
"""IA de Contexto — simula leitura de clima/volatilidade."""
import time, random
from core.base_agent import BaseAgent
class IAContexto(BaseAgent):
    TICK_MS = 2200
    def _bind(self): pass
    def tick(self):
        v = round(random.uniform(0.2,0.9),2)
        self.state.set("context.volatility", v)
        if v>0.85:
            self.state.push_event({"agent": self.name, "msg": "Volatilidade alta", "ts": time.time()})
