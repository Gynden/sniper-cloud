# -*- coding: utf-8 -*-
"""IA Estratégica — executa a ESTRATÉGIA ATIVA (vinda do aprendiz/promoção) em tempo real."""
import time
from core.base_agent import BaseAgent
from core.bus import Event

def color_of(spin):
    return "white" if spin.get("white") else spin.get("color")

class IAEstrategica(BaseAgent):
    TICK_MS = 400

    def _bind(self):
        self.buffer = []
        self.bus.on("spin.new", self.on_spin)
        self.bus.on("strategy.promote", self.on_promote)

    def on_spin(self, evt):
        self.buffer.append(evt.data)
        self.buffer = self.buffer[-200:]

    def on_promote(self, evt):
        # recebe estratégia ativa (trial/production)
        strat = evt.data.get("strategy")
        if strat:
            self.state.set("active.strategy", strat)
            self.state.push_event({"agent": self.name, "msg": f"Estratégia ativa: {strat.get('id')} ({strat.get('type')})", "ts": time.time()})

    # Interpretadores mínimos de política (espelham backtest)
    def _predict_repeat(self, window, repeat_n):
        if len(self.buffer) < max(repeat_n, window): return None
        seq = [color_of(s) for s in self.buffer[-repeat_n:]]
        if len(set(seq)) == 1:
            return seq[-1]
        return None

    def _predict_alternation(self, alt_len, window):
        if len(self.buffer) < alt_len+1: return None
        seq = [color_of(s) for s in self.buffer[-(alt_len+1):]]
        ok = True
        for i in range(1, len(seq)):
            if seq[i]==seq[i-1]: ok=False; break
        if ok:
            last = seq[-1]
            return "red" if last=="black" else ("black" if last=="red" else None)
        return None

    def _predict_cluster(self, cluster_th, window):
        if len(self.buffer) < cluster_th: return None
        seq = [color_of(s) for s in self.buffer[-cluster_th:]]
        if len(set(seq))==1:
            c = seq[-1]
            return "black" if c=="red" else ("red" if c=="black" else None)
        return None

    def tick(self):
        strat = self.state.get("active.strategy")
        if not strat or not self.buffer: return
        stype = strat.get("type")
        p = strat.get("params", {})
        suggestion = None
        if stype == "repeat_pattern":
            suggestion = self._predict_repeat(p.get("window",5), p.get("repeat_n",3))
        elif stype == "alternation":
            suggestion = self._predict_alternation(p.get("alt_len",3), p.get("window",6))
        elif stype == "cluster_count":
            suggestion = self._predict_cluster(p.get("cluster_th",3), p.get("window",8))

        if suggestion:
            proposal = {"when": int(time.time()), "suggest": suggestion, "source":"estrategica", "strategy_id": strat.get("id"), "confidence": 0.5}
            self.state.set("signal.proposed", proposal)
            self.bus.emit(Event("signal.proposed", proposal))
