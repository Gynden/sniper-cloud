# -*- coding: utf-8 -*-
import os, json, math
from datetime import datetime
from collections import deque
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ===================== Estado / Regras =====================
HISTORY_MAX = 2000
history = deque(maxlen=HISTORY_MAX)  # ints 0..14 (0=branco)

bot_active   = False
current_mode = "COLORS"              # "WHITE" | "COLORS"

GALE_STEPS = [1, 2, 4]               # só para CORES
MAX_GALES  = 2

trade   = None                       # trade ativo (apenas CORES)
signals = deque(maxlen=700)          # histórico de sinais (mais recente primeiro)

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

# ====== Sinais
def white_signal(seq):
    """Branco: gatilhos objetivos para *chamar*, sem gale."""
    if not seq: return {"ok": False, "reasons": [], "detail": ""}
    tail = list(seq)[-240:] if len(seq)>=240 else list(seq)
    gaps, gap = gaps_from_seq(tail)
    if not gaps: return {"ok": False, "reasons": [], "detail": ""}

    mu = sum(gaps)/len(gaps)
    var = sum((g-mu)**2 for g in gaps)/max(1, len(gaps)-1)
    sd  = math.sqrt(var)
    p90 = pct(gaps, 90)

    reasons=[]
    if gap <= 8:       reasons.append("pós-branco ≤8")
    if gap >= p90:     reasons.append("gap ≥ P90")
    if gap >= mu + sd: reasons.append("gap ≥ μ+1σ")

    ok = len(reasons) >= 1
    return {"ok": ok, "reasons": reasons, "detail": f"gap={gap} • μ≈{mu:.1f} • σ≈{sd:.1f} • P90≈{p90:.0f}"}

def color_signal(seq):
    """Heurística simples, estável e legível para CORES."""
    if not seq: return {"ok": False}
    r10,b10,_ = count_in_last(seq, 10)
    r20,b20,_ = count_in_last(seq, 20)
    cur, s = current_streak(seq)
    reasons=[]

    if cur and s==2:
        reasons.append("2→3")
        return {"ok": True, "target": cur, "reasons": reasons}

    if cur and s>=5:
        reasons.append(f"quebrar streak {s}")
        return {"ok": True, "target": ('B' if cur=='R' else 'R'), "reasons": reasons}

    tot20 = max(1, r20+b20)
    if r20/tot20 >= 0.62:
        reasons.append("dominância R (20)")
        return {"ok": True, "target": 'R', "reasons": reasons}
    if b20/tot20 >= 0.62:
        reasons.append("dominância B (20)")
        return {"ok": True, "target": 'B', "reasons": reasons}

    if abs(r10-b10) >= 4:
        tgt = 'R' if r10>b10 else 'B'
        reasons.append("desbalanceio (10)")
        return {"ok": True, "target": tgt, "reasons": reasons}

    return {"ok": False}

def estimate_probs(seq):
    pW = 1.0/15.0
    sw = white_signal(seq)
    if sw["ok"]: pW = min(0.40, pW + 0.12)   # boost máximo 40%

    r20,b20,_ = count_in_last(seq, 20)
    tot=max(1, r20+b20)
    pR_raw=(r20+1)/(tot+2)                   # smoothing
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
        "target": target,
        "status": status,             # 'open'|'win'|'loss'
        "gale": gale,
        "came_n": came_n,
        "reasons": reasons or []
    })

# ====== Engine
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
        if sc["ok"]: open_trade_colors(sc["target"], sc["reasons"])
    else:
        # um giro único por passo
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

