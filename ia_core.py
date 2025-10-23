# ia_core.py
# Implementação simples/compatível para SpectraAI e FeatureExtractor.
# - FeatureExtractor(K) => make(history_colors) -> vetor fixo de features
# - SpectraAI(feat_dim, ...) => decide(features, history) -> (color, conf, probs)
#   probs é um dict {"red": p, "black": p, "white": p}, somando ~1.0
#   feedback(history, new_color) é um no-op leve (mantém interface)

from __future__ import annotations
from typing import List, Tuple, Dict
import math
import random
from collections import Counter

Color = str  # "red" | "black" | "white"

class FeatureExtractor:
    """
    Extrator simples baseado em janelas:
    - Frequências das últimas K rodadas
    - Streak (run length) da última cor
    - One-hot da última cor
    - Deltas curtos de frequência
    Tamanho do vetor é fixo para um dado K.
    """
    def __init__(self, K: int = 7):
        self.K = int(K)
        # dimensão:
        #   freq (3) + streak(2) + last one-hot(3) + diffs(3) + bias(1) = 12
        #   e fazemos isso em 1 janela principal -> 12
        # Se quiser aumentar a dimensão, pode compor multi-janelas.
        self.dim = 12

    def make(self, history: List[Color]) -> List[float]:
        h = list(history)[-max(self.K, 1):]
        n = len(h)
        cnt = Counter(h)
        red = cnt.get("red", 0) / max(1, n)
        black = cnt.get("black", 0) / max(1, n)
        white = cnt.get("white", 0) / max(1, n)

        # streak (run length) da última cor (exclui white em streak curta)
        streak_len = 0
        last = h[-1] if n else None
        for i in range(n-1, -1, -1):
            if h[i] == last:
                streak_len += 1
            else:
                break
        streak_norm = streak_len / max(1, self.K)

        # one-hot última cor
        oh_r = 1.0 if last == "red" else 0.0
        oh_b = 1.0 if last == "black" else 0.0
        oh_w = 1.0 if last == "white" else 0.0

        # diffs de frequência em sub-janelas (metade K vs final K)
        mid = max(1, self.K // 2)
        h_mid = h[-mid:]
        m = len(h_mid)
        cnt_mid = Counter(h_mid)
        red_mid = cnt_mid.get("red", 0) / max(1, m)
        black_mid = cnt_mid.get("black", 0) / max(1, m)
        white_mid = cnt_mid.get("white", 0) / max(1, m)

        d_red = red_mid - red
        d_blk = black_mid - black
        d_wht = white_mid - white

        # bias
        bias = 1.0

        feats = [
            red, black, white,            # 3
            streak_norm, float(n)/max(1,self.K),  # 2
            oh_r, oh_b, oh_w,            # 3
            d_red, d_blk, d_wht,         # 3
            bias                          # 1
        ]
        # garantir tamanho fixo
        if len(feats) < self.dim:
            feats += [0.0] * (self.dim - len(feats))
        elif len(feats) > self.dim:
            feats = feats[:self.dim]
        return feats

    def __len__(self):
        # permite usar len(_feature.make([])) como no seu app, caso precise
        return self.dim


class SpectraAI:
    """
    Classificador leve (heurístico) com interface:
    - decide(features, history) -> (color, confidence, probs)
    - feedback(history, new_color) -> atualiza um pouco a exploração

    Estratégia:
      - converte features -> logits simples por cor
      - softmax -> probs
      - confiança = max(probs)
      - decisão = argmax
    """
    def __init__(self, feat_dim: int, alpha: float = 0.7,
                 eps_start: float = 0.15, eps_min: float = 0.02, eps_decay: float = 0.999):
        self.dim = int(feat_dim)
        # pesos simples fixos (para rodar sem treinar)
        random.seed(42)
        self.w_red  = [random.uniform(-0.2, 0.2) for _ in range(self.dim)]
        self.w_blk  = [random.uniform(-0.2, 0.2) for _ in range(self.dim)]
        self.w_wht  = [random.uniform(-0.2, 0.2) for _ in range(self.dim)]
        self.alpha = float(alpha)
        self.eps = float(eps_start)
        self.eps_min = float(eps_min)
        self.eps_decay = float(eps_decay)

    def _dot(self, w: List[float], x: List[float]) -> float:
        s = 0.0
        L = min(len(w), len(x))
        for i in range(L):
            s += w[i] * x[i]
        return s

    def _softmax3(self, a: float, b: float, c: float) -> Tuple[float, float, float]:
        m = max(a,b,c)
        ea, eb, ec = math.exp(a-m), math.exp(b-m), math.exp(c-m)
        s = ea + eb + ec + 1e-9
        return ea/s, eb/s, ec/s

    def decide(self, features: List[float], history: List[Color]) -> Tuple[Color, float, Dict[str, float]]:
        # exploração ocasional (epsilon): empurra leve para equilíbrio
        if random.random() < self.eps:
            pr = pb = 0.49
            pw = 0.02  # branco é raro
        else:
            # logits lineares
            a = self._dot(self.w_red, features)
            b = self._dot(self.w_blk, features)
            # força white ser mais raro, com um viés negativo
            c = self._dot(self.w_wht, features) - 1.2
            pr, pb, pw = self._softmax3(a, b, c)

        # normalizar (garante soma 1)
        s = max(1e-9, pr + pb + pw)
        pr, pb, pw = pr/s, pb/s, pw/s

        # decisão
        best_color = "red"
        best_p = pr
        if pb > best_p: best_color, best_p = "black", pb
        if pw > best_p: best_color, best_p = "white", pw

        # confiança = probabilidade da classe escolhida
        confidence = float(best_p)

        probs = {"red": float(pr), "black": float(pb), "white": float(pw)}
        return best_color, confidence, probs

    def feedback(self, history: List[Color], new_color: Color) -> None:
        # decay da exploração a cada feedback
        self.eps = max(self.eps_min, self.eps * self.eps_decay)
        # (poderia ajustar pesos aqui se quiser; mantemos simples para compatibilidade)
        return
