# Spectra X — Backend (Flask)
# - IA adaptativa simples (pesos por estratégia com atualização online)
# - Confluência + Confidence Score
# - Regra: "só manda novo sinal quando terminar os gales do atual"
# - Endpoints:
#   POST /api/push_round  -> {"result":"red|black|white"}  (alimenta histórico)
#   GET  /api/state       -> estado completo (histórico, sinal ativo, métricas)
#   POST /api/config      -> ajusta config (stake, max_gales, modo, thresholds)
#   POST /api/reset       -> zera histórico e métricas

import math, time, json, statistics, random
from collections import deque, defaultdict
from datetime import datetime
from typing import Deque, Dict, List, Optional
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ========================= Config =========================
HISTORY_MAX            = 600
SHOW_LAST              = 50
DEFAULT_STAKE          = 2.00
DEFAULT_MAX_GALES      = 2
DEFAULT_MODE           = "hybrid"   # "neural" | "hybrid" | "manual"
CONFLUENCE_MIN_COLOR   = 2          # votos mínimos (red/black)
CONFLUENCE_MIN_WHITE   = 2          # votos mínimos (white)
CONFIDENCE_MIN         = 0.62       # só dispara se confiança >= 62%
EW_LEARN_RATE          = 0.12       # taxa de aprendizado dos pesos das estratégias
WHITE_ODDS             = 14.0       # payout líquido aproximado do branco
COLOR_ODDS             = 1.0        # payout líquido aproximado da cor

# ========================= Estado =========================
history: Deque[str] = deque(maxlen=HISTORY_MAX)    # valores: "red" | "black" | "white"
metrics = {
    "total_signals": 0,
    "wins": 0,
    "losses": 0,
    "bank_result": 0.0,
    "per_color": {"red":{"signals":0,"wins":0,"losses":0},
                  "black":{"signals":0,"wins":0,"losses":0},
                  "white":{"signals":0,"wins":0,"losses":0}},
}
config = {
    "stake": DEFAULT_STAKE,
    "max_gales": DEFAULT_MAX_GALES,
    "mode": DEFAULT_MODE,
    "confluence_min_color": CONFLUENCE_MIN_COLOR,
    "confluence_min_white": CONFLUENCE_MIN_WHITE,
    "confidence_min": CONFIDENCE_MIN,
}

# Sinal ativo controla gales e bloqueia novos sinais até encerrar
active_signal: Optional[Dict] = None

# Pesos das estratégias (aprendizado online)
strategy_weights: Dict[str, float] = {
    "repeat_three": 1.00,
    "anti_chop": 0.90,
    "cluster_bias": 0.90,
    "recent_white_near": 1.05,
    "long_streak_break": 0.85,
    "time_window_bias": 0.80,   # simulação de janela horária
}

# ========================= Funções Auxiliares =========================
def now_iso():
    return datetime.utcnow().isoformat() + "Z"

def softmax(scores: Dict[str, float]):
    # Estabilizado
    m = max(scores.values()) if scores else 0.0
    exps = {k: math.exp(v - m) for k, v in scores.items()}
    total = sum(exps.values()) or 1.0
    return {k: v/total for k, v in exps.items()}

def normalize_votes(votes: Dict[str, float]) -> Dict[str, float]:
    s = sum(max(0.0, x) for x in votes.values()) or 1.0
    return {k: max(0.0, v) / s for k, v in votes.items()}

def recent_counts(n=10):
    last = list(history)[-n:]
    return {
        "red":   last.count("red"),
        "black": last.count("black"),
        "white": last.count("white"),
        "n": len(last)
    }

# ========================= Estratégias =========================
def strat_repeat_three():
    """Se últimas 2 iguais, tende a repetir a 3ª."""
    if len(history) < 2: return {}
    if history[-1] == history[-2] and history[-1] in ("red","black"):
        return {history[-1]: 1.0}
    return {}

def strat_anti_chop():
    """Evita alternância ABAB -> sugere manter a última cor."""
    if len(history) < 4: return {}
    h = list(history)
    if h[-4] != h[-3] and h[-3] != h[-2] and h[-2] != h[-1] and {h[-4],h[-2]}=={"red","black"} and {h[-3],h[-1]}=={"red","black"}:
        # padrão alternante forte -> manter a última
        if h[-1] in ("red","black"):
            return {h[-1]: 1.0}
    return {}

