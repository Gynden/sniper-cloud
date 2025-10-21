# app.py — SPECTRA X (IA de Sinais) - painel Spectra minimalista
import time, math
from collections import deque, defaultdict
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request, send_file, make_response
from flask_cors import CORS

app = Flask(__name__, static_url_path="", static_folder=".", template_folder=".")
CORS(app)

# ========================= Config =========================
HISTORY_MAX = 1200
CONFLUENCE_MIN_WHITE = 2
CONFLUENCE_MIN_COLOR = 1
SELECAO_RISCO = "conservador"      # "conservador" | "agressivo"
EVAL_SAME_SPIN = False
STRICT_ONE_AT_A_TIME = True
DATA_SOURCE = "tipminer"

# ===== Gerenciamento de banca / sessão =====
BANKROLL          = 100.0
STAKE_PCT_BASE    = 0.02
MODE_RISK         = "normal"  # "seguro" | "normal" | "agressivo"

STOP_WIN_PCT      = 0.05
STOP_LOSS_PCT     = 0.05

CONF_GOOD         = 0.60
CONF_OK           = 0.55
WR_BAD            = 0.48
WR_GOOD_RESUME    = 0.55

# ========================= Estado =========================
history = deque(maxlen=HISTORY_MAX)   # roleta (0..14)
signals = deque(maxlen=2000)          # log para estatística/relatório
last_snapshot = []
bot_on = False
mode_selected = "CORES"               # "BRANCO" | "CORES"

open_trade = None
trade_counter = 0
cool_white = 0
cool_color = 0

last_ingest_wall = None
last_ingest_mono = None

# ========================= Utils =========================
def is_red(n):   return 1 <= n <= 7
def color_code(n):
    if n == 0: return "W"
    return "R" if is_red(n) else "B"

def now_iso_utc():  return datetime.now(timezone.utc).isoformat()
def now_local_hms(): return datetime.now().strftime("%H:%M:%S")

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

def whites_in_window(seq, k): return sum(1 for v in seq[-k:] if v==0)
def gap_white(seq):
    for i in range(len(seq)-1, -1, -1):
        if seq[i] == 0:
            return (len(seq)-1) - i
    return len(seq)

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
            history.append(n); added += 1
        return added

    a = snap; b = snap[::-1]
    oa = overlap(last_snapshot, a)
    ob = overlap(last_snapshot, b)
    chosen = a if oa >= ob else b
    k = max(oa, ob)

    if k >= len(chosen):
        last_snapshot = list(chosen); return 0

    new_tail = chosen[k:]
    for n in new_tail:
        history.append(n); added += 1
    last_snapshot = list(chosen)
    return added

def tick_cooldowns():
    global cool_white, cool_color
    if cool_white > 0: cool_white -= 1
    if cool_color > 0: cool_color -= 1

# ========================= IA leve ======================
class SpectraLearner:
    """Contagens tipo 'beta' simples."""
    def __init__(self):
        self.red_s = 1.0; self.red_f = 1.0
        self.white_s = 1.0; self.white_f = 13.0
        self.last_preds = deque(maxlen=10)
        self.recent_outcomes = deque(maxlen=60)

    def predict(self):
        p_red = self.red_s / (self.red_s + self.red_f)
        p_white = self.white_s / (self.white_s + self.white_f)
        p_red   = max(0.05, min(0.95, p_red))
        p_white = max(0.01, min(0.40, p_white))
        return p_red, p_white

    def update_from_spin(self, outcome_color):
        if outcome_color == "W":
            self.white_s += 1.0
        else:
            self.white_f += 1.0
            if outcome_color == "R": self.red_s += 1.0
            else:                    self.red_f += 1.0

    def register_decision_quality(self, win: bool):
        self.recent_outcomes.append(1 if win else 0)

    def winrate60(self):
        n = max(1, len(self.recent_outcomes))
        return sum(self.recent_outcomes)/n

    def push_pred_major(self, p_major):
        self.last_preds.append(max(1e-6, min(1-1e-6, p_major)))

    def entropy_high(self):
        if len(self.last_preds) < self.last_preds.maxlen: return False
        def H(p): return - (p*math.log(p) + (1-p)*math.log(1-p))
        e = sum(H(p) for p in self.last_preds)/len(self.last_preds)
        return e > 0.95

