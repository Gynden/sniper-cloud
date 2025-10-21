# -*- coding: utf-8 -*-
# SNIPER BLAZE — backend Flask
# - Trava de 1 sinal por vez até terminar GALES
# - Telemetria round/latência/ETA
# - Estatísticas por estratégia
# - Export CSV de sinais

import csv, io
from collections import deque, defaultdict
from datetime import datetime, timezone
from flask import Flask, jsonify, request, send_file, Response
from flask_cors import CORS

app = Flask(__name__, static_url_path="", static_folder=".", template_folder=".")
CORS(app)

# ========================= Config =========================
HISTORY_MAX = 600
CONFLUENCE_MIN_WHITE = 2
CONFLUENCE_MIN_COLOR = 2
SELECAO_RISCO = "conservador"     # "conservador" | "agressivo"
EVAL_SAME_SPIN = False
STRICT_ONE_AT_A_TIME = True

# Simulação (para estatísticas / P&L)
SIM_STAKE = 1.0
NET_ODDS_COLOR = 1.0   # lucro líquido por unidade (ex.: 1.0 = 1x)
NET_ODDS_WHITE = 14.0

# Telemetria de rodada
DEFAULT_SPIN_MS = 12000
SPIN_ALPHA = 0.30            # suavização ao estimar média de rodada
HEALTH_LAG_WARN_MS = 25000   # acima disso: alerta de atraso
DATA_SOURCE = "unknown"      # "tipminer" | "blaze" | "unknown"

# ========================= Estado =========================
history = deque(maxlen=HISTORY_MAX)
signals = deque(maxlen=1000)
last_snapshot = []
bot_on = False
mode_selected = "CORES"   # "BRANCO" | "CORES"

open_trade = None
cool_white = 0
cool_color = 0

# Telemetria interna
round_id = 0
last_spin_ts = None            # server ts do último giro recebido
avg_spin_ms = DEFAULT_SPIN_MS  # média móvel
last_ingest_latency_ms = 0     # se o cliente mandar "client_sent_at", podemos calcular; aqui usamos delta de chegada

# Estatísticas por estratégia
# stats[name] = {"entries":int, "win":int, "loss":int, "pl":float}
stats_by_strategy = defaultdict(lambda: {"entries":0, "win":0, "loss":0, "pl":0.0})

# ========================= Helpers =========================
def is_red(n):   return 1 <= n <= 7
def is_black(n): return 8 <= n <= 14
def color_code(n):
    if n == 0: return "W"
    return "R" if is_red(n) else "B"

def now_dt():
    return datetime.now(timezone.utc)

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
    global last_snapshot, round_id, last_spin_ts, avg_spin_ms
    if not snapshot: return 0
    snap = [int(x) for x in snapshot if isinstance(x, int) and 0 <= x <= 14]
    if not snap: return 0

    added = 0
    if not last_snapshot:
        last_snapshot = list(snap)
        for n in snap:
            history.append(n)
            added += 1
        if added:
            round_id += added
            last_spin_ts = now_dt()
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
        history.append(n)
        added += 1

    if added:
        # Telemetria de rodada: atualização de média
        now = now_dt()
        global last_spin_ts
        if last_spin_ts is not None:
            dt_ms = (now - last_spin_ts).total_seconds() * 1000.0
            if 1000 <= dt_ms <= 40000:  # janela razoável
                avg_spin_ms = (1-SPIN_ALPHA)*avg_spin_ms + SPIN_ALPHA*dt_ms
        last_spin_ts = now
        round_id += added

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
    return c, last

def alternancias(seq, depth=12):
    cols = last_k_colors(seq, depth)
    if len(cols) < 2: return 0
    alt = 0
    for i in range(1, len(cols)):
        if cols[i] != cols[i-1]:
            alt += 1
    return alt

def idx_whites(seq): return [i for i, v in enumerate(seq) if v == 0]
def gap_white(seq):
    ids = idx_whites(seq)
    if not ids: return len(seq)
    return (len(seq)-1) - ids[-1]
def whites_in_window(seq, k): return sum(1 for v in seq[-k:] if v==0)

def tick_cooldowns():
    global cool_white, cool_color
    if cool_white > 0: cool_white -= 1
    if cool_color > 0: cool_color -= 1