def strat_cluster_bias():
    """Se houve cluster recente de uma cor (>=3 em 5), favorece essa cor."""
    if len(history) < 5: return {}
    last5 = list(history)[-5:]
    r = last5.count("red")
    b = last5.count("black")
    out = {}
    if r >= 3: out["red"] = 1.0
    if b >= 3: out["black"] = 1.0
    return out

def strat_recent_white_near():
    """White tende a aparecer em janelas próximas (heurística)."""
    # Se saiu white nas últimas 8, dá leve viés para white
    last8 = list(history)[-8:]
    if "white" in last8:
        return {"white": 1.0}
    return {}

def strat_long_streak_break():
    """Streak muito longa tende a quebrar (se >4, aposta na cor oposta)."""
    if not history: return {}
    last = history[-1]
    # conta streak atual
    streak = 1
    for i in range(len(history)-2, -1, -1):
        if history[i] == last: streak += 1
        else: break
    if last in ("red","black") and streak >= 5:
        return {"red" if last=="black" else "black": 1.0}
    return {}

def strat_time_window_bias():
    """Simula viés horário (ex.: certos horários favorecem mais cores).
       Para demo: usa o minuto atual para alternar sutilmente."""
    m = datetime.utcnow().minute
    if m % 10 in (0,1,2):   return {"red": 0.8}
    if m % 10 in (3,4,5):   return {"black": 0.8}
    if m % 10 in (6,7):     return {"white": 0.6}
    return {}

STRATEGY_FUNCS = {
    "repeat_three": strat_repeat_three,
    "anti_chop": strat_anti_chop,
    "cluster_bias": strat_cluster_bias,
    "recent_white_near": strat_recent_white_near,
    "long_streak_break": strat_long_streak_break,
    "time_window_bias": strat_time_window_bias,
}

def compute_votes() -> Dict[str, float]:
    """Combina votos das estratégias ponderados por pesos aprendidos."""
    raw = defaultdict(float)
    for name, func in STRATEGY_FUNCS.items():
        out = func()
        w = strategy_weights.get(name, 1.0)
        for k, v in out.items():
            raw[k] += v * w
    # normaliza para [0..1] relativo
    norm = normalize_votes(raw)
    return norm  # dict color->0..1

def decide_signal():
    """Decide se abre novo sinal (respeitando bloqueio por gales)."""
    global active_signal
    if active_signal and active_signal.get("status") == "running":
        return None  # bloqueado até encerrar gales

    votes = compute_votes()  # 0..1
    # transforma votos em "scores" de decisão
    scores = {
        "red":   votes.get("red", 0.0),
        "black": votes.get("black", 0.0),
        "white": votes.get("white", 0.0) * 1.15,  # leve bônus por payoff maior
    }
    # Confluências mínimas (em termos de votos relativos)
    conf_color = config["confluence_min_color"] / 10.0  # mapeia 2 -> 0.2 etc (heurístico)
    conf_white = config["confluence_min_white"] / 10.0

    # escolhe melhor
    best_color = max(scores, key=lambda k: scores[k])
    best_score = scores[best_color]

    # Confidence por softmax
    probs = softmax(scores)
    confidence = probs.get(best_color, 0.0)

    # Aplica thresholds
    if best_color in ("red","black"):
        if best_score < conf_color or confidence < config["confidence_min"]:
            return None
    else:
        if best_score < conf_white or confidence < config["confidence_min"]:
            return None

    # Abre sinal
    active_signal = {
        "created_at": now_iso(),
        "color": best_color,
        "confidence": round(confidence, 3),
        "scores": scores,
        "gale_step": 0,
        "max_gales": config["max_gales"],
        "stake": config["stake"],
        "status": "running",
        "result": None,
        "history_snapshot_size": len(history),
        "strategy_votes": compute_votes(),
    }
    metrics["total_signals"] += 1
    metrics["per_color"][best_color]["signals"] += 1
    return active_signal

def odds_for(color: str) -> float:
    return WHITE_ODDS if color == "white" else COLOR_ODDS

