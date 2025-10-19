# -*- coding: utf-8 -*-
import os, json, math
from collections import deque
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ===================== Estado & Config ======================
HISTORY_MAX = 2500
history = deque(maxlen=HISTORY_MAX)     # ints 0..14 (0=white)

_last_snapshot = []
_last_src = "—"

bot_active   = False
current_mode = "COLORS"                 # "WHITE" | "COLORS"
conf_min     = 0.60                     # 0.50..0.90 (mão do usuário no UI)

GALE_STEPS = [1,2,4]                    # só CORES
MAX_GALES  = 2

trade   = None                          # trade ativo (cores)
signals = deque(maxlen=900)             # feed (mais recente primeiro)

# métricas simples de sessão
session_closed = 0
session_wins   = 0

# ===================== Helpers gerais =======================
def is_red(n):   return 1 <= n <= 7
def is_black(n): return 8 <= n <= 14
def to_color(n): return 'W' if n==0 else ('R' if is_red(n) else 'B')

def last_k_colors(seq, k, ignore_white=True):
    out=[]
    for v in reversed(seq):
        if v==0 and ignore_white: continue
        out.append(to_color(v))
        if len(out)>=k: break
    return list(reversed(out))

def count_in_last(seq, k):
    lst=last_k_colors(seq, k)
    return lst.count('R'), lst.count('B'), lst

def current_streak(seq):
    last=None; s=0
    for v in reversed(seq):
        c=to_color(v)
        if c=='W': break
        if last is None or c==last: s+=1; last=c
        else: break
    return (last if last in ('R','B') else None), s

def alt_run(lst):
    if not lst: return 0
    r=1
    for i in range(len(lst)-2, -1, -1):
        if lst[i]==lst[i+1]: break
        r+=1
    return r

def gaps_from_seq(seq):
    idx=[i for i,v in enumerate(seq) if v==0]
    gaps=[]
    for a,b in zip(idx, idx[1:]): gaps.append(b-a)
    gap_atual=(len(seq)-1-idx[-1]) if idx else len(seq)
    return gaps, gap_atual

def pct(lst, p):
    if not lst: return 0.0
    s=sorted(lst); k=max(0, min(len(s)-1, int(round((p/100)*(len(s)-1)))))
    return float(s[k])

# ===================== Motores de Sinal =====================

def engine_white(seq):
    """Branco sem gale: só chama quando gap está realmente ‘forte’."""
    if not seq: return {"ok": False, "reasons": [], "detail": "", "conf":0.0}

    tail = list(seq)[-240:] if len(seq)>=240 else list(seq)
    gaps, gap = gaps_from_seq(tail)
    if not gaps: return {"ok": False, "reasons": [], "detail": "", "conf":0.0}

    mu = sum(gaps)/len(gaps)
    var = sum((g-mu)**2 for g in gaps)/max(1, len(gaps)-1)
    sd  = math.sqrt(var)
    p90 = pct(gaps,90)
    p95 = pct(gaps,95)

    reasons=[]; score=0.0

    if gap <= 8:       reasons.append("pós-0 ≤8");    score+=0.35
    if gap >= mu+sd:   reasons.append("gap ≥ μ+1σ");  score+=0.25
    if gap >= p90:     reasons.append("gap ≥ P90");    score+=0.22
    if gap >= p95:     reasons.append("seca (P95)");   score+=0.18

    conf = max(0.0, min(0.98, score))
    ok   = (conf >= conf_min) and (gap >= 5)
    detail = f"gap={gap} • μ≈{mu:.1f} • σ≈{sd:.1f} • P90≈{p90:.0f}"
    return {"ok": ok, "reasons": reasons, "detail": detail, "conf": conf}