# ========================= Estratégias (mesmas do seu código) =======
WHITE_STRATS = [
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

    {"name": "Alternância longa (8+)", "max_gales": 2,
     "check": lambda s: alternancias(s,12) >= 8},
    {"name": "Alternância perfeita e quebra", "max_gales": 1,
     "check": lambda s: alternancias(s,6) >= 4 and streak_len_color(s)[0]>=2},

    {"name": "Cluster ativo (W≤10)", "max_gales": 1,
     "check": lambda s: whites_in_window(s,10)>=1},
    {"name": "Dois clusters próximos", "max_gales": 2,
     "check": lambda s: whites_in_window(s,20)>=2 and gap_white(s)<=10},
    {"name": "Triplo em 40", "max_gales": 1,
     "check": lambda s: whites_in_window(s,40)>=3},

    {"name": "Marcador (pseudo 00/15/30/45)", "max_gales": 1,
     "check": lambda s: (len(s) % 15)==0},
    {"name": "Pós-queda de payout (proxy: muro de cor)", "max_gales": 2,
     "check": lambda s: streak_len_color(s)[0] >= 5},

    {"name": "Score local ≥2 (gap alto + branco recente)", "max_gales": 2,
     "check": lambda s: (gap_white(s)>=18) and (whites_in_window(s,12)>=1)},
    {"name": "Taxa local ≥10% e 5 sem branco", "max_gales": 2,
     "check": lambda s: whites_in_window(s,20)>=2 and gap_white(s)>=5},

    {"name": "Densidade baixa (≤2 em 50)", "max_gales": 2,
     "check": lambda s: whites_in_window(s,50) <= 2},
    {"name": "Pós-saturação (50 sem branco)", "max_gales": 3,
     "check": lambda s: gap_white(s) >= 50},
]

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

COLOR_STRATS = [
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

    {"name":"Alternância 4+ quebra", "max_gales":2,
     "check": lambda s: (alternancias(s,10)>=4 and streak_len_color(s)[0]>=2, last_color(s))},
    {"name":"Alternância curta → repetição", "max_gales":1,
     "check": lambda s: (alternancias(s,6)>=2 and streak_len_color(s)[0]>=2, last_color(s))},
    {"name":"Alternância falha", "max_gales":1,
     "check": lambda s: (alternancias(s,8)>=3 and streak_len_color(s)[0]==2, last_color(s))},
    {"name":"Alternância estendida (≥6)", "max_gales":2,
     "check": lambda s: (alternancias(s,12)>=6, last_color(s))},

    {"name":"Gap da COR (8+)", "max_gales":2,
     "check": lambda s: (lambda st,lc: (last_k_colors(s,12).count(lc)==0, lc))( *streak_len_color(s) )},
    {"name":"Taxa 20 sub-representada", "max_gales":1,
     "check": lambda s: (lambda r,b: ((r<=8 or b<=8), "R" if r<=8 else "B"))(*counts_last(s,20))},
    {"name":"Retorno à média (≤40% em 50)", "max_gales":2,
     "check": lambda s: (lambda r,b: ((r+b>=20 and (r<=0.4*(r+b) or b<=0.4*(r+b))), "R" if r<=0.4*(r+b) else "B"))(*counts_last(s,50))},

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

    {"name":"Domínio 20", "max_gales":2,
     "check": lambda s: (dom_color_20(s) is not None, dom_color_20(s))},
    {"name":"Confirmação com última cor + densidade", "max_gales":1,
     "check": lambda s: (lambda r,b,lc: ((lc=="R" and r>=11) or (lc=="B" and b>=11), lc))( *counts_last(s,20), last_color(s) )},

    {"name":"Score 3-de-5 (simulado)", "max_gales":2,
     "check": lambda s: (lambda lc,st,alt,r20,b20: (
        sum([1 if st>=2 else 0,
             1 if alt<=3 else 0,
             1 if (lc=='R' and r20>=11) or (lc=='B' and b20>=11) else 0,
             1 if (r20-b20>=4) or (b20-r20>=4) else 0,
             1 if whites_in_window(s,8)==0 else 0])>=3, lc))(
                 last_color(s), *streak_len_color(s), *counts_last(s,20))}
]

def selecionar_representante(strats):
    if not strats: return None
    if SELECAO_RISCO == "agressivo":
        return max(strats, key=lambda x: x["max_gales"])
    return min(strats, key=lambda x: x["max_gales"])

def select_white_with_confluence(seq):
    matches = []
    for strat in WHITE_STRATS:
        try:
            if strat["check"](seq):
                matches.append(strat)
        except Exception:
            continue
    if len(matches) < CONFLUENCE_MIN_WHITE:
        return None, 0
    rep = selecionar_representante(matches)
    return rep, len(matches)

