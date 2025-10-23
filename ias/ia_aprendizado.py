# -*- coding: utf-8 -*-
"""IA de Aprendizado — pool de estratégias (bandit) + promoção para produção."""
import time, random
from core.base_agent import BaseAgent
from core.bus import Event

class IAAprendizado(BaseAgent):
    TICK_MS = 1200
    def _bind(self):
        self.pool = {}  # id -> stats
        self.active_id = None
        self.bus.on("strategy.score", self.on_score)

    def on_score(self, evt):
        s = evt.data.get("strategy")
        sid = s.get("id")
        score = evt.data.get("score", 0)
        if sid not in self.pool:
            self.pool[sid] = {"strategy": s, "score": score, "trials":0, "wins":0, "returns":0.0}
        else:
            self.pool[sid]["score"] = (self.pool[sid]["score"]*0.7 + score*0.3)
        # promoção automática simples
        if score >= 0.6:
            self.active_id = sid
            self.state.set("learning.pool", {k:{"score":v["score"]} for k,v in self.pool.items()})
            self.bus.emit(Event("strategy.promote", {"strategy": s, "mode": "trial"}))

    def tick(self):
        # opcional: demover estratégia se score cair
        if self.active_id and self.pool.get(self.active_id, {}).get("score",0) < 0.45:
            self.active_id = None
            self.state.push_event({"agent": self.name, "msg": "Active strategy demoted by low score.", "ts": time.time()})
