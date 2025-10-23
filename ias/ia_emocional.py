# -*- coding: utf-8 -*-
"""IA Emocional — mensagens curtas contextuais."""
from core.base_agent import BaseAgent
class IAEmocional(BaseAgent):
    TICK_MS = 1600
    def _bind(self):
        self.bus.on("signal.result", self.on_result)
    def on_result(self, evt):
        r = evt.data.get("result")
        self.state.set("emotional.last", "🎉 Boa!" if r=="win" else "💡 Pausa curta e foco.")
    def tick(self): pass
