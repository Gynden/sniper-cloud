# app.py
# Flask API para o painel SNIPER BLAZE — com trava de 1 sinal por vez,
# estado enriquecido, saúde da conexão e estatísticas por estratégia.

import math, time, json
from collections import deque, defaultdict
from datetime import datetime, timezone
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

app = Flask(__name__, static_url_path="", static_folder=".", template_folder=".")
CORS(app)

# ========================= Config =========================
HISTORY_MAX = 600
CONFLUENCE_MIN_WHITE = 2
CONFLUENCE_MIN_COLOR = 2
SELECAO_RISCO = "conservador"         # "conservador" | "agressivo"
EVAL_SAME_SPIN = False
STRICT_ONE_AT_A_TIME = True           # trava explícita

# Fonte de dados (apenas rótulo informativo para UI)
DATA_SOURCE = "tipminer"              # "blaze" | "tipminer" | "mock"

# ========================= Estado =========================
history = deque(maxlen=HISTORY_MAX)   # números 0..14 (0 = branco)
signals = deque(maxlen=800)           # sinais/logs
last_snapshot = []                    # p/ merge
bot_on = False
mode_selected = "CORES"               # "BRANCO" | "CORES"

# Trade aberto
# {'id':int,'type':'white'|'color','target':'W'|'R'|'B','step':0,'max_gales':int,
#  'strategy':str,'confluence':int,'opened_at':int,'opened_at_ts':float,'phase':'analyzing'|'open'}
open_trade = None
trade_counter = 0

cool_white = 0
cool_color = 0

# Telemetria da ingest
last_ingest_wall = None   # datetime
last_ingest_mono = None   # time.monotonic()

# ========================= Helpers =========================
def is_red(n):   return 1 <= n <= 7
def is_black(n): return 8 <= n <= 14
def color_code(n):
    if n == 0: return "W"
    return "R" if is_red(n) else "B"

def now_iso():
    return datetime.now(timezone.utc).isoformat()

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
        for n in snap:
            history.append(n)
            added += 1
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
    return c, last  # (tamanho, 'R'/'B' ou None)

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

# ========================= Estratégias ======================
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

def other(c): return "B" if c=="R" else "R"

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

# ========================= Seletores =========================
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

# ========================= Probabilidades p/ UI ======================
def estimate_probs(seq):
    baseW = 1.0/15.0  # 6.67% base
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

# ========================= Logs / sinais =========================
def format_label(target, step, max_gales, conf=None):
    name = {"W":"BRANCO","R":"VERMELHO","B":"PRETO"}[target]
    conf_txt = (f" — Confluência: {conf}" if conf else "")
    if step == 0:
        return f"{name} (até {max_gales} gale{'s' if max_gales!=1 else ''}){conf_txt}"
    else:
        return f"{name} — GALE {step}"

def append_signal_entry(mode, target, step, status="open", came=None,
                        strategy=None, max_gales=0, conf=None, phase=None, trade_id=None):
    came_color = None
    if isinstance(came, int):
        came_color = color_code(came)
    mismatch = False
    if status in ("WIN","LOSS") and came_color and target in ("W","R","B"):
        if came_color == target and status == "LOSS":
            mismatch = True
    signals.appendleft({
        "ts": now_hhmmss(),
        "mode": mode, "target": target,
        "status": status, "phase": phase,
        "gale": step, "max_gales": max_gales,
        "label": format_label(target, step, max_gales, conf),
        "strategy": strategy, "confluence": conf,
        "came": came, "came_color": came_color,
        "mismatch": mismatch,
        "trade_id": trade_id
    })

# ========================= Estatísticas =============================
def stats_by_strategy():
    """
    Retorna um dicionário:
    { 'Estratégia X': {'entries':N,'win':W,'loss':L,'assert':pct,'pl_sim':valor} }
    P&L simulado: +1 por WIN, -1 por LOSS (unidade de stake).
    """
    agg = defaultdict(lambda: {"entries":0,"win":0,"loss":0})
    for s in signals:
        if s.get("status") in ("open","ANALYZING","GALE"):
            continue
        strat = s.get("strategy") or "—"
        agg[strat]["entries"] += 1
        if s.get("status") == "WIN":
            agg[strat]["win"] += 1
        elif s.get("status") == "LOSS":
            agg[strat]["loss"] += 1
    out = {}
    for k,v in agg.items():
        n = max(1, v["entries"])
        assertv = round(100.0 * v["win"] / n, 1)
        pl = v["win"] - v["loss"]
        out[k] = {
            "entries": v["entries"],
            "win": v["win"],
            "loss": v["loss"],
            "assert": assertv,
            "pl_sim": pl
        }
    return out