def engine_colors(seq):
    """
    Cores com gale (até G2):
    - Momentum: 2→3 segue; 4 segue; 5+ quebra
    - Alternância longa quebra
    - Dominância 20/30 ≥62%
    - Desbalanceio forte nos últimos 10
    """
    if not seq: return {"ok": False, "reasons": [], "conf":0.0}

    r10,b10,_ = count_in_last(seq, 10)
    r20,b20,_ = count_in_last(seq, 20)
    r30,b30,_ = count_in_last(seq, 30)
    cur, s    = current_streak(seq)
    alt       = alt_run(last_k_colors(seq, 12))

    reasons=[]; score=0.0; target=None

    if cur and s==2:
        target=cur; reasons.append("2→3"); score+=0.28
    if cur and s==4 and (target is None or target==cur):
        target=cur; reasons.append("muro de 4"); score+=0.22
    if cur and s>=5:
        target=('B' if cur=='R' else 'R'); reasons.append(f"quebrar streak {s}"); score+=0.35
    if alt>=6 and target is None:
        last = last_k_colors(seq,1)
        if last: target=last[-1]; reasons.append("anti-alternância"); score+=0.2

    tot20=max(1,r20+b20); tot30=max(1,r30+b30)
    if r20/tot20>=0.62 and (target is None or target=='R'):
        target='R'; reasons.append("dominância R 20"); score+=0.18
    if b20/tot20>=0.62 and (target is None or target=='B'):
        target='B'; reasons.append("dominância B 20"); score+=0.18
    if r30/tot30>=0.62 and (target is None or target=='R'):
        target='R'; reasons.append("dominância R 30"); score+=0.12
    if b30/tot30>=0.62 and (target is None or target=='B'):
        target='B'; reasons.append("dominância B 30"); score+=0.12

    if abs(r10-b10)>=4 and target is None:
        target='R' if r10>b10 else 'B'
        reasons.append("desbalanceio 10"); score+=0.16

    conf = max(0.0, min(0.98, score))
    ok   = (target is not None) and (conf >= conf_min)
    return {"ok":ok, "target":target, "reasons":reasons, "conf":conf}

def estimate_probs(seq):
    """Distribuição (W/R/B) + pick recomendado (só informativo)."""
    baseW = 1.0/15.0
    sw = engine_white(seq)
    pW = min(0.40, baseW + 0.20*sw["conf"])

    r20,b20,_ = count_in_last(seq, 20); tot=max(1,r20+b20)
    pR_raw=(r20+1)/(tot+2); pB_raw=(b20+1)/(tot+2)
    rem = max(0.0, 1.0-pW); pR = pR_raw*rem; pB = pB_raw*rem

    s = pW+pR+pB; pW,pR,pB = pW/s, pR/s, pB/s
    pick = max([("W",pW),("R",pR),("B",pB)], key=lambda x:x[1])
    return {"W":pW,"R":pR,"B":pB,"pick":pick}

# ===================== Signals / Trades =====================

def _append_signal(mode, target, status="open", gale=None, came_n=None, reasons=None, conf=0.0):
    signals.appendleft({
        "ts": datetime.now().strftime("%H:%M:%S"),
        "mode": mode,
        "target": target,               # 'W'|'R'|'B'
        "status": status,               # 'open'|'win'|'loss'
        "gale": gale,                   # 0|1|2
        "came_n": came_n,
        "reasons": reasons or [],
        "conf": round(conf*100)
    })

def open_trade_colors(target, reasons, conf):
    global trade
    trade = {
        "target": target, "step": 0, "await_unique": True,
        "opened_last": history[-1] if len(history) else None,
        "reasons": reasons, "conf": conf
    }
    _append_signal("COLORS", target, "open", gale=0, reasons=reasons, conf=conf)

def close_trade(result, came_n=None):
    global trade, session_closed, session_wins
    if not trade: return
    g = trade["step"]
    for s in signals:
        if s["status"]=="open" and s["mode"]=="COLORS":
            s["status"]=result; s["gale"]=g; s["came_n"]=came_n
            break
    session_closed += 1
    if result=="win": session_wins += 1
    trade = None

def process_new_number(n:int):
    """Chamado sempre que chega número novo (se bot ON)."""
    global trade, session_closed, session_wins

    if current_mode=="WHITE":
        sw = engine_white(history)
        if sw["ok"]:
            _append_signal("WHITE","W","open",conf=sw["conf"],reasons=sw["reasons"])
            # resolve com o próximo número (n já é o atual)
            if n==0: signals[0]["status"]="win";  signals[0]["came_n"]=0; session_wins+=1
            else:    signals[0]["status"]="loss"; signals[0]["came_n"]=n
            session_closed+=1
        return

    # CORES
    if not trade:
        sc = engine_colors(history)
        if sc["ok"]:
            open_trade_colors(sc["target"], sc["reasons"], sc["conf"])
    else:
        if trade.get("await_unique", False):
            if trade.get("opened_last", None) == n: return
            trade["await_unique"]=False; trade["opened_last"]=None

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

