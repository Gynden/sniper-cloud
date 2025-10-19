# -*- coding: utf-8 -*-
import os, json, math
from datetime import datetime
from collections import deque
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ========================= Estado ==================================
HISTORY_MAX = 2500
history = deque(maxlen=HISTORY_MAX)  # ints 0..14 (0 = branco)

# snapshot anti-duplicata (bookmarklet envia janela toda)
_last_snapshot = []
_last_src = "—"

# Bot & config
bot_active   = False
current_mode = "COLORS"            # "WHITE" | "COLORS"
conf_min     = 0.55                # limiar para disparar sinais automáticos (0–1)

# Gales (apenas CORES)
GALE_STEPS = [1, 2, 4]
MAX_GALES  = 2

trade   = None                     # trade ativo para cores
signals = deque(maxlen=900)        # histórico (mais recente primeiro)

# ========================= Helpers =================================
def is_red(n):   return 1 <= n <= 7
def is_black(n): return 8 <= n <= 14

def to_color(n):
    if n==0: return 'W'
    return 'R' if is_red(n) else 'B'

def last_k_colors(seq, k, ignore_white=True):
    out=[]
    for v in reversed(seq):
        if v==0 and ignore_white: continue
        out.append(to_color(v))
        if len(out)>=k: break
    return list(reversed(out))

def count_in_last(seq, k):
    lst = last_k_colors(seq, k)
    return lst.count('R'), lst.count('B'), lst

def gaps_from_seq(seq):
    idx=[i for i,v in enumerate(seq) if v==0]
    gaps=[]
    for a,b in zip(idx, idx[1:]): gaps.append(b-a)
    gap_atual = (len(seq)-1-idx[-1]) if idx else len(seq)
    return gaps, gap_atual

def pct(lst, p):
    if not lst: return 0.0
    s=sorted(lst); k=max(0, min(len(s)-1, int(round((p/100)*(len(s)-1)))))
    return float(s[k])

def current_streak(seq):
    last=None; s=0
    for v in reversed(seq):
        c = to_color(v)
        if c=='W': break
        if last is None or c==last: s+=1; last=c
        else: break
    return (last if last in ('R','B') else None), s

def alt_run(lst):
    if not lst: return 0
    r=1
    for i in range(len(lst)-2,-1,-1):
        if lst[i]==lst[i+1]: break
        r+=1
    return r

# ========================= Prob/Engines =============================

def white_signal(seq):
    """
    Motor BRANCO (sem gale): chama apenas quando há “força” real.
    Anti-spam: exige gap suficiente e filtros percentis.
    """
    if not seq: return {"ok": False, "reasons": [], "detail": "", "conf": 0.0}
    tail = list(seq)[-240:] if len(seq)>=240 else list(seq)
    gaps, gap = gaps_from_seq(tail)
    if not gaps: return {"ok": False, "reasons": [], "detail": "", "conf": 0.0}

    mu = sum(gaps)/len(gaps)
    var = sum((g-mu)**2 for g in gaps)/max(1, len(gaps)-1)
    sd  = math.sqrt(var)
    p85 = pct(gaps,85)
    p90 = pct(gaps,90)
    p95 = pct(gaps,95)

    reasons=[]; score=0

    if gap <= 8:       reasons.append("pós-branco ≤8"); score += 0.35
    if gap >= mu+sd:   reasons.append("gap ≥ μ+1σ");   score += 0.25
    if gap >= p90:     reasons.append("gap ≥ P90");     score += 0.25
    if gap >= p95:     reasons.append("seca (P95)");    score += 0.15

    # confiança “amarrada” (0.35–0.9 aprox) + hard cap 0.4 de prob
    conf = max(0.0, min(0.95, score))
    ok = conf >= conf_min and gap >= 5   # evita spam muito curto
    detail = f"gap={gap} • μ≈{mu:.1f} • σ≈{sd:.1f} • P90≈{p90:.0f}"
    return {"ok":ok, "reasons":reasons, "detail":detail, "conf":conf}