learner = SpectraLearner()

# ========================= White legado ======================
WHITE_STRATS = [
    {"name": "Gap 18+", "max_gales": 2, "check": lambda s: gap_white(s) >= 18},
    {"name": "Cluster recente (≤10)", "max_gales": 1, "check": lambda s: whites_in_window(s,10)>=1},
    {"name": "Baixa densidade (≤2 em 50)", "max_gales": 2, "check": lambda s: whites_in_window(s,50) <= 2},
]
def select_white(seq):
    matches=[st for st in WHITE_STRATS if st["check"](seq)]
    if len(matches) < CONFLUENCE_MIN_WHITE: return None,0
    rep = min(matches, key=lambda x: x["max_gales"]) if SELECAO_RISCO=="conservador" else max(matches, key=lambda x: x["max_gales"])
    return rep, len(matches)

# ========================= Probs para UI ======================
def estimate_probs_ai(seq):
    p_red, p_white = learner.predict()
    p_black = 1.0 - p_red
    rem = max(0.0001, 1.0 - p_white)
    s = p_red + p_black
    p_red = rem*(p_red/s); p_black = rem*(p_black/s)
    best = max([("W",p_white),("R",p_red),("B",p_black)], key=lambda x: x[1])
    return {"W":p_white,"R":p_red,"B":p_black,"rec":best}

# ========================= Logging ======================
def format_label(target, step, max_gales, conf=None):
    name = {"W":"BRANCO","R":"VERMELHO","B":"PRETO"}[target]
    conf_txt = (f" — Confluência: {conf}" if conf else "")
    if step == 0:  return f"{name} (até {max_gales} gale{'s' if max_gales!=1 else ''}){conf_txt}"
    return f"{name} — GALE {step}"

def append_signal_entry(mode, target, step, status="open", came=None,
                        strategy=None, max_gales=0, conf=None, phase=None, trade_id=None):
    came_color = color_code(came) if isinstance(came, int) else None
    signals.appendleft({
        "ts": now_local_hms(),
        "iso_ts": now_iso_utc(),
        "mode": mode, "target": target,
        "status": status, "phase": phase,
        "gale": step, "max_gales": max_gales,
        "label": format_label(target, step, max_gales, conf),
        "strategy": strategy or ("IA Spectra X" if mode=="CORES" else "Estratégia branca"),
        "confluence": conf,
        "came": came, "came_color": came_color,
        "trade_id": trade_id
    })

# ========================= Estatísticas ======================
def stats_by_strategy():
    agg = defaultdict(lambda: {"entries":0,"win":0,"loss":0})
    for s in signals:
        if s.get("status") in ("open","ANALYZING","GALE"): continue
        strat = s.get("strategy") or "—"
        agg[strat]["entries"] += 1
        if s.get("status") == "WIN":  agg[strat]["win"]  += 1
        if s.get("status") == "LOSS": agg[strat]["loss"] += 1
    out={}
    for k,v in agg.items():
        n=max(1,v["entries"]); assertv=round(100.0*v["win"]/n,1); pl=v["win"]-v["loss"]
        out[k]={"entries":v["entries"],"win":v["win"],"loss":v["loss"],"assert":assertv,"pl_sim":pl}
    return out

def hourly_assertivity(last_hours=24, kind_filter=None):
    now = datetime.now(timezone.utc)
    buckets = {}
    for i in range(last_hours, -1, -1):
        h = (now - timedelta(hours=i)).replace(minute=0, second=0, microsecond=0)
        buckets[h] = {"win":0,"loss":0}
    for s in signals:
        if s.get("status") not in ("WIN","LOSS"): continue
        if kind_filter and s.get("mode") != kind_filter: continue
        try: ts = datetime.fromisoformat(s["iso_ts"])
        except: continue
        ts = ts.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        if ts in buckets:
            if s["status"]=="WIN": buckets[ts]["win"]  += 1
            else:                  buckets[ts]["loss"] += 1
    out=[]
    for h in sorted(buckets.keys()):
        w=buckets[h]["win"]; l=buckets[h]["loss"]; n=max(1,w+l)
        out.append({"t":h.isoformat().replace("+00:00","Z"),
                    "win":w,"loss":l,"acc": round(w/n,4)})
    return out

