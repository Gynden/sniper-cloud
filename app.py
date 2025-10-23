# app.py — Spectra X (compatível com /ingest do bookmarklet) + Auto-Strategy (GA)
import os, random, uuid
from collections import deque
from datetime import datetime
from typing import Deque, Dict, Optional, List, Tuple
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

# === IA base do seu projeto (mantida) ===
from ia_core import SpectraAI, FeatureExtractor

app = Flask(__name__, static_url_path="", static_folder=".", template_folder=".")
# CORS amplo (UI hospeda em outra origem)
CORS(app, resources={r"/*": {"origins": "*"}})

# ---------------- Config ----------------
HISTORY_MAX, SHOW_LAST = 2000, 60
WHITE_ODDS, COLOR_ODDS = 14.0, 1.0

config = dict(stake=2.0, max_gales=2, confidence_min=0.60, invert_mapping=False)
history: Deque[str] = deque(maxlen=HISTORY_MAX)
metrics = {"total_signals": 0, "wins": 0, "losses": 0, "bank_result": 0.0}
active_signal: Optional[Dict] = None

# ---------------- IA (seu Spectra) ----------------
_feature = FeatureExtractor(K=7)
_feat_dim = len(_feature.make([]))
_ai = SpectraAI(feat_dim=_feat_dim, alpha=0.72, eps_start=0.15, eps_min=0.02, eps_decay=0.9992)

# ---------------- Utils ----------------
def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"

def odds_for(color: str) -> float:
    return WHITE_ODDS if color == "white" else COLOR_ODDS

def number_to_color(n: int) -> Optional[str]:
    if n == 0:
        return "white"
    if config.get("invert_mapping"):
        if 1 <= n <= 7: return "black"
        if 8 <= n <= 14: return "red"
    else:
        if 1 <= n <= 7: return "red"
        if 8 <= n <= 14: return "black"
    return None

def recent_counts(n=10):
    last = list(history)[-n:]
    return {"red": last.count("red"), "black": last.count("black"),
            "white": last.count("white"), "n": len(last)}

# =====================================================================
#                AUTO-STRATEGY (GERAR → BACKTESTAR → PROMOVER)
# =====================================================================

# Tipos "primordiais" de padrões (o GA aprende os PARÂMETROS; não há regra fixa)
TEMPLATE_TYPES = ["repeat_pattern", "alternation", "cluster_count"]

def _predict_by_strategy(stype: str, params: Dict, seq: List[str]) -> Optional[str]:
    """Dado um tipo + parâmetros, tenta prever a próxima cor com base na cauda de 'seq'."""
    if not seq:
        return None
    if stype == "repeat_pattern":
        repeat_n = int(params.get("repeat_n", 3))
        if len(seq) < repeat_n: return None
        last = seq[-repeat_n:]
        if len(set(last)) == 1:
            return last[-1]  # repetir
        return None
    if stype == "alternation":
        alt_len = int(params.get("alt_len", 3))
        if len(seq) < alt_len + 1: return None
        tail = seq[-(alt_len + 1):]
        ok = True
        for i in range(1, len(tail)):
            if tail[i] == tail[i-1]:
                ok = False; break
        if ok:
            # continua alternando
            last = tail[-1]
            return "red" if last == "black" else ("black" if last == "red" else None)
        return None
    if stype == "cluster_count":
        cluster_th = int(params.get("cluster_th", 3))
        if len(seq) < cluster_th: return None
        tail = seq[-cluster_th:]
        if len(set(tail)) == 1:
            c = tail[-1]
            # hipótese simples: reversão
            return "black" if c == "red" else ("red" if c == "black" else None)
        return None
    return None

def _random_strategy(gen: int = 0) -> Dict:
    t = random.choice(TEMPLATE_TYPES)
    if t == "repeat_pattern":
        params = {"repeat_n": random.randint(2, 5)}
    elif t == "alternation":
        params = {"alt_len": random.randint(2, 6)}
    else:
        params = {"cluster_th": random.randint(2, 6)}
    return {"id": str(uuid.uuid4())[:8], "type": t, "params": params, "meta": {"gen": gen}}

def _backtest_candidate(candidate: Dict, colors: List[str], horizon: int = 300) -> Dict:
    """Backtest leve em janela curta sobre histórico de cores."""
    data = colors[-horizon:] if horizon and len(colors) > horizon else colors[:]
    if len(data) < 20:
        return {"id": candidate["id"], "score": 0.0, "winrate": 0.0, "roi": 0}

    wins = losses = 0
    # varremos do início ao fim, e em cada passo tentamos prever o "próximo"
    for i in range(3, len(data)):
        past = data[:i]
        pred = _predict_by_strategy(candidate["type"], candidate["params"], past)
        if pred is None:
            continue
        actual = data[i]
        if pred == actual:
            wins += 1
        else:
            losses += 1

    total = max(1, wins + losses)
    winrate = wins / total
    roi = wins - losses  # placeholder: substitua pelo seu modelo com payout/gales
    score = round(winrate * 0.7 + max(0, roi / total) * 0.3, 4)
    return {"id": candidate["id"], "score": score, "winrate": round(winrate, 3), "roi": roi}

