# -*- coding: utf-8 -*-
"""IA de Desempenho — telemetria simples."""
import time
from core.base_agent import BaseAgent
class IADesempenho(BaseAgent):
    TICK_MS = 1800
    def _bind(self): pass
    def tick(self):
        self.state.update("perf", {"ok": True, "ts": int(time.time())})