# ===================== Snapshot Merge =======================
def merge_snapshot(snapshot):
    global _last_snapshot
    if not snapshot: return 0
    snap = [int(x) for x in snapshot if isinstance(x,int) and 0<=x<=14]
    if not snap: return 0

    def ov(a,b,kmax=60):
        kmax=min(kmax,len(a),len(b))
        for k in range(kmax,0,-1):
            if a[-k:]==b[-k:]: return k
        return 0

    added=0
    if not _last_snapshot:
        _last_snapshot=list(snap)
        for n in snap: history.append(n); added+=1
        return added

    a=snap; b=snap[::-1]
    ova=ov(_last_snapshot,a); ovb=ov(_last_snapshot,b)
    chosen=a if ova>=ovb else b; k=max(ova,ovb)
    if k>=len(chosen):
        _last_snapshot=list(chosen); return 0

    for n in chosen[k:]:
        history.append(n); added+=1
    _last_snapshot=list(chosen)
    return added

# ===================== UI — Painel NOVO =====================
HTML = """<!doctype html><meta charset="utf-8">
<title>SNIPER BLAZE PRO — COMMAND</title>
<meta name="viewport" content="width=device-width,initial-scale=1" />
<style>
:root{
  --bg:#080a12; --bg2:#0b0f1a; --edge:#18213a;
  --glass: rgba(255,255,255,.04); --glass2: rgba(255,255,255,.06);
  --txt:#e8eef9; --muted:#9aa6bd; --accent:#7e9bff; --good:#18d2a5; --bad:#ff5c7a;
  --red:#ff3c62; --black:#0f1116; --chip:#0e1422;
}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(1200px 600px at 10% -10%, #141e38 0%, transparent 60%) ,linear-gradient(180deg,#09101f 0%,#070a12 100%);
     color:var(--txt);font:14px/1.5 Inter,system-ui,Segoe UI,Arial}
.wrap{display:grid;grid-template-columns: 340px 1fr;min-height:100vh}
@media(max-width:1080px){.wrap{grid-template-columns:1fr}}
aside{
  position:sticky;top:0;height:100vh;padding:16px;
  background:linear-gradient(180deg, rgba(255,255,255,.02), rgba(255,255,255,.03));
  border-right:1px solid var(--edge);backdrop-filter: blur(8px);
}
.brand{display:flex;align-items:center;gap:12px;margin-bottom:12px}
.brand .dot{width:10px;height:10px;border-radius:50%;background:var(--good);box-shadow:0 0 12px var(--good)}
.brand h1{margin:0;font:800 15px/1 Inter;letter-spacing:.3px}
.section{margin:18px 0 8px;color:var(--muted);font:700 12px/1 Inter}
.card{background:var(--glass);border:1px solid var(--edge);border-radius:14px;padding:12px;box-shadow:0 10px 28px rgba(0,0,0,.35)}
.controls{display:grid;gap:10px}
.row{display:flex;gap:10px;align-items:center}
.btn{cursor:pointer;border:1px solid var(--edge);background:var(--chip);color:#cfe0ff;padding:8px 12px;border-radius:10px}
.btn.on{background:var(--good);border-color:#1a8d75;color:#06251f}
.badge{padding:6px 10px;border-radius:999px;border:1px solid var(--edge);background:var(--glass2);color:#c8d4ef}
.seg{display:flex;border:1px solid var(--edge);border-radius:10px;overflow:hidden}
.seg button{flex:1;border:0;padding:9px 10px;background:transparent;color:#bfc8e1;cursor:pointer}
.seg button.on{background:var(--accent);color:#fff}
.range{display:flex;align-items:center;gap:10px}
input[type=range]{width:100%}
.small{color:var(--muted);font-size:12px}

main{display:grid;grid-template-rows:auto 1fr}
header{display:flex;align-items:center;gap:10px;padding:12px 18px;border-bottom:1px solid var(--edge);
  background:linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,.02));backdrop-filter: blur(8px)}
.kpi{display:flex;gap:10px}
.kpi .pill{background:var(--glass2);border:1px solid var(--edge);padding:6px 10px;border-radius:999px;color:#d3defc}
.kpi b{color:#aebbd8;margin-right:6px}

.content{display:grid;grid-template-columns: 1.2fr .8fr;gap:16px;padding:16px}
@media(max-width:1200px){.content{grid-template-columns:1fr}}
.tile{background:var(--glass);border:1px solid var(--edge);border-radius:16px;padding:14px;box-shadow:0 16px 36px rgba(0,0,0,.45)}
.h{margin:0 0 10px;font:700 13px/1 Inter;color:#d7e2ff}

#call{
  display:grid;grid-template-columns: 240px 1fr;gap:16px;align-items:center;
}
@media(max-width:820px){#call{grid-template-columns:1fr}}

.radar{width:240px;height:240px;border-radius:50%;display:grid;place-items:center;
  background:
    radial-gradient(closest-side, rgba(255,255,255,.04) 92%, transparent 94% 100%),
    conic-gradient(var(--good) 0deg, var(--good) 0deg, rgba(255,255,255,.08) 0deg);
  border:1px solid var(--edge);box-shadow:0 20px 40px rgba(0,0,0,.5), inset 0 0 60px rgba(30,230,170,.06);
}
.radar .c{font:800 40px/1 Inter}
.radar .t{margin-top:4px;color:#a9b6d7}

.pick{
  height:240px;border:1px dashed var(--edge);border-radius:14px;
  display:flex;align-items:center;justify-content:center;font:800 22px/1 Inter
}
.pick.w{background:#fff;color:#111}
.pick.r{background:var(--red);color:#fff}
.pick.b{background:#0f1116;color:#fff}

#reasons{margin-top:12px}
.tag{display:inline-block;margin:6px 6px 0 0;padding:6px 10px;border:1px solid var(--edge);
     background:var(--glass2);border-radius:999px;color:#cfe0ff;font-size:12px}

.stack{display:flex;gap:8px;overflow:auto;padding-bottom:6px}
.sq{min-width:52px;height:52px;border-radius:12px;display:flex;align-items:center;justify-content:center;font:800 16px/1 Inter}
.w{background:#ffffff;color:#111}.r{background:var(--red)}.b{background:#0f1115;color:#fff}

.feed{max-height:540px;overflow:auto;border-top:1px dashed var(--edge)}
.item{display:grid;grid-template-columns:80px 70px 1fr 70px 50px;gap:10px;align-items:center;padding:10px 0;border-bottom:1px dotted #1b2642}
.status{font:800 11px/1 Inter}
.win{color:var(--good)} .loss{color:var(--bad)} .open{color:#8aa2ff}
.circle{width:16px;height:16px;border-radius:50%}
.cR{background:var(--red)} .cB{background:#0f1115;border:1px solid #2a2f38} .cW{background:#fff;border:1px solid #cfd5e4}

.chart{background:var(--glass);border:1px solid var(--edge);border-radius:14px;padding:12px}
.bar{height:10px;background:#0d1425;border:1px solid #1f2b46;border-radius:8px;overflow:hidden}
.bar>span{display:block;height:100%}
.neonW{background:#fff;box-shadow:0 0 10px rgba(255,255,255,.55)}
.neonR{background:var(--red);box-shadow:0 0 12px rgba(255,61,98,.45)}
.neonB{background:#0f1116;box-shadow:0 0 10px rgba(0,0,0,.6)}
</style>

<div class="wrap">
  <aside>
    <div class="brand"><div class="dot"></div><h1>SNIPER BLAZE PRO</h1></div>

    <div class="section">Controles</div>
    <div class="controls card">
      <div class="row">
        <span class="badge">Modo</span>
        <div class="seg" style="flex:1">
          <button id="mW">⚪ Branco</button>
          <button id="mC" class="on">🎯 Cores</button>
        </div>
      </div>
      <div class="row">
        <span class="badge">Bot</span>
        <div id="bOn" class="btn">ON</div>
        <div id="bOff" class="btn on">OFF</div>
      </div>
      <div class="range">
        <span class="badge">Limiar</span>
        <input id="rng" type="range" min="50" max="90" value="60">
        <div id="rngv" class="badge">60%</div>
      </div>
      <div class="row small">
        <span class="badge">Preset</span>
        <div class="btn" onclick="preset(55)">Conservador</div>
        <div class="btn" onclick="preset(60)">Normal</div>
        <div class="btn" onclick="preset(68)">Agressivo</div>
      </div>
    </div>

    <div class="section">Probabilidades</div>
    <div class="card chart">
      <div style="display:flex;justify-content:space-between"><span>⚪ Branco</span><span id="pw">0%</span></div>
      <div class="bar" style="margin:6px 0 10px"><span id="bw" class="neonW" style="width:0%"></span></div>
      <div style="display:flex;justify-content:space-between"><span>🔴 Vermelho</span><span id="pr">0%</span></div>
      <div class="bar" style="margin:6px 0 10px"><span id="br" class="neonR" style="width:0%"></span></div>
      <div style="display:flex;justify-content:space-between"><span>⚫ Preto</span><span id="pb">0%</span></div>
      <div class="bar"><span id="bb" class="neonB" style="width:0%"></span></div>
    </div>
  </aside>

  <main>
    <header>
      <div class="kpi">
        <div class="pill"><b>Modo</b> <span id="kMode">COLORS</span></div>
        <div class="pill"><b>Bot</b> <span id="kBot">OFF</span></div>
        <div class="pill"><b>WR</b> <span id="kWR">0.0%</span></div>
        <div class="pill"><b>Sinais</b> <span id="kSig">0</span></div>
        <div class="pill"><b>Fonte</b> <span id="kSrc">—</span></div>
        <div class="pill"><b>Detalhe</b> <span id="kDetail">—</span></div>
      </div>
    </header>

    <div class="content">
      <section class="tile">
        <h3 class="h">Próxima entrada</h3>
        <div id="call">
          <div>
            <div id="radar" class="radar"><div class="c" id="cVal">0%</div><div class="t" id="cTxt">Confiança</div></div>
          </div>
          <div>
            <div id="pick" class="pick">— aguardando padrão —</div>
            <div id="reasons"></div>
          </div>
        </div>
        <div style="margin-top:14px">
          <div class="h">Últimos giros</div>
          <div id="stack" class="stack"></div>
        </div>
      </section>

      <section class="tile">
        <h3 class="h">Histórico de sinais</h3>
        <div class="feed" id="feed"></div>
      </section>
    </div>
  </main>
</div>

<script>
const $=s=>document.querySelector(s);
async function post(u,b){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});}
function preset(v){ $('#rng').value=v; $('#rngv').textContent=v+'%'; post('/conf',{conf:v/100}); }

$('#mW').onclick=()=>post('/mode',{mode:'WHITE'});
$('#mC').onclick=()=>post('/mode',{mode:'COLORS'});
$('#bOn').onclick=()=>{ $('#bOn').classList.add('on'); $('#bOff').classList.remove('on'); post('/bot',{active:true}); }
$('#bOff').onclick=()=>{ $('#bOff').classList.add('on'); $('#bOn').classList.remove('on'); post('/bot',{active:false}); }
$('#rng').oninput=e=>{ $('#rngv').textContent=e.target.value+'%'; post('/conf',{conf:parseInt(e.target.value,10)/100}); }

function paintRadar(p){ // p = 0..1
  const deg=Math.round(p*360);
  $('#radar').style.background =
  `radial-gradient(closest-side, rgba(255,255,255,.05) 92%, transparent 94% 100%),`+
  `conic-gradient(#18d2a5 ${deg}deg, rgba(255,255,255,.08) ${deg}deg)`;
  $('#cVal').textContent=Math.round(p*100)+'%';
}

function paintPick(p){
  const el=$('#pick');
  if(!p){ el.className='pick'; el.textContent='— aguardando padrão —'; return; }
  const [tgt,val]=p;
  el.className='pick '+(tgt==='W'?'w':(tgt==='R'?'r':'b'));
  el.textContent=(tgt==='W'?'⚪ Branco':(tgt==='R'?'🔴 Vermelho':'⚫ Preto'))+' • '+Math.round(val*100)+'%';
  paintRadar(val);
}

function chips(lst){
  const s=$('#stack'); s.innerHTML='';
  lst.forEach(n=>{
    const d=document.createElement('div');
    d.className='sq '+(n===0?'w':(n<=7?'r':'b')); d.textContent=n===0?'0':n; s.appendChild(d);
  });
}

function feed(list){
  const f=$('#feed'); f.innerHTML='';
  list.forEach(x=>{
    const d=document.createElement('div'); d.className='item';
    let dot = x.target==='R'?'cR':(x.target==='B'?'cB':'cW');
    d.innerHTML = `
      <div class="small">${x.ts}</div>
      <div class="small"><span class="circle ${dot}"></span> ${x.target}</div>
      <div class="small">${(x.reasons||[]).slice(0,3).join(' • ')}</div>
      <div class="status ${x.status}">${x.status.toUpperCase()}</div>
      <div class="small">${x.came_n??'-'}</div>`;
    f.appendChild(d);
  });
}

async function tick(){
  try{
    const r=await fetch('/state'); const s=await r.json();

    $('#kMode').textContent=s.mode; $('#kBot').textContent=s.bot_active?'ON':'OFF';
    $('#kSig').textContent=s.sigs; $('#kWR').textContent=s.wr.toFixed(1)+'%';
    $('#kSrc').textContent=s.src||'—'; $('#kDetail').textContent=s.detail||'—';

    if(s.mode==='WHITE'){ $('#mW').classList.add('on'); $('#mC').classList.remove('on'); } else { $('#mC').classList.add('on'); $('#mW').classList.remove('on'); }
    if(s.bot_active){ $('#bOn').classList.add('on'); $('#bOff').classList.remove('on'); } else { $('#bOff').classList.add('on'); $('#bOn').classList.remove('on'); }

    chips(s.last||[]);
    const p=s.probs||{W:0,R:0,B:0};
    $('#pw').textContent=(p.W*100).toFixed(1)+'%'; $('#pr').textContent=(p.R*100).toFixed(1)+'%'; $('#pb').textContent=(p.B*100).toFixed(1)+'%';
    $('#bw').style.width=(p.W*100)+'%'; $('#br').style.width=(p.R*100)+'%'; $('#bb').style.width=(p.B*100)+'%';

    paintPick(s.pick);
    $('#reasons').innerHTML = (s.reasons||[]).map(x=>`<span class="tag">${x}</span>`).join('') || '<span class="small">—</span>';

    feed(s.signals||[]);
  }catch(e){}
}
setInterval(tick, 900); tick();
</script>
"""

