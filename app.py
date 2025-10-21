# app.py
# SPECTRA X — IA de Sinais para Blaze
# Flask API do painel com:
# - IA on-line embutida (aprende a cada giro)
# - Detecção de "mercado ruim" (winrate/entropia)
# - Trava 1 sinal por vez + gales
# - Estatísticas por estratégia (retrocompatíveis)
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

# Fonte de dados (informativo p/ UI)
DATA_SOURCE = "tipminer"              # "blaze" | "tipminer" | "mock"

# ========================= Estado =========================
history = deque(maxlen=HISTORY_MAX)   # números 0..14 (0 = branco)
signals = deque(maxlen=800)           # sinais/logs
last_snapshot = []                    # p/ merge
bot_on = False
mode_selected = "CORES"               # "BRANCO" | "CORES"

# Sinal/trade aberto
open_trade = None
trade_counter = 0

cool_white = 0
cool_color = 0

# Telemetria da ingest
last_ingest_wall = None   # datetime
last_ingest_mono = None   # time.monotonic()

# ========================= Helpers gerais =========================
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

# ========================= IA embutida ======================
# - SpectraLearner (aprendizado on-line)
# - RegimeDetector (mercado ruim por entropia/winrate)
# - BankrollAdvisor (stake sugerida)
import numpy as np
from collections import deque as _deque

try:
    from sklearn.linear_model import SGDClassifier
    _HAS_SK = True
except Exception:
    _HAS_SK = False

_CL_COLOR = np.array([0,1])  # 0=BLACK,1=RED
_CL_WHITE = np.array([0,1])  # 0=NO,1=WHITE

class SpectraLearner:
    def __init__(self, hist_max=2000):
        self.hist = _deque(maxlen=hist_max)
        self._boot_white = True
        self._boot_color = True
        # fallback beta (sem sklearn)
        self._wins_red = 1.0; self._loss_red = 1.0
        self._wins_white = 1.0; self._loss_white = 13.0
        if _HAS_SK:
            self.m_color = SGDClassifier(loss="log_loss", random_state=7)
            self.m_white = SGDClassifier(loss="log_loss", random_state=7)
        else:
            self.m_color = None; self.m_white = None

    def _features(self, ctx):
        ult = ctx.get("ultimos", [])
        lat = float(ctx.get("lat_ms", 0))/1000.0
        hora = ctx.get("hora", datetime.utcnow().hour)
        dow  = ctx.get("dow", datetime.utcnow().weekday())
        conf = float(ctx.get("confluencia", 0))
        strat_bits = ctx.get("estrats_bits", [])

        cores = [u["cor"] for u in ult]
        nums  = [u["num"] for u in ult]
        n = len(ult)

        def prop(c): return (cores.count(c)/n) if n else 0.0

        streak = 0
        if n:
            last=None
            for i in range(n-1,-1,-1):
                c = cores[i]
                if c=='W':
                    if streak==0: continue
                    else: break
                if last is None or c==last: streak+=1; last=c
                else: break

        alt=0; k=min(10,max(0,n-1)); st=max(0,n-k)
        for i in range(st,n-1):
            if cores[i] != 'W' and cores[i+1] != 'W' and cores[i]!=cores[i+1]:
                alt+=1

        dist_w = 999
        for i in range(n-1,-1,-1):
            if cores[i]=='W': dist_w=(n-1)-i; break

        ult20 = nums[-20:] if n else []
        total20 = max(1,len(ult20))
        pares   = sum(1 for x in ult20 if x%2==0)
        impares = total20 - pares
        p_par   = pares/total20
        p_imp   = impares/total20

        h_norm = float(hora)/23.0
        d_norm = float(dow)/6.0

        x = [
            prop('R'), prop('B'), prop('W'),
            float(streak), float(alt)/10.0,
            float(dist_w)/40.0,
            p_par, p_imp,
            conf/6.0, lat, h_norm, d_norm
        ] + list(map(float, strat_bits or []))
        return np.array(x, dtype=float)

    def predict(self, ctx):
        x = self._features(ctx).reshape(1,-1)
        if not _HAS_SK:
            p_red   = self._wins_red / (self._wins_red + self._loss_red)
            p_white = self._wins_white / (self._wins_white + self._loss_white)
            p_red   = float(max(0.05, min(0.95, p_red)))
            p_white = float(max(0.01, min(0.40, p_white)))
            return p_red, p_white
        p_red=0.5; p_white=0.07
        if not self._boot_color: p_red = float(self.m_color.predict_proba(x)[0,1])
        if not self._boot_white: p_white= float(self.m_white.predict_proba(x)[0,1])
        return p_red, p_white

    def update(self, ctx, resultado):
        c = resultado.get("cor_saida")
        x = self._features(ctx).reshape(1,-1)
        # WHITE
        y_w = np.array([1 if c=='W' else 0])
        if _HAS_SK:
            if self._boot_white:
                self.m_white.partial_fit(x, y_w, classes=_CL_WHITE); self._boot_white=False
            else:
                self.m_white.partial_fit(x, y_w)
        else:
            if c=='W': self._wins_white+=1.0
            else:      self._loss_white+=1.0
        # COLOR (ignora branco)
        if c!='W':
            y_c = np.array([1 if c=='R' else 0])
            if _HAS_SK:
                if self._boot_color:
                    self.m_color.partial_fit(x, y_c, classes=_CL_COLOR); self._boot_color=False
                else:
                    self.m_color.partial_fit(x, y_c)
            else:
                if c=='R': self._wins_red+=1.0
                else:      self._loss_red+=1.0

