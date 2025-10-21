# Spectra X — Backend (Flask) + Front (SPA)
# IA adaptativa + gales + ingest automático do BookWebLear
# - Serve index.html na raiz (mesma origem da API)
# - Usa PORT do Render
# - Thread de ingest que busca rodadas e alimenta /api/push_round internamente
#
# ENV (opcional):
#   BOOKWEBLEAR_URL="https://<sua-url>/endpoint"   # fonte padrão
#   INGEST_INTERVAL="3"                             # segundos
#
# Rotas novas:
#   GET  /api/ingest/state
#   POST /api/ingest/config   -> {enabled:bool, url:str, interval:int}

import os, re, json, math, time, random, threading
from collections import deque, defaultdict
from datetime import datetime
from typing import Deque, Dict, Optional, Any, List

import requests
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

# ===== Flask / Static =====
app = Flask(__name__, static_url_path="", static_folder=".", template_folder=".")
CORS(app)

# ===== Config geral =====
HISTORY_MAX            = 600
SHOW_LAST              = 50
DEFAULT_STAKE          = 2.00
DEFAULT_MAX_GALES      = 2
DEFAULT_MODE           = "hybrid"   # "neural" | "hybrid" | "manual"
CONFLUENCE_MIN_COLOR   = 2          # votos mínimos (red/black)
CONFLUENCE_MIN_WHITE   = 2          # votos mínimos (white)
CONFIDENCE_MIN         = 0.62       # confiança mínima
EW_LEARN_RATE          = 0.12       # taxa de aprendizado dos pesos
WHITE_ODDS             = 14.0       # payout do white
COLOR_ODDS             = 1.0        # payout da cor

# ===== Estado =====
history: Deque[str] = deque(maxlen=HISTORY_MAX)    # "red" | "black" | "white"
metrics = {
    "total_signals": 0,
    "wins": 0,
    "losses": 0,
    "bank_result": 0.0,
    "per_color": {"red":{"signals":0,"wins":0,"losses":0},
                  "black":{"signals":0,"wins":0,"losses":0},
                  "white":{"signals":0,"wins":0,"losses":0}},
}
config = {
    "stake": DEFAULT_STAKE,
    "max_gales": DEFAULT_MAX_GALES,
    "mode": DEFAULT_MODE,
    "confluence_min_color": CONFLUENCE_MIN_COLOR,
    "confluence_min_white": CONFLUENCE_MIN_WHITE,
    "confidence_min": CONFIDENCE_MIN,
}

active_signal: Optional[Dict] = None  # controla gales e bloqueio

# ===== IA: pesos/estratégias =====
strategy_weights: Dict[str, float] = {
    "repeat_three": 1.00,
    "anti_chop": 0.90,
    "cluster_bias": 0.90,
    "recent_white_near": 1.05,
    "long_streak_break": 0.85,
    "time_window_bias": 0.80,
}

def now_iso(): return datetime.utcnow().isoformat()+"Z"
def softmax(scores: Dict[str, float]):
    if not scores: return {}
    m = max(scores.values()); exps = {k: math.exp(v-m) for k,v in scores.items()}
    tot = sum(exps.values()) or 1.0
    return {k: v/tot for k,v in exps.items()}
def normalize_votes(votes: Dict[str, float]) -> Dict[str, float]:
    s = sum(max(0.0, x) for x in votes.values()) or 1.0
    return {k: max(0.0, v)/s for k, v in votes.items()}
def recent_counts(n=10):
    last = list(history)[-n:]
    return {"red": last.count("red"), "black": last.count("black"), "white": last.count("white"), "n": len(last)}

# ===== Estratégias =====
def strat_repeat_three():
    if len(history) < 2: return {}
    if history[-1]==history[-2] and history[-1] in ("red","black"):
        return {history[-1]: 1.0}
    return {}
def strat_anti_chop():
    if len(history)<4: return {}
    h=list(history)
    if h[-4]!=h[-3] and h[-3]!=h[-2] and h[-2]!=h[-1] and {h[-4],h[-2]}=={"red","black"} and {h[-3],h[-1]}=={"red","black"}:
        if h[-1] in ("red","black"): return {h[-1]:1.0}
    return {}
def strat_cluster_bias():
    if len(history)<5: return {}
    last5=list(history)[-5:]; r=last5.count("red"); b=last5.count("black"); out={}
    if r>=3: out["red"]=1.0
    if b>=3: out["black"]=1.0
    return out
def strat_recent_white_near():
    last8=list(history)[-8:]
    if "white" in last8: return {"white":1.0}
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
    "repeat_three": strat_repeat_three,
    "anti_chop": strat_anti_chop,
    "cluster_bias": strat_cluster_bias,
    "recent_white_near": strat_recent_white_near,
    "long_streak_break": strat_long_streak_break,
    "time_window_bias": strat_time_window_bias,
}

def compute_votes() -> Dict[str, float]:
    raw=defaultdict(float)
    for name,func in STRATEGY_FUNCS.items():
        out=func(); w=strategy_weights.get(name,1.0)
        for k,v in out.items(): raw[k]+=v*w
    return normalize_votes(raw)