# ===== Sessão / Stake / Coach =====
def session_pnl_units():
    u = 0
    for s in signals:
        if s.get("status") == "WIN":  u += 1
        if s.get("status") == "LOSS": u -= 1
    return u

def bankroll_delta_and_stops():
    units = session_pnl_units()
    stake_med = max(0.5, BANKROLL * STAKE_PCT_BASE)
    delta = units * stake_med
    hit_win  = (delta >=  BANKROLL * STOP_WIN_PCT)
    hit_loss = (delta <= -BANKROLL * STOP_LOSS_PCT)
    return delta, hit_win, hit_loss

def suggest_stake(conf_pct, mkt_bad):
    base = STAKE_PCT_BASE
    if MODE_RISK == "seguro":      base *= 0.5
    elif MODE_RISK == "agressivo": base *= 1.5
    if mkt_bad:
        pct = base * 0.5
        return round(BANKROLL * pct, 2), "reduzido"
    if conf_pct >= 100*CONF_GOOD:
        pct = base * 1.0
    elif conf_pct >= 100*CONF_OK:
        pct = base * 0.5
    else:
        pct = 0.0
    return round(BANKROLL * pct, 2), ("normal" if pct else "aguardar")

def coach_message(state):
    msgs = []
    if state.get("market_bad"): msgs.append("Mercado irregular (winrate baixo/entropia).")
    if state.get("stop_hit") == "win":  msgs.append("Stop Win atingido — pausar.")
    if state.get("stop_hit") == "loss": msgs.append("Stop Loss atingido — pausar.")
    ot = state.get("open_trade")
    if ot:
        msgs.append(f"Sinal {ot.get('target')} • GALE {ot.get('step',0)}/{ot.get('max_gales',0)}.")
    else:
        rec = state.get("probs",{}).get("rec",{})
        if rec:
            tgt, p = rec.get("tgt"), rec.get("p")
            if not state.get("market_bad") and p >= 55:
                lab = {"R":"Vermelho","B":"Preto","W":"Branco"}.get(tgt,"—")
                msgs.append(f"Entrada sugerida: {lab} ({p}%).")
            else:
                msgs.append("Aguardando padrão com confiança.")
    return " ".join(msgs[:2]) or "—"

# ========================= Motor ====================================
def trade_lock_active():
    return STRICT_ONE_AT_A_TIME and (open_trade is not None)

def try_open_trade_if_needed():
    global open_trade, cool_color, cool_white, trade_counter
    if trade_lock_active() or not bot_on: return
    seq = list(history)
    if not seq: return

    p_red, p_white = learner.predict()
    p_major = max(p_white, p_red, 1.0-p_red)
    learner.push_pred_major(p_major)

    if mode_selected == "BRANCO":
        if cool_white > 0: return
        rep, conf = select_white(seq)
        if rep:
            trade_counter += 1
            open_trade = {
                "id": trade_counter, "type": "white", "target": "W",
                "step": 0, "max_gales": rep["max_gales"],
                "strategy": rep["name"], "confluence": conf,
                "opened_at": len(seq), "opened_at_ts": time.time(),
                "phase": "analyzing"
            }
            append_signal_entry("BRANCO","W",0,status="ANALYZING",
                                strategy=rep["name"],max_gales=rep["max_gales"],conf=conf,
                                phase="analyzing", trade_id=trade_counter)
    else:
        if cool_color > 0: return
        tgt = "R" if p_red >= (1.0 - p_red) else "B"
        trade_counter += 1
        open_trade = {
            "id": trade_counter, "type": "color", "target": tgt,
            "step": 0, "max_gales": 2,
            "strategy": "IA Spectra X", "confluence": 1,
            "opened_at": len(seq), "opened_at_ts": time.time(),
            "phase": "analyzing"
        }
        append_signal_entry("CORES",tgt,0,status="ANALYZING",
                            strategy="IA Spectra X",max_gales=2,conf=1,
                            phase="analyzing", trade_id=trade_counter)