# ===================== UI — Layout PRO =====================
HTML = """<!doctype html><meta charset="utf-8">
<title>SNIPER BLAZE PRO — Web</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  :root{
    --bg:#0b0c10; --panel:#10131b; --panel2:#0e1118; --edge:#1b2233;
    --text:#e8eef6; --muted:#9aa7b7; --accent:#5d8cff; --accent2:#12d6a3;
    --red:#ff476a; --black:#111418; --chip:#151a26; --shadow:rgba(0,0,0,.35);
  }
  *{box-sizing:border-box}
  body{margin:0;background:linear-gradient(180deg,#0b0c10 0%,#0d0f17 100%);
       color:var(--text); font:14px/1.5 Inter,ui-sans-serif,system-ui,Segoe UI,Arial}
  .app{display:grid;grid-template-columns:260px 1fr;min-height:100vh}
  @media(max-width:980px){ .app{grid-template-columns:1fr} .aside{position:static;height:auto} }
  .aside{background:var(--panel2);border-right:1px solid var(--edge);padding:16px;position:sticky;top:0;height:100vh}
  .brand{display:flex;align-items:center;gap:10px;margin-bottom:18px}
  .dot{width:10px;height:10px;border-radius:50%;background:var(--accent2);box-shadow:0 0 14px var(--accent2)}
  h1{margin:0;font:800 15px/1 Inter;letter-spacing:.3px}
  .section-title{font:700 12px/1 Inter;color:var(--muted);margin:18px 0 8px 2px;letter-spacing:.3px}
  .seg{display:flex;background:#0d1220;border:1px solid #1e2740;border-radius:12px;overflow:hidden}
  .seg button{flex:1;background:transparent;border:0;color:var(--muted);padding:8px 10px;cursor:pointer}
  .seg button.on{background:var(--accent);color:#fff}
  .switch{position:relative;width:52px;height:28px;background:#151d2d;border:1px solid #25314a;border-radius:999px;cursor:pointer}
  .switch i{position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;background:#c7d0ff;transition:.2s}
  .switch.on{background:#16333a;border-color:#2f8e7a}
  .switch.on i{left:27px;background:#12d6a3}
  .chip{background:var(--chip);border:1px solid var(--edge);border-radius:10px;padding:6px 10px;color:#cfd6e3;font-size:12px}
  .bar{height:10px;background:#121827;border:1px solid #202a3e;border-radius:7px;overflow:hidden}
  .bar>span{display:block;height:100%}
  .neonW{background:#fff;box-shadow:0 0 10px rgba(255,255,255,.5)}
  .neonR{background:var(--red);box-shadow:0 0 10px rgba(255,71,106,.4)}
  .neonB{background:#0f1115;box-shadow:0 0 10px rgba(10,10,12,.35)}
  .pick{height:96px;border:1px dashed #2a3246;border-radius:12px;display:flex;align-items:center;justify-content:center;font:800 16px/1 Inter}
  .main{display:grid;grid-template-rows:auto 1fr;min-height:100vh}
  header{display:flex;align-items:center;gap:12px;padding:12px 18px;border-bottom:1px solid var(--edge);background:var(--panel)}
  header .sp{margin-left:auto;display:flex;gap:8px;align-items:center}
  .content{padding:16px;display:grid;gap:16px}
  @media(min-width:1000px){ .content{grid-template-columns:1.2fr .8fr} }
  .card{background:var(--panel);border:1px solid var(--edge);border-radius:14px;box-shadow:0 10px 24px var(--shadow);padding:14px}
  .h{margin:0 0 10px 0;font:700 13px/1 Inter;color:#d5def0}
  .stack{display:flex;gap:8px;overflow:auto;padding-bottom:6px}
  .sq{min-width:48px;height:48px;border-radius:12px;display:flex;align-items:center;justify-content:center;font:800 16px/1 Inter}
  .w{background:#ffffff;color:#111}.r{background:var(--red)}.b{background:#0f1115;color:#fff}
  table{width:100%;border-collapse:collapse}
  th,td{padding:10px;border-bottom:1px solid #1d2537;font-size:13px}
  th{color:#b7c1d7;text-align:left}
  tbody tr:nth-child(even){background:#0f1422}
  .badge{padding:4px 8px;border-radius:8px;font:800 11px/1 Inter}
  .ok{color:#12d6a3}.ko{color:#ff587a}
  .kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
  .kpi{background:#0e131f;border:1px solid var(--edge);border-radius:10px;padding:10px;text-align:center}
  .kpi b{display:block;font-size:11px;color:var(--muted);margin-bottom:4px}
  .kpi .v{font:800 16px/1 Inter}
</style>

<div class="app">
  <!-- Sidebar -->
  <aside class="aside">
    <div class="brand"><div class="dot"></div><h1>SNIPER BLAZE PRO</h1></div>

    <div class="section-title">Modo</div>
    <div class="seg" id="segMode">
      <button id="mW">⚪ Branco</button>
      <button id="mC" class="on">🎯 Cores</button>
    </div>

    <div class="section-title">Bot</div>
    <div style="display:flex;align-items:center;gap:10px">
      <span class="chip">Estado</span>
      <div id="sw" class="switch"><i></i></div>
    </div>

    <div class="section-title">KPIs</div>
    <div class="kpis">
      <div class="kpi"><b>Modo</b><div class="v" id="kMode">COLORS</div></div>
      <div class="kpi"><b>Bot</b><div class="v" id="kBot">OFF</div></div>
      <div class="kpi"><b>Sinais</b><div class="v" id="kSig">0</div></div>
    </div>

    <div class="section-title">Chances da rodada</div>
    <div class="card" style="padding:10px">
      <div style="display:flex;justify-content:space-between"><span>⚪ Branco</span><span id="pw">0%</span></div>
      <div class="bar" style="margin:6px 0 10px"><span id="bw" class="neonW" style="width:0%"></span></div>
      <div style="display:flex;justify-content:space-between"><span>🔴 Vermelho</span><span id="pr">0%</span></div>
      <div class="bar" style="margin:6px 0 10px"><span id="br" class="neonR" style="width:0%"></span></div>
      <div style="display:flex;justify-content:space-between"><span>⚫ Preto</span><span id="pb">0%</span></div>
      <div class="bar"><span id="bb" class="neonB" style="width:0%"></span></div>
    </div>

    <div class="section-title">Cor + indicada</div>
    <div id="pick" class="pick">— sem entrada —</div>
    <div id="why" style="margin-top:8px;color:var(--muted);font-size:12px">Motivos: —</div>
  </aside>

  <!-- Main -->
  <div class="main">
    <header>
      <div class="chip">Web</div>
      <div class="sp">
        <div class="chip" id="topMode">COLORS</div>
        <div class="chip" id="topBot">OFF</div>
        <div class="chip" id="detail">—</div>
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

$('#mW').onclick = ()=>{ post('/mode',{mode:'WHITE'}) };
$('#mC').onclick = ()=>{ post('/mode',{mode:'COLORS'}) };
$('#sw').onclick  = async ()=>{
  const on = $('#sw').classList.toggle('on');
  if(on){ await post('/bot',{active:true}); }else{ await post('/bot',{active:false}); }
};

function paintStack(lst){
  const st=$('#stack'); st.innerHTML='';
  lst.forEach(n=>{
    const d=document.createElement('div'); d.className='sq '+(n===0?'w':(n<=7?'r':'b')); d.textContent=n===0?'0':n; st.appendChild(d);
  });
}

function showPick(pick){
  const el=$('#pick');
  if(!pick){ el.style.background='transparent'; el.style.color='#e8eef6'; el.textContent='— sem entrada —'; return; }
  const [tgt,pv]=pick;
  el.style.color=(tgt==='W'?'#111':'#fff');
  el.style.background=(tgt==='W'?'#ffffff':(tgt==='R'?'#ff476a':'#0f1115'));
  el.textContent=(tgt==='W'?'⚪ Branco':(tgt==='R'?'🔴 Vermelho':'⚫ Preto'))+' • '+Math.round(pv*100)+'%';
}

function paintSigs(list){
  $('#kSig').textContent = list.length;
  const tb=$('#sigBody'); tb.innerHTML='';
  list.forEach(s=>{
    const tr=document.createElement('tr');
    const st = s.status==='open' ? '<span class="badge" style="color:#8aa0ff">OPEN</span>' :
               (s.status==='win' ? '<span class="badge ok">WIN</span>' : '<span class="badge ko">LOSS</span>');
    tr.innerHTML = `<td>${s.ts}</td><td>${s.mode}</td><td>${s.target}</td><td>${st}</td><td>${s.gale??'-'}</td><td>${s.came_n??'-'}</td>`;
    tb.appendChild(tr);
  });
}

async function tick(){
  try{
    const r=await fetch('/state'); const s=await r.json();

    // topo / kpis
    $('#topMode').textContent=s.mode; $('#kMode').textContent=s.mode;
    $('#topBot').textContent=s.bot_active?'ON':'OFF'; $('#kBot').textContent=s.bot_active?'ON':'OFF';
    if(s.mode==='WHITE'){ $('#mW').classList.add('on'); $('#mC').classList.remove('on'); } else { $('#mC').classList.add('on'); $('#mW').classList.remove('on'); }
    if(s.bot_active){ $('#sw').classList.add('on'); } else { $('#sw').classList.remove('on'); }

    // giros
    paintStack(s.last||[]);

    // probs
    const p=s.probs||{W:0,R:0,B:0};
    $('#pw').textContent=(p.W*100).toFixed(1)+'%'; $('#pr').textContent=(p.R*100).toFixed(1)+'%'; $('#pb').textContent=(p.B*100).toFixed(1)+'%';
    $('#bw').style.width=(p.W*100)+'%'; $('#br').style.width=(p.R*100)+'%'; $('#bb').style.width=(p.B*100)+'%';

    // pick + razões
    if(s.pick) showPick(s.pick); else showPick(null);
    $('#why').textContent='Motivos: '+(s.reasons && s.reasons.length ? s.reasons.join(', ') : '—');
    $('#detail').textContent=s.detail||'—';

    // histórico
    paintSigs(s.signals||[]);
  }catch(e){}
}
setInterval(tick, 900); tick();
</script>
"""

# ===================== API =====================
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
