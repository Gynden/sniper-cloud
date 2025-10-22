import os, math, json, random
from collections import deque, defaultdict
from datetime import datetime
from typing import Deque, Dict, Optional, Any, List

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_url_path="", static_folder=".", template_folder=".")
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ===== Config =====
HISTORY_MAX, SHOW_LAST = 600, 50
DEFAULTS = dict(stake=2.0, max_gales=2, mode="hybrid",
                confluence_min_color=2, confluence_min_white=2, confidence_min=0.62)
WHITE_ODDS, COLOR_ODDS = 14.0, 1.0
EW_LEARN_RATE = 0.12

# ===== Estado =====
history: Deque[str] = deque(maxlen=HISTORY_MAX)    # "red"|"black"|"white"
metrics = {
    "total_signals": 0, "wins": 0, "losses": 0, "bank_result": 0.0,
    "per_color": {"red":{"signals":0,"wins":0,"losses":0},
                  "black":{"signals":0,"wins":0,"losses":0},
                  "white":{"signals":0,"wins":0,"losses":0}},
}
config = DEFAULTS.copy()
active_signal: Optional[Dict] = None

# ===== IA =====
strategy_weights: Dict[str, float] = {
    "repeat_three": 1.00, "anti_chop": 0.90, "cluster_bias": 0.90,
    "recent_white_near": 1.05, "long_streak_break": 0.85, "time_window_bias": 0.80,
}

def now_iso(): return datetime.utcnow().isoformat()+"Z"

def softmax(scores):
    if not scores: return {}
    m = max(scores.values()); exps = {k: math.exp(v-m) for k,v in scores.items()}
    tot = sum(exps.values()) or 1.0
    return {k: v/tot for k,v in exps.items()}

def normalize_votes(votes):
    s = sum(max(0.0, x) for x in votes.values()) or 1.0
    return {k: max(0.0, v)/s for k, v in votes.items()}

def recent_counts(n=10):
    last = list(history)[-n:]
    return {"red": last.count("red"), "black": last.count("black"), "white": last.count("white"), "n": len(last)}

def strat_repeat_three():
    if len(history) < 2: return {}
    if history[-1]==history[-2] and history[-1] in ("red","black"): return {history[-1]:1.0}
    return {}
def strat_anti_chop():
    if len(history) < 4: return {}
    h=list(history)
    if h[-4]!=h[-3] and h[-3]!=h[-2] and h[-2]!=h[-1] and {h[-4],h[-2]}=={"red","black"} and {h[-3],h[-1]}=={"red","black"}:
        if h[-1] in ("red","black"): return {h[-1]:1.0}
    return {}
def strat_cluster_bias():
    if len(history) < 5: return {}
    last5=list(history)[-5:]; out={}
    if last5.count("red")>=3: out["red"]=1.0
    if last5.count("black")>=3: out["black"]=1.0
    return out
def strat_recent_white_near():
    if "white" in list(history)[-8:]: return {"white":1.0}
    return {}
def strat_long_streak_break():
    if not history: return {}
    last=history[-1]; streak=1
    for i in range(len(history)-2,-1,-1):
        if history[i]==last: streak+=1
        else: break
    if last in ("red","black") and streak>=5:
        return {"red" if last=="black" else "black":1.0}
    return {}
def strat_time_window_bias():
    m = datetime.utcnow().minute
    if m%10 in (0,1,2): return {"red":0.8}
    if m%10 in (3,4,5): return {"black":0.8}
    if m%10 in (6,7):   return {"white":0.6}
    return {}

STRATEGY_FUNCS = {
    "repeat_three": strat_repeat_three, "anti_chop": strat_anti_chop,
    "cluster_bias": strat_cluster_bias, "recent_white_near": strat_recent_white_near,
    "long_streak_break": strat_long_streak_break, "time_window_bias": strat_time_window_bias,
}

def compute_votes():
    raw=defaultdict(float)
    for name,func in STRATEGY_FUNCS.items():
        out=func(); w=strategy_weights.get(name,1.0)
        for k,v in out.items(): raw[k]+=v*w
    return normalize_votes(raw)

def odds_for(color): return WHITE_ODDS if color=="white" else COLOR_ODDS

def apply_learning(win, signal):
    global strategy_weights
    delta = EW_LEARN_RATE if win else -EW_LEARN_RATE
    contrib = signal.get("strategy_votes", {}).get(signal["color"], 0.0)
    for name in strategy_weights.keys():
        jitter = (random.random()-0.5)*0.02
        strategy_weights[name] = max(0.1, strategy_weights[name] + delta*(0.5+contrib) + jitter)

