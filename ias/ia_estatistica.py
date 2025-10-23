# -*- coding: utf-8 -*-
"""IA Estatística — histórico + BACKTEST dos candidatos."""
import time
from core.base_agent import BaseAgent
from core.bus import Event

def color_of(spin): return "white" if spin.get("white") else spin.get("color")

class IAEstatistica(BaseAgent):
    TICK_MS = 800
    def _bind(self):
        self.history = []
        self.bus.on("spin.new", self.on_spin)
        self.bus.on("strategy.candidate", self.on_candidate)

    def on_spin(self, evt):
        self.history.append(evt.data)
        self.history = self.history[-5000:]

    def on_candidate(self, evt):
        s = evt.data
        score = self.backtest(s, horizon=300)
        payload = {"strategy": s, **score}
        self.state.push_event({"agent": self.name, "msg": f"Score {s.get('id')}: {score['score']}", "ts": time.time()})
        self.bus.emit(Event("strategy.score", payload))

    def _bt_repeat(self, data, n, window):
        wins=loss=0
        for i in range(n, len(data)):
            seq = [color_of(x) for x in data[i-n:i]]
            if len(set(seq))==1:
                pred = seq[-1]
                actual = color_of(data[i])
                wins += (pred==actual)
                loss += (pred!=actual)
        return wins, loss

    def _bt_alternation(self, data, alt_len, window):
        wins=loss=0
        for i in range(alt_len, len(data)):
            seq = [color_of(x) for x in data[i-alt_len:i+1]]
            alt = True
            for k in range(1,len(seq)):
                if seq[k]==seq[k-1]: alt=False; break
            if alt:
                last = seq[-1]
                pred = "red" if last=="black" else ("black" if last=="red" else None)
                if pred:
                    actual = color_of(data[i])
                    wins += (pred==actual)
                    loss += (pred!=actual)
        return wins, loss

    def _bt_cluster(self, data, cluster_th, window):
        wins=loss=0
        for i in range(cluster_th, len(data)):
            seq = [color_of(x) for x in data[i-cluster_th:i]]
            if len(set(seq))==1:
                c = seq[-1]
                pred = "black" if c=="red" else ("red" if c=="black" else None)
                if pred:
                    actual = color_of(data[i])
                    wins += (pred==actual)
                    loss += (pred!=actual)
        return wins, loss

    def backtest(self, strategy, horizon=300):
        data = list(self.history)[-horizon:]
        if not data: return {"score":0,"roi":0,"winrate":0,"dd":0}
        t = strategy.get("type")
        p = strategy.get("params",{})
        if t=="repeat_pattern":
            w,l = self._bt_repeat(data, p.get("repeat_n",3), p.get("window",5))
        elif t=="alternation":
            w,l = self._bt_alternation(data, p.get("alt_len",3), p.get("window",6))
        elif t=="cluster_count":
            w,l = self._bt_cluster(data, p.get("cluster_th",3), p.get("window",8))
        else:
            w,l = 0,0
        total = max(1, w+l)
        winrate = w/total
        roi = w - l
        score = round(winrate*0.7 + max(0,roi/total)*0.3, 4)
        return {"score": score, "roi": roi, "winrate": round(winrate,3), "dd": 0}