def odds_for(color:str)->float: return WHITE_ODDS if color=="white" else COLOR_ODDS

def apply_learning(win: bool, signal: Dict):
    global strategy_weights
    delta = EW_LEARN_RATE if win else -EW_LEARN_RATE
    votes = signal.get("strategy_votes", {})
    for name in strategy_weights.keys():
        contrib = votes.get(signal["color"], 0.0)
        jitter = (random.random()-0.5)*0.02
        strategy_weights[name] = max(0.1, strategy_weights[name] + delta*(0.5+contrib) + jitter)

def decide_signal():
    global active_signal
    if active_signal and active_signal.get("status")=="running": return None
    votes = compute_votes()
    scores = {"red":votes.get("red",0.0), "black":votes.get("black",0.0), "white":votes.get("white",0.0)*1.15}
    conf_color = config["confluence_min_color"]/10.0
    conf_white = config["confluence_min_white"]/10.0
    best_color = max(scores, key=lambda k:scores[k]); best_score = scores[best_color]
    probs = softmax(scores); confidence = probs.get(best_color,0.0)
    if best_color in ("red","black"):
        if best_score<conf_color or confidence<config["confidence_min"]: return None
    else:
        if best_score<conf_white or confidence<config["confidence_min"]: return None
    active_signal = {
        "created_at": now_iso(), "color": best_color, "confidence": round(confidence,3),
        "scores": scores, "gale_step": 0, "max_gales": config["max_gales"], "stake": config["stake"],
        "status": "running", "result": None, "history_snapshot_size": len(history),
        "strategy_votes": compute_votes(),
    }
    metrics["total_signals"] += 1
    metrics["per_color"][best_color]["signals"] += 1
    return active_signal

def evaluate_active_signal(new_result:str):
    global active_signal
    s = active_signal
    if not s or s["status"]!="running": return
    target = s["color"]
    hit = (new_result==target)
    if target=="white": hit = (new_result=="white")
    if hit:
        s["status"]="finished"; s["result"]="WIN"
        gain = s["stake"]*odds_for(target)
        metrics["wins"]+=1; metrics["per_color"][target]["wins"]+=1
        metrics["bank_result"]+=gain
        apply_learning(True, s)
    else:
        if s["gale_step"]<s["max_gales"]:
            s["gale_step"] += 1
        else:
            s["status"]="finished"; s["result"]="LOSS"
            loss_total = s["stake"]*(s["gale_step"]+1)
            metrics["losses"]+=1; metrics["per_color"][target]["losses"]+=1
            metrics["bank_result"]-=loss_total
            apply_learning(False, s)

# ===== Ingest: BookWebLear =====
# Config do coletor
ingest_cfg = {
    "enabled": False,
    "url": os.environ.get("BOOKWEBLEAR_URL", "").strip(),
    "interval": int(os.environ.get("INGEST_INTERVAL", "3")),
    "last_token": None,   # identificador da última rodada vista (flexível)
    "ok": False,
    "last_error": None,
}

def _map_color(val: Any) -> Optional[str]:
    """Normaliza vários formatos em 'red' | 'black' | 'white'."""
    if val is None: return None
    s = str(val).strip().lower()
    # números/eventuais códigos
    if s in ("0","white","branco","w"): return "white"
    if s in ("r","red","vermelho","1"): return "red"
    if s in ("b","black","preto","2"): return "black"
    # regex defensivo
    if "white" in s or "branc" in s: return "white"
    if "red" in s or "verm" in s: return "red"
    if "black" in s or "pret" in s: return "black"
    return None

def _parse_bookweblear_payload(text: str) -> List[Dict[str,Any]]:
    """
    Tenta extrair uma LISTA de itens tipo:
      [{"id": "...", "color": "red"|"black"|"white", "time": "..."}]
    Aceita JSON (preferido) ou HTML/Texto com 'red|black|white'.
    """
    items: List[Dict[str,Any]] = []
    # 1) Tenta JSON direto
    try:
        data = json.loads(text)
        # Pode ser dict com 'results' ou list
        seq = data.get("results") if isinstance(data, dict) else data
        if isinstance(seq, list):
            for obj in seq:
                if isinstance(obj, dict):
                    # tenta pegar campos comuns
                    cid = obj.get("id") or obj.get("uuid") or obj.get("round") or obj.get("hash") or obj.get("time") or obj.get("ts")
                    color = _map_color(obj.get("color") or obj.get("result") or obj.get("roll") or obj.get("type"))
                    if color:
                        items.append({"id": str(cid or len(items)), "color": color, "raw": obj})
        # se não for lista, cai para regex
    except Exception:
        pass

    if items:
        return items

    # 2) Regex em HTML/texto — pega última(s) ocorrências de red/black/white
    #   exemplo de snippet: <div class="ball red">...</div>
    regex = re.compile(r"(white|branco|red|vermelh\w*|black|pret\w*)", re.I)
    found = regex.findall(text)
    for i, token in enumerate(found):
        color = _map_color(token)
        if color:
            items.append({"id": f"rx-{i}", "color": color, "raw": token})
    return items