def select_color_with_confluence(seq):
    votes = {"R": [], "B": []}
    for strat in COLOR_STRATS:
        try:
            ok, tgt = strat["check"](seq)
            if ok and tgt in ("R","B"):
                votes[tgt].append(strat)
        except Exception:
            continue
    r_votes = len(votes["R"]); b_votes = len(votes["B"])
    target = None; conf = 0
    if r_votes >= CONFLUENCE_MIN_COLOR or b_votes >= CONFLUENCE_MIN_COLOR:
        if r_votes > b_votes:   target, conf = "R", r_votes
        elif b_votes > r_votes: target, conf = "B", b_votes
        else:
            dom = dom_color_20(seq)
            if dom in ("R","B"):
                target, conf = dom, len(votes[dom])
    if not target: return None, None, 0
    rep = selecionar_representante(votes[target])
    return rep, target, conf

# ========================= Probabilidade p/ UI ======================
def estimate_probs(seq):
    baseW = 1.0/15.0
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

# ========================= Logs / Sinais ============================
def format_label(target, step, max_gales, conf=None):
    name = {"W":"BRANCO","R":"VERMELHO","B":"PRETO"}[target]
    conf_txt = (f" — Confluência: {conf}" if conf else "")
    if step == 0:
        return f"{name} (até {max_gales} gale{'s' if max_gales!=1 else ''}){conf_txt}"
    else:
        return f"{name} — GALE {step}"

def append_signal_entry(mode, target, step, status="open", came=None,
                        strategy=None, max_gales=0, conf=None, phase=None,
                        trade_started_ts=None):
    came_color = None
    if isinstance(came, int):
        came_color = color_code(came)

    signals.appendleft({
        "ts": now_hhmmss(),
        "ts_iso": now_dt().isoformat(),
        "mode": mode, "target": target,
        "status": status, "phase": phase,
        "gale": step, "max_gales": max_gales,
        "label": format_label(target, step, max_gales, conf),
        "strategy": strategy, "confluence": conf,
        "came": came, "came_color": came_color,
        "trade_started_ts": trade_started_ts
    })

def update_stats_on_close(strategy, target, result):
    if not strategy: return
    rec = stats_by_strategy[strategy]
    rec["entries"] += 1
    if result == "WIN":
        rec["win"] += 1
        rec["pl"] += (NET_ODDS_WHITE if target=="W" else NET_ODDS_COLOR) * SIM_STAKE
    elif result == "LOSS":
        rec["loss"] += 1
        rec["pl"] -= SIM_STAKE * (1 + (open_trade["max_gales"] if open_trade else 0))  # custo bruto aprox.

# ========================= Motor ===================================
def trade_lock_active():
    if not STRICT_ONE_AT_A_TIME:
        return False
    return open_trade is not None

def try_open_trade_if_needed():
    global open_trade, cool_color, cool_white
    if trade_lock_active() or not bot_on:
        return
    seq = list(history)
    if not seq: return

    if mode_selected == "BRANCO":
        if cool_white > 0: return
        rep, conf = select_white_with_confluence(seq)
        if rep:
            open_trade = {
                "type": "white", "target": "W", "step": 0,
                "max_gales": rep["max_gales"], "strategy": rep["name"],
                "confluence": conf, "opened_at": len(seq),
                "phase": "analyzing", "started_ts": now_dt().isoformat()
            }
            append_signal_entry("BRANCO","W",0,status="ANALYZING",
                                strategy=rep["name"],max_gales=rep["max_gales"],conf=conf,
                                phase="analyzing", trade_started_ts=open_trade["started_ts"])
    elif mode_selected == "CORES":
        if cool_color > 0: return
        rep, tgt, conf = select_color_with_confluence(seq)
        if rep and tgt:
            open_trade = {
                "type": "color", "target": tgt, "step": 0,
                "max_gales": rep["max_gales"], "strategy": rep["name"],
                "confluence": conf, "opened_at": len(seq),
                "phase": "analyzing", "started_ts": now_dt().isoformat()
            }
            append_signal_entry("CORES",tgt,0,status="ANALYZING",
                                strategy=rep["name"],max_gales=rep["max_gales"],conf=conf,
                                phase="analyzing", trade_started_ts=open_trade["started_ts"])