def process_new_number(n):
    """Processa giro novo e resolve trade aberto."""
    global open_trade, cool_color, cool_white

    tick_cooldowns()
    learner.update_from_spin(color_code(n))  # IA aprende TODO giro

    if not open_trade:
        try_open_trade_if_needed()
        return

    # Mudou o modo? cancela
    if (mode_selected == "BRANCO" and open_trade.get("type") == "color") or \
       (mode_selected == "CORES"  and open_trade.get("type") == "white"):
        open_trade = None
        return

    # ANALYZING -> OPEN
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

    # Avaliar no giro subsequente
    if not EVAL_SAME_SPIN:
        if len(history) <= open_trade["opened_at"]:
            return
    else:
        if len(history) < open_trade["opened_at"]:
            return

    tgt  = open_trade["target"]
    came = color_code(n)
    accurate_hit = (came == tgt) and (came in ("R","B") if tgt in ("R","B") else (came=="W"))

    if accurate_hit:
        append_signal_entry(
            "BRANCO" if open_trade["type"]=="white" else "CORES",
            tgt, open_trade["step"], status="WIN", came=n,
            strategy=open_trade["strategy"], max_gales=open_trade["max_gales"],
            conf=open_trade.get("confluence"), trade_id=open_trade["id"]
        )
        learner.register_decision_quality(True)
        if open_trade["type"]=="white": cool_white = 5
        else:                           cool_color = 2
        open_trade = None
    else:
        if open_trade["step"] >= open_trade["max_gales"]:
            append_signal_entry(
                "BRANCO" if open_trade["type"]=="white" else "CORES",
                tgt, open_trade["step"], status="LOSS", came=n,
                strategy=open_trade["strategy"], max_gales=open_trade["max_gales"],
                conf=open_trade.get("confluence"), trade_id=open_trade["id"]
            )
            learner.register_decision_quality(False)
            if open_trade["type"]=="white": cool_white = 5
            else:                           cool_color = 2
            open_trade = None
        else:
            open_trade["step"] += 1
            append_signal_entry(
                "BRANCO" if open_trade["type"]=="white" else "CORES",
                tgt, open_trade["step"], status="GALE", came=n,
                strategy=open_trade["strategy"], max_gales=open_trade["max_gales"],
                conf=open_trade.get("confluence"), trade_id=open_trade["id"]
            )

# ========================= Rotas ====================================
@app.route("/")
def index():
    return send_file("index.html")

@app.route("/state")
def state():
    seq = list(history)
    probs = estimate_probs_ai(seq) if seq else {"W":0.066,"R":0.467,"B":0.467,"rec":("B",0.467)}

    # Telemetria
    server_time = now_iso_utc()
    seconds_since_last = None
    latency_ms = None
    live_ok = True
    if last_ingest_wall is not None:
        seconds_since_last = max(0.0, (datetime.now(timezone.utc) - last_ingest_wall).total_seconds())
        live_ok = seconds_since_last < 30.0
    if last_ingest_mono is not None:
        latency_ms = int((time.monotonic() - last_ingest_mono) * 1000)

    lock_reason = None
    if STRICT_ONE_AT_A_TIME and open_trade is not None:
        lock_reason = "Aguardando terminar GALES do sinal atual"

    strat_stats = stats_by_strategy()

    # ===== Autopause & stake & coach =====
    wr60 = learner.winrate60()
    market_bad = (wr60 < WR_BAD) or learner.entropy_high()
    delta, hit_win, hit_loss = bankroll_delta_and_stops()
    stop_hit = "win" if hit_win else ("loss" if hit_loss else None)
    autopause = market_bad or bool(stop_hit)

    conf_pct = round(100*probs["rec"][1], 1) if isinstance(probs["rec"], tuple) else probs["rec"]["p"]
    stake_value, stake_mode = suggest_stake(conf_pct, autopause)

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

        "confluence": {"white_min": CONFLUENCE_MIN_WHITE, "color_min": CONFLUENCE_MIN_COLOR, "selecao_risco": SELECAO_RISCO},
        "eval_same_spin": EVAL_SAME_SPIN,
        "strict_one_at_a_time": STRICT_ONE_AT_A_TIME,
        "lock_reason": lock_reason,

        "data_source": DATA_SOURCE,
        "server_time": server_time,
        "seconds_since_last": seconds_since_last,
        "latency_ms": latency_ms,
        "live_ok": live_ok,

        "round_id": len(seq),
        "trade_id": open_trade["id"] if open_trade else None,
        "strategy_stats": strat_stats,

        # sessão/coach
        "winrate_60": round(wr60,3),
        "market_bad": market_bad,
        "autopause": autopause,
        "stop_hit": stop_hit,
        "session_delta": round(delta,2),
        "stake_suggested": {"value": stake_value, "mode": stake_mode},
        "coach": coach_message({
            "market_bad": market_bad,
            "stop_hit": stop_hit,
            "open_trade": open_trade,
            "probs": {"rec":{"tgt": probs["rec"][0], "p": round(100*probs["rec"][1],1)}}
        })
    }
    return jsonify(out)

