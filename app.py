# -*- coding: utf-8 -*-
import os, math, json
from datetime import datetime
from collections import deque
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ======== Estado ========
HISTORY_MAX = 2000
history = deque(maxlen=HISTORY_MAX)   # ints 0..14 (0 = branco)
current_mode = "WHITE"                 # "WHITE" | "COLORS"

# ======== Helpers ========
def is_red(n):   return 1 <= n <= 7
def is_black(n): return 8 <= n <= 14

def last_k_colors(seq, k, ignore_white=True):
    out=[]
    for v in reversed(seq):
        if v==0 and ignore_white: continue
        if v==0: out.append('W')
        else: out.append('R' if is_red(v) else 'B')
        if len(out)>=k: break
    return list(reversed(out))

def count_in_last(seq, k):
    lst = last_k_colors(seq, k)
    return lst.count("R"), lst.count("B"), lst

def current_streak(seq):
    """('R'|'B'|None, len). Ignora brancos."""
    last=None; s=0
    for v in reversed(seq):
        c=('W' if v==0 else ('R' if is_red(v) else 'B'))
        if c=='W': break
        if last is None or c==last:
            s+=1; last=c
        else:
            break
    return (last if last in ('R','B') else None), s

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

# ======== Heurísticas de Sinais (compactas) ========
def white_signal(seq):
    """
    Retorna {"ok":bool, "reasons":[...], "detail":str}
    Lógica enxuta baseada em gap alto e alguns filtros simples.
    """
    if not seq: return {"ok": False}
    tail = list(seq)[-240:] if len(seq)>=240 else list(seq)
    gaps, gap = gaps_from_seq(tail)
    if not gaps: return {"ok": False}

    mu = sum(gaps)/len(gaps)
    p90 = pct(gaps, 90)
    p95 = pct(gaps, 95)
    rate8 = sum(1 for g in gaps if g<=8)/len(gaps)  # frequência de brancos próximos
    r10, b10, _ = count_in_last(tail, 10)
    reasons=[]

    # gatilhos
    if gap>=max(20, mu*1.2): reasons.append("gap alto")
    if gap>=p90: reasons.append("≥P90")
    if gap>=p95: reasons.append("≥P95")
    if rate8>=0.45 and gap<=8: reasons.append("pós-branco ≤8 frequente")
    if max(r10,b10)>=7: reasons.append("tendência de cor (pré-branco)")

    ok = (len(reasons)>=2) and (gap>=max(16, mu))
    return {"ok": ok, "reasons": reasons, "detail": f"gap={gap} μ≈{mu:.1f} P90≈{p90:.0f}"}

def color_signal(seq):
    """
    Retorna {"ok":bool, "target":'R'|'B', "reasons":[...]}
    Heurística curta: sequência, alternância e dominância recente.
    """
    if not seq: return {"ok": False}
    r10,b10,_ = count_in_last(seq, 10)
    r20,b20,_ = count_in_last(seq, 20)
    cur, s = current_streak(seq)
    reasons=[]

    # 1) repetir a 3ª
    if cur and s==2:
        reasons.append("2→3")
        return {"ok": True, "target": cur, "reasons": reasons}

    # 2) quebrar sequências longas
    if cur and s>=5:
        reasons.append(f"quebrar streak {s}")
        return {"ok": True, "target": ('B' if cur=='R' else 'R'), "reasons": reasons}

    # 3) dominância 20/30
    tot20=max(1, r20+b20)
    if r20/tot20>=0.62:
        reasons.append("dominância R em 20")
        return {"ok": True, "target": 'R', "reasons": reasons}
    if b20/tot20>=0.62:
        reasons.append("dominância B em 20")
        return {"ok": True, "target": 'B', "reasons": reasons}

    # 4) leve favorecimento 10 últimos
    if abs(r10-b10)>=4:
        tgt = 'R' if r10>b10 else 'B'
        reasons.append("desbalanceio em 10")
        return {"ok": True, "target": tgt, "reasons": reasons}

    return {"ok": False}