def color_signal(seq):
    """
    Motor CORES (com gale): heurística híbrida
    - Momentum (streak ≥5 quebra; 2→3 segue; 4 segue; alternância longa quebra)
    - Dominância 20/30 (≥62%)
    - Reversão média quando 10 está desbalanceado
    Retorno inclui target ('R'|'B'), razões e “conf” (0–1).
    """
    if not seq: return {"ok": False, "reasons": [], "conf": 0.0}

    r10,b10,_ = count_in_last(seq, 10)
    r20,b20,_ = count_in_last(seq, 20)
    r30,b30,_ = count_in_last(seq, 30)
    cur, s = current_streak(seq)
    alt = alt_run(last_k_colors(seq, 12))
    reasons=[]; score=0; target=None

    # 1) Momentum claro
    if cur and s==2:
        target=cur; reasons.append("2→3"); score+=0.28
    if cur and s==4 and target is None:
        target=cur; reasons.append("muro de 4"); score+=0.22
    if cur and s>=5:
        target=('B' if cur=='R' else 'R'); reasons.append(f"quebrar streak {s}"); score+=0.34
    if alt>=6 and target is None:
        # quebra alternância
        lst = last_k_colors(seq,1)
        if lst: target=lst[-1]; reasons.append("anti-alternância"); score+=0.2

    # 2) Dominância de sessão
    tot20=max(1,r20+b20); tot30=max(1,r30+b30)
    if r20/tot20>=0.62 and (target is None or target=='R'):
        target='R'; reasons.append("dominância R 20"); score+=0.18
    if b20/tot20>=0.62 and (target is None or target=='B'):
        target='B'; reasons.append("dominância B 20"); score+=0.18
    if r30/tot30>=0.62 and (target is None or target=='R'):
        target='R'; reasons.append("dominância R 30"); score+=0.12
    if b30/tot30>=0.62 and (target is None or target=='B'):
        target='B'; reasons.append("dominância B 30"); score+=0.12

    # 3) Desbalanceio 10 → segue o forte
    if abs(r10-b10)>=4 and target is None:
        target = 'R' if r10>b10 else 'B'
        reasons.append("desbalanceio (10)"); score+=0.18

    ok = (target is not None) and (score>=conf_min)
    conf = max(0.0, min(0.95, score))
    return {"ok":ok, "target":target, "reasons":reasons, "conf":conf}

def estimate_probs(seq):
    """Probabilidade relativa (W/R/B) + pick recomendado."""
    baseW = 1.0/15.0
    sw = white_signal(seq)
    pW = min(0.40, baseW + 0.12* (1 if sw["ok"] else 0) + 0.2*sw["conf"])

    r20,b20,_ = count_in_last(seq, 20)
    tot=max(1, r20+b20)
    # smoothing
    pR_raw=(r20+1)/(tot+2)
    pB_raw=(b20+1)/(tot+2)
    rem = max(0.0, 1.0 - pW)
    pR = pR_raw * rem
    pB = pB_raw * rem

    s = pW+pR+pB
    pW,pR,pB = pW/s, pR/s, pB/s
    pick = max([("W",pW),("R",pR),("B",pB)], key=lambda x:x[1])
    return {"W":pW,"R":pR,"B":pB,"pick":pick}

# ========================= Signals/Trades ==========================

def _append_signal(mode, target, status="open", gale=None, came_n=None, reasons=None, conf=0.0):
    signals.appendleft({
        "ts": datetime.now().strftime("%H:%M:%S"),
        "mode": mode,
        "target": target,              # 'W'|'R'|'B'
        "status": status,              # 'open'|'win'|'loss'
        "gale": gale,                  # 0|1|2 ou None
        "came_n": came_n,              # número real
        "reasons": reasons or [],
        "conf": round(conf*100)
    })

def open_trade_colors(target, reasons, conf):
    global trade
    trade = {
        "target": target,              # 'R'|'B'
        "step": 0,
        "opened_last": history[-1] if len(history) else None,
        "await_unique": True,
        "reasons": reasons, "conf": conf
    }
    _append_signal("COLORS", target, "open", gale=0, reasons=reasons, conf=conf)

