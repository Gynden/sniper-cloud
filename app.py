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
signals = deque(maxlen=300)              # sinais fechados/abertos (log leve)
last_snapshot = []                       # último snapshot para merge sem duplicar

# Controle do bot
bot_on = False
mode_selected = "CORES"                  # "BRANCO" ou "CORES"

open_trade = None                        # trade aberto (dict)
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

def streak_len_color(seq):
    """Comprimento da streak atual de cor (ignorando brancos)."""
    c = 0
    last = None
    for v in reversed(seq):
        if v == 0: 
            if c==0: continue
            else: break
        col = "R" if is_red(v) else "B"
        if last is None or col == last:
            c += 1
            last = col
        else:
            break
    return c, last  # (tamanho, 'R'/'B' ou None)

def alternancias(seq, depth=12):
    """Conta alternâncias 'R' <-> 'B' nas últimas N cores (ignorando brancos)."""
    cols = last_k_colors(seq, depth)
    if len(cols) < 2: return 0
    alt = 0
    for i in range(1, len(cols)):
        if cols[i] != cols[i-1]:
            alt += 1
    return alt

def idx_whites(seq):
    return [i for i, v in enumerate(seq) if v == 0]

def gap_white(seq):
    ids = idx_whites(seq)
    if not ids: return len(seq)
    return (len(seq)-1) - ids[-1]

def whites_in_window(seq, k):
    return sum(1 for v in seq[-k:] if v==0)

# ========================= Estratégias — BRANCO ======================
# Cada item: {"name": str, "max_gales": int, "check": lambda seq -> (bool matched)}
WHITE_STRATS = [
    # Grupo Sequência / Repetição
    {"name": "Repetição curta (2x em 12)", "max_gales": 1,
     "check": lambda s: whites_in_window(s,12) >= 2},
    {"name": "Repetição 3x em 40", "max_gales": 1,
     "check": lambda s: whites_in_window(s,40) >= 3},
    {"name": "Após trinca de cor", "max_gales": 2,
     "check": lambda s: streak_len_color(s)[0] >= 3},
    {"name": "Alternância curta quebrada", "max_gales": 2,
     "check": lambda s: alternancias(s,8) >= 3 and streak_len_color(s)[0] >= 2},
    {"name": "Dupla repetida (VV PP)", "max_gales": 1,
     "check": lambda s: len(last_k_colors(s,5))>=4 and last_k_colors(s,4)[0]==last_k_colors(s,4)[1]!=last_k_colors(s,4)[2]==last_k_colors(s,4)[3]},
    {"name": "Sem repetir cor por 7+", "max_gales": 2,
     "check": lambda s: alternancias(s,10) >= 7},

    # Grupo Espaçamento / Distância
    {"name": "Gap 15–20", "max_gales": 2,
     "check": lambda s: 15 <= gap_white(s) <= 20},
    {"name": "Gap 25+", "max_gales": 3,
     "check": lambda s: gap_white(s) >= 25},
    {"name": "Follow-up curto (≤4)", "max_gales": 1,
     "check": lambda s: gap_white(s) <= 4 and whites_in_window(s,10)>=1},
    {"name": "Espelho 10", "max_gales": 2,
     "check": lambda s: gap_white(s) in (9,10,11)},
    {"name": "Ciclo de 12", "max_gales": 2,
     "check": lambda s: gap_white(s) % 12 == 0 and gap_white(s)>0},
    {"name": "Intervalos ímpares", "max_gales": 1,
     "check": lambda s: gap_white(s) in (9,11,13,15)},
    {"name": "Retorno (duplo cluster)", "max_gales": 2,
     "check": lambda s: whites_in_window(s,20)>=2 and gap_white(s)>=8},
    {"name": "Recuperação gap 30", "max_gales": 3,
     "check": lambda s: gap_white(s) >= 30},

    # Alternância / Mudanças
    {"name": "Alternância longa (8+)", "max_gales": 2,
     "check": lambda s: alternancias(s,12) >= 8},
    {"name": "Alternância perfeita e quebra", "max_gales": 1,
     "check": lambda s: alternancias(s,6) >= 4 and streak_len_color(s)[0]>=2},

    # Clusters de brancos
    {"name": "Cluster ativo (W≤10)", "max_gales": 1,
     "check": lambda s: whites_in_window(s,10)>=1},
    {"name": "Dois clusters próximos", "max_gales": 2,
     "check": lambda s: whites_in_window(s,20)>=2 and gap_white(s)<=10},
    {"name": "Triplo em 40", "max_gales": 1,
     "check": lambda s: whites_in_window(s,40)>=3},

    # Temporais / Contexto (placeholders simples, sem relógio real)
    {"name": "Marcador (pseudo 00/15/30/45)", "max_gales": 1,
     "check": lambda s: (len(s) % 15)==0},
    {"name": "Pós-queda de payout (proxy: muro de cor)", "max_gales": 2,
     "check": lambda s: streak_len_color(s)[0] >= 5},

    # Confluências / Score
    {"name": "Score local ≥2 (gap alto + branco recente)", "max_gales": 2,
     "check": lambda s: (gap_white(s)>=18) and (whites_in_window(s,12)>=1)},
    {"name": "Taxa local ≥10% e 5 sem branco", "max_gales": 2,
     "check": lambda s: whites_in_window(s,20)>=2 and gap_white(s)>=5},

    # Estatísticas / Longo prazo
    {"name": "Densidade baixa (≤2 em 50)", "max_gales": 2,
     "check": lambda s: whites_in_window(s,50) <= 2},
    {"name": "Pós-saturação (50 sem branco)", "max_gales": 3,
     "check": lambda s: gap_white(s) >= 50},
]

