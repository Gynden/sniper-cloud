# ia_core.py
# Learner on-line para prever p_red (vermelho) e p_white (branco)
import numpy as np
from collections import deque
from datetime import datetime

try:
    from sklearn.linear_model import SGDClassifier
    _HAS_SK = True
except Exception:
    _HAS_SK = False

CLASSES_COLOR = np.array([0, 1])  # 0=BLACK, 1=RED
CLASSES_WHITE = np.array([0, 1])  # 0=NOT WHITE, 1=WHITE

class SpectraLearner:
    """
    - predict(ctx) -> (p_red, p_white)
    - update(ctx, resultado) -> treina on-line
    ctx esperado:
      {
        "ultimos": [{"cor": 'R'|'B'|'W', "num": int}, ...],
        "lat_ms": int,
        "hora": int (0..23),
        "dow": int (0..6),
        "confluencia": int,
        "estrats_bits": list[int]  # opcional
      }
    resultado:
      {"cor_saida": 'R'|'B'|'W'}
    """
    def __init__(self, hist_max=2000):
        self.hist = deque(maxlen=hist_max)
        self._boot_white = True
        self._boot_color = True
        # fallback sem sklearn: mantemos contagens beta simples
        self._wins_red = 1.0; self._loss_red = 1.0
        self._wins_white = 1.0; self._loss_white = 13.0  # branco é raro

        if _HAS_SK:
            self.m_color = SGDClassifier(loss="log_loss", random_state=7)
            self.m_white = SGDClassifier(loss="log_loss", random_state=7)
        else:
            self.m_color = None
            self.m_white = None

    # ---------- features ----------
    def _features(self, ctx):
        ult = ctx.get("ultimos", [])
        lat = float(ctx.get("lat_ms", 0)) / 1000.0
        hora = ctx.get("hora", datetime.utcnow().hour)
        dow  = ctx.get("dow",  datetime.utcnow().weekday())
        conf = float(ctx.get("confluencia", 0))
        strat_bits = ctx.get("estrats_bits", [])

        cores = [u["cor"] for u in ult]
        nums  = [u["num"] for u in ult]
        n = len(ult)

        def prop(c):
            return (cores.count(c)/n) if n else 0.0

        # streak atual (R/B)
        streak = 0
        if n:
            last = None
            for i in range(n-1, -1, -1):
                c = cores[i]
                if c == 'W':
                    if streak == 0: continue
                    else: break
                if last is None or c == last:
                    streak += 1
                    last = c
                else:
                    break

        # alternâncias nas últimas 10
        alt = 0
        k = min(10, max(0, n-1))
        st = max(0, n-k)
        for i in range(st, n-1):
            if cores[i] != 'W' and cores[i+1] != 'W' and cores[i] != cores[i+1]:
                alt += 1

        # distância do último branco
        dist_w = 999
        for i in range(n-1, -1, -1):
            if cores[i] == 'W':
                dist_w = (n-1)-i; break

        # paridade nos últimos 20
        ult20 = nums[-20:] if n else []
        total20 = max(1, len(ult20))
        pares   = sum(1 for x in ult20 if x % 2 == 0)
        impares = total20 - pares
        p_par   = pares/total20
        p_imp   = impares/total20

        # hora/dia normalizados
        h_norm = float(hora)/23.0
        d_norm = float(dow)/6.0

        x = [
            prop('R'), prop('B'), prop('W'),           # proporções recentes
            float(streak), float(alt)/10.0,            # streak + alternância
            float(dist_w)/40.0,                        # dist normalizada
            p_par, p_imp,
            conf/6.0,                                  # conf normalizada
            lat,
            h_norm, d_norm
        ] + list(map(float, strat_bits or []))
        return np.array(x, dtype=float)

    # ---------- predição ----------
    def predict(self, ctx):
        x = self._features(ctx).reshape(1, -1)
        # fallback sem sklearn: médias beta simples
        if not _HAS_SK:
            p_red   = self._wins_red / (self._wins_red + self._loss_red)
            p_white = self._wins_white / (self._wins_white + self._loss_white)
            p_red   = float(max(0.05, min(0.95, p_red)))
            p_white = float(max(0.01, min(0.40, p_white)))
            return p_red, p_white

        # sklearn
        p_red = 0.5
        p_white = 0.07
        if not self._boot_color:
            p_red = float(self.m_color.predict_proba(x)[0,1])
        if not self._boot_white:
            p_white = float(self.m_white.predict_proba(x)[0,1])
        return p_red, p_white

    # ---------- atualização ----------
    def update(self, ctx, resultado):
        c = resultado.get("cor_saida")
        x = self._features(ctx).reshape(1, -1)

        # WHITE
        y_w = np.array([1 if c=='W' else 0])
        if _HAS_SK:
            if self._boot_white:
                self.m_white.partial_fit(x, y_w, classes=CLASSES_WHITE)
                self._boot_white = False
            else:
                self.m_white.partial_fit(x, y_w)
        else:
            # beta simples
            if c == 'W': self._wins_white += 1.0
            else:        self._loss_white += 1.0

        # COLOR somente quando não for branco
        if c != 'W':
            y_c = np.array([1 if c=='R' else 0])
            if _HAS_SK:
                if self._boot_color:
                    self.m_color.partial_fit(x, y_c, classes=CLASSES_COLOR)
                    self._boot_color = False
                else:
                    self.m_color.partial_fit(x, y_c)
            else:
                # beta simplificada: considerar RED como "sucesso"
                if c == 'R': self._wins_red += 1.0
                else:        self._loss_red += 1.0
