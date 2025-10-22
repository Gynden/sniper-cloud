# ia_core.py
# IA avançada: contextual bandit + regressão logística online (multiclasse)
# + minerador de padrões (n-gram lift) para prior automático
# Aprende continuamente a partir do histórico (online SGD)

from __future__ import annotations
import math, random, time
from collections import deque, defaultdict, Counter
from typing import List, Dict, Tuple, Optional

ACTIONS = ["red", "black", "white"]
A2I = {a:i for i,a in enumerate(ACTIONS)}

# ---------- util ----------
def softmax(x: List[float]) -> List[float]:
    if not x: return []
    m = max(x)
    ex = [math.exp(v - m) for v in x]
    s = sum(ex) or 1.0
    return [v/s for v in ex]

def clip(v, lo, hi): return lo if v < lo else hi if v > hi else v

# ---------- Feature extractor ----------
class FeatureExtractor:
    """
    Gera features numéricas a partir do histórico:
    - one-hot das últimas K jogadas
    - contagens em janelas (5/10/20)
    - streak atual por cor
    - tempo desde último white
    - momentum (diferença de contagens recentes)
    - hora/minuto (sin/cos)
    - volatilidade (trocas de cor recentes)
    """
    def __init__(self, K:int=6):
        self.K = K

    def _one_hot_last(self, hist: List[str]) -> List[float]:
        feats=[]
        for i in range(self.K):
            if len(hist) - 1 - i >= 0:
                c = hist[-1 - i]
                vec = [0.0,0.0,0.0]
                vec[A2I[c]] = 1.0
            else:
                vec = [0.0,0.0,0.0]
            feats.extend(vec)
        return feats

    def _counts_window(self, hist: List[str], w:int) -> List[float]:
        last = hist[-w:] if len(hist)>=w else hist
        n = max(1, len(last))
        return [last.count("red")/n, last.count("black")/n, last.count("white")/n]

    def _streaks(self, hist: List[str]) -> List[float]:
        # streak atual de cada cor
        out=[]
        for col in ACTIONS:
            s=0
            for i in range(len(hist)-1, -1, -1):
                if hist[i]==col: s+=1
                else: break
            out.append(float(s))
        return out

    def _since_white(self, hist: List[str]) -> List[float]:
        d=0
        for i in range(len(hist)-1, -1, -1):
            if hist[i]=="white": break
            d+=1
        return [float(d)]

    def _momentum(self, hist: List[str]) -> List[float]:
        a = hist[-10:] if len(hist)>=10 else hist
        b = hist[-5:] if len(hist)>=5 else hist
        def vec(seg):
            n=max(1,len(seg))
            return [seg.count("red")/n, seg.count("black")/n, seg.count("white")/n]
        va, vb = vec(a), vec(b)
        return [vb[i]-va[i] for i in range(3)]

    def _timeofday(self) -> List[float]:
        t = time.gmtime()
        m = t.tm_hour*60 + t.tm_min
        x = 2*math.pi * (m/1440.0)
        return [math.sin(x), math.cos(x)]

    def _volatility(self, hist: List[str]) -> List[float]:
        h = hist[-12:] if len(hist)>=12 else hist
        swaps = 0
        for i in range(1,len(h)):
            if h[i]!=h[i-1]: swaps+=1
        return [swaps/float(max(1,len(h)-1))]

    def make(self, hist: List[str]) -> List[float]:
        feats=[]
        feats += self._one_hot_last(hist)
        feats += self._counts_window(hist, 5)
        feats += self._counts_window(hist, 10)
        feats += self._counts_window(hist, 20)
        feats += self._streaks(hist)
        feats += self._since_white(hist)
        feats += self._momentum(hist)
        feats += self._timeofday()
        feats += self._volatility(hist)
        # bias
        feats.append(1.0)
        return feats

# ---------- Online Logistic Regression (multiclass) ----------
class OnlineLogReg:
    """
    Modelo linear multiclasses (3) com SGD + regularização L2
    """
    def __init__(self, dim:int, lr:float=0.05, l2:float=1e-4):
        self.dim = dim
        self.lr  = lr
        self.l2  = l2
        # pesos W: 3 x dim
        self.W = [[0.0]*dim for _ in ACTIONS]

    def predict_proba(self, x: List[float]) -> List[float]:
        z = [sum(wi*xi for wi,xi in zip(w,x)) for w in self.W]
        return softmax(z)

    def update(self, x: List[float], y_idx:int):
        p = self.predict_proba(x)
        # gradiente: (p - y_onehot) * x
        for k in range(3):
            g = (p[k] - (1.0 if k==y_idx else 0.0))
            for j in range(self.dim):
                self.W[k][j] -= self.lr * (g * x[j] + self.l2 * self.W[k][j])