# ======== Probabilidade simples (para UI) ========
def estimate_probs(seq):
    """Retorna probs ~[0,1] p/ W/R/B + recomendação."""
    # branco base ~6.67% com pequenos reforços
    pW = 1.0/15.0
    sigW = white_signal(seq)
    if sigW["ok"]:
        pW = min(0.40, pW + 0.08 + 0.02*len(sigW["reasons"]))  # até ~40%

    r20,b20,_ = count_in_last(seq, 20)
    tot = max(1, r20+b20)
    pR_raw = (r20 + 1) / (tot + 2)
    pB_raw = (b20 + 1) / (tot + 2)
    rem = max(0.0, 1.0 - pW)
    pR = pR_raw * rem
    pB = pB_raw * rem
    s = pW+pR+pB
    pW,pR,pB = pW/s, pR/s, pB/s
    rec = max([("W",pW),("R",pR),("B",pB)], key=lambda x:x[1])
    return {"W":pW,"R":pR,"B":pB,"rec":rec}

# ======== HTML (UI com seletor de modo) ========
HTML = """<!doctype html><meta charset="utf-8">
<title>SNIPER BLAZE PRO — WEB</title>
<style>
  :root{--bg:#0f0821;--card:#1a0f38;--muted:#c9c4de;--accent:#7b2cbf;--green:#28c08a;--red:#ff3b4d;--ink:#e8e6f3}
  body{margin:0;background:var(--bg);color:#fff;font:16px/1.4 system-ui,Segoe UI,Arial}
  header{padding:14px 18px;font-weight:700}
  .wrap{padding:10px 18px;display:grid;grid-template-columns:280px 1fr;gap:12px}
  .box{display:flex;gap:8px;flex-direction:column}
  .card{background:var(--card);border-radius:12px;padding:12px}
  .k{display:flex;align-items:center;gap:8px;margin-bottom:8px}
  button{background:#24124a;border:0;color:#fff;padding:8px 12px;border-radius:10px;cursor:pointer}
  button.primary{background:var(--accent)}
  .chip{display:inline-block;margin-right:8px;padding:6px 10px;border-radius:999px;background:#261541;color:#ddd;font-size:12px}
  .grid{display:grid;grid-template-columns:repeat(5,64px);gap:8px;align-items:start}
  .sq{width:60px;height:60px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-weight:700}
  .w {background:#fff;color:#111}
  .r {background:#ff2e2e}
  .b {background:#0e0e10}
  h3{margin:0 0 12px 0;font-size:16px}
  .bar{height:16px;background:#2b1e4d;border-radius:10px;position:relative}
  .bar>span{position:absolute;left:0;top:0;bottom:0;border-radius:10px}
  .lbl{display:flex;justify-content:space-between;margin:6px 0 10px 0;color:var(--muted);font-size:14px}
  .hint{color:var(--muted);font-size:13px}
</style>
<header>SNIPER BLAZE PRO — WEB</header>

<div class="wrap">
  <div class="box">
    <div class="card">
      <div class="k">
        <b>Modo:</b>
        <button id="btnW">⚪ BRANCO</button>
        <button id="btnC">🔴⚫ CORES</button>
        <span id="modeTag" class="chip">WHITE</span>
      </div>
      <div class="hint">Troque o modo aqui. O servidor calcula o sinal com base no histórico que chega do SubBot.</div>
    </div>

    <div class="card">
      <h3>Chances da rodada</h3>
      <div class="lbl">⚪ Branco <span id="pw">0%</span></div>
      <div class="bar"><span id="bw" style="background:#fff;width:0%"></span></div>
      <div class="lbl">🔴 Vermelho <span id="pr">0%</span></div>
      <div class="bar"><span id="br" style="background:#ff2e2e;width:0%"></span></div>
      <div class="lbl">⚫ Preto <span id="pb">0%</span></div>
      <div class="bar"><span id="bb" style="background:#0e0e10;width:0%"></span></div>
    </div>

    <div class="card">
      <h3>Cor + indicada</h3>
      <div id="pick" class="sq" style="width:180px;height:110px;border:2px solid #2b1e4d">—</div>
      <div id="why" class="hint" style="margin-top:8px">Motivos: —</div>
    </div>
  </div>

  <div class="card">
    <h3>Últimos giros</h3>
    <div id="boxes" class="grid"></div>
    <div id="detail" class="hint" style="margin-top:10px">—</div>
  </div>
</div>

<script>
async function setMode(m){
  await fetch('/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:m})});
}
document.getElementById('btnW').onclick=()=>setMode('WHITE');
document.getElementById('btnC').onclick=()=>setMode('COLORS');

function paintBoxes(lst){
  const boxes=document.getElementById('boxes'); boxes.innerHTML='';
  lst.forEach(n=>{
    const d=document.createElement('div'); d.className='sq';
    if(n===0){d.classList.add('w'); d.textContent='0';}
    else if(n<=7){d.classList.add('r'); d.textContent=n;}
    else {d.classList.add('b'); d.textContent=n;}
    boxes.appendChild(d);
  });
}

function setPick(tgt, pct){
  const el=document.getElementById('pick');
  el.textContent='';
  el.style.color = tgt==='W' ? '#111' : '#fff';
  if(tgt==='W') el.style.background='#fff';
  if(tgt==='R') el.style.background='#ff2e2e';
  if(tgt==='B') el.style.background='#0e0e10';
  el.innerHTML = (tgt==='W'?'⚪ Branco':(tgt==='R'?'🔴 Vermelho':'⚫ Preto')) + '<br><b>'+Math.round(pct*100)+'%</b>';
}

async function tick(){
  try{
    const r=await fetch('/state'); const s=await r.json();
    document.getElementById('modeTag').textContent=s.mode;
    paintBoxes(s.last||[]);
    document.getElementById('pw').textContent=(s.probs.W*100).toFixed(1)+'%';
    document.getElementById('pr').textContent=(s.probs.R*100).toFixed(1)+'%';
    document.getElementById('pb').textContent=(s.probs.B*100).toFixed(1)+'%';
    document.getElementById('bw').style.width=(s.probs.W*100)+'%';
    document.getElementById('br').style.width=(s.probs.R*100)+'%';
    document.getElementById('bb').style.width=(s.probs.B*100)+'%';
    if(s.pick && s.pick[0]) setPick(s.pick[0], s.pick[1]);
    document.getElementById('why').textContent = 'Motivos: ' + (s.reasons && s.reasons.length ? s.reasons.join(', ') : '—');
    document.getElementById('detail').textContent = s.detail || '—';
  }catch(e){}
}
setInterval(tick, 900); tick();
</script>
"""