# ========================= Estratégias — CORES =======================
# Cada item: {"name": str, "max_gales": int, "check": lambda seq -> (matched, target 'R'|'B')}
def dom_color_20(seq):
    r20, b20 = counts_last(seq, 20)
    if r20 + b20 < 6: return None
    if r20 >= 0.6*(r20+b20): return "R"
    if b20 >= 0.6*(r20+b20): return "B"
    return None

def last_color(seq):
    for v in reversed(seq):
        if v==0: continue
        return "R" if is_red(v) else "B"
    return None

def other(c): return "B" if c=="R" else "R"

COLOR_STRATS = [
    # Repetição / Streaks
    {"name":"Repete 2→3", "max_gales":2,
     "check": lambda s: (lambda st,lc: (st>=2, lc))( *streak_len_color(s) )},
    {"name":"Repete 3→4", "max_gales":1,
     "check": lambda s: (lambda st,lc: (st>=3, lc))( *streak_len_color(s) )},
    {"name":"Repete 4→5", "max_gales":1,
     "check": lambda s: (lambda st,lc: (st>=4, lc))( *streak_len_color(s) )},
    {"name":"Streak curta eco", "max_gales":2,
     "check": lambda s: (lambda st,lc: (st==2 and (counts_last(s,15)[0]>=8 or counts_last(s,15)[1]>=8), lc))( *streak_len_color(s) )},
    {"name":"Primeira duplicação", "max_gales":0,
     "check": lambda s: (lambda st,lc: (st==2 and len(s)%50<2, lc))( *streak_len_color(s) )},
    {"name":"Streak pós-inércia", "max_gales":1,
     "check": lambda s: (lambda st,lc: (st>=1 and last_k_colors(s,8).count(lc)==1, lc))( *streak_len_color(s) )},

    # Alternância
    {"name":"Alternância 4+ quebra", "max_gales":2,
     "check": lambda s: (alternancias(s,10)>=4 and streak_len_color(s)[0]>=2, last_color(s))},
    {"name":"Alternância curta → repetição", "max_gales":1,
     "check": lambda s: (alternancias(s,6)>=2 and streak_len_color(s)[0]>=2, last_color(s))},
    {"name":"Alternância falha", "max_gales":1,
     "check": lambda s: (alternancias(s,8)>=3 and streak_len_color(s)[0]==2, last_color(s))},
    {"name":"Alternância estendida (≥6)", "max_gales":2,
     "check": lambda s: (alternancias(s,12)>=6, last_color(s))},

    # Distância / Frequência
    {"name":"Gap da COR (8+)", "max_gales":2,
     "check": lambda s: (lambda st,lc: (last_k_colors(s,12).count(lc)==0, lc))( *streak_len_color(s) )},
    {"name":"Taxa 20 sub-representada", "max_gales":1,
     "check": lambda s: (lambda r,b: ((r<=8 or b<=8), "R" if r<=8 else "B"))(*counts_last(s,20))},
    {"name":"Retorno à média (≤40% em 50)", "max_gales":2,
     "check": lambda s: (lambda r,b: ((r+b>=20 and (r<=0.4*(r+b) or b<=0.4*(r+b))), "R" if r<=0.4*(r+b) else "B"))(*counts_last(s,50))},

    # Compostos
    {"name":"Sanduíche (COR-OUTRA-COR)", "max_gales":1,
     "check": lambda s: (lambda cols: (len(cols)>=3 and cols[-3]==cols[-1]!=cols[-2], cols[-1] if len(cols)>=3 else None))( last_k_colors(s,5) )},
    {"name":"2x + inversão + 2x", "max_gales":1,
     "check": lambda s: (lambda cols: (
        len(cols)>=5 and cols[-5]==cols[-4]!=cols[-3]==cols[-2] and cols[-1]==cols[-2], cols[-1] if len(cols)>=1 else None))( last_k_colors(s,7) )},
    {"name":"Bloco 2-2-1", "max_gales":2,
     "check": lambda s: (lambda cols: (
        len(cols)>=5 and cols[-5]==cols[-4]!=cols[-3]==cols[-2]!=cols[-1], cols[-1] if len(cols)>=1 else None))( last_k_colors(s,6) )},
    {"name":"Triângulo (OUTRA, COR, OUTRA, COR)", "max_gales":1,
     "check": lambda s: (lambda cols: (
        len(cols)>=4 and cols[-4]!=cols[-3]==cols[-1]!=cols[-2] and cols[-3]==cols[-1], cols[-1] if len(cols)>=1 else None))( last_k_colors(s,6) )},

    # Ritmo / Dominância
    {"name":"Domínio 20", "max_gales":2,
     "check": lambda s: (dom_color_20(s) is not None, dom_color_20(s))},
    {"name":"Confirmação com última cor + densidade", "max_gales":1,
     "check": lambda s: (lambda r,b,lc: ((lc=="R" and r>=11) or (lc=="B" and b>=11), lc))( *counts_last(s,20), last_color(s) )},

    # Scores / Bias dinâmico (simples)
    {"name":"Score 3-de-5 (simulado)", "max_gales":2,
     "check": lambda s: (lambda lc,st,alt,r20,b20: (
        sum([
            1 if st>=2 else 0,
            1 if alt<=3 else 0,
            1 if (lc=="R" and r20>=11) or (lc=="B" and b20>=11) else 0,
            1 if (r20-b20>=4) or (b20-r20>=4) else 0,
            1 if whites_in_window(s,8)==0 else 0
        ])>=3, lc))( last_color(s), *streak_len_color(s), *counts_last(s,20) )},
]

