# regime.py
import math, collections

def _entropy(p, eps=1e-9):
    p = max(eps, min(1.0-eps, p))
    return - (p*math.log(p) + (1.0-p)*math.log(1.0-p))

class RegimeDetector:
    def __init__(self, win_window=60, ent_window=10, ent_thr=0.95):
        self.last_preds = collections.deque(maxlen=ent_window)
        self.last_outcomes = collections.deque(maxlen=win_window)
        self.ent_thr = ent_thr

    def update_pred(self, p_major):
        self.last_preds.append(p_major)

    def update_outcome(self, win01):
        self.last_outcomes.append(1 if win01 else 0)

    def winrate(self):
        n = max(1, len(self.last_outcomes))
        return sum(self.last_outcomes)/n

    def entropia_alta(self):
        if len(self.last_preds) < self.last_preds.maxlen: return False
        e = sum(_entropy(p) for p in self.last_preds)/len(self.last_preds)
        return e > self.ent_thr

    def mercado_ruim(self):
        return (self.winrate() < 0.48) or self.entropia_alta()
