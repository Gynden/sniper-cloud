# app.py — Spectra X (compatível com /ingest do bookmarklet)
import os
from collections import deque
from datetime import datetime
from typing import Deque, Dict, Optional
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

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

# ---------------- IA ----------------
_feature = FeatureExtractor(K=7)
_feat_dim = len(_feature.make([]))
_ai = SpectraAI(feat_dim=_feat_dim, alpha=0.72, eps_start=0.15, eps_min=0.02, eps_decay=0.9992)

# ---------------- Utils ----------------
def now_iso(): return datetime.utcnow().isoformat() + "Z"
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

def recent_counts(n=10):
    last = list(history)[-n:]
    return {"red": last.count("red"), "black": last.count("black"),
            "white": last.count("white"), "n": len(last)}

def apply_result(new_color: str):
    global active_signal
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
    global active_signal
    if active_signal and active_signal.get("status") == "running":
        return
    feats = _feature.make(list(history))
    color, conf, mix = _ai.decide(feats, list(history))
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
        "confirmed": False
    }
    metrics["total_signals"] += 1

# ---------------- Rotas ----------------
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
        "metrics": {**metrics, "winrate": round(wr, 3)},
        "config": config,
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
    global history, metrics, active_signal, _ai
    history.clear()
    metrics.update({"total_signals": 0, "wins": 0, "losses": 0, "bank_result": 0.0})
    active_signal = None
    _ai = SpectraAI(feat_dim=_feat_dim, alpha=0.72, eps_start=0.15, eps_min=0.02, eps_decay=0.9992)
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
        return jsonify({"ok": False, "error": "expected {history:[...]}"})
    added = 0
    for n in seq[-25:]:  # processa só a cauda para evitar duplicatas em massa
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