@app.route("/predict")
def predict():
    p_red, p_white = learner.predict()
    return jsonify({"ok": True, "p_red": p_red, "p_black": 1-p_red, "p_white": p_white})

@app.route("/stats")
def stats():
    return jsonify({
        "ok": True,
        "winrate_60": learner.winrate60(),
        "mercado_ruim": (learner.winrate60() < WR_BAD) or learner.entropy_high(),
        "entropia_alta": learner.entropy_high(),
        "open_trade": open_trade
    })

@app.route("/history")
def history_api():
    try: hours = int(request.args.get("hours","24"))
    except: hours = 24
    kind = request.args.get("kind") or None
    points = hourly_assertivity(last_hours=max(1,min(168,hours)), kind_filter=kind if kind in ("CORES","BRANCO") else None)
    return jsonify({"ok": True, "points": points})

# ===== NOVO: endpoint de performance (PnL, streaks, heatmap) =====
@app.route("/performance")
def performance():
    # PnL acumulado por ordem cronológica (unidades +1/-1)
    curve = []
    cum = 0
    # signals é appendleft(); para cronologia, iteramos do fim para o começo
    for s in reversed(signals):
        if s.get("status") not in ("WIN","LOSS"): continue
        ts = s.get("iso_ts") or now_iso_utc()
        if s["status"] == "WIN":  cum += 1
        else:                     cum -= 1
        curve.append({"t": ts, "pnl": cum})

    # Streaks (atual e máximos)
    cur_streak = {"side": None, "len": 0}
    max_win = 0; max_loss = 0
    # calcular máximos
    run = 0; side = None
    for s in reversed(signals):
        if s.get("status") not in ("WIN","LOSS"): continue
        this = 1 if s["status"]=="WIN" else -1
        if side is None or (this>0 and side>0) or (this<0 and side<0):
            run += 1; side = this
        else:
            if side>0: max_win = max(max_win, run)
            else:      max_loss = max(max_loss, run)
            run = 1; side = this
    if side is not None:
        if side>0: max_win = max(max_win, run)
        else:      max_loss = max(max_loss, run)

    # streak atual (olhando da esquerda — mais recente primeiro)
    run = 0; side = None
    for s in signals:
        if s.get("status") not in ("WIN","LOSS"): continue
        this = 1 if s["status"]=="WIN" else -1
        if side is None: side = this
        if (this>0 and side>0) or (this<0 and side<0):
            run += 1
        else:
            break
    if side is not None:
        cur_streak["side"] = "WIN" if side>0 else "LOSS"
        cur_streak["len"]  = run

    # Heatmap por hora (últimos 7 dias)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    buckets = {h: {"win":0,"tot":0} for h in range(24)}
    for s in signals:
        if s.get("status") not in ("WIN","LOSS"): continue
        try: ts = datetime.fromisoformat(s["iso_ts"])
        except: continue
        if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff: continue
        h = ts.hour
        buckets[h]["tot"] += 1
        if s["status"]=="WIN": buckets[h]["win"] += 1
    heat = []
    for h in range(24):
        w=buckets[h]["win"]; t=buckets[h]["tot"]; acc = (w/max(1,t))
        heat.append({"hour": h, "acc": round(acc,4), "n": t})

    return jsonify({"ok": True, "pnl_curve": curve, "streaks": {"current": cur_streak, "max_win": max_win, "max_loss": max_loss}, "hour_heatmap": heat})

