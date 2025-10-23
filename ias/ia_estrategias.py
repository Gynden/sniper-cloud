# -*- coding: utf-8 -*-
"""IA de Estratégias (GA) — gera candidatos, muta e envia para backtest."""
import time, random, uuid
from core.base_agent import BaseAgent
from core.bus import Event

TEMPLATE_TYPES = ["repeat_pattern","alternation","cluster_count"]

def random_strategy(gen=0):
    t = random.choice(TEMPLATE_TYPES)
    if t == "repeat_pattern":
        params = {"repeat_n": random.randint(2,5), "window": random.randint(3,8)}
    elif t == "alternation":
        params = {"alt_len": random.randint(2,6), "window": random.randint(3,10)}
    else:
        params = {"cluster_th": random.randint(2,6), "window": random.randint(5,12)}
    return {"id": str(uuid.uuid4())[:8], "type": t, "params": params, "meta": {"gen": gen, "origin":"ga"}}

class IAEstrategias(BaseAgent):
    TICK_MS = 1000
    def _bind(self):
        self.pool = []
        self.generation = 0
        self.bus.on("strategy.score", self.on_score)

    def on_score(self, evt):
        s = evt.data.get("strategy"); score = evt.data.get("score",0)
        s = dict(s); s["meta"]["fitness"] = score
        self.pool.append(s)
        self.pool = sorted(self.pool, key=lambda x: x["meta"].get("fitness",0), reverse=True)[:100]

    def tick(self):
        if len(self.pool) < 20:
            for _ in range(20-len(self.pool)):
                self.bus.emit(Event("strategy.candidate", random_strategy(self.generation)))
            self.generation += 1
            return
        parents = self.pool[:10]
        for _ in range(10):
            a = random.choice(parents); b = random.choice(parents)
            child = self.crossover(a,b); child = self.mutate(child)
            child["meta"]["gen"] = self.generation
            self.bus.emit(Event("strategy.candidate", child))
        self.generation += 1

    def crossover(self, a, b):
        child = {"id": a["id"]+b["id"][:4], "type": a["type"], "params": {}, "meta": {"origin":"ga"}}
        for k in a["params"]:
            child["params"][k] = a["params"][k] if random.random()<0.5 else b["params"].get(k, a["params"][k])
        return child

    def mutate(self, s):
        for k in list(s["params"].keys()):
            if isinstance(s["params"][k], int) and random.random()<0.3:
                s["params"][k] = max(1, s["params"][k] + random.randint(-1,1))
        return s
