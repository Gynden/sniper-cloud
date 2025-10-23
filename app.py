# app.py — Spectra X (compatível com /ingest do bookmarklet) + Auto-Strategy (GA)
# Modo de operação SIMPLIFICADO: "colors" (padrão) e "white"

import os, random, uuid
from collections import deque
from datetime import datetime
from typing import Deque, Dict, Optional, List, Tuple
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

# IA base do projeto
from ia_core import SpectraAI, FeatureExtractor

app = Flask(__name__, static_url_path="", static_folder=".", template_folder=".")
CORS(app, resources={r"/*": {"origins": "*"}})

# ---------------- Config ----------------
HISTORY_MAX, SHOW_LAST = 2000, 60
WHITE_ODDS, COLOR_ODDS = 14.0, 1.0

# entry_mode: "colors" (padrão) | "white"
config = dict(
    stake=2.0, max_gales=2, confidence_min=0.60, invert_mapping=False,
    entry_mode="colors",
)

# Histórico: objetos {n, color}
history: Deque[Dict] = deque(maxlen=HISTORY_MAX)
metrics = {"total_signals": 0, "wins": 0, "losses": 0, "bank_result": 0.0}
active_signal: Optional[Dict] = None

# ---------------- IA (Spectra) ----------------
_feature = FeatureExtractor(K=7)
_feat_dim = len(_feature)
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

def colors_only() -> List[str]:
    return [it["color"] for it in list(history)]

def recent_counts(n=10):
    last = list(history)[-n:]
    reds = sum(1 for x in last if x["color"] == "red")
    blacks = sum(1 for x in last if x["color"] == "black")
    whites = sum(1 for x in last if x["color"] == "white")
    return {"red": reds, "black": blacks, "white": whites, "n": len(last)}

# =====================================================================
#                AUTO-STRATEGY (GERAR → BACKTESTAR → PROMOVER)
# =====================================================================

TEMPLATE_TYPES = ["repeat_pattern", "alternation", "cluster_count"]

def _predict_by_strategy(stype: str, params: Dict, seq_colors: List[str]) -> Optional[str]:
    if not seq_colors:
        return None
    if stype == "repeat_pattern":
        repeat_n = int(params.get("repeat_n", 3))
        if len(seq_colors) < repeat_n: return None
        last = seq_colors[-repeat_n:]
        if len(set(last)) == 1:
            return last[-1]
        return None
    if stype == "alternation":
        alt_len = int(params.get("alt_len", 3))
        if len(seq_colors) < alt_len + 1: return None
        tail = seq_colors[-(alt_len + 1):]
        ok = True
        for i in range(1, len(tail)):
            if tail[i] == tail[i-1]:
                ok = False; break
        if ok:
            last = tail[-1]
            return "red" if last == "black" else ("black" if last == "red" else None)
        return None
    if stype == "cluster_count":
        cluster_th = int(params.get("cluster_th", 3))
        if len(seq_colors) < cluster_th: return None
        tail = seq_colors[-cluster_th:]
        if len(set(tail)) == 1:
            c = tail[-1]
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

def _backtest_candidate(candidate: Dict, seq_colors: List[str], horizon: int = 300) -> Dict:
    data = seq_colors[-horizon:] if horizon and len(seq_colors) > horizon else seq_colors[:]
    if len(data) < 20:
        return {"id": candidate["id"], "score": 0.0, "winrate": 0.0, "roi": 0}

    wins = losses = 0
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
    roi = wins - losses
    score = round(winrate * 0.7 + max(0, roi / total) * 0.3, 4)
    return {"id": candidate["id"], "score": score, "winrate": round(winrate, 3), "roi": roi}

