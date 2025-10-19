# -*- coding: utf-8 -*-
import os, json, math
from datetime import datetime
from collections import deque
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ===================== Estado =====================
HISTORY_MAX = 2000
history = deque(maxlen=HISTORY_MAX)           # ints 0..14 (0 = branco)
bot_active = False
current_mode = "COLORS"                       # "WHITE" | "COLORS" (deixo CORES por padrão p/ ter mais sinais)

# Gales (somente CORES)
GALE_STEPS = [1, 2, 4]
MAX_GALES  = 2

trade = None                                   # dict p/ trade de cores
signals = deque(maxlen=400)                    # histórico (mais recente primeiro)

# ===================== Helpers =====================
def is_red(n):   return 1 <= n <= 7
def is_black(n): return 8 <= n <= 14

def last_k_colors(seq, k, ignore_white=True):
    out=[]
    for v in reversed(seq):
        if v==0 and ignore_white: 
            continue
        out.append('W' if v==0 else ('R' if is_red(v) else 'B'))
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
        c = 'W' if v==0 else ('R' if is_red(v) else 'B')
        if c=='W': break
        if last is None or c==last: s+=1; last=c
        else: break
    return (last if last in ('R','B') else None), s

# ===================== Sinais ======================
def white_signal(seq):
    """Branco: mais ativo. Dispara se gap<=8 OU gap≥P90 OU gap≥μ+1σ."""
    if not seq: return {"ok": False, "reasons": [], "detail": ""}
    tail = list(seq)[-240:] if len(seq)>=240 else list(seq)
    gaps, gap = gaps_from_seq(tail)
    if not gaps: return {"ok": False, "reasons": [], "detail": ""}

    mu = sum(gaps)/len(gaps)
    var = sum((g-mu)**2 for g in gaps)/max(1, len(gaps)-1)
    sd  = math.sqrt(var)
    p90 = pct(gaps, 90)

    reasons=[]
    if gap <= 8:           reasons.append("pós-branco ≤8")
    if gap >= p90:         reasons.append("gap ≥ P90")
    if gap >= mu + sd:     reasons.append("gap ≥ μ+1σ")

    ok = len(reasons) >= 1
    return {"ok": ok, "reasons": reasons, "detail": f"gap={gap} μ≈{mu:.1f} σ≈{sd:.1f} P90≈{p90:.0f}"}

def color_signal(seq):
    """CORES: padrões simples e frequentes."""
    if not seq: return {"ok": False}
    r10,b10,_ = count_in_last(seq, 10)
    r20,b20,_ = count_in_last(seq, 20)
    cur, s = current_streak(seq)
    reasons=[]

    # 2 -> 3
    if cur and s==2:
        reasons.append("2→3")
        return {"ok": True, "target": cur, "reasons": reasons}

    # quebra de streak longa
    if cur and s>=5:
        reasons.append(f"quebrar streak {s}")
        return {"ok": True, "target": ('B' if cur=='R' else 'R'), "reasons": reasons}

    # dominância 20
    tot20 = max(1, r20+b20)
    if r20/tot20 >= 0.62:
        reasons.append("dominância R em 20")
        return {"ok": True, "target": 'R', "reasons": reasons}
    if b20/tot20 >= 0.62:
        reasons.append("dominância B em 20")
        return {"ok": True, "target": 'B', "reasons": reasons}

    # desbalanceio 10
    if abs(r10-b10) >= 4:
        tgt = 'R' if r10>b10 else 'B'
        reasons.append("desbalanceio em 10")
        return {"ok": True, "target": tgt, "reasons": reasons}

    return {"ok": False}

def estimate_probs(seq):
    pW = 1.0/15.0
    sw = white_signal(seq)
    if sw["ok"]: pW = min(0.40, pW + 0.12)

    r20,b20,_ = count_in_last(seq, 20)
    tot=max(1, r20+b20)
    pR_raw=(r20+1)/(tot+2)
    pB_raw=(b20+1)/(tot+2)
    rem = max(0.0, 1.0 - pW)
    pR = pR_raw * rem
    pB = pB_raw * rem
    s = pW+pR+pB
    pW,pR,pB = pW/s, pR/s, pB/s
    rec = max([("W",pW),("R",pR),("B",pB)], key=lambda x:x[1])
    return {"W":pW,"R":pR,"B":pB,"rec":rec}

def _append_signal(mode, target, status="open", gale=None, came_n=None, reasons=None):
    signals.appendleft({
        "ts": datetime.now().strftime("%H:%M:%S"),
        "mode": mode,
        "target": target,             # 'W'|'R'|'B'
        "status": status,             # 'open'|'win'|'loss'
        "gale": gale,                 # 0..2 (somente CORES)
        "came_n": came_n,
        "reasons": reasons or []
    })