def apply_learning(win: bool, signal: Dict):
    """Ajusta pesos das estratégias proporcionalmente ao acerto/erro."""
    global strategy_weights
    # Atualiza todos os pesos levemente na direção do acerto/erro
    delta = EW_LEARN_RATE if win else -EW_LEARN_RATE
    # Estratégias que votaram mais para a cor do sinal recebem mais ajuste
    votes = signal.get("strategy_votes", {})
    for name in strategy_weights.keys():
        # Proxy: se a estratégia, via compute_votes, deu força à cor do sinal
        contrib = votes.get(signal["color"], 0.0)
        # Ruído mínimo para manter diversidade
        jitter = (random.random() - 0.5) * 0.02
        strategy_weights[name] = max(0.1, strategy_weights[name] + delta * (0.5 + contrib) + jitter)

def evaluate_active_signal(new_result: str):
    """Avalia o round recebido, avança gale ou encerra."""
    global active_signal
    s = active_signal
    if not s or s["status"] != "running":
        return

    target = s["color"]
    hit = (new_result == target) or (target in ("red","black") and new_result in ("red","black") and target == new_result)

    # WHITE é exatamente 'white'
    if target == "white":
        hit = (new_result == "white")

    if hit:
        # WIN
        s["status"] = "finished"
        s["result"] = "WIN"
        gain = s["stake"] * odds_for(target)
        metrics["wins"] += 1
        metrics["per_color"][target]["wins"] += 1
        metrics["bank_result"] += gain
        apply_learning(True, s)
    else:
        # LOSS -> tenta gale se houver
        if s["gale_step"] < s["max_gales"]:
            s["gale_step"] += 1
            # mantém status "running" até finalizar todos os gales
        else:
            s["status"] = "finished"
            s["result"] = "LOSS"
            loss_total = s["stake"] * (s["gale_step"] + 1)
            metrics["losses"] += 1
            metrics["per_color"][target]["losses"] += 1
            metrics["bank_result"] -= loss_total
            apply_learning(False, s)

# ========================= Endpoints =========================
@app.post("/api/push_round")
def push_round():
    """
    Body: {"result":"red|black|white"}
    Simula ingest da rodada (ou use para repassar o resultado real).
    Avalia sinal ativo e tenta abrir novo sinal (se possível).
    """
    data = request.get_json(force=True, silent=True) or {}
    result = (data.get("result") or "").lower().strip()
    if result not in ("red","black","white"):
        return jsonify({"ok": False, "error": "result must be red|black|white"}), 400

    history.append(result)

    # Avalia sinal vigente
    if active_signal and active_signal.get("status") == "running":
        evaluate_active_signal(result)

    # Se não há sinal rodando, tenta abrir um novo
    signal_info = None
    if not active_signal or active_signal.get("status") != "running":
        signal_info = decide_signal()

    return jsonify({
        "ok": True,
        "accepted": result,
        "active_signal": active_signal,
        "maybe_new_signal": signal_info,
        "history_tail": list(history)[-SHOW_LAST:],
        "metrics": metrics,
        "config": config
    })

@app.get("/api/state")
def get_state():
    # taxa de acerto
    wr = (metrics["wins"] / max(1, metrics["total_signals"])) if metrics["total_signals"] else 0.0
    return jsonify({
        "ok": True,
        "history_tail": list(history)[-SHOW_LAST:],
        "counts10": recent_counts(10),
        "active_signal": active_signal,
        "metrics": {**metrics, "winrate": round(wr, 3)},
        "config": config,
        "strategy_weights": strategy_weights,
        "time": now_iso()
    })

@app.post("/api/config")
def set_config():
    data = request.get_json(force=True, silent=True) or {}
    for k in ("stake","max_gales","mode","confluence_min_color","confluence_min_white","confidence_min"):
        if k in data:
            config[k] = data[k]
    return jsonify({"ok": True, "config": config})

@app.post("/api/reset")
def reset_all():
    global history, metrics, active_signal, strategy_weights
    history = deque(maxlen=HISTORY_MAX)
    active_signal = None
    metrics = {
        "total_signals": 0,
        "wins": 0,
        "losses": 0,
        "bank_result": 0.0,
        "per_color": {"red":{"signals":0,"wins":0,"losses":0},
                      "black":{"signals":0,"wins":0,"losses":0},
                      "white":{"signals":0,"wins":0,"losses":0}},
    }
    # mantém pesos, mas pode opcionalmente zerar
    return jsonify({"ok": True, "cleared": True, "strategy_weights": strategy_weights})

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)