# ===================== API =========================
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
        trade=None
        return jsonify(ok=True, mode=current_mode)
    return jsonify(ok=False), 400

@app.post("/bot")
def set_bot():
    global bot_active, trade
    data = request.get_json(force=True, silent=True) or {}
    bot_active = bool(data.get("active", False))
    if not bot_active: trade=None
    return jsonify(ok=True, bot_active=bot_active)

@app.post("/conf")
def set_conf():
    global conf_min
    data = request.get_json(force=True, silent=True) or {}
    v = float(data.get("conf", conf_min))
    conf_min = max(0.50, min(0.90, v))
    return jsonify(ok=True, conf=conf_min)

@app.get("/state")
def state():
    last = list(history)[-20:]
    probs = estimate_probs(history)
    src = _last_src
    wr  = (100.0*session_wins/session_closed) if session_closed>0 else 0.0

    pick=None; reasons=[]; detail=""
    if current_mode=="WHITE":
        sw = engine_white(history); reasons=sw["reasons"]; detail=sw["detail"]
        pick = ("W", probs["W"]) if sw["ok"] else None
    else:
        if trade:
            pick=(trade["target"], probs[trade["target"]])
            reasons = trade.get("reasons", [])
        else:
            sc = engine_colors(history); reasons=sc.get("reasons", [])
            pick = (sc["target"], probs[sc["target"]]) if sc.get("ok") else None

    return jsonify(
        last=last, mode=current_mode, bot_active=bot_active,
        probs={"W":probs["W"],"R":probs["R"],"B":probs["B"]},
        pick=pick, reasons=reasons, detail=detail, src=src,
        signals=list(signals), wr=wr, sigs=len(signals)
    )

@app.post("/ingest")
def ingest():
    global _last_src
    data = request.get_json(force=True)
    hist = data.get("history") or []
    src  = data.get("src") or "?"
    _last_src = src
    added = merge_snapshot(hist)

    if bot_active and added>0:
        for n in hist[-added:]:
            if isinstance(n,int) and 0<=n<=14:
                process_new_number(n)

    return jsonify(ok=True, added=added, time=datetime.now().isoformat())

if __name__ == "__main__":
    port=int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
