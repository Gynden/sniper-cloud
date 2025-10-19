import math, time, json
from collections import deque
from datetime import datetime
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ========================= Estado em memória =========================
HISTORY_MAX = 600
history = deque(maxlen=HISTORY_MAX)      # números 0..14 (0 = branco)
signals = deque(maxlen=200)              # sinais fechados/abertos (log leve)
last_snapshot = []                       # último snapshot para merge sem duplicar

# Controle do bot
bot_on = False
mode_selected = "CORES"                  # "BRANCO" ou "CORES"

# Gales: apenas CORES
GALE_STEPS = [1, 2]                      # G0, G1
MAX_GALES  = 1

open_trade = None                        # trade aberto
cool_white = 0
cool_color = 0

# ========================= Helpers gerais =========================
def is_red(n):   return 1 <= n <= 7
def is_black(n): return 8 <= n <= 14
def color_code(n):
    if n == 0: return "W"
    return "R" if is_red(n) else "B"

def now_hhmmss():
    return datetime.now().strftime("%H:%M:%S")

def overlap(a, b, kmax=60):
    kmax = min(kmax, len(a), len(b))
    for k in range(kmax, 0, -1):
        if a[-k:] == b[-k:]:
            return k
    return 0

def merge_snapshot(snapshot):
    """Adiciona somente itens novos do snapshot (detecta direção)."""
    global last_snapshot
    if not snapshot: return 0
    snap = [int(x) for x in snapshot if isinstance(x, int) and 0 <= x <= 14]
    if not snap: return 0

    added = 0
    if not last_snapshot:
        last_snapshot = list(snap)
        for n in snap: history.append(n); added += 1
        return added

    a = snap
    b = snap[::-1]
    oa = overlap(last_snapshot, a)
    ob = overlap(last_snapshot, b)
    chosen = a if oa >= ob else b
    k = max(oa, ob)

    if k >= len(chosen):
        last_snapshot = list(chosen)
        return 0

    new_tail = chosen[k:]
    for n in new_tail:
        history.append(n); added += 1
    last_snapshot = list(chosen)
    return added

def last_k_colors(seq, k, ignore_white=True):
    out=[]
    for v in reversed(seq):
        if v==0 and ignore_white: continue
        out.append("R" if is_red(v) else "B")
        if len(out)>=k: break
    return list(reversed(out))

def counts_last(seq, k):
    lst = last_k_colors(seq, k)
    return lst.count("R"), lst.count("B")

# ========================= Engines de sinais =========================
def white_engine(seq):
    """Heurística leve para BRANCO: avalia gaps e repetição recente."""
    if not seq: return {"ok": False}
    idx = [i for i, v in enumerate(seq) if v == 0]
    if not idx:
        gap = len(seq)
        gaps = []
    else:
        gap = (len(seq)-1) - idx[-1]
        gaps = [b-a for a, b in zip(idx, idx[1:])]

    if not gaps:
        mu = 25.0
        p90 = 40.0
    else:
        mu = sum(gaps)/len(gaps)
        sgaps = sorted(gaps)
        p90 = sgaps[int(0.9*(len(sgaps)-1))]

    reasons = []
    strong = False
    if 1 <= gap <= 8: reasons.append("Branco recente (≤8)")
    if gap >= (mu*1.25): reasons.append("Gap acima da média"); strong = True
    if gap >= p90: reasons.append("Gap ≥ P90"); strong = True
    if len(seq) >= 6:
        tail = seq[-6:]
        reds  = sum(1 for x in tail if is_red(x))
        blacks= sum(1 for x in tail if is_black(x))
        if reds>=5 or blacks>=5: reasons.append("Muro de cor")

    ok = (len(reasons) >= 2) and strong
    return {"ok": ok, "detail": f"gap={gap} μ≈{mu:.1f} P90≈{p90:.0f}", "reasons": reasons}

def color_engine(seq):
    """Heurística simples: dominância nos últimos 20 + continuidade curta."""
    if not seq: return {"ok": False}
    r20, b20 = counts_last(seq, 20)
    r10, b10 = counts_last(seq, 10)
    reasons = []

    dom = None
    if r20 + b20 >= 10:
        if r20 >= 0.6*(r20+b20): dom = "R"; reasons.append("Domínio R (20)")
        if b20 >= 0.6*(r20+b20): dom = "B"; reasons.append("Domínio B (20)")

    lst = last_k_colors(seq, 6)
    if len(lst) >= 3 and lst[-1] == lst[-2] == lst[-3]:
        reasons.append("3 seguidas")
        dom = lst[-1]

    if r10 + b10 >= 8 and abs(r10 - b10) >= 4:
        reasons.append("Assimetria 10 forte")
        dom = "R" if r10 > b10 else "B"

    if not dom:
        return {"ok": False}
    return {"ok": True, "target": dom, "reasons": reasons}