@app.route("/report")
def report():
    rows = []
    for s in list(signals)[:300]:
        if s.get("status") in ("open","ANALYZING","GALE"): continue
        rows.append(f"<tr><td>{s['iso_ts']}</td><td>{s['mode']}</td><td>{s['strategy']}</td><td>{s['target']}</td><td>{s['status']}</td></tr>")
    html = f"""
    <html><head><meta charset='utf-8'><title>Relatório SPECTRA X</title>
    <style>body{{font:14px Arial;color:#111}} table{{border-collapse:collapse;width:100%}} th,td{{border:1px solid #ddd;padding:6px}} th{{background:#eee}}</style>
    </head><body>
    <h2>Relatório — SPECTRA X</h2>
    <p>Gerado em {now_iso_utc()}</p>
    <p>Winrate(60): {round(learner.winrate60()*100,1)}% — Mercado: {"Ruim" if (learner.winrate60()<WR_BAD or learner.entropy_high()) else "OK"}</p>
    <h3>Últimas entradas</h3>
    <table><thead><tr><th>Quando</th><th>Modo</th><th>Estratégia</th><th>Alvo</th><th>Resultado</th></tr></thead>
    <tbody>{''.join(rows) or "<tr><td colspan='5'>Sem dados.</td></tr>"}</tbody></table>
    <script>window.onload=()=>window.print()</script>
    </body></html>"""
    resp = make_response(html); resp.headers["Content-Type"]="text/html; charset=utf-8"
    return resp

@app.route("/ingest", methods=["POST"])
def ingest():
    global last_ingest_wall, last_ingest_mono
    payload = request.get_json(silent=True) or {}
    snap = payload.get("history") or []
    added = merge_snapshot(snap)
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
           EVAL_SAME_SPIN, STRICT_ONE_AT_A_TIME, CONFLUENCE_MIN_WHITE, CONFLUENCE_MIN_COLOR, SELECAO_RISCO
    data = request.get_json(silent=True) or {}

    if "bot_on" in data: bot_on = bool(data["bot_on"])
    if "mode" in data:
        m = str(data["mode"]).strip().upper()
        if m in ("BRANCO","CORES"):
            mode_selected = m; open_trade = None; cool_white = 0; cool_color = 0
    if "confluence_white" in data:
        try: CONFLUENCE_MIN_WHITE = max(1, int(data["confluence_white"]))
        except: pass
    if "confluence_color" in data:
        try: CONFLUENCE_MIN_COLOR = max(1, int(data["confluence_color"]))
        except: pass
    if "risk" in data and data["risk"] in ("conservador","agressivo"):
        SELECAO_RISCO = data["risk"]
    if "eval_same_spin" in data: EVAL_SAME_SPIN = bool(data["eval_same_spin"])
    if "strict_one_at_a_time" in data: STRICT_ONE_AT_A_TIME = bool(data["strict_one_at_a_time"])

    # novos ajustes de banca
    global BANKROLL, STAKE_PCT_BASE, MODE_RISK, STOP_WIN_PCT, STOP_LOSS_PCT
    if "bankroll" in data:
        try: BANKROLL = float(data["bankroll"])
        except: pass
    if "stake_pct" in data:
        try: STAKE_PCT_BASE = max(0.001, float(data["stake_pct"]))
        except: pass
    if "risk_mode" in data and data["risk_mode"] in ("seguro","normal","agressivo"):
        MODE_RISK = data["risk_mode"]
    if "stop_win_pct" in data:
        try: STOP_WIN_PCT = max(0.0, float(data["stop_win_pct"]))
        except: pass
    if "stop_loss_pct" in data:
        try: STOP_LOSS_PCT = max(0.0, float(data["stop_loss_pct"]))
        except: pass

    return jsonify({
        "ok": True,
        "bot_on": bot_on,
        "mode": mode_selected,
        "confluence_white": CONFLUENCE_MIN_WHITE,
        "confluence_color": CONFLUENCE_MIN_COLOR,
        "risk": SELECAO_RISCO,
        "eval_same_spin": EVAL_SAME_SPIN,
        "strict_one_at_a_time": STRICT_ONE_AT_A_TIME,
        "bankroll": BANKROLL,
        "stake_pct": STAKE_PCT_BASE,
        "risk_mode": MODE_RISK,
        "stop_win_pct": STOP_WIN_PCT,
        "stop_loss_pct": STOP_LOSS_PCT
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