def close_trade(result, came_n=None):
    global trade
    if not trade: return
    g = trade["step"]
    for s in signals:
        if s["status"]=="open" and s["mode"]=="COLORS":
            s["status"]=result
            s["gale"]=g
            s["came_n"]=came_n
            break
    trade = None

def process_new_number(n:int):
    """Chamada quando chega GIRO novo (bot ON)."""
    global trade
    if current_mode=="WHITE":
        sw = white_signal(history)
        if sw["ok"]:
            _append_signal("WHITE","W","open",gale=None,reasons=sw["reasons"],conf=sw["conf"])
            # resolve no próximo número (n já é o que saiu agora)
            if n==0: signals[0]["status"]="win";  signals[0]["came_n"]=0
            else:    signals[0]["status"]="loss"; signals[0]["came_n"]=n
        return

    # CORES
    if not trade:
        sc = color_signal(history)
        if sc["ok"]:
            open_trade_colors(sc["target"], sc["reasons"], sc["conf"])
    else:
        # um giro “válido” por passo
        if trade.get("await_unique", False):
            if trade.get("opened_last", None) == n:
                return
            trade["await_unique"]=False
            trade["opened_last"]=None

        tgt = trade["target"]
        hit = (n!=0) and ((is_red(n) and tgt=='R') or (is_black(n) and tgt=='B'))
        if hit:
            close_trade("win", came_n=n)
        else:
            if trade["step"]>=MAX_GALES:
                close_trade("loss", came_n=n)
            else:
                trade["step"] += 1
                trade["await_unique"]=False

# ========================= Merge Snapshot ==========================
def merge_snapshot(snapshot):
    """
    Recebe um snapshot (lista de ints 0..14) e adiciona só a parte nova.
    Detecta direção (LTR/RTL) procurando maior overlap com último snapshot.
    """
    global _last_snapshot
    if not snapshot: return 0

    snap = [int(x) for x in snapshot if isinstance(x,int) and 0<=x<=14]
    if not snap: return 0

    def ov(a,b,kmax=60):
        kmax=min(kmax,len(a),len(b))
        for k in range(kmax,0,-1):
            if a[-k:]==b[-k:]:
                return k
        return 0

    added=0
    if not _last_snapshot:
        _last_snapshot=list(snap)
        for n in snap: history.append(n); added+=1
        return added

    a = snap
    b = snap[::-1]
    ova = ov(_last_snapshot, a)
    ovb = ov(_last_snapshot, b)
    chosen = a if ova>=ovb else b
    k = max(ova, ovb)

    if k>=len(chosen):
        _last_snapshot=list(chosen)
        return 0

    new_tail = chosen[k:]
    for n in new_tail:
        history.append(n); added+=1
    _last_snapshot=list(chosen)
    return added