class StrategyGA:
    def __init__(self):
        self.pool: List[Dict] = []
        self.generation: int = 0
        self.active: Optional[Dict] = None
        self.active_score: float = 0.0

    def evaluate_and_update(self, seq_colors: List[str]):
        if len(seq_colors) < 60:
            return

        target_pool = 30
        while len(self.pool) < target_pool:
            self.pool.append(_random_strategy(self.generation))

        scored: List[Tuple[float, Dict]] = []
        for cand in self.pool:
            res = _backtest_candidate(cand, seq_colors, horizon=300)
            cand["meta"]["fitness"] = res["score"]
            cand["meta"]["winrate"] = res["winrate"]
            cand["meta"]["roi"] = res["roi"]
            scored.append((res["score"], cand))

        scored.sort(key=lambda x: x[0], reverse=True)
        self.pool = [c for _, c in scored[:60]]

        parents = self.pool[:10]
        children = []
        for _ in range(10):
            a = random.choice(parents)
            b = random.choice(parents)
            t = random.choice([a["type"], b["type"]])
            if t == "repeat_pattern":
                p = {"repeat_n": random.choice([a["params"].get("repeat_n",3), b["params"].get("repeat_n",3)])}
            elif t == "alternation":
                p = {"alt_len": random.choice([a["params"].get("alt_len",3), b["params"].get("alt_len",3)])}
            else:
                p = {"cluster_th": random.choice([a["params"].get("cluster_th",3), b["params"].get("cluster_th",3)])}
            child = {"id": (a["id"]+b["id"])[:8], "type": t, "params": p, "meta": {"gen": self.generation+1}}
            # mutação leve
            if t == "repeat_pattern" and random.random() < 0.35:
                child["params"]["repeat_n"] = max(2, int(child["params"]["repeat_n"] + random.choice([-1,0,1])))
            if t == "alternation" and random.random() < 0.35:
                child["params"]["alt_len"] = max(2, int(child["params"]["alt_len"] + random.choice([-1,0,1])))
            if t == "cluster_count" and random.random() < 0.35:
                child["params"]["cluster_th"] = max(2, int(child["params"]["cluster_th"] + random.choice([-1,0,1])))
            children.append(child)
        self.pool.extend(children)
        self.generation += 1

        best = self.pool[0]
        best_score = best["meta"].get("fitness", 0.0)
        if best_score >= 0.60 and best_score >= self.active_score:
            self.active = {k: v for k, v in best.items() if k != "meta"}
            self.active_score = best_score

    def predict(self, seq_colors: List[str]) -> Optional[str]:
        if not self.active:
            return None
        return _predict_by_strategy(self.active["type"], self.active["params"], seq_colors)

_ga = StrategyGA()

# =====================================================================
#                           LÓGICA DE SINAIS
# =====================================================================

def _pass_mode_filter(color: str) -> bool:
    """Filtra conforme o modo selecionado pelo usuário: 'colors' ou 'white'."""
    mode = config.get("entry_mode", "colors")
    if mode == "white":
        return color == "white"
    if mode == "colors":
        return color in ("red", "black")
    return True

def apply_result(new_color: str):
    """Conta resultado APENAS após confirmação do usuário."""
    global active_signal
    _ai.feedback(colors_only(), new_color)

    if not active_signal or active_signal.get("status") != "running":
        return
    if not active_signal.get("confirmed", False):
        return  # ainda não confirmou -> não conta

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
    1) Só decide se não há sinal em execução.
    2) Tenta GA; se passar no modo, usa.
    3) Fallback Spectra com threshold e filtro do modo.
    """
    global active_signal
    if active_signal and active_signal.get("status") == "running":
        return

    seq_colors = colors_only()

    # GA
    _ga.evaluate_and_update(seq_colors)
    ga_pred = _ga.predict(seq_colors)
    if ga_pred is not None and _ga.active_score >= 0.60 and _pass_mode_filter(ga_pred):
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
            "strategy": _ga.active,
            "source": "auto_strategy"
        }
        metrics["total_signals"] += 1
        return

    # Spectra fallback
    feats = _feature.make(seq_colors)
    color, conf, mix = _ai.decide(feats, seq_colors)
    if conf >= config["confidence_min"] and _pass_mode_filter(color):
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

@app.get("/api/config")
def read_config():
    return jsonify({"ok": True, "config": config})

@app.get("/api/state")
def state():
    wr = metrics["wins"] / max(1, metrics["wins"] + metrics["losses"])
    return jsonify({
        "ok": True,
        "history_tail": list(history)[-SHOW_LAST:],   # [{n, color}, ...]
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

# Config (mudar entry_mode etc.)
@app.post("/api/config")
def update_config():
    payload = request.get_json(force=True, silent=True) or {}
    allowed = {"stake", "max_gales", "confidence_min", "invert_mapping", "entry_mode"}
    for k, v in payload.items():
        if k in allowed:
            config[k] = v
    return jsonify({"ok": True, "config": config})

# endpoint do bookmarklet
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
    for n in seq[-25:]:
        try:
            n_int = int(n)
            c = number_to_color(n_int)
            if c is not None:
                history.append({"n": n_int, "color": c})
                apply_result(c)  # só conta após confirmar
                added += 1
        except Exception:
            pass

    if added:
        maybe_decide_signal()

    return jsonify({"ok": True, "added": added, "len": len(history)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