class StrategyGA:
    """GA muito simples: gera, avalia, seleciona top e cria filhos com mutação/crossover."""
    def __init__(self):
        self.pool: List[Dict] = []   # candidatos com fitness na meta
        self.generation: int = 0
        self.active: Optional[Dict] = None  # melhor estratégia promovida
        self.active_score: float = 0.0

    def evaluate_and_update(self, colors: List[str]):
        """Gera/evolui e reavalia pool quando houver histórico suficiente."""
        if len(colors) < 60:
            return

        # 1) se o pool está pequeno, gera aleatórios
        target_pool = 30
        while len(self.pool) < target_pool:
            self.pool.append(_random_strategy(self.generation))

        # 2) backtest de todos (rápido)
        scored: List[Tuple[float, Dict]] = []
        for cand in self.pool:
            res = _backtest_candidate(cand, colors, horizon=300)
            cand["meta"]["fitness"] = res["score"]
            cand["meta"]["winrate"] = res["winrate"]
            cand["meta"]["roi"] = res["roi"]
            scored.append((res["score"], cand))

        # 3) selecionar top-N
        scored.sort(key=lambda x: x[0], reverse=True)
        self.pool = [c for _, c in scored[:60]]

        # 4) elitismo e procriação
        parents = self.pool[:10]
        children = []
        for _ in range(10):
            a = random.choice(parents)
            b = random.choice(parents)
            child = self._crossover(a, b)
            child = self._mutate(child)
            child["meta"]["gen"] = self.generation + 1
            children.append(child)
        self.pool.extend(children)
        self.generation += 1

        # 5) promover melhor se bater threshold
        best = self.pool[0]
        best_score = best["meta"].get("fitness", 0.0)
        if best_score >= 0.60 and best_score >= self.active_score:
            self.active = {k: v for k, v in best.items() if k != "meta"}
            self.active_score = best_score

    def _crossover(self, a: Dict, b: Dict) -> Dict:
        t = random.choice([a["type"], b["type"]])  # às vezes troca o tipo
        params = {}
        if t == "repeat_pattern":
            ra = a.get("params", {}).get("repeat_n", 3)
            rb = b.get("params", {}).get("repeat_n", 3)
            params["repeat_n"] = random.choice([ra, rb])
        elif t == "alternation":
            ra = a.get("params", {}).get("alt_len", 3)
            rb = b.get("params", {}).get("alt_len", 3)
            params["alt_len"] = random.choice([ra, rb])
        else:
            ra = a.get("params", {}).get("cluster_th", 3)
            rb = b.get("params", {}).get("cluster_th", 3)
            params["cluster_th"] = random.choice([ra, rb])
        return {"id": (a["id"] + b["id"])[:8], "type": t, "params": params, "meta": {"gen": self.generation + 1}}

    def _mutate(self, s: Dict) -> Dict:
        p = s["params"]
        if s["type"] == "repeat_pattern":
            if random.random() < 0.35:
                p["repeat_n"] = max(2, int(p.get("repeat_n", 3) + random.choice([-1, 0, 1])))
        elif s["type"] == "alternation":
            if random.random() < 0.35:
                p["alt_len"] = max(2, int(p.get("alt_len", 3) + random.choice([-1, 0, 1])))
        else:
            if random.random() < 0.35:
                p["cluster_th"] = max(2, int(p.get("cluster_th", 3) + random.choice([-1, 0, 1])))
        return s

    def predict(self, colors: List[str]) -> Optional[str]:
        """Tenta prever a próxima cor usando a estratégia ativa."""
        if not self.active:
            return None
        return _predict_by_strategy(self.active["type"], self.active["params"], colors)

# Instância global do GA
_ga = StrategyGA()

# =====================================================================
#                           LÓGICA DE SINAIS
# =====================================================================

def apply_result(new_color: str):
    global active_signal
    # feedback para seu modelo Spectra
    _ai.feedback(list(history), new_color)

    if not active_signal or active_signal.get("status") != "running":
        return

    hit = (new_color == active_signal["color"]) or (active_signal["color"] == "white" and new_color == "white")
    if hit:
        active_signal["status"], active_signal["result"] = "finished", "WIN"
        metrics["wins"] += 1
        metrics["bank_result"] += active_signal["stake"] * odds_for(active_signal["color"])
    else:
        if active_signal["gale_step"] < active_signal["max_gales"]:
            active_signal["gale_step"] += 1
        else:
            active_signal["status"], active_signal["result"] = "finished", "LOSS"
            metrics["losses"] += 1
            metrics["bank_result"] -= active_signal["stake"] * (active_signal["gale_step"] + 1)

