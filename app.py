# app.py
import os
from collections import deque
from datetime import datetime
from typing import Deque, Dict, Optional, Any, List

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

from ia_core import SpectraAI, FeatureExtractor

app = Flask(__name__, static_url_path="", static_folder=".", template_folder=".")
CORS(app, resources={r"/api/*": {"origins": "*"}})

# -------- Config --------
HISTORY_MAX, SHOW_LAST = 2000, 60
DEFAULTS = dict(
    stake=2.0, max_gales=2, mode="hybrid",
    confluence_min_color=2, confluence_min_white=2, confidence_min=0.60,
    invert_mapping=False
)
WHITE_ODDS, COLOR_ODDS = 14.0, 1.0

# -------- Estado --------
history: Deque[str] = deque(maxlen=HISTORY_MAX)  # 'red'|'black'|'white'
metrics = {"total_signals": 0, "wins": 0, "losses": 0, "bank_result": 0.0}
config = DEFAULTS.copy()
active_signal: Optional[Dict] = None

# -------- IA --------
_feature = FeatureExtractor(K=7)
_feat_dim = len(_feature.make([]))
_ai = SpectraAI(feat_dim=_feat_dim, alpha=0.72, eps_start=0.15, eps_min=0.02, eps_decay=0.9992)

def now_iso(): return datetime.utcnow().isoformat()+"Z"

def number_to_color(n: int) -> Optional[str]:
    if n == 0: return "white"
    if config.get("invert_mapping"):
        if 1 <= n <= 7: return "black"
        if 8 <= n <= 14: return "red"
    else:
        if 1 <= n <= 7: return "red"
        if 8 <= n <= 14: return "black"
    return None

def recent_counts(n=10):
    last = list(history)[-n:]
    return {"red": last.count("red"), "black": last.count("black"), "white": last.count("white"), "n": len(last)}

def odds_for(color:str)->float: return WHITE_ODDS if color=="white" else COLOR_ODDS

# -------- Decisão/feedback --------
def maybe_decide_signal():
    global active_signal
    if active_signal and active_signal.get("status")=="running":
        return
    feats = _feature.make(list(history))
    color, conf, mix = _ai.decide(feats, list(history))
    if conf < config["confidence_min"]:
        return
    active_signal = {
        "created_at": now_iso(),
        "color": color,
        "confidence": round(conf,3),
        "probs": mix,
        "gale_step": 0,
        "max_gales": config["max_gales"],
        "stake": config["stake"],
        "status": "running",
        "result": None,
        "confirmed": False,
        "confirmed_at": None
    }
    metrics["total_signals"] += 1

def apply_result(new_color: str):
    global active_signal
    _ai.feedback(list(history), new_color)
    if not active_signal or active_signal.get("status")!="running":
        return
    hit = (new_color == active_signal["color"]) or (active_signal["color"]=="white" and new_color=="white")
    if hit:
        active_signal["status"]="finished"; active_signal["result"]="WIN"
        metrics["wins"] += 1
        metrics["bank_result"] += active_signal["stake"] * odds_for(active_signal["color"])
    else:
        if active_signal["gale_step"] < active_signal["max_gales"]:
            active_signal["gale_step"] += 1
        else:
            active_signal["status"]="finished"; active_signal["result"]="LOSS"
            metrics["losses"] += 1
            metrics["bank_result"] -= active_signal["stake"] * (active_signal["gale_step"]+1)

# -------- Rotas --------
@app.get("/")
def root(): return send_from_directory(".", "index.html")

@app.get("/api/health")
def health(): return jsonify({"ok": True, "time": now_iso()})

@app.get("/api/state")
def state():
    wr = metrics["wins"] / max(1, metrics["wins"] + metrics["losses"])
    return jsonify({
        "ok": True,
        "history_tail": list(history)[-SHOW_LAST:],
        "counts10": recent_counts(10),
        "active_signal": active_signal,
        "metrics": {**metrics, "winrate": round(wr,3)},
        "config": config,
        "ai": _ai.info(),
        "time": now_iso()
    })

@app.post("/api/config")
def set_config():
    data = request.get_json(force=True, silent=True) or {}
    # (rota mantida, mas o front não expõe controles)
    for k in ("stake","max_gales","mode","confluence_min_color","confluence_min_white","confidence_min","invert_mapping"):
        if k in data: config[k] = data[k]
    return jsonify({"ok": True, "config": config})

@app.post("/api/reset")
def reset_all():
    global history, metrics, active_signal, _ai
    history = deque(maxlen=HISTORY_MAX)
    metrics = {"total_signals": 0, "wins": 0, "losses": 0, "bank_result": 0.0}
    active_signal = None
    _ai = SpectraAI(feat_dim=_feat_dim, alpha=0.72, eps_start=0.15, eps_min=0.02, eps_decay=0.9992)
    return jsonify({"ok": True})

@app.post("/api/confirm")
def confirm_entry():
    global active_signal
    if not active_signal or active_signal.get("status")!="running":
        return jsonify({"ok": False, "error": "no running signal"}), 400
    active_signal["confirmed"] = True
    active_signal["confirmed_at"] = now_iso()
    return jsonify({"ok": True, "active_signal": active_signal})

@app.post("/api/push_round")
def push_round():
    data = request.get_json(force=True, silent=True) or {}
    result = (data.get("result") or "").lower().strip()
    if not result and "number" in data:
        try: result = number_to_color(int(data["number"])) or ""
        except: result = ""
    if result not in ("red","black","white"):
        return jsonify({"ok": False, "error":"send {result:'red|black|white'} or {number:0..14}"}), 400
    history.append(result)
    apply_result(result)
    maybe_decide_signal()
    return jsonify({"ok": True})

@app.post("/api/push_many")
def push_many():
    data = request.get_json(force=True, silent=True) or {}
    items = data.get("results") or []
    acc = 0
    for it in items:
        col = None
        if isinstance(it, str):
            s = it.lower().strip()
            col = s if s in ("red","black","white") else None
        else:
            try: col = number_to_color(int(it))
            except: col = None
        if not col: continue
        history.append(col); acc += 1
        apply_result(col)
        maybe_decide_signal()
    return jsonify({"ok": True, "accepted": acc})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