# ========================= Probabilidades p/ UI ======================
def estimate_probs(seq):
    baseW = 1.0/15.0  # 6.67%
    we = white_engine(seq)
    extraW = 0.0
    if we["ok"]:
        extraW = 0.08
    else:
        if len(we.get("reasons", [])) >= 2: extraW = 0.03
    pW = min(0.40, baseW + extraW)

    r20,b20 = counts_last(seq,20)
    tot = max(1, r20+b20)
    pR_raw = (r20+1)/(tot+2)
    pB_raw = (b20+1)/(tot+2)
    rem = max(0.0, 1.0 - pW)
    pR = pR_raw * rem
    pB = pB_raw * rem
    s = pW+pR+pB
    pW,pR,pB = pW/s, pR/s, pB/s
    rec = max([("W",pW),("R",pR),("B",pB)], key=lambda x: x[1])
    return {"W":pW,"R":pR,"B":pB,"rec":rec}

# ========================= Trades / gales ============================
def append_signal_entry(mode, target, step, status="open", came=None, g=None):
    signal = {
        "ts": now_hhmmss(),
        "mode": mode,
        "target": target,          # 'W'|'R'|'B'
        "status": status,          # 'open'|'WIN'|'LOSS'
        "gale": g if g is not None else step,
        "came": came               # número que saiu
    }
    signals.appendleft(signal)

def try_open_trade_if_needed():
    global open_trade, cool_color, cool_white
    if open_trade or not bot_on: return
    seq = list(history)
    if not seq: return

    if mode_selected == "BRANCO":
        if cool_white > 0: return
        sig = white_engine(seq)
        if sig["ok"]:
            open_trade = {
                "type": "white",
                "target": "W",
                "step": 0,
                "opened_at": len(seq)
            }
            append_signal_entry("BRANCO", "W", 0, status="open")
    else:
        if cool_color > 0: return
        sig = color_engine(seq)
        if sig["ok"]:
            tgt = sig["target"]
            open_trade = {
                "type": "color",
                "target": tgt,
                "step": 0,
                "opened_at": len(seq)
            }
            append_signal_entry("CORES", tgt, 0, status="open")

def process_new_number(n):
    """Chamado a cada número novo e avalia trade aberto."""
    global open_trade, cool_color, cool_white
    if not open_trade: 
        try_open_trade_if_needed()
        return

    # só considera números *após* a abertura
    if open_trade and len(history) <= open_trade["opened_at"]:
        return

    if open_trade["type"] == "white":
        hit = (n == 0)
        if hit:
            append_signal_entry("BRANCO","W", open_trade["step"], status="WIN", came=n)
            open_trade = None
            cool_white = 5
        else:
            append_signal_entry("BRANCO","W", open_trade["step"], status="LOSS", came=n)
            open_trade = None
            cool_white = 5

    else:  # color
        tgt = open_trade["target"]  # 'R' ou 'B'
        came = color_code(n)
        hit = (n != 0) and (came == tgt)
        if hit:
            append_signal_entry("CORES", tgt, open_trade["step"], status="WIN", came=n)
            open_trade = None
            cool_color = 2
        else:
            # errou → gale?
            if open_trade["step"] >= MAX_GALES:
                append_signal_entry("CORES", tgt, open_trade["step"], status="LOSS", came=n)
                open_trade = None
                cool_color = 2
            else:
                open_trade["step"] += 1   # vai para G1 e aguarda o próximo número

# ========================= Rotas HTTP ================================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/state")
def state():
    seq = list(history)
    probs = estimate_probs(seq) if seq else {"W":0.066,"R":0.467,"B":0.467,"rec":("B",0.467)}
    out = {
        "ok": True,
        "bot_on": bot_on,
        "mode": mode_selected,
        "history": seq[-20:],
        "last_50": seq[-50:],
        "probs": {
            "W": round(100*probs["W"],1),
            "R": round(100*probs["R"],1),
            "B": round(100*probs["B"],1),
            "rec": {"tgt": probs["rec"][0], "p": round(100*probs["rec"][1],1)}
        },
        "signals": list(signals)[:40],
        "cooldowns": {"white": max(0,cool_white), "color": max(0,cool_color)},
        "open_trade": open_trade
    }
    return jsonify(out)

@app.route("/ingest", methods=["POST"])
def ingest():
    payload = request.get_json(silent=True) or {}
    snap = payload.get("history") or []
    added = merge_snapshot(snap)
    # processa apenas os novos itens
    if added:
        for n in snap[-added:]:
            process_new_number(n)
    return jsonify({"ok": True, "added": added, "time": datetime.now().isoformat()})

@app.route("/control", methods=["POST"])
def control():
    global bot_on, mode_selected, open_trade, cool_color, cool_white
    data = request.get_json(silent=True) or {}
    if "bot_on" in data: bot_on = bool(data["bot_on"])
    if "mode" in data and str(data["mode"]).upper() in ("BRANCO","CORES"):
        mode_selected = str(data["mode"]).upper()
        # Ao trocar o modo, encerra trade aberto
        open_trade = None
        cool_white = cool_color = 0
    return jsonify({"ok": True, "bot_on": bot_on, "mode": mode_selected})

# ========================= Boot =====================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