def maybe_decide_signal():
    """
    Política de decisão:
    1) Tenta estratégia ativa descoberta pelo GA (se score alto e previsão disponível).
    2) Se não houver previsão, usa seu SpectraAI como fallback (com threshold de confiança).
    """
    global active_signal
    if active_signal and active_signal.get("status") == "running":
        return

    colors = list(history)

    # === 1) evolve/avaliar GA e tentar previsão
    _ga.evaluate_and_update(colors)
    ga_pred = _ga.predict(colors)
    if ga_pred is not None and _ga.active_score >= 0.60:
        # confiança derivada do score da estratégia ativa (0.60..1.0 -> 0.60..0.95 aprox)
        conf = min(0.95, max(0.60, float(_ga.active_score)))
        active_signal = {
            "created_at": now_iso(),
            "color": ga_pred,
            "confidence": round(conf, 3),
            "probs": {"ga_score": round(_ga.active_score, 3)},
            "gale_step": 0,
            "max_gales": config["max_gales"],
            "stake": config["stake"],
            "status": "running",
            "result": None,
            "confirmed": False,
            "strategy": _ga.active,  # para debug/telemetria
            "source": "auto_strategy"
        }
        metrics["total_signals"] += 1
        return

    # === 2) fallback no SpectraAI
    feats = _feature.make(colors)
    color, conf, mix = _ai.decide(feats, colors)
    if conf < config["confidence_min"]:
        return
    active_signal = {
        "created_at": now_iso(),
        "color": color,
        "confidence": round(conf, 3),
        "probs": mix,
        "gale_step": 0,
        "max_gales": config["max_gales"],
        "stake": config["stake"],
        "status": "running",
        "result": None,
        "confirmed": False,
        "source": "spectra_ai"
    }
    metrics["total_signals"] += 1

# ---------------- Rotas ----------------
@app.get("/")
def root():
    return send_from_directory(".", "index.html")

@app.get("/api/health")
def health():
    return jsonify({"ok": True, "time": now_iso()})

@app.get("/api/state")
def state():
    wr = metrics["wins"] / max(1, metrics["wins"] + metrics["losses"])
    return jsonify({
        "ok": True,
        "history_tail": list(history)[-SHOW_LAST:],
        "counts10": recent_counts(10),
        "active_signal": active_signal,
        "metrics": {**metrics, "winrate": round(wr, 3)},
        "config": config,
        "auto_strategy": {
            "active": _ga.active,
            "active_score": round(_ga.active_score, 3) if _ga.active else None,
            "pool_size": len(_ga.pool),
            "generation": _ga.generation
        },
        "time": now_iso(),
    })

@app.post("/api/confirm")
def confirm():
    global active_signal
    if not active_signal or active_signal.get("status") != "running":
        return jsonify({"ok": False, "error": "no running signal"}), 400
    active_signal["confirmed"] = True
    active_signal["confirmed_at"] = now_iso()
    return jsonify({"ok": True, "signal": active_signal})

@app.post("/api/reset")
def reset_all():
    global history, metrics, active_signal, _ai, _ga
    history.clear()
    metrics.update({"total_signals": 0, "wins": 0, "losses": 0, "bank_result": 0.0})
    active_signal = None
    _ai = SpectraAI(feat_dim=_feat_dim, alpha=0.72, eps_start=0.15, eps_min=0.02, eps_decay=0.9992)
    _ga = StrategyGA()
    return jsonify({"ok": True})

# === endpoint que o bookmarklet chama ===
@app.post("/ingest")
def ingest():
    """
    Recebe: { history: [14, 3, 0, 9, ...], ts?, url?, src? }
    """
    data = request.get_json(force=True, silent=True) or {}
    seq = data.get("history")
    if not isinstance(seq, list) or not seq:
        return jsonify({"ok": False, "error": "expected {history:[...]}"}), 400

    added = 0
    # processa só a cauda para evitar duplicatas em massa
    for n in seq[-25:]:
        try:
            c = number_to_color(int(n))
            if c:
                history.append(c)
                apply_result(c)
                added += 1
        except Exception:
            pass

    if added:
        maybe_decide_signal()

    return jsonify({"ok": True, "added": added, "len": len(history)})

# (Opcional) endpoint para inspecionar rapidamente o melhor indivíduo
@app.get("/api/strategies/active")
def api_active_strategy():
    if not _ga.active:
        return jsonify({"ok": True, "active": None})
    return jsonify({"ok": True, "active": _ga.active, "score": round(_ga.active_score, 3)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