# ---------- Pattern miner (n-gram) ----------
class PatternMiner:
    """
    Mantém contagens de n-grams (n=2..4) e calcula lift para sugerir priors.
    Ex.: se a sequência 'red,red,black' historicamente gera white acima do esperado,
    o miner retorna um bias para white naquela condição.
    """
    def __init__(self, max_n:int=4):
        self.max_n = max_n
        self.counts = {n: Counter() for n in range(2, max_n+1)}
        self.next_counts = {n: {a:Counter() for a in ACTIONS} for n in range(2, max_n+1)}
        self.total_by_action = Counter()

    def observe(self, hist: List[str]):
        if not hist: return
        self.total_by_action[hist[-1]] += 1
        for n in range(2, self.max_n+1):
            if len(hist) >= n:
                seq = tuple(hist[-n:])
                self.counts[n][seq] += 1
                # para próximo passo
                prev = tuple(hist[-(n-1):])
                # próximo é hist[-1], mas bias é para "depois de prev"
                self.next_counts[n].setdefault(prev, Counter())
                self.next_counts[n][prev][hist[-1]] += 1

    def prior_for(self, window: List[str]) -> Dict[str, float]:
        if not window: return {}
        pri = defaultdict(float)
        base = {a: max(1, self.total_by_action[a]) for a in ACTIONS}
        total_base = sum(base.values())
        base_prob = {a: base[a]/total_base for a in ACTIONS}

        for n in range(self.max_n, 1, -1):
            if len(window) >= (n-1):
                prev = tuple(window[-(n-1):])
                nxt = self.next_counts[n].get(prev)
                if not nxt: continue
                tot = sum(nxt.values()) or 1.0
                for a,cnt in nxt.items():
                    p = cnt/tot
                    lift = p / max(1e-6, base_prob[a])
                    pri[a] += lift - 1.0  # >0 favorece
                break
        # normaliza para 0..1
        # transforma pri (que pode ser negativo) em pesos positivos
        minv = min(pri.values()) if pri else 0.0
        adj = {k: (v - minv) for k,v in pri.items()} if pri else {}
        s = sum(adj.values()) or 1.0
        return {k: v/s for k,v in adj.items()} if adj else {}

# ---------- Contextual Bandit + Ensemble ----------
class SpectraAI:
    """
    Núcleo que decide a próxima cor:
    - Features -> OnlineLogReg -> prob_model
    - PatternMiner -> prior_patterns
    - Ensemble: prob = normalize( alpha * prob_model + (1-alpha) * prior )
    - Exploração controlada (epsilon decrescente)
    - Atualiza online no feedback (quando chega a rodada real)
    """
    def __init__(self, feat_dim:int, alpha:float=0.7, eps_start:float=0.12, eps_min:float=0.02, eps_decay:float=0.999):
        self.model = OnlineLogReg(feat_dim, lr=0.05, l2=1e-4)
        self.miner = PatternMiner(max_n=4)
        self.alpha = alpha
        self.eps   = eps_start
        self.eps_min = eps_min
        self.eps_decay = eps_decay
        self.last_context: Optional[List[float]] = None
        self.last_action: Optional[int] = None
        self.last_created_at: Optional[str] = None

    def decide(self, feats: List[float], window: List[str]) -> Tuple[str, float, Dict[str,float]]:
        pm = self.model.predict_proba(feats)
        prior = self.miner.prior_for(window)
        # mistura
        mix = []
        for i,a in enumerate(ACTIONS):
            p = self.alpha * pm[i] + (1.0 - self.alpha) * prior.get(a, 1.0/3.0)
            mix.append(p)
        mix = softmax(mix)

        # exploração
        if random.random() < self.eps:
            idx = random.randrange(3)
        else:
            idx = max(range(3), key=lambda i: mix[i])

        self.eps = max(self.eps_min, self.eps * self.eps_decay)
        self.last_context = feats[:]
        self.last_action  = idx
        self.last_created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return ACTIONS[idx], mix[idx], {a:mix[i] for i,a in enumerate(ACTIONS)}

    def feedback(self, window: List[str], true_color: str):
        # treina modelo
        if self.last_context is None or self.last_action is None: 
            # ainda sem decisão anterior (arranque frio)
            self.miner.observe(window)
            return
        y_idx = A2I[true_color]
        self.model.update(self.last_context, y_idx)
        # miner aprende sempre
        self.miner.observe(window)

    # debug/info
    def info(self) -> Dict:
        return {
            "alpha": self.alpha, "eps": round(self.eps,4),
            "weights_shape": [len(w) for w in self.model.W]
        }