# ========================= Motores (seleção de estratégia) ===========
def select_white_strategy(seq):
    """Retorna a primeira estratégia de BRANCO que bater.
       Se quiser confluência, agregue várias e exija >=2 matches."""
    matches = []
    for strat in WHITE_STRATS:
        try:
            if strat["check"](seq):
                matches.append(strat)
        except Exception:
            continue
    if not matches:
        return None
    # Ordenação simples por max_gales (opcional: priorizar menor risco)
    matches.sort(key=lambda x: (-x["max_gales"]))
    return matches[0]

def select_color_strategy(seq):
    """Retorna (estratégia, target 'R'|'B') para CORES, se houver match."""
    for strat in COLOR_STRATS:
        try:
            m, tgt = strat["check"](seq)
            if m and tgt in ("R","B"):
                return strat, tgt
        except Exception:
            continue
    return None, None

# ========================= Probabilidades p/ UI ======================
def estimate_probs(seq):
    baseW = 1.0/15.0  # 6.67% base
    # Heurística leve
    w_gap = gap_white(seq)
    extraW = 0.0
    if w_gap >= 18: extraW += 0.03
    if whites_in_window(seq,10)>=1: extraW += 0.02
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

# ========================= Sinais / Logs =============================
def format_label(target, step, max_gales):
    """Exibe o rótulo do sinal (com gale)."""
    name = {"W":"BRANCO","R":"VERMELHO","B":"PRETO"}[target]
    if step == 0:
        return f"{name} (até {max_gales} gale{'s' if max_gales!=1 else ''})"
    else:
        return f"{name} — GALE {step}"