# ======== Endpoints ========
@app.get("/")
def index():
    return render_template_string(HTML)

@app.post("/mode")
def set_mode():
    global current_mode
    data = request.get_json(force=True, silent=True) or {}
    mode = (data.get("mode") or "").upper()
    if mode in ("WHITE","COLORS"):
        current_mode = mode
        return jsonify(ok=True, mode=current_mode)
    return jsonify(ok=False, error="mode must be WHITE or COLORS"), 400

@app.get("/state")
def state():
    lst = list(history)[-5:]
    probs = estimate_probs(history)
    reasons=[]; detail=''
    pick = probs["rec"]
    # Se modo WHITE e white_signal ok -> força WHITE
    if current_mode == "WHITE":
        sw = white_signal(history)
        reasons = sw["reasons"]; detail = sw.get("detail","")
        if sw["ok"]:
            pick = ("W", probs["W"])
    elif current_mode == "COLORS":
        sc = color_signal(history)
        reasons = sc.get("reasons",[])
        if sc.get("ok"):
            tgt = sc.get("target")
            pct = probs[tgt]
            pick = (tgt, pct)
    return jsonify(
        last=lst, size=len(history),
        mode=current_mode,
        probs=probs, pick=pick, reasons=reasons, detail=detail
    )

@app.post("/ingest")
def ingest():
    data = request.get_json(force=True)
    hist = data.get("history") or []
    added=0
    for n in hist:
        if isinstance(n,int) and 0<=n<=14:
            history.append(n); added+=1
    return jsonify(added=added, ok=True, time=datetime.now().isoformat())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