# ========================= Motor ====================================
def trade_lock_active():
    if not STRICT_ONE_AT_A_TIME:
        return False
    return open_trade is not None

def try_open_trade_if_needed():
    global open_trade, cool_color, cool_white, trade_counter
    if trade_lock_active() or not bot_on:
        return
    seq = list(history)
    if not seq: return

    if mode_selected == "BRANCO":
        if cool_white > 0: return
        rep, conf = select_white_with_confluence(seq)
        if rep:
            trade_counter += 1
            open_trade = {
                "id": trade_counter,
                "type": "white", "target": "W", "step": 0,
                "max_gales": rep["max_gales"], "strategy": rep["name"],
                "confluence": conf, "opened_at": len(seq),
                "opened_at_ts": time.time(),
                "phase": "analyzing"
            }
            append_signal_entry("BRANCO","W",0,status="ANALYZING",
                                strategy=rep["name"],max_gales=rep["max_gales"],conf=conf,
                                phase="analyzing", trade_id=trade_counter)
    elif mode_selected == "CORES":
        if cool_color > 0: return
        rep, tgt, conf = select_color_with_confluence(seq)
        if rep and tgt:
            trade_counter += 1
            open_trade = {
                "id": trade_counter,
                "type": "color", "target": tgt, "step": 0,
                "max_gales": rep["max_gales"], "strategy": rep["name"],
                "confluence": conf, "opened_at": len(seq),
                "opened_at_ts": time.time(),
                "phase": "analyzing"
            }
            append_signal_entry("CORES",tgt,0,status="ANALYZING",
                                strategy=rep["name"],max_gales=rep["max_gales"],conf=conf,
                                phase="analyzing", trade_id=trade_counter)

def process_new_number(n):
    global open_trade, cool_color, cool_white

    tick_cooldowns()

    if not open_trade:
        try_open_trade_if_needed()
        return

    # Cancelamento defensivo se o modo mudou
    if (mode_selected == "BRANCO" and open_trade.get("type") == "color") or \
       (mode_selected == "CORES"  and open_trade.get("type") == "white"):
        open_trade = None
        return

    # Fase: ANALYZING -> OPEN
    if open_trade.get("phase") == "analyzing":
        open_trade["phase"] = "open"
        append_signal_entry(
            "BRANCO" if open_trade["type"]=="white" else "CORES",
            open_trade["target"], open_trade["step"],
            status="open", strategy=open_trade["strategy"],
            max_gales=open_trade["max_gales"], conf=open_trade.get("confluence"),
            phase="open", trade_id=open_trade["id"]
        )
        return

    # Avaliação do resultado
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
                                conf=open_trade.get("confluence"), trade_id=open_trade["id"])
            open_trade = None; cool_white = 5
        else:
            if open_trade["step"] >= open_trade["max_gales"]:
                append_signal_entry("BRANCO","W", open_trade["step"], status="LOSS", came=n,
                                    strategy=open_trade["strategy"], max_gales=open_trade["max_gales"],
                                    conf=open_trade.get("confluence"), trade_id=open_trade["id"])
                open_trade = None; cool_white = 5
            else:
                open_trade["step"] += 1
                append_signal_entry("BRANCO","W", open_trade["step"], status="GALE", came=n,
                                    strategy=open_trade["strategy"], max_gales=open_trade["max_gales"],
                                    conf=open_trade.get("confluence"), trade_id=open_trade["id"])
    else:  # color
        tgt = open_trade["target"]
        came = color_code(n)
        hit = (n != 0) and (came == tgt)
        if hit:
            append_signal_entry("CORES", tgt, open_trade["step"], status="WIN", came=n,
                                strategy=open_trade["strategy"], max_gales=open_trade["max_gales"],
                                conf=open_trade.get("confluence"), trade_id=open_trade["id"])
            open_trade = None; cool_color = 2
        else:
            if open_trade["step"] >= open_trade["max_gales"]:
                append_signal_entry("CORES", tgt, open_trade["step"], status="LOSS", came=n,
                                    strategy=open_trade["strategy"], max_gales=open_trade["max_gales"],
                                    conf=open_trade.get("confluence"), trade_id=open_trade["id"])
                open_trade = None; cool_color = 2
            else:
                open_trade["step"] += 1
                append_signal_entry("CORES", tgt, open_trade["step"], status="GALE", came=n,
                                    strategy=open_trade["strategy"], max_gales=open_trade["max_gales"],
                                    conf=open_trade.get("confluence"), trade_id=open_trade["id"])

