# app.py — Spectra X (GA + Spectra) com Market Guard (pausa automática em mercado ruim)
import os, random, uuid, math
from collections import deque, defaultdict
from datetime import datetime
from typing import Deque, Dict, Optional, List, Tuple
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

# IA base (seus arquivos)
from ia_core import SpectraAI, FeatureExtractor

app = Flask(__name__, static_url_path="", static_folder=".", template_folder=".")
CORS(app, resources={r"/*": {"origins": "*"}})

# ---------------- Config ----------------
HISTORY_MAX, SHOW_LAST = 2000, 60
WHITE_ODDS, COLOR_ODDS = 14.0, 1.0

config = dict(
    stake=2.0,
    max_gales=2,
    confidence_min=0.60,    # Spectra mínimo
    invert_mapping=False,
    entry_mode="colors",    # "colors" | "white"

    # Market Guard
    market_guard_enabled=True,
    guard_window=80,        # quantos últimos giros avaliar
    guard_min_len=30,       # mínimo de dados para decidir
    guard_score_min=0.58,   # abaixo disso = instável (pausa)
    guard_cooldown=20,      # giros de “resfriamento” antes de reavaliar
)

# ---------------- Estado ----------------
history: Deque[Dict] = deque(maxlen=HISTORY_MAX)   # [{n, color}]
metrics = {"total_signals": 0, "wins": 0, "losses": 0, "bank_result": 0.0}

active_signal: Optional[Dict] = None
_last_finished: Optional[Dict] = None     # entregue uma vez via /api/state
silence_count = 0                         # giros desde o último sinal
market_pause_left = 0                     # giros restantes em pausa
_market_cache = {"status": "unknown", "score": None, "reason": "—"}  # espelho p/ UI

# ---------------- IA ----------------
_feature = FeatureExtractor(K=7)
_feat_dim = len(_feature)
_ai = SpectraAI(feat_dim=_feat_dim, alpha=0.72, eps_start=0.15, eps_min=0.02, eps_decay=0.9992)

# ---------------- Utils ----------------
def now_iso() -> str: return datetime.utcnow().isoformat() + "Z"
def odds_for(color: str) -> float: return WHITE_ODDS if color == "white" else COLOR_ODDS

def number_to_color(n: int) -> Optional[str]:
    if n == 0: return "white"
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
#                     Market Guard (detecção de instabilidade)
# =====================================================================
def _market_score(seq: List[str]) -> Tuple[float, str]:
    """
    Mede 'previsibilidade' local (0..1). Maior é melhor (mais estável).
    Combina:
      - Markov 1-step (transições mais prováveis)
      - taxa de brancos (penaliza em modo cores)
      - desequilíbrio de cores (muito 50/50 + alternância = baixa)
    """
    W = min(config["guard_window"], len(seq))
    if W < config["guard_min_len"]:
        return 0.0, "histórico insuficiente"

    tail = seq[-W:]

    # ignora white para transições de cores
    no_white = [c for c in tail if c in ("red", "black")]
    if len(no_white) < max(10, int(0.4*W)):
        return 0.45, "muitas ocorrências de branco"

    # Markov 1-step
    trans = {"red": defaultdict(int), "black": defaultdict(int)}
    prev = None
    for c in tail:
        if prev in ("red", "black"):
            trans[prev][c] += 1
        prev = c
    def best_prob(trow):
        s = sum(trow.values()) or 1
        return max((v/s for v in trow.values()), default=0.5)
    p_rb = best_prob(trans["red"])
    p_br = best_prob(trans["black"])
    markov = (p_rb + p_br) / 2.0  # 0.5 ~ aleatório; 0.7+ já razoável

    # alternância
    alt_cnt = 0; comp_cnt = 0
    for i in range(1, len(no_white)):
        if no_white[i] != no_white[i-1]: alt_cnt += 1
        comp_cnt += 1
    alternation = (alt_cnt/comp_cnt) if comp_cnt else 0.5
    alt_penalty = max(0.0, alternation - 0.55) * 0.6  # alternância >55% penaliza

    # white rate (penaliza se modo cores)
    white_rate = sum(1 for c in tail if c == "white") / W
    white_penalty = white_rate * (0.7 if config["entry_mode"] == "colors" else 0.0)

    # balance das cores
    r, b = no_white.count("red"), no_white.count("black")
    balance = abs(r - b) / max(1, r + b)  # 0 equilibrado, 1 dominance
    balance_bonus = min(0.10, balance * 0.15)  # leve bônus se há tendência

    score = max(0.0, min(1.0, markov - alt_penalty - white_penalty + balance_bonus))
    # motivo principal
    if score < 0.5 and white_rate > 0.12:
        reason = f"excesso de branco ({white_rate:.0%})"
    elif score < 0.55 and alternation > 0.6:
        reason = f"muita alternância ({alternation:.0%})"
    elif score < 0.55:
        reason = "padrões fracos"
    else:
        reason = "estável"
    return score, reason