# ========================= UI (Dashboard PRO) =======================
HTML = """<!doctype html><meta charset="utf-8">
<title>SNIPER BLAZE PRO — Cloud</title>
<meta name="viewport" content="width=device-width,initial-scale=1" />
<style>
:root{
  --bg:#0b0c10; --panel:#0e1118; --panel2:#10131b; --edge:#1a2233;
  --text:#e9eef7; --muted:#9aa4b7; --accent:#6ea8ff; --good:#12d6a3; --bad:#ff5b7a;
  --red:#ff3f62; --black:#0f1116; --chip:#131a27; --shadow:rgba(0,0,0,.35);
}
*{box-sizing:border-box}
body{margin:0;background:linear-gradient(180deg,#0b0c10 0%,#0d0f16 100%);
     color:var(--text);font:14px/1.5 Inter,system-ui,Segoe UI,Arial}
.app{display:grid;grid-template-columns:280px 1fr;min-height:100vh}
@media(max-width:980px){.app{grid-template-columns:1fr}}
.aside{background:var(--panel);border-right:1px solid var(--edge);padding:16px;position:sticky;top:0;height:100vh}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:16px}
.logo{width:12px;height:12px;border-radius:50%;background:var(--good);box-shadow:0 0 14px var(--good)}
h1{margin:0;font:800 15px/1 Inter}
.section{margin:18px 0 10px;color:var(--muted);font:700 12px/1 Inter;letter-spacing:.3px}
.card{background:var(--panel2);border:1px solid var(--edge);border-radius:14px;padding:12px;box-shadow:0 8px 24px var(--shadow)}
.kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.kpi{text-align:center;background:var(--panel2);border:1px solid var(--edge);border-radius:10px;padding:10px}
.kpi b{display:block;color:var(--muted);font-size:11px}
.kpi .v{font:800 16px/1 Inter}
.seg{display:flex;border:1px solid #24314a;background:#0c1220;border-radius:12px;overflow:hidden}
.seg button{flex:1;border:0;background:transparent;color:var(--muted);padding:8px 10px;cursor:pointer}
.seg button.on{background:var(--accent);color:#fff}
.switch{position:relative;width:58px;height:30px;background:#141c2b;border:1px solid #2a3856;border-radius:999px;cursor:pointer}
.switch i{position:absolute;top:3px;left:3px;width:24px;height:24px;border-radius:50%;background:#cdd6ff;transition:.2s}
.switch.on{background:#16333a;border-color:#2f8e7a}
.switch.on i{left:31px;background:#12d6a3}
.slider{display:flex;align-items:center;gap:10px}
input[type=range]{width:100%}
.small{color:var(--muted);font-size:12px}
.bar{height:10px;background:#121827;border:1px solid #1f2a42;border-radius:7px;overflow:hidden}
.bar>span{display:block;height:100%}
.neonW{background:#fff;box-shadow:0 0 10px rgba(255,255,255,.5)}
.neonR{background:var(--red);box-shadow:0 0 10px rgba(255,71,106,.4)}
.neonB{background:#0f1115;box-shadow:0 0 10px rgba(10,10,12,.35)}
.pick{height:96px;border:1px dashed #2a3246;border-radius:12px;display:flex;align-items:center;justify-content:center;font:800 16px/1 Inter}
.main{display:grid;grid-template-rows:auto 1fr}
header{display:flex;align-items:center;gap:10px;padding:12px 18px;border-bottom:1px solid var(--edge);background:var(--panel)}
.chip{background:var(--chip);border:1px solid var(--edge);padding:6px 10px;border-radius:10px;color:#cfd7e6}
.sp{margin-left:auto;display:flex;gap:8px}
.content{padding:16px;display:grid;gap:16px}
@media(min-width:1100px){.content{grid-template-columns:1.2fr .8fr}}
.h{margin:0 0 10px 0;font:700 13px/1 Inter;color:#d7e1f4}
.stack{display:flex;gap:8px;overflow:auto;padding-bottom:6px}
.sq{min-width:52px;height:52px;border-radius:12px;display:flex;align-items:center;justify-content:center;font:800 16px/1 Inter}
.w{background:#ffffff;color:#111}.r{background:var(--red)}.b{background:#0f1115;color:#fff}
table{width:100%;border-collapse:collapse}
th,td{padding:10px;border-bottom:1px solid #1d2537;font-size:13px}
th{color:#b7c1d7;text-align:left}
tbody tr:nth-child(even){background:#0f1422}
.badge{padding:4px 8px;border-radius:8px;font:800 11px/1 Inter}
.win{color:var(--good)} .loss{color:var(--bad)} .open{color:#8ba1ff}
.reason{display:inline-block;margin:4px 6px 0 0;padding:4px 8px;border:1px solid #2a3246;border-radius:999px;color:#bcd3ff;font-size:11px}
</style>

<div class="app">
  <aside class="aside">
    <div class="brand"><div class="logo"></div><h1>SNIPER BLAZE PRO</h1></div>

    <div class="section">Modo</div>
    <div class="seg" id="segMode">
      <button id="mW">⚪ Branco</button>
      <button id="mC" class="on">🎯 Cores</button>
    </div>

    <div class="section">Bot</div>
    <div style="display:flex;align-items:center;gap:12px">
      <div class="chip">Estado</div>
      <div id="sw" class="switch"><i></i></div>
    </div>

    <div class="section">Limiar de confiança</div>
    <div class="card slider">
      <input id="rng" type="range" min="50" max="90" value="55" />
      <div id="rngv" class="chip">55%</div>
    </div>

    <div class="section">KPIs</div>
    <div class="kpis">
      <div class="kpi"><b>Modo</b><div class="v" id="kMode">COLORS</div></div>
      <div class="kpi"><b>Bot</b><div class="v" id="kBot">OFF</div></div>
      <div class="kpi"><b>Sinais</b><div class="v" id="kSig">0</div></div>
    </div>

    <div class="section">Chances da rodada</div>
    <div class="card">
      <div style="display:flex;justify-content:space-between"><span>⚪ Branco</span><span id="pw">0%</span></div>
      <div class="bar" style="margin:6px 0 10px"><span id="bw" class="neonW" style="width:0%"></span></div>
      <div style="display:flex;justify-content:space-between"><span>🔴 Vermelho</span><span id="pr">0%</span></div>
      <div class="bar" style="margin:6px 0 10px"><span id="br" class="neonR" style="width:0%"></span></div>
      <div style="display:flex;justify-content:space-between"><span>⚫ Preto</span><span id="pb">0%</span></div>
      <div class="bar"><span id="bb" class="neonB" style="width:0%"></span></div>
    </div>

    <div class="section">Cor + indicada</div>
    <div id="pick" class="pick">— sem entrada —</div>
    <div id="why" class="small" style="margin-top:8px">Razões: —</div>
  </aside>

  <div class="main">
    <header>
      <div class="chip">Cloud</div>
      <div class="sp">
        <div class="chip" id="topMode">COLORS</div>
        <div class="chip" id="topBot">OFF</div>
        <div class="chip" id="detail">—</div>
        <div class="chip" id="src">fonte: —</div>
      </div>
    </header>

    <div class="content">
      <section class="card">
        <h3 class="h">Últimos giros</h3>
        <div id="stack" class="stack"></div>
      </section>

      <section class="card">
        <h3 class="h">Histórico de sinais</h3>
        <table>
          <thead><tr><th>Hora</th><th>Modo</th><th>Alvo</th><th>Status</th><th>Gale</th><th>Saiu</th></tr></thead>
          <tbody id="sigBody"></tbody>
        </table>
      </section>
    </div>
  </div>
</div>

<script>
async function post(u,b){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});}
const $ = s=>document.querySelector(s);

$('#mW').onclick = ()=> post('/mode',{mode:'WHITE'});
$('#mC').onclick = ()=> post('/mode',{mode:'COLORS'});
$('#sw').onclick  = async ()=>{
  const on=$('#sw').classList.toggle('on');
  await post('/bot',{active:on});
};
$('#rng').oninput = e=>{
  $('#rngv').textContent = e.target.value+'%';
  post('/conf',{conf: parseInt(e.target.value,10)/100.0});
};

function paintStack(lst){
  const st=$('#stack'); st.innerHTML='';
  lst.forEach(n=>{
    const d=document.createElement('div'); d.className='sq '+(n===0?'w':(n<=7?'r':'b')); d.textContent=n===0?'0':n; st.appendChild(d);
  });
}
function showPick(p){
  const el=$('#pick');
  if(!p){ el.style.background='transparent'; el.style.color='#e9eef7'; el.textContent='— sem entrada —'; return; }
  const [tgt,pv]=p;
  el.style.color=(tgt==='W'?'#111':'#fff');
  el.style.background=(tgt==='W'?'#ffffff':(tgt==='R'?'#ff3f62':'#0f1115'));
  el.textContent=(tgt==='W'?'⚪ Branco':(tgt==='R'?'🔴 Vermelho':'⚫ Preto'))+' • '+Math.round(pv*100)+'%';
}
function paintSigs(list){
  $('#kSig').textContent=list.length;
  const tb=$('#sigBody'); tb.innerHTML='';
  list.forEach(s=>{
    const tr=document.createElement('tr');
    const cls = s.status==='open'?'open':(s.status==='win'?'win':'loss');
    tr.innerHTML = `<td>${s.ts}</td><td>${s.mode}</td><td>${s.target}</td><td><span class="badge ${cls}">${s.status.toUpperCase()}</span></td><td>${s.gale??'-'}</td><td>${s.came_n??'-'}</td>`;
    tb.appendChild(tr);
  });
}

async function tick(){
  try{
    const r=await fetch('/state'); const s=await r.json();

    $('#topMode').textContent=s.mode; $('#kMode').textContent=s.mode;
    $('#topBot').textContent=s.bot_active?'ON':'OFF'; $('#kBot').textContent=s.bot_active?'ON':'OFF';
    if(s.mode==='WHITE'){ $('#mW').classList.add('on'); $('#mC').classList.remove('on'); } else { $('#mC').classList.add('on'); $('#mW').classList.remove('on'); }
    if(s.bot_active){ $('#sw').classList.add('on'); } else { $('#sw').classList.remove('on'); }

    paintStack(s.last||[]);
    const p=s.probs||{W:0,R:0,B:0};
    $('#pw').textContent=(p.W*100).toFixed(1)+'%'; $('#pr').textContent=(p.R*100).toFixed(1)+'%'; $('#pb').textContent=(p.B*100).toFixed(1)+'%';
    $('#bw').style.width=(p.W*100)+'%'; $('#br').style.width=(p.R*100)+'%'; $('#bb').style.width=(p.B*100)+'%';

    if(s.pick) showPick(s.pick); else showPick(null);
    $('#why').innerHTML='Razões: '+(s.reasons && s.reasons.length? s.reasons.map(x=>`<span class="reason">${x}</span>`).join('') : '—');
    $('#detail').textContent=s.detail||'—';
    $('#src').textContent='fonte: '+(s.src||'—');

    paintSigs(s.signals||[]);
  }catch(e){}
}
setInterval(tick, 900); tick();
</script>
"""