def append_signal_entry(mode, target, step, status="open", came=None, strategy=None, max_gales=0):
    signal = {
        "ts": now_hhmmss(),
        "mode": mode,                     # 'BRANCO'|'CORES'
        "target": target,                 # 'W'|'R'|'B'
        "status": status,                 # 'open'|'WIN'|'LOSS'|'GALE'
        "gale": step,                     # 0,1,2,3...
        "max_gales": max_gales,           # limite daquela estratégia
        "label": format_label(target, step, max_gales),
        "strategy": strategy,
        "came": came
    }
    signals.appendleft(signal)

# ========================= Abertura / Gestão do trade ================
def try_open_trade_if_needed():
    global open_trade, cool_color, cool_white
    if open_trade or not bot_on: return
    seq = list(history)
    if not seq: return

    if mode_selected == "BRANCO":
        if cool_white > 0: return
        sel = select_white_strategy(seq)
        if sel:
            open_trade = {
                "type": "white",
                "target": "W",
                "step": 0,
                "max_gales": sel["max_gales"],
                "strategy": sel["name"],
                "opened_at": len(seq)
            }
            append_signal_entry("BRANCO","W",0,status="open",strategy=sel["name"],max_gales=sel["max_gales"])
    else:
        if cool_color > 0: return
        strat, tgt = select_color_strategy(seq)
        if strat and tgt:
            open_trade = {
                "type": "color",
                "target": tgt,
                "step": 0,
                "max_gales": strat["max_gales"],
                "strategy": strat["name"],
                "opened_at": len(seq)
            }
            append_signal_entry("CORES",tgt,0,status="open",strategy=strat["name"],max_gales=strat["max_gales"])

def process_new_number(n):
    """Chamado a cada número novo e avalia trade aberto."""
    global open_trade, cool_color, cool_white
    # Primeiro tenta abrir trade se não houver
    if not open_trade:
        try_open_trade_if_needed()
        return

    # só considera números *após* a abertura
    if open_trade and len(history) <= open_trade["opened_at"]:
        return

    if open_trade["type"] == "white":
        hit = (n == 0)
        if hit:
            append_signal_entry("BRANCO","W", open_trade["step"], status="WIN", came=n,
                                strategy=open_trade["strategy"], max_gales=open_trade["max_gales"])
            open_trade = None
            cool_white = 5
        else:
            # errou branco → tenta gale?
            if open_trade["step"] >= open_trade["max_gales"]:
                append_signal_entry("BRANCO","W", open_trade["step"], status="LOSS", came=n,
                                    strategy=open_trade["strategy"], max_gales=open_trade["max_gales"])
                open_trade = None
                cool_white = 5
            else:
                # promove para GALE
                open_trade["step"] += 1
                append_signal_entry("BRANCO","W", open_trade["step"], status="GALE", came=n,
                                    strategy=open_trade["strategy"], max_gales=open_trade["max_gales"])

    else:  # color
        tgt = open_trade["target"]  # 'R' ou 'B'
        came = color_code(n)
        hit = (n != 0) and (came == tgt)
        if hit:
            append_signal_entry("CORES", tgt, open_trade["step"], status="WIN", came=n,
                                strategy=open_trade["strategy"], max_gales=open_trade["max_gales"])
            open_trade = None
            cool_color = 2
        else:
            # errou → gale?
            if open_trade["step"] >= open_trade["max_gales"]:
                append_signal_entry("CORES", tgt, open_trade["step"], status="LOSS", came=n,
                                    strategy=open_trade["strategy"], max_gales=open_trade["max_gales"])
                open_trade = None
                cool_color = 2
            else:
                open_trade["step"] += 1
                append_signal_entry("CORES", tgt, open_trade["step"], status="GALE", came=n,
                                    strategy=open_trade["strategy"], max_gales=open_trade["max_gales"])

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
        "signals": list(signals)[:60],
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