def process_new_number(n):
    global open_trade, cool_color, cool_white

    tick_cooldowns()

    if not open_trade:
        try_open_trade_if_needed()
        return

    # Troca de modo cancela trade
    if (mode_selected == "BRANCO" and open_trade.get("type") == "color") or \
       (mode_selected == "CORES"  and open_trade.get("type") == "white"):
        open_trade = None
        return

    # Promover analyzing -> open
    if open_trade.get("phase") == "analyzing":
        open_trade["phase"] = "open"
        append_signal_entry("BRANCO" if open_trade["type"]=="white" else "CORES",
                            open_trade["target"], open_trade["step"],
                            status="open", strategy=open_trade["strategy"],
                            max_gales=open_trade["max_gales"],
                            conf=open_trade.get("confluence"),
                            phase="open", trade_started_ts=open_trade["started_ts"])
        return

    # Avaliar resultado (latência configurável)
    if not EVAL_SAME_SPIN:
        if len(history) <= open_trade["opened_at"]:
            return
    else:
        if len(history) < open_trade["opened_at"]:
            return

    if open_trade["type"] == "white":
        hit = (n == 0)
        if hit:
            append_signal_entry("BRANCO","W", open_trade["step"], status="WIN", came=n,
                                strategy=open_trade["strategy"], max_gales=open_trade["max_gales"],
                                conf=open_trade.get("confluence"), trade_started_ts=open_trade["started_ts"])
            update_stats_on_close(open_trade["strategy"], "W", "WIN")
            open_trade = None; cool_white = 5
        else:
            if open_trade["step"] >= open_trade["max_gales"]:
                append_signal_entry("BRANCO","W", open_trade["step"], status="LOSS", came=n,
                                    strategy=open_trade["strategy"], max_gales=open_trade["max_gales"],
                                    conf=open_trade.get("confluence"), trade_started_ts=open_trade["started_ts"])
                update_stats_on_close(open_trade["strategy"], "W", "LOSS")
                open_trade = None; cool_white = 5
            else:
                open_trade["step"] += 1
                append_signal_entry("BRANCO","W", open_trade["step"], status="GALE", came=n,
                                    strategy=open_trade["strategy"], max_gales=open_trade["max_gales"],
                                    conf=open_trade.get("confluence"), trade_started_ts=open_trade["started_ts"])
    else:
        tgt = open_trade["target"]
        came = color_code(n)
        hit = (n != 0) and (came == tgt)
        if hit:
            append_signal_entry("CORES", tgt, open_trade["step"], status="WIN", came=n,
                                strategy=open_trade["strategy"], max_gales=open_trade["max_gales"],
                                conf=open_trade.get("confluence"), trade_started_ts=open_trade["started_ts"])
            update_stats_on_close(open_trade["strategy"], tgt, "WIN")
            open_trade = None; cool_color = 2
        else:
            if open_trade["step"] >= open_trade["max_gales"]:
                append_signal_entry("CORES", tgt, open_trade["step"], status="LOSS", came=n,
                                    strategy=open_trade["strategy"], max_gales=open_trade["max_gales"],
                                    conf=open_trade.get("confluence"), trade_started_ts=open_trade["started_ts"])
                update_stats_on_close(open_trade["strategy"], tgt, "LOSS")
                open_trade = None; cool_color = 2
            else:
                open_trade["step"] += 1
                append_signal_entry("CORES", tgt, open_trade["step"], status="GALE", came=n,
                                    strategy=open_trade["strategy"], max_gales=open_trade["max_gales"],
                                    conf=open_trade.get("confluence"), trade_started_ts=open_trade["started_ts"])

# ========================= Rotas HTTP ================================
@app.route("/")
def index():
    return send_file("index.html")

@app.route("/state")
def state():
    seq = list(history)
    probs = estimate_probs(seq) if seq else {"W":0.066,"R":0.467,"B":0.467,"rec":("B",0.467)}

    # Telemetria de saúde
    now = now_dt()
    if last_spin_ts is None:
        eta_ms = None
        lag_ms = None
    else:
        since_ms = (now - last_spin_ts).total_seconds()*1000.0
        eta_ms = max(0, int(max(2000, avg_spin_ms) - since_ms))  # ETA aproximada
        lag_ms = int(since_ms)

    # Estatísticas rápidas por estratégia (calc de assertividade)
    stats_out = []
    for name, s in stats_by_strategy.items():
        total = max(1, s["entries"])
        acc = round(100.0 * s["win"]/total, 1)
        stats_out.append({
            "strategy": name,
            "entries": s["entries"],
            "win": s["win"],
            "loss": s["loss"],
            "acc": acc,
            "pl": round(s["pl"], 2)
        })
    stats_out.sort(key=lambda x: (-x["entries"], -x["acc"]))

    out = {
        "ok": True,
        "bot_on": bot_on,
        "mode": mode_selected,
        "history": seq[-20:], "last_50": seq[-50:],
        "probs": {
            "W": round(100*probs["W"],1),
            "R": round(100*probs["R"],1),
            "B": round(100*probs["B"],1),
            "rec": {"tgt": probs["rec"][0], "p": round(100*probs["rec"][1],1)}
        },
        "signals": list(signals)[:80],
        "cooldowns": {"white": max(0,cool_white), "color": max(0,cool_color)},
        "open_trade": open_trade,
        "confluence": {
            "white_min": CONFLUENCE_MIN_WHITE,
            "color_min": CONFLUENCE_MIN_COLOR,
            "selecao_risco": SELECAO_RISCO
        },
        "eval_same_spin": EVAL_SAME_SPIN,
        "strict_one_at_a_time": STRICT_ONE_AT_A_TIME,

        # Telemetria extra
        "server_time": now.isoformat(),
        "round_id": round_id,
        "avg_spin_ms": int(avg_spin_ms),
        "eta_ms": None if eta_ms is None else int(eta_ms),
        "latency_ms": last_ingest_latency_ms,
        "lag_ms": None if last_spin_ts is None else int((now - last_spin_ts).total_seconds()*1000.0),
        "data_source": DATA_SOURCE,

        # Saúde e stats
        "health": {
            "ok": (lag_ms is None) or (lag_ms < HEALTH_LAG_WARN_MS),
            "lag_ms": lag_ms,
            "warn_threshold_ms": HEALTH_LAG_WARN_MS
        },
        "stats_by_strategy": stats_out
    }
    return jsonify(out)