# ========================= API =====================================

@app.get("/")
def index():
    return render_template_string(HTML)

@app.post("/mode")
def set_mode():
    global current_mode, trade
    data = request.get_json(force=True, silent=True) or {}
    mode = (data.get("mode") or "").upper()
    if mode in ("WHITE","COLORS"):
        current_mode = mode
        trade = None
        return jsonify(ok=True, mode=current_mode)
    return jsonify(ok=False, error="mode must be WHITE or COLORS"), 400

@app.post("/bot")
def toggle_bot():
    global bot_active, trade
    data = request.get_json(force=True, silent=True) or {}
    bot_active = bool(data.get("active", False))
    if not bot_active: trade = None
    return jsonify(ok=True, bot_active=bot_active)

@app.post("/conf")
def set_conf():
    global conf_min
    data = request.get_json(force=True, silent=True) or {}
    v = float(data.get("conf", conf_min))
    conf_min = max(0.5, min(0.9, v))
    return jsonify(ok=True, conf=conf_min)

@app.get("/state")
def state():
    last = list(history)[-20:]
    probs = estimate_probs(history)
    src = _last_src

    pick=None; reasons=[]; detail=""
    if current_mode=="WHITE":
        sw = white_signal(history)
        reasons = sw["reasons"]; detail = sw["detail"]
        if sw["ok"]: pick=("W", probs["W"])
    else:
        if trade:
            pick=(trade["target"], probs[trade["target"]])
            reasons=trade.get("reasons", [])
        else:
            sc = color_signal(history)
            reasons = sc.get("reasons", [])
            if sc.get("ok"): pick=(sc["target"], probs[sc["target"]])

    return jsonify(
        last=last, mode=current_mode, bot_active=bot_active,
        probs={"W":probs["W"],"R":probs["R"],"B":probs["B"]},
        pick=pick, reasons=reasons, detail=detail, src=src,
        signals=list(signals)
    )

@app.post("/ingest")
def ingest():
    global _last_src
    data = request.get_json(force=True)
    hist = data.get("history") or []
    src  = data.get("src") or "?"
    _last_src = src
    added = merge_snapshot(hist)

    # processa os novos
    if bot_active and added>0:
        # só os que entraram agora (no fim da fila)
        for n in hist[-added:]:
            if isinstance(n,int) and 0<=n<=14:
                process_new_number(n)

    return jsonify(ok=True, added=added, time=datetime.now().isoformat())

if __name__ == "__main__":
    port=int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