# ===================== Engine ======================
def open_trade_colors(target, reasons):
    global trade
    trade = {
        "target": target,                # 'R'|'B'
        "step": 0,                       # G0
        "opened_last": history[-1] if len(history) else None,
        "await_unique": True,
        "reasons": reasons
    }
    _append_signal("COLORS", target, "open", gale=0, reasons=reasons)

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

def process_new_number(n):
    global trade
    if current_mode=="WHITE":
        sw = white_signal(history)
        if sw["ok"]:
            _append_signal("WHITE", "W", "open", gale=None, reasons=sw["reasons"])
            if n==0: signals[0]["status"]="win";  signals[0]["came_n"]=0
            else:    signals[0]["status"]="loss"; signals[0]["came_n"]=n
        return

    # CORES
    if not trade:
        sc = color_signal(history)
        if sc["ok"]:
            open_trade_colors(sc["target"], sc["reasons"])
    else:
        # ignorar duplicata do número visto na abertura
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

# ===================== HTML (UI novo) ======================
HTML = """<!doctype html><meta charset="utf-8">
<title>SNIPER BLAZE PRO — WEB</title>
<style>
  :root{
    --bg:#0e0a1b;--card:#15102a;--edge:#241b46;--ink:#e9e7f5;--muted:#bfb9d8;
    --acc:#7b2cbf;--ok:#2bd99f;--bad:#ff4d67;--dark:#0e0e10;--red:#ff2e2e;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 Inter,Segoe UI,system-ui,Arial}
  header{padding:16px 20px;font-weight:800;letter-spacing:.4px}
  .page{padding:10px 20px;display:grid;grid-template-columns:320px 1fr;gap:14px}
  .card{background:var(--card);border:1px solid var(--edge);border-radius:16px;padding:14px}
  h3{margin:0 0 12px 0;font:700 16px/1 Inter}
  .row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .btn{background:#221a43;border:1px solid #2f255e;color:#fff;padding:8px 12px;border-radius:12px;cursor:pointer}
  .btn.primary{background:var(--acc)}
  .chip{background:#231b44;border:1px solid #2f255e;color:var(--muted);padding:6px 10px;border-radius:999px;font-size:12px}
  .bar{height:14px;background:#1f1840;border-radius:10px;overflow:hidden;margin-top:4px;margin-bottom:10px}
  .bar>span{display:block;height:100%}
  .grid{display:grid;grid-template-columns:repeat(10,56px);gap:8px}
  .sq{height:56px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-weight:800}
  .w{background:#fff;color:#111}.r{background:var(--red)}.b{background:var(--dark);color:#fff}
  table{width:100%;border-collapse:collapse}
  th,td{padding:8px;border-bottom:1px solid var(--edge);color:#dcd7ef}
  th{color:#bfb9d8;text-align:left}
  .ok{color:var(--ok);font-weight:700}
  .bad{color:var(--bad);font-weight:700}
  #pick{width:100%;height:110px;border-radius:14px;border:1px dashed var(--edge);display:flex;align-items:center;justify-content:center;font-weight:800}
</style>

<header>SNIPER BLAZE PRO — WEB</header>
<div class="page">

  <div class="card">
    <div class="row" style="margin-bottom:6px">
      <b>Modo:</b>
      <button class="btn" id="mWhite">⚪ BRANCO</button>
      <button class="btn" id="mColors">🎯 CORES</button>
      <span id="modeTag" class="chip">COLORS</span>
    </div>

    <div class="row" style="margin-bottom:8px">
      <b>Bot:</b>
      <button class="btn primary" id="bStart">▶ Iniciar</button>
      <button class="btn" id="bStop">■ Parar</button>
      <span id="botTag" class="chip">OFF</span>
    </div>

    <h3>Chances da rodada</h3>
    <div class="row" style="justify-content:space-between;color:var(--muted)">⚪ Branco <span id="pw">0%</span></div>
    <div class="bar"><span id="bw" style="background:#fff;width:0%"></span></div>
    <div class="row" style="justify-content:space-between;color:var(--muted)">🔴 Vermelho <span id="pr">0%</span></div>
    <div class="bar"><span id="br" style="background:var(--red);width:0%"></span></div>
    <div class="row" style="justify-content:space-between;color:var(--muted)">⚫ Preto <span id="pb">0%</span></div>
    <div class="bar"><span id="bb" style="background:var(--dark);width:0%"></span></div>

    <h3>Cor + indicada</h3>
    <div id="pick">— sem entrada —</div>
    <div id="why" style="color:var(--muted);margin-top:8px;font-size:13px">Motivos: —</div>
  </div>

  <div class="card">
    <div class="row" style="justify-content:space-between;margin-bottom:8px">
      <h3>Últimos giros</h3>
      <div id="detail" style="color:var(--muted)">—</div>
    </div>
    <div id="boxes" class="grid" style="margin-bottom:12px"></div>

    <h3>Histórico de Sinais</h3>
    <table><thead>
      <tr><th>Hora</th><th>Modo</th><th>Alvo</th><th>Status</th><th>Gale</th><th>Saiu</th></tr>
    </thead><tbody id="sigBody"></tbody></table>
  </div>

</div>

<script>
async function post(u,b){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});}
document.getElementById('mWhite').onclick=()=>post('/mode',{mode:'WHITE'});
document.getElementById('mColors').onclick=()=>post('/mode',{mode:'COLORS'});
document.getElementById('bStart').onclick=()=>post('/bot',{active:true});
document.getElementById('bStop').onclick =()=>post('/bot',{active:false});

function paintBoxes(lst){
  const boxes=document.getElementById('boxes'); boxes.innerHTML='';
  lst.forEach(n=>{
    const d=document.createElement('div'), c=n===0?'w':(n<=7?'r':'b');
    d.className='sq '+c; d.textContent=n===0?'0':n; boxes.appendChild(d);
  });
}

function showPick(pick){
  const el=document.getElementById('pick');
  if(!pick){ el.style.background='transparent'; el.style.color='#e9e7f5'; el.textContent='— sem entrada —'; return; }
  const [tgt,pv]=pick;
  el.style.color=(tgt==='W'?'#111':'#fff');
  el.style.background=(tgt==='W'?'#fff':(tgt==='R'?'#ff2e2e':'#0e0e10'));
  el.textContent=(tgt==='W'?'⚪ Branco':(tgt==='R'?'🔴 Vermelho':'⚫ Preto'))+' • '+Math.round(pv*100)+'%';
}

function paintSigs(list){
  const tb=document.getElementById('sigBody'); tb.innerHTML='';
  list.forEach(s=>{
    const tr=document.createElement('tr');
    const st = s.status==='open'?'open':(s.status==='win'?'<span class="ok">WIN</span>':'<span class="bad">LOSS</span>');
    tr.innerHTML = `<td>${s.ts}</td><td>${s.mode}</td><td>${s.target}</td><td>${st}</td><td>${s.gale??'-'}</td><td>${s.came_n??'-'}</td>`;
    tb.appendChild(tr);
  });
}

async function tick(){
  try{
    const r=await fetch('/state'); const s=await r.json();
    document.getElementById('modeTag').textContent=s.mode;
    document.getElementById('botTag').textContent=s.bot_active?'ON':'OFF';
    paintBoxes(s.last||[]);
    const p=s.probs||{W:0,R:0,B:0};
    document.getElementById('pw').textContent=(p.W*100).toFixed(1)+'%';
    document.getElementById('pr').textContent=(p.R*100).toFixed(1)+'%';
    document.getElementById('pb').textContent=(p.B*100).toFixed(1)+'%';
    document.getElementById('bw').style.width=(p.W*100)+'%';
    document.getElementById('br').style.width=(p.R*100)+'%';
    document.getElementById('bb').style.width=(p.B*100)+'%';
    if(s.pick) showPick(s.pick); else showPick(null);
    document.getElementById('why').textContent='Motivos: '+(s.reasons&&s.reasons.length?s.reasons.join(', '):'—');
    document.getElementById('detail').textContent=s.detail||'—';
    paintSigs(s.signals||[]);
  }catch(e){}
}
setInterval(tick, 900); tick();
</script>
"""