@app.route("/ingest", methods=["POST"])
def ingest():
    payload = request.get_json(silent=True) or {}
    # Latência básica se o cliente enviar client_sent_at (ISO)
    client_sent_at = payload.get("client_sent_at")
    global last_ingest_latency_ms
    if client_sent_at:
        try:
            cdt = datetime.fromisoformat(client_sent_at.replace("Z","+00:00"))
            last_ingest_latency_ms = int((now_dt() - cdt).total_seconds()*1000.0)
        except:
            last_ingest_latency_ms = 0

    snap = payload.get("history") or []
    added = merge_snapshot(snap)

    if added:
        for n in snap[-added:]:
            process_new_number(n)
    else:
        tick_cooldowns()
        if not trade_lock_active():
            try_open_trade_if_needed()

    return jsonify({"ok": True, "added": added, "time": now_dt().isoformat()})

@app.route("/control", methods=["POST"])
def control():
    global bot_on, mode_selected, open_trade, cool_color, cool_white, EVAL_SAME_SPIN, STRICT_ONE_AT_A_TIME, DATA_SOURCE
    data = request.get_json(silent=True) or {}

    if "bot_on" in data:
        bot_on = bool(data["bot_on"])

    if "mode" in data:
        m = str(data["mode"]).strip().upper()
        if m in ("BRANCO","CORES"):
            mode_selected = m
            open_trade = None
            cool_white = 0
            cool_color = 0

    if "confluence_white" in data:
        try:
            val = int(data["confluence_white"])
            if val >= 1: globals()["CONFLUENCE_MIN_WHITE"] = val
        except: pass
    if "confluence_color" in data:
        try:
            val = int(data["confluence_color"])
            if val >= 1: globals()["CONFLUENCE_MIN_COLOR"] = val
        except: pass

    if "risk" in data and data["risk"] in ("conservador","agressivo"):
        globals()["SELECAO_RISCO"] = data["risk"]
    if "eval_same_spin" in data:
        EVAL_SAME_SPIN = bool(data["eval_same_spin"])
    if "strict_one_at_a_time" in data:
        STRICT_ONE_AT_A_TIME = bool(data["strict_one_at_a_time"])
    if "data_source" in data:
        DATA_SOURCE = str(data["data_source"])

    return jsonify({
        "ok": True,
        "bot_on": bot_on,
        "mode": mode_selected,
        "confluence_white": CONFLUENCE_MIN_WHITE,
        "confluence_color": CONFLUENCE_MIN_COLOR,
        "risk": SELECAO_RISCO,
        "eval_same_spin": EVAL_SAME_SPIN,
        "strict_one_at_a_time": STRICT_ONE_AT_A_TIME,
        "data_source": DATA_SOURCE
    })

@app.route("/export_signals")
def export_signals():
    # CSV em memória
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["ts_iso","mode","target","status","phase","gale","max_gales","strategy","confluence","came","came_color","trade_started_ts"])
    for s in reversed(signals):  # ordem cronológica
        w.writerow([
            s.get("ts_iso",""), s.get("mode",""), s.get("target",""),
            s.get("status",""), s.get("phase",""), s.get("gale",""),
            s.get("max_gales",""), s.get("strategy",""), s.get("confluence",""),
            s.get("came",""), s.get("came_color",""), s.get("trade_started_ts","")
        ])
    return Response(
        output.getvalue().encode("utf-8"),
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="signals.csv"'}
    )

# ========================= Boot =====================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