def decide_signal():
    global active_signal
    if active_signal and active_signal.get("status")=="running": return None
    votes = compute_votes()
    scores = {"red":votes.get("red",0.0), "black":votes.get("black",0.0), "white":votes.get("white",0.0)*1.15}
    conf_color = config["confluence_min_color"]/10.0
    conf_white = config["confluence_min_white"]/10.0
    best = max(scores, key=lambda k:scores[k]); score=scores[best]
    conf = softmax(scores).get(best,0.0)
    if (best in ("red","black") and (score<conf_color or conf<config["confidence_min"])) or \
       (best=="white" and (score<conf_white or conf<config["confidence_min"])):
        return None
    active_signal = {
        "created_at": now_iso(), "color": best, "confidence": round(conf,3),
        "scores": scores, "gale_step": 0, "max_gales": config["max_gales"],
        "stake": config["stake"], "status": "running", "result": None,
        "history_snapshot_size": len(history), "strategy_votes": compute_votes(),
    }
    metrics["total_signals"] += 1
    metrics["per_color"][best]["signals"] += 1
    return active_signal

def evaluate_active_signal(new_result):
    global active_signal
    s=active_signal
    if not s or s["status"]!="running": return
    hit = (new_result == s["color"])
    if s["color"]=="white": hit = (new_result=="white")
    if hit:
        s["status"]="finished"; s["result"]="WIN"
        metrics["wins"]+=1; metrics["per_color"][s["color"]]["wins"]+=1
        metrics["bank_result"] += s["stake"]*odds_for(s["color"])
        apply_learning(True, s)
    else:
        if s["gale_step"] < s["max_gales"]:
            s["gale_step"] += 1
        else:
            s["status"]="finished"; s["result"]="LOSS"
            metrics["losses"]+=1; metrics["per_color"][s["color"]]["losses"]+=1
            metrics["bank_result"] -= s["stake"]*(s["gale_step"]+1)
            apply_learning(False, s)

# ===== util: mapeia número -> cor (padrão da Blaze Double)
# Por padrão: 0=white, 1–7=red, 8–14=black (se seu lobby for invertido, troque RED_RANGE/BLACK_RANGE)
RED_RANGE   = set(range(1,8))    # 1..7
BLACK_RANGE = set(range(8,15))   # 8..14
def number_to_color(n: int) -> Optional[str]:
    if n == 0: return "white"
    if n in RED_RANGE: return "red"
    if n in BLACK_RANGE: return "black"
    return None

# ===== rotas =====
@app.get("/")
def root(): return send_from_directory(".", "index.html")

@app.get("/api/health")
def health(): return jsonify({"ok": True, "time": now_iso()})

@app.get("/api/state")
def get_state():
    wr = (metrics["wins"]/max(1, metrics["total_signals"])) if metrics["total_signals"] else 0.0
    return jsonify({
        "ok": True,
        "history_tail": list(history)[-SHOW_LAST:],
        "counts10": recent_counts(10),
        "active_signal": active_signal,
        "metrics": {**metrics, "winrate": round(wr,3)},
        "config": config,
        "strategy_weights": strategy_weights,
        "time": now_iso()
    })

@app.post("/api/push_round")
def push_round():
    data = request.get_json(force=True, silent=True) or {}
    # aceita: {"result":"red|black|white"} OU {"number": 0..14}
    result = (data.get("result") or "").lower().strip()
    if not result:
        if "number" in data:
            try:
                n = int(data["number"])
                result = number_to_color(n) or ""
            except: result = ""
    if result not in ("red","black","white"):
        return jsonify({"ok": False, "error": "send {result:'red|black|white'} or {number:0..14}"}), 400

    history.append(result)
    if active_signal and active_signal.get("status")=="running":
        evaluate_active_signal(result)
    if not active_signal or active_signal.get("status")!="running":
        decide_signal()
    return jsonify({"ok": True})

@app.post("/api/push_many")
def push_many():
    data = request.get_json(force=True, silent=True) or {}
    items: List[Any] = data.get("results") or []
    accepted = 0
    for r in items:
        color = None
        if isinstance(r, str):
            rr = r.lower().strip()
            color = rr if rr in ("red","black","white") else None
        else:
            try:
                color = number_to_color(int(r))
            except:
                color = None
        if not color: continue
        history.append(color); accepted += 1
        if active_signal and active_signal.get("status")=="running":
            evaluate_active_signal(color)
        if not active_signal or active_signal.get("status")!="running":
            decide_signal()
    return jsonify({"ok": True, "accepted": accepted})

@app.post("/api/config")
def set_config():
    data = request.get_json(force=True, silent=True) or {}
    for k in ("stake","max_gales","mode","confluence_min_color","confluence_min_white","confidence_min"):
        if k in data: config[k] = data[k]
    return jsonify({"ok": True, "config": config})

@app.post("/api/reset")
def reset_all():
    global history, metrics, active_signal
    history = deque(maxlen=HISTORY_MAX)
    active_signal = None
    metrics.update({
        "total_signals": 0, "wins": 0, "losses": 0, "bank_result": 0.0,
        "per_color": {"red":{"signals":0,"wins":0,"losses":0},
                      "black":{"signals":0,"wins":0,"losses":0},
                      "white":{"signals":0,"wins":0,"losses":0}}
    })
    return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