# RegimeDetector simples (entropia/winrate)
import math as _math
import collections as _collections
def _entropy(p, eps=1e-9):
    p = max(eps, min(1.0-eps, p))
    return - (p*_math.log(p) + (1.0-p)*_math.log(1.0-p))

class RegimeDetector:
    def __init__(self, win_window=60, ent_window=10, ent_thr=0.95):
        self.last_preds = _collections.deque(maxlen=ent_window)
        self.last_outcomes = _collections.deque(maxlen=win_window)
        self.ent_thr = ent_thr
    def update_pred(self, p_major): self.last_preds.append(p_major)
    def update_outcome(self, win01): self.last_outcomes.append(1 if win01 else 0)
    def winrate(self):
        n = max(1, len(self.last_outcomes))
        return sum(self.last_outcomes)/n
    def entropia_alta(self):
        if len(self.last_preds) < self.last_preds.maxlen: return False
        e = sum(_entropy(p) for p in self.last_preds)/len(self.last_preds)
        return e > self.ent_thr
    def mercado_ruim(self):
        return (self.winrate() < 0.48) or self.entropia_alta()

# BankrollAdvisor (pronto p/ uso futuro)
class BankrollAdvisor:
    def __init__(self, banca=20.0, frac_base=0.01, frac_cap=0.02):
        self.banca=banca; self.frac_base=frac_base; self.frac_cap=frac_cap
    def set_banca(self, v): self.banca=float(v)
    def stake_sugerida(self, p_win_est, odds_net=1.0):
        if p_win_est is None: return round(self.banca*self.frac_base,2)
        edge = max(0.0, (p_win_est*odds_net - (1.0 - p_win_est)))
        f = min(self.frac_cap, max(self.frac_base, edge/(odds_net+1e-6)))
        return round(self.banca*f, 2)

# Instâncias da IA
learner = SpectraLearner(hist_max=2000)
regime  = RegimeDetector()
bank    = BankrollAdvisor(banca=20.0)

# ========================= Estratégias (legado p/ confluência/estatísticas) ======================
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
    rep = min(matches, key=lambda x: x["max_gales"]) if SELECAO_RISCO=="conservador" else max(matches, key=lambda x: x["max_gales"])
    return rep, len(matches)

def select_color_with_confluence(seq):
    # votos de R/B por estratégias (legado p/ estatística/visual)
    votes = {"R": [], "B": []}
    COLOR_STRATS = []  # (mantive removido p/ simplificar; podemos reativar depois)
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
    rep = min(votes[target], key=lambda x: x["max_gales"]) if SELECAO_RISCO=="conservador" else max(votes[target], key=lambda x: x["max_gales"])
    return rep, target, conf