def _update_market_cache(seq: List[str]):
    global _market_cache
    score, reason = _market_score(seq)
    status = "stable" if score >= config["guard_score_min"] else "unstable"
    _market_cache = {"status": status, "score": round(score, 3), "reason": reason}

# =====================================================================
#                Auto-Strategy (GA simples) + Spectra
# =====================================================================
TEMPLATE_TYPES = ["repeat_pattern", "alternation", "cluster_count"]

def _predict_by_strategy(stype: str, params: Dict, seq_colors: List[str]) -> Optional[str]:
    if not seq_colors:
        return None
    if stype == "repeat_pattern":
        r = int(params.get("repeat_n", 3))
        if len(seq_colors) < r: return None
        last = seq_colors[-r:]
        return last[-1] if len(set(last)) == 1 else None
    if stype == "alternation":
        L = int(params.get("alt_len", 3))
        if len(seq_colors) < L + 1: return None
        tail = seq_colors[-(L + 1):]
        if any(tail[i] == tail[i-1] for i in range(1,len(tail))): return None
        return "red" if tail[-1] == "black" else ("black" if tail[-1] == "red" else None)
    if stype == "cluster_count":
        t = int(params.get("cluster_th", 3))
        if len(seq_colors) < t: return None
        tail = seq_colors[-t:]
        if len(set(tail)) == 1:
            c = tail[-1]
            return "black" if c == "red" else ("red" if c == "black" else None)
        return None
    return None

def _random_strategy(gen: int = 0) -> Dict:
    t = random.choice(TEMPLATE_TYPES)
    if t == "repeat_pattern": params = {"repeat_n": random.randint(2, 5)}
    elif t == "alternation": params = {"alt_len": random.randint(2, 6)}
    else: params = {"cluster_th": random.randint(2, 6)}
    return {"id": str(uuid.uuid4())[:8], "type": t, "params": params, "meta": {"gen": gen}}