def _fetch_bookweblear(url: str) -> List[Dict[str,Any]]:
    headers = {"User-Agent":"SpectraX/1.0"}
    r = requests.get(url, timeout=10, headers=headers)
    r.raise_for_status()
    return _parse_bookweblear_payload(r.text)

def _ingest_push(color: str):
    """Empilha a cor no histórico e avalia o sinal ativo (mesma lógica do /api/push_round)."""
    color = color.lower().strip()
    if color not in ("red","black","white"): return
    history.append(color)
    if active_signal and active_signal.get("status")=="running":
        evaluate_active_signal(color)
    if not active_signal or active_signal.get("status")!="running":
        decide_signal()

def _ingest_thread():
    while True:
        try:
            if ingest_cfg["enabled"] and ingest_cfg["url"]:
                data = _fetch_bookweblear(ingest_cfg["url"])
                # Heurística: usa o último item como "mais recente"
                # Se a fonte vier em ordem, cuidamos para não duplicar com last_token
                pushed = 0
                token_used = ingest_cfg["last_token"]
                # Procura do fim pro começo para manter ordem cronológica
                for item in reversed(data):
                    tok = str(item.get("id"))
                    color = _map_color(item.get("color"))
                    if not color: continue
                    if tok and token_used and tok <= token_used:
                        continue
                    _ingest_push(color)
                    pushed += 1
                    token_used = tok or token_used
                ingest_cfg["last_token"] = token_used
                ingest_cfg["ok"] = True
                ingest_cfg["last_error"] = None
            time.sleep(max(1, int(ingest_cfg["interval"])))
        except Exception as e:
            ingest_cfg["ok"] = False
            ingest_cfg["last_error"] = str(e)
            time.sleep(max(2, int(ingest_cfg["interval"])))

# Starta thread daemon
threading.Thread(target=_ingest_thread, daemon=True).start()

# ===== Rotas REST =====
@app.get("/")
def root():
    return send_from_directory(".", "index.html")

@app.get("/api/health")
def health():
    return jsonify({"ok": True, "time": now_iso()})

@app.get("/api/state")
def get_state():
    wr = (metrics["wins"] / max(1, metrics["total_signals"])) if metrics["total_signals"] else 0.0
    return jsonify({
        "ok": True,
        "history_tail": list(history)[-SHOW_LAST:],
        "counts10": recent_counts(10),
        "active_signal": active_signal,
        "metrics": {**metrics, "winrate": round(wr, 3)},
        "config": config,
        "strategy_weights": strategy_weights,
        "ingest": {
            "enabled": ingest_cfg["enabled"],
            "url_set": bool(ingest_cfg["url"]),
            "interval": ingest_cfg["interval"],
            "ok": ingest_cfg["ok"],
            "last_token": ingest_cfg["last_token"],
            "last_error": ingest_cfg["last_error"],
        },
        "time": now_iso()
    })

@app.post("/api/push_round")
def push_round():
    data = request.get_json(force=True, silent=True) or {}
    result = (data.get("result") or "").lower().strip()
    if result not in ("red","black","white"):
        return jsonify({"ok": False, "error": "result must be red|black|white"}), 400
    history.append(result)
    if active_signal and active_signal.get("status")=="running":
        evaluate_active_signal(result)
    signal_info = None
    if not active_signal or active_signal.get("status")!="running":
        signal_info = decide_signal()
    return jsonify({
        "ok": True, "accepted": result,
        "active_signal": active_signal, "maybe_new_signal": signal_info,
        "history_tail": list(history)[-SHOW_LAST:], "metrics": metrics, "config": config
    })

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
    # não zera pesos da IA (mantém aprendizado)
    return jsonify({"ok": True, "cleared": True, "strategy_weights": strategy_weights})

# ===== Rotas de ingest =====
@app.get("/api/ingest/state")
def ingest_state():
    return jsonify({"ok": True, **ingest_cfg, "url": ingest_cfg.get("url")})

@app.post("/api/ingest/config")
def ingest_config():
    data = request.get_json(force=True, silent=True) or {}
    if "enabled" in data: ingest_cfg["enabled"] = bool(data["enabled"])
    if "url" in data: ingest_cfg["url"] = str(data["url"]).strip()
    if "interval" in data:
        try: ingest_cfg["interval"] = max(1, int(data["interval"]))
        except: pass
    # reset do token se trocar de URL
    if "url" in data: ingest_cfg["last_token"] = None
    return jsonify({"ok": True, "ingest": ingest_cfg})

# (Opcional) demo via POST para testar manualmente
@app.post("/api/demo_tick")
def demo_tick():
    u=random.random()
    result="red" if u<0.45 else ("black" if u<0.90 else "white")
    # reutiliza push_round
    with app.test_request_context(json={"result":result}):
        return push_round()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