# ========================= Probabilidades (IA) ======================
def _build_ctx_for_ai(seq, confluencia=0, estrats_bits=None):
    ult = []
    for v in seq[-40:]:
        if v == 0: ult.append({"cor":"W","num":0})
        else:      ult.append({"cor": "R" if is_red(v) else "B", "num": int(v)})
    lat_ms = int((time.monotonic() - last_ingest_mono) * 1000) if last_ingest_mono is not None else 0
    now = datetime.now()
    return {
        "ultimos": ult,
        "lat_ms": lat_ms,
        "hora": now.hour,
        "dow": now.weekday(),
        "confluencia": int(confluencia or 0),
        "estrats_bits": list(estrats_bits or [])
    }

def estimate_probs_ai(seq):
    if not seq:
        return {"W":0.066,"R":0.467,"B":0.467,"rec":("B",0.467)}
    ctx = _build_ctx_for_ai(seq)
    try:
        p_red, p_white = learner.predict(ctx)
        p_white = max(0.01, min(0.40, float(p_white)))
        p_red   = max(0.05, min(0.94, float(p_red)))
        p_black = max(0.01, min(0.94, 1.0 - p_red))
        rem = max(0.0001, 1.0 - p_white)
        s = (p_red + p_black)
        if s <= 1e-6:
            p_red = rem*0.5; p_black = rem*0.5
        else:
            p_red   = rem * (p_red / s)
            p_black = rem * (p_black / s)
        best = max([("W",p_white),("R",p_red),("B",p_black)], key=lambda x: x[1])
        return {"W":p_white,"R":p_red,"B":p_black,"rec":best}
    except Exception:
        # fallback heurístico simples
        baseW = 1.0/15.0
        extraW = 0.03 if gap_white(seq)>=18 else 0.0
        if whites_in_window(seq,10)>=1: extraW += 0.02
        pW = min(0.40, baseW + extraW)
        r20,b20 = counts_last(seq,20); tot=max(1,r20+b20)
        pR_raw=(r20+1)/(tot+2); pB_raw=(b20+1)/(tot+2)
        rem=max(0.0,1.0-pW); pR=pR_raw*rem; pB=pB_raw*rem
        s=pW+pR+pB; pW,pR,pB = pW/s,pR/s,pB/s
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
    # Mantemos seu abridor legado por enquanto (UI/estatística).
    # Em próxima etapa, trocamos para decisão 100% pela IA com thresholds.
    global open_trade, cool_color, cool_white, trade_counter
    if trade_lock_active() or not bot_on:
        return
    seq = list(history)
    if not seq: return

    # IA: capturar probabilidade para registrar no Regime
    try:
        ctx_tmp = _build_ctx_for_ai(seq)
        p_red, p_white = learner.predict(ctx_tmp)
        p_major = max(p_white, p_red, 1.0 - p_red)
        regime.update_pred(float(p_major))
    except Exception:
        pass

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
        # Escolha por IA: R x B (mantendo confluência mínima simbólica=1)
        # (para não mudar muito o seu fluxo já rodando)
        try:
            p_red, p_white = learner.predict(_build_ctx_for_ai(seq))
            tgt = "R" if p_red >= (1.0 - p_red) else "B"
            conf = 1  # placeholder de confluência para UI
            trade_counter += 1
            open_trade = {
                "id": trade_counter,
                "type": "color", "target": tgt, "step": 0,
                "max_gales": 2, "strategy": "IA Spectra X",
                "confluence": conf, "opened_at": len(seq),
                "opened_at_ts": time.time(),
                "phase": "analyzing"
            }
            append_signal_entry("CORES",tgt,0,status="ANALYZING",
                                strategy="IA Spectra X",max_gales=2,conf=conf,
                                phase="analyzing", trade_id=trade_counter)
        except Exception:
            pass

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

    # Avaliação do resultado (depende do timing)
    if not EVAL_SAME_SPIN:
        if len(history) <= open_trade["opened_at"]:
            return
    else:
        if len(history) < open_trade["opened_at"]:
            return

    # WHITE
    if open_trade["type"] == "white":
        hit = (n == 0)
        if hit:
            append_signal_entry("BRANCO","W", open_trade["step"], status="WIN", came=n,
                                strategy=open_trade["strategy"], max_gales=open_trade["max_gales"],
                                conf=open_trade.get("confluence"), trade_id=open_trade["id"])
            # >>> IA UPDATE
            try:
                ctx_ai = _build_ctx_for_ai(list(history), confluencia=open_trade.get("confluence"))
                learner.update(ctx_ai, {"cor_saida": "W"})
                regime.update_outcome(1)
            except Exception:
                pass
            open_trade = None; cool_white = 5
        else:
            if open_trade["step"] >= open_trade["max_gales"]:
                append_signal_entry("BRANCO","W", open_trade["step"], status="LOSS", came=n,
                                    strategy=open_trade["strategy"], max_gales=open_trade["max_gales"],
                                    conf=open_trade.get("confluence"), trade_id=open_trade["id"])
                # >>> IA UPDATE
                try:
                    ctx_ai = _build_ctx_for_ai(list(history), confluencia=open_trade.get("confluence"))
                    learner.update(ctx_ai, {"cor_saida": ("R" if is_red(n) else "B")})
                    regime.update_outcome(0)
                except Exception:
                    pass
                open_trade = None; cool_white = 5
            else:
                open_trade["step"] += 1
                append_signal_entry("BRANCO","W", open_trade["step"], status="GALE", came=n,
                                    strategy=open_trade["strategy"], max_gales=open_trade["max_gales"],
                                    conf=open_trade.get("confluence"), trade_id=open_trade["id"])

    # COLOR
    else:
        tgt = open_trade["target"]
        came = color_code(n)
        hit = (n != 0) and (came == tgt)
        if hit:
            append_signal_entry("CORES", tgt, open_trade["step"], status="WIN", came=n,
                                strategy=open_trade["strategy"], max_gales=open_trade["max_gales"],
                                conf=open_trade.get("confluence"), trade_id=open_trade["id"])
            # >>> IA UPDATE
            try:
                ctx_ai = _build_ctx_for_ai(list(history), confluencia=open_trade.get("confluence"))
                learner.update(ctx_ai, {"cor_saida": came})
                regime.update_outcome(1)
            except Exception:
                pass
            open_trade = None; cool_color = 2
        else:
            if open_trade["step"] >= open_trade["max_gales"]:
                append_signal_entry("CORES", tgt, open_trade["step"], status="LOSS", came=n,
                                    strategy=open_trade["strategy"], max_gales=open_trade["max_gales"],
                                    conf=open_trade.get("confluence"), trade_id=open_trade["id"])
                # >>> IA UPDATE
                try:
                    ctx_ai = _build_ctx_for_ai(list(history), confluencia=open_trade.get("confluence"))
                    learner.update(ctx_ai, {"cor_saida": came})
                    regime.update_outcome(0)
                except Exception:
                    pass
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
    probs = estimate_probs_ai(seq) if seq else {"W":0.066,"R":0.467,"B":0.467,"rec":("B",0.467)}

    # Saúde da ingest
    server_time = now_iso()
    seconds_since_last = None
    latency_ms = None
    live_ok = True
    if last_ingest_wall is not None:
        seconds_since_last = max(0.0, (datetime.now(timezone.utc) - last_ingest_wall).total_seconds())
        live_ok = seconds_since_last < 30.0
    if last_ingest_mono is not None:
        latency_ms = int((time.monotonic() - last_ingest_mono) * 1000)

    round_id = len(seq)
    eta_ms = None

    lock_reason = None
    if STRICT_ONE_AT_A_TIME and open_trade is not None:
        lock_reason = "Aguardando terminar GALES do sinal atual"

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
        "strategy_stats": strat_stats,

        # Regime
        "winrate_60": round(regime.winrate(),3) if regime else None,
        "market_bad": regime.mercado_ruim() if regime else None
    }
    return jsonify(out)

@app.route("/predict")
def predict():
    seq = list(history)
    ctx = _build_ctx_for_ai(seq, confluencia=open_trade.get("confluence") if open_trade else 0)
    p_red, p_white = learner.predict(ctx)
    p_black = max(0.0, 1.0 - p_red)
    return jsonify({
        "ok": True,
        "p_red": round(float(p_red),4),
        "p_black": round(float(p_black),4),
        "p_white": round(float(p_white),4),
        "winrate_60": round(regime.winrate(),4) if regime else None
    })

@app.route("/stats")
def stats():
    wr60 = regime.winrate() if regime else None
    bad  = regime.mercado_ruim() if regime else None
    ent  = regime.entropia_alta() if regime else None
    return jsonify({
        "ok": True,
        "winrate_60": wr60,
        "mercado_ruim": bad,
        "entropia_alta": ent,
        "open_trade": open_trade
    })

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