# ========================= Rotas ====================================
@app.route("/")
def index():
    return send_file("index.html")

@app.route("/state")
def state():
    seq = list(history)
    probs = estimate_probs(seq) if seq else {"W":0.066,"R":0.467,"B":0.467,"rec":("B",0.467)}

    # Saúde da ingest
    server_time = now_iso()
    seconds_since_last = None
    latency_ms = None
    live_ok = True
    if last_ingest_wall is not None:
        seconds_since_last = max(0.0, (datetime.now(timezone.utc) - last_ingest_wall).total_seconds())
        live_ok = seconds_since_last < 30.0
    if last_ingest_mono is not None:
        latency_ms = int((time.monotonic() - last_ingest_mono) * 1000)  # approx desde a última chegada

    # Round/ETA placeholders (não temos relógio do servidor da roleta aqui)
    round_id = len(seq)   # simples: id incremental local
    eta_ms = None

    # Trava/lock reason
    lock_reason = None
    if STRICT_ONE_AT_A_TIME and open_trade is not None:
        lock_reason = "Aguardando terminar GALES do sinal atual"

    # Estatísticas
    strat_stats = stats_by_strategy()

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
        "lock_reason": lock_reason,

        # Telemetria
        "data_source": DATA_SOURCE,
        "server_time": server_time,
        "seconds_since_last": seconds_since_last,
        "latency_ms": latency_ms,
        "live_ok": live_ok,

        # Round/trade
        "round_id": round_id,
        "eta_ms": eta_ms,
        "trade_id": open_trade["id"] if open_trade else None,
        "opened_at_ts": open_trade["opened_at_ts"] if open_trade else None,
        "gales_remaining": (open_trade["max_gales"] - open_trade["step"]) if open_trade else None,

        # Estatísticas
        "strategy_stats": strat_stats
    }
    return jsonify(out)

@app.route("/ingest", methods=["POST"])
def ingest():
    global last_ingest_wall, last_ingest_mono
    payload = request.get_json(silent=True) or {}
    snap = payload.get("history") or []
    added = merge_snapshot(snap)

    # marca ingest
    last_ingest_wall = datetime.now(timezone.utc)
    last_ingest_mono = time.monotonic()

    if added:
        for n in snap[-added:]:
            process_new_number(n)
    else:
        tick_cooldowns()
        if not trade_lock_active():
            try_open_trade_if_needed()
    return jsonify({"ok": True, "added": added, "time": datetime.now().isoformat()})

@app.route("/control", methods=["POST"])
def control():
    global bot_on, mode_selected, open_trade, cool_color, cool_white, \
           EVAL_SAME_SPIN, STRICT_ONE_AT_A_TIME
    data = request.get_json(silent=True) or {}

    if "bot_on" in data:
        bot_on = bool(data["bot_on"])

    if "mode" in data:
        m = str(data["mode"]).strip().upper()
        if m in ("BRANCO","CORES"):
            mode_selected = m
            # Ao trocar o modo, encerra trade aberto e zera cooldowns
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

    return jsonify({
        "ok": True,
        "bot_on": bot_on,
        "mode": mode_selected,
        "confluence_white": CONFLUENCE_MIN_WHITE,
        "confluence_color": CONFLUENCE_MIN_COLOR,
        "risk": SELECAO_RISCO,
        "eval_same_spin": EVAL_SAME_SPIN,
        "strict_one_at_a_time": STRICT_ONE_AT_A_TIME
    })

# ========================= Boot =====================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