def _backtest_candidate(candidate: Dict, seq_colors: List[str], horizon: int = 300) -> Dict:
    data = seq_colors[-horizon:] if horizon and len(seq_colors) > horizon else seq_colors[:]
    if len(data) < 20: return {"id": candidate["id"], "score": 0.0, "winrate": 0.0, "roi": 0}
    wins = losses = 0
    for i in range(3, len(data)):
        pred = _predict_by_strategy(candidate["type"], candidate["params"], data[:i])
        if pred is None: continue
        wins += 1 if pred == data[i] else 0
        losses += 1 if pred != data[i] else 0
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
        if len(seq_colors) < 30:  # menos exigente para responder mais cedo
            return
        while len(self.pool) < 30:
            self.pool.append(_random_strategy(self.generation))
        scored = []
        for cand in self.pool:
            res = _backtest_candidate(cand, seq_colors, horizon=300)
            cand["meta"]["fitness"] = res["score"]
            scored.append((res["score"], cand))
        scored.sort(key=lambda x: x[0], reverse=True)
        self.pool = [c for _, c in scored[:60]]
        parents = self.pool[:10]; children=[]
        for _ in range(10):
            a,b = random.choice(parents), random.choice(parents)
            t = random.choice([a["type"], b["type"]])
            if t == "repeat_pattern":
                p = {"repeat_n": random.choice([a["params"].get("repeat_n",3), b["params"].get("repeat_n",3)])}
                if random.random()<0.35: p["repeat_n"]=max(2,p["repeat_n"]+random.choice([-1,0,1]))
            elif t == "alternation":
                p = {"alt_len": random.choice([a["params"].get("alt_len",3), b["params"].get("alt_len",3)])}
                if random.random()<0.35: p["alt_len"]=max(2,p["alt_len"]+random.choice([-1,0,1]))
            else:
                p = {"cluster_th": random.choice([a["params"].get("cluster_th",3), b["params"].get("cluster_th",3)])}
                if random.random()<0.35: p["cluster_th"]=max(2,p["cluster_th"]+random.choice([-1,0,1]))
            children.append({"id": (a["id"]+b["id"])[:8], "type": t, "params": p, "meta": {"gen": self.generation+1}})
        self.pool.extend(children); self.generation += 1
        best = self.pool[0]; best_score = best["meta"].get("fitness", 0.0)
        if best_score >= 0.55 and best_score >= self.active_score:
            self.active = {k: v for k, v in best.items() if k != "meta"}
            self.active_score = best_score

    def predict(self, seq_colors: List[str]) -> Optional[str]:
        if not self.active: return None
        return _predict_by_strategy(self.active["type"], self.active["params"], seq_colors)

_ga = StrategyGA()

# =====================================================================
#                           LÓGICA DE SINAIS
# =====================================================================
def _pass_mode_filter(color: str) -> bool:
    mode = config.get("entry_mode", "colors")
    if mode == "white":  return color == "white"
    if mode == "colors": return color in ("red","black")
    return True

def _finish_signal(result: str):
    global active_signal, _last_finished, metrics
    if not active_signal: return
    active_signal["status"], active_signal["result"] = "finished", result
    if result == "WIN":
        metrics["wins"] += 1
        metrics["bank_result"] += active_signal["stake"] * odds_for(active_signal["color"])
    else:
        metrics["losses"] += 1
        metrics["bank_result"] -= active_signal["stake"] * (active_signal["gale_step"] + 1)
    _last_finished = dict(active_signal)
    active_signal = None  # zera painel sempre ao terminar

def apply_result(new_color: str):
    """Atualiza o resultado da operação em andamento (sem confirmar)."""
    global active_signal, silence_count
    _ai.feedback(colors_only(), new_color)
    if not active_signal or active_signal.get("status") != "running":
        return
    if new_color == active_signal["color"] or (active_signal["color"] == "white" and new_color == "white"):
        _finish_signal("WIN"); return
    if active_signal["gale_step"] < active_signal["max_gales"]:
        active_signal["gale_step"] += 1
    else:
        _finish_signal("LOSS")

def _maybe_pause_market(seq: List[str]) -> bool:
    """
    Retorna True se deve PAUSAR (não emitir sinais agora).
    Controla cooldown e status em cache.
    """
    global market_pause_left
    if not config.get("market_guard_enabled", True):
        _update_market_cache(seq)
        return False

    if market_pause_left > 0:
        market_pause_left -= 1
        _update_market_cache(seq)
        _market_cache["status"] = "unstable"
        _market_cache["reason"] = f"{_market_cache['reason']} • aguardando estabilizar"
        return True

    # reavalia condição de mercado
    _update_market_cache(seq)
    if _market_cache["status"] == "unstable":
        market_pause_left = config["guard_cooldown"]
        return True
    return False

