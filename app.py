# app.py
# -*- coding: utf-8 -*-

import json
import math
import threading
from collections import deque
from datetime import datetime
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS

app = Flask(__name__, template_folder="templates", static_folder="static")
CORS(app)

# ------------------ Estado do servidor ------------------

HISTORY_MAX = 2000
history = deque(maxlen=HISTORY_MAX)   # números 0..14 (0 = branco)
signals = deque(maxlen=500)           # log de sinais emitidos
state_lock = threading.Lock()

# modo e bot
MODE_WHITE = "WHITE"
MODE_COLORS = "COLORS"
mode_selected = MODE_COLORS
bot_active = False

# snapshot anti-duplicado
_last_snapshot = []
_snapshot_lock = threading.Lock()

last_status = "ok"
last_src = "—"

# ------------------ Helpers ------------------

def is_red(n):   return 1 <= n <= 7
def is_black(n): return 8 <= n <= 14

def color_of(n):
    if n == 0: return "W"
    return "R" if is_red(n) else "B"

def to_color_code(n):
    if n == 0: return 0
    return 2 if is_red(n) else 1

def last_k_colors(seq, k, ignore_white=True):
    out = []
    for v in reversed(seq):
        if v == 0 and ignore_white:
            continue
        out.append("R" if is_red(v) else "B")
        if len(out) >= k:
            break
    return list(reversed(out))

def count_in_last(seq, k):
    lst = last_k_colors(seq, k)
    return lst.count("R"), lst.count("B"), lst

def pct(lst, p):
    if not lst: return 0.0
    s = sorted(lst)
    k = int(max(0, min(len(s)-1, round((p/100)*(len(s)-1)))))
    return float(s[k])

def gaps_from_seq(seq):
    idx = [i for i, v in enumerate(seq) if v == 0]
    gaps = []
    for a, b in zip(idx, idx[1:]):
        gaps.append(b - a)
    gap_atual = (len(seq)-1-idx[-1]) if idx else len(seq)
    return gaps, gap_atual

def merge_snapshot_into_history(snapshot):
    """
    Adiciona apenas o que é novo. Aceita snapshot LTR/RTL.
    Não duplica nem regride.
    """
    global _last_snapshot
    try:
        snap = [int(x) for x in snapshot if isinstance(x, int) and 0 <= x <= 14]
    except Exception:
        return 0
    if not snap:
        return 0

    def ov(a, b, kmax=60):
        kmax = min(kmax, len(a), len(b))
        for k in range(kmax, 0, -1):
            if a[-k:] == b[-k:]:
                return k
        return 0

    added = 0
    with _snapshot_lock:
        if not _last_snapshot:
            _last_snapshot = list(snap)
            with state_lock:
                for n in snap:
                    history.append(n); added += 1
            return added

        a = snap
        b = snap[::-1]
        ova = ov(_last_snapshot, a)
        ovb = ov(_last_snapshot, b)
        chosen = a if ova >= ovb else b
        k = max(ova, ovb)

        if k >= len(chosen):
            _last_snapshot = list(chosen)
            return 0

        new_tail = chosen[k:]
        if new_tail:
            with state_lock:
                for n in new_tail:
                    history.append(n); added += 1
        _last_snapshot = list(chosen)
    return added

# ---- Probabilidades simples para UI (estáveis) ----

def estimate_probs(seq):
    """
    Heurística leve e estável. Não usa aleatoriedade.
    - Branco: parte de 6.67% e ganha leve boost se gaps altos.
    - Cores: frequência recente nos últimos 20 (com suavização Laplace).
    """
    base_white = 1.0 / 15.0  # ~6.67%
    pW = base_white

    gaps, gap = gaps_from_seq(seq)
    if gaps:
        mu = sum(gaps)/len(gaps)
        sd = (sum((g-mu)**2 for g in gaps)/max(1, len(gaps)-1))**0.5
        p90 = pct(gaps, 90)
        # boosts seguros e limitados
        if gap >= mu + 1.0*sd:     pW += 0.03
        if gap >= p90:             pW += 0.03
        pW = min(0.40, max(base_white, pW))

    r20, b20, _ = count_in_last(seq, 20)
    tot = r20 + b20
    if tot == 0:
        pR_raw = pB_raw = 0.5
    else:
        pR_raw = (r20 + 1) / (tot + 2)
        pB_raw = (b20 + 1) / (tot + 2)

    rem = max(0.0, 1.0 - pW)
    pR = pR_raw * rem
    pB = pB_raw * rem

    s = pW + pR + pB
    if s > 0:
        pW, pR, pB = pW/s, pR/s, pB/s

    rec = max([("W", pW), ("R", pR), ("B", pB)], key=lambda x: x[1])
    return {"W": pW, "R": pR, "B": pB, "rec": rec}

# ------------------ Flask endpoints ------------------

@app.route("/health")
def health():
    return jsonify(ok=True, time=datetime.utcnow().isoformat()+"Z")

@app.route("/")
def index():
    # UI simples e resiliente. Carrega dados de /state a cada 1s
    return render_template("index.html")

@app.route("/state")
def api_state():
    with state_lock:
        rolls = list(history)[-20:]
        mod   = mode_selected
        bot   = bot_active
    probs = estimate_probs(list(history))
    return jsonify({
        "ok": True,
        "mode": mod,
        "bot_active": bot,
        "last": rolls,
        "probs": probs,
        "status": last_status,
        "src": last_src
    })

@app.route("/mode", methods=["POST"])
def api_mode():
    global mode_selected
    try:
        data = request.get_json(force=True, silent=True) or {}
        m = str(data.get("mode", "")).upper().strip()
        if m not in (MODE_WHITE, MODE_COLORS):
            return jsonify(ok=False, error="mode must be WHITE or COLORS"), 400
        mode_selected = m
        return jsonify(ok=True, mode=mode_selected)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400

@app.route("/bot", methods=["POST"])
def api_bot():
    global bot_active
    try:
        data = request.get_json(force=True, silent=True) or {}
        act = str(data.get("action", "")).lower().strip()
        if act == "start":
            bot_active = True
        elif act == "stop":
            bot_active = False
        else:
            return jsonify(ok=False, error="action must be start or stop"), 400
        return jsonify(ok=True, bot_active=bot_active)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400

@app.route("/ingest", methods=["POST", "OPTIONS"])
def api_ingest():
    global last_status, last_src
    try:
        payload = request.get_json(force=True, silent=True) or {}
        hist = payload.get("history") or []
        src  = payload.get("src", "?")
        added = merge_snapshot_into_history(hist)
        last_src = src
        last_status = f"recebidos {len(hist)} (novos {added}) de {src} • {datetime.now().strftime('%H:%M:%S')}"
        return jsonify(ok=True, added=added, time=datetime.utcnow().isoformat()+"Z")
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400

# ------------------ Main local ------------------

if __name__ == "__main__":
    # Local: python app.py
    app.run(host="0.0.0.0", port=5000, debug=False)