# ===================== Endpoints ======================
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
        trade = None  # ao trocar modo, encerra trade pendente
        return jsonify(ok=True, mode=current_mode)
    return jsonify(ok=False, error="mode must be WHITE or COLORS"), 400

@app.post("/bot")
def toggle_bot():
    global bot_active, trade
    data = request.get_json(force=True, silent=True) or {}
    bot_active = bool(data.get("active", False))
    if not bot_active: trade = None
    return jsonify(ok=True, bot_active=bot_active)

@app.get("/state")
def state():
    lst = list(history)[-20:]
    probs = estimate_probs(history)
    pick = None
    reasons = []
    detail = ""

    if current_mode=="WHITE":
        sw = white_signal(history)
        reasons = sw["reasons"]; detail = sw["detail"]
        if sw["ok"]: pick=("W", probs["W"])
    else:
        sc = color_signal(history)
        reasons = sc.get("reasons", [])
        if sc.get("ok"): pick=(sc["target"], probs[sc["target"]])
        if trade: pick=(trade["target"], probs[trade["target"]]); reasons=trade.get("reasons", reasons)

    return jsonify(
        last=lst, mode=current_mode, bot_active=bot_active,
        probs=probs, pick=pick, reasons=reasons, detail=detail,
        trade=trade, signals=list(signals)
    )

@app.post("/ingest")
def ingest():
    data = request.get_json(force=True)
    hist = data.get("history") or []
    added=0
    for n in hist:
        if isinstance(n,int) and 0<=n<=14:
            history.append(n); added+=1
            if bot_active: process_new_number(n)
    return jsonify(ok=True, added=added, time=datetime.now().isoformat())

if __name__ == "__main__":
    port=int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