def maybe_decide_signal():
    """
    Emite novo sinal se:
      - Não há um sinal em execução
      - Market Guard permitir
      - Alguma das IAs achar uma oportunidade
    """
    global active_signal, silence_count
    if active_signal and active_signal.get("status") == "running":
        return

    seq_colors = colors_only()

    # Market guard (pausar quando instável)
    if _maybe_pause_market(seq_colors):
        return

    # === Auto-Strategy (GA)
    _ga.evaluate_and_update(seq_colors)
    ga_pred = _ga.predict(seq_colors)
    if ga_pred is not None and _ga.active_score >= 0.55 and _pass_mode_filter(ga_pred):
        conf = min(0.95, max(0.60, float(_ga.active_score)))
        active_signal = {
            "created_at": now_iso(),
            "color": ga_pred,
            "confidence": round(conf, 3),
            "probs": {"ga_score": round(_ga.active_score, 3)},
            "gale_step": 0, "max_gales": config["max_gales"], "stake": config["stake"],
            "status": "running", "result": None, "source": "auto_strategy", "strategy": _ga.active
        }
        metrics["total_signals"] += 1
        silence_count = 0
        return

    # === Spectra fallback (com mínimo padrão)
    feats = _feature.make(seq_colors)
    color, conf, mix = _ai.decide(feats, seq_colors)
    if conf >= config["confidence_min"] and _pass_mode_filter(color):
        active_signal = {
            "created_at": now_iso(),
            "color": color,
            "confidence": round(conf, 3),
            "probs": mix,
            "gale_step": 0, "max_gales": config["max_gales"], "stake": config["stake"],
            "status": "running", "result": None, "source": "spectra_ai"
        }
        metrics["total_signals"] += 1
        silence_count = 0

# ---------------- Rotas ----------------
@app.get("/")
def root(): return send_from_directory(".", "index.html")

@app.get("/api/health")
def health(): return jsonify({"ok": True, "time": now_iso()})

@app.get("/api/config")
def read_config(): return jsonify({"ok": True, "config": config})

@app.get("/api/state")
def state():
    global _last_finished
    wr = metrics["wins"] / max(1, metrics["wins"] + metrics["losses"])
    payload = {
        "ok": True,
        "history_tail": list(history)[-SHOW_LAST:],
        "counts10": recent_counts(10),
        "active_signal": active_signal,          # None quando terminou
        "last_finished": _last_finished,         # operação concluída mais recente (1x)
        "metrics": {**metrics, "winrate": round(wr, 3)},
        "config": config,
        "market": _market_cache,                 # <-- status do mercado
        "auto_strategy": {
            "active": _ga.active,
            "active_score": round(_ga.active_score, 3) if _ga.active else None,
            "pool_size": len(_ga.pool),
            "generation": _ga.generation
        },
        "time": now_iso(),
    }
    _last_finished = None
    return jsonify(payload)

@app.post("/api/reset")
def reset_all():
    global history, metrics, active_signal, _ai, _ga, _last_finished, silence_count, market_pause_left
    history.clear()
    metrics.update({"total_signals": 0, "wins": 0, "losses": 0, "bank_result": 0.0})
    active_signal = None
    _last_finished = None
    silence_count = 0
    market_pause_left = 0
    _update_market_cache([])
    _ai = SpectraAI(feat_dim=_feat_dim, alpha=0.72, eps_start=0.15, eps_min=0.02, eps_decay=0.9992)
    _ga = StrategyGA()
    return jsonify({"ok": True})

@app.post("/api/config")
def update_config():
    payload = request.get_json(force=True, silent=True) or {}
    allowed = {
        "stake", "max_gales", "confidence_min", "invert_mapping", "entry_mode",
        "market_guard_enabled", "guard_window", "guard_min_len", "guard_score_min", "guard_cooldown",
    }
    for k, v in payload.items():
        if k in allowed:
            config[k] = v
    return jsonify({"ok": True, "config": config})

# === endpoint do bookmarklet ===
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
                apply_result(c)
                added += 1
        except Exception:
            pass

    if added:
        global silence_count
        silence_count += added
        maybe_decide_signal()

    return jsonify({"ok": True, "added": added, "len": len(history)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
