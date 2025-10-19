# -*- coding: utf-8 -*-
import os, math, json, time
from datetime import datetime
from collections import deque
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ================= Estado =================
HISTORY_MAX = 2000
history = deque(maxlen=HISTORY_MAX)          # ints 0..14 (0 = branco)
current_mode = "WHITE"                       # "WHITE" | "COLORS"
bot_active = False

# Trade APENAS para CORES
GALE_STEPS = [1, 2, 4]   # unidades (ex.: 1u, 2u, 4u)
MAX_GALES  = 2           # G0..G2
trade = None             # dict ativo para cores
signals = deque(maxlen=200)  # histórico de sinais (mais recentes primeiro)

# ================= Helpers =================
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
    last=None; s=0
    for v in reversed(seq):
        c=('W' if v==0 else ('R' if is_red(v) else 'B'))
        if c=='W': break
        if last is None or c==last: s+=1; last=c
        else: break
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

# ================= Heurísticas compactas =================
def white_signal(seq):
    """Retorna {"ok":bool, "reasons":[...], "detail":str}"""
    if not seq: return {"ok": False}
    tail = list(seq)[-240:] if len(seq)>=240 else list(seq)
    gaps, gap = gaps_from_seq(tail)
    if not gaps: return {"ok": False}
    mu = sum(gaps)/len(gaps)
    p90 = pct(gaps, 90)
    p95 = pct(gaps, 95)
    rate8 = sum(1 for g in gaps if g<=8)/len(gaps)
    r10, b10, _ = count_in_last(tail, 10)
    reasons=[]
    if gap>=max(20, mu*1.2): reasons.append("gap alto")
    if gap>=p90: reasons.append("≥P90")
    if gap>=p95: reasons.append("≥P95")
    if rate8>=0.45 and gap<=8: reasons.append("pós-branco≤8 frequente")
    if max(r10,b10)>=7: reasons.append("tendência pré-branco")
    ok = (len(reasons)>=2) and (gap>=max(16, mu))
    return {"ok": ok, "reasons": reasons, "detail": f"gap={gap} μ≈{mu:.1f} P90≈{p90:.0f}"}

def color_signal(seq):
    """Retorna {"ok":bool, "target":'R'|'B', "reasons":[...]}"""
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
    tot20=max(1, r20+b20)
    if r20/tot20>=0.62:
        reasons.append("dominância R em 20")
        return {"ok": True, "target": 'R', "reasons": reasons}
    if b20/tot20>=0.62:
        reasons.append("dominância B em 20")
        return {"ok": True, "target": 'B', "reasons": reasons}
    if abs(r10-b10)>=4:
        tgt = 'R' if r10>b10 else 'B'
        reasons.append("desbalanceio em 10")
        return {"ok": True, "target": tgt, "reasons": reasons}
    return {"ok": False}

def estimate_probs(seq):
    pW = 1.0/15.0
    sigW = white_signal(seq)
    if sigW["ok"]:
        pW = min(0.40, pW + 0.08 + 0.02*len(sigW["reasons"]))
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

def _append_signal(mode, target, status="open", gale_step=None, came_n=None, reasons=None):
    """Guarda no histórico (mais recente primeiro)."""
    signals.appendleft({
        "ts": datetime.now().strftime("%H:%M:%S"),
        "mode": mode,                   # "WHITE"|"COLORS"
        "target": target,               # 'W'|'R'|'B'
        "status": status,               # "open"|"win"|"loss"
        "gale": gale_step,              # 0..2 (só cores)
        "came_n": came_n,               # número real
        "reasons": reasons or []
    })

# ================= Engine: start/stop + processamento =================
def open_trade_colors(target, reasons):
    global trade
    trade = {
        "type":"COLORS",
        "target":target,          # 'R'|'B'
        "step":0,                 # G0
        "opened_at_len": len(history),
        "await_first_unique": True,
        "last_seen": history[-1] if len(history) else None,
        "reasons": reasons
    }
    _append_signal("COLORS", target, "open", gale_step=0, reasons=reasons)

def close_trade(result, came_n=None):
    """Fecha trade de cores e marca WIN/LOSS no histórico."""
    global trade
    if not trade: return
    g = trade["step"]
    # Atualiza primeiro sinal "open" para win/loss
    for s in signals:
        if s["status"]=="open" and s["mode"]=="COLORS":
            s["status"]=result
            s["gale"]=g
            s["came_n"]=came_n
            break
    trade = None

def process_new_number(n):
    """É chamado a cada ingest (apenas se bot_active=True)."""
    global trade
    if current_mode=="WHITE":
        # WHITE = one-shot, sem gale
        sig = white_signal(history)
        if sig["ok"]:
            _append_signal("WHITE", "W", "open", gale_step=None, reasons=sig["reasons"])
            # como é one-shot, consideramos 'resultado' no próximo número:
            # se vier 0 agora -> WIN; senão -> LOSS e pronto (sem gale)
            if n==0:
                signals[0]["status"]="win"
                signals[0]["came_n"]=0
            else:
                signals[0]["status"]="loss"
                signals[0]["came_n"]=n
        return

    # COLORS com gale
    if not trade:
        sigC = color_signal(history)
        if sigC["ok"]:
            open_trade_colors(sigC["target"], sigC["reasons"])
    else:
        # protege: ignora duplicata do número visto na abertura
        if trade.get("await_first_unique", False):
            if trade.get("last_seen", None) == n:
                return
            trade["await_first_unique"]=False
            trade["last_seen"]=None

        # valida alvo
        tgt = trade["target"]
        hit = (n!=0) and ((is_red(n) and tgt=='R') or (is_black(n) and tgt=='B'))
        if hit:
            close_trade("win", came_n=n)
        else:
            # errou -> sobe gale ou perde
            if trade["step"]>=MAX_GALES:
                close_trade("loss", came_n=n)
            else:
                trade["step"] += 1

# ================= HTML UI =================
HTML = """<!doctype html><meta charset="utf-8">
<title>SNIPER BLAZE PRO — WEB</title>
<style>
  :root{--bg:#0f0821;--card:#1a0f38;--muted:#c9c4de;--accent:#7b2cbf;--green:#28c08a;--red:#ff3b4d;--ink:#e8e6f3}
  body{margin:0;background:var(--bg);color:#fff;font:16px/1.4 system-ui,Segoe UI,Arial}
  header{padding:14px 18px;font-weight:700}
  .wrap{padding:10px 18px;display:grid;grid-template-columns:320px 1fr;gap:12px}
  .card{background:var(--card);border-radius:12px;padding:12px}
  .k{display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap}
  button{background:#24124a;border:0;color:#fff;padding:8px 12px;border-radius:10px;cursor:pointer}
  button.primary{background:var(--accent)}
  .chip{display:inline-block;margin-left:8px;padding:6px 10px;border-radius:999px;background:#261541;color:#ddd;font-size:12px}
  h3{margin:0 0 12px 0;font-size:16px}
  .bar{height:16px;background:#2b1e4d;border-radius:10px;position:relative}
  .bar>span{position:absolute;left:0;top:0;bottom:0;border-radius:10px}
  .lbl{display:flex;justify-content:space-between;margin:6px 0 10px 0;color:var(--muted);font-size:14px}
  .grid{display:grid;grid-template-columns:repeat(6,64px);gap:8px}
  .sq{width:60px;height:60px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-weight:700}
  .w{background:#fff;color:#111}.r{background:#ff2e2e}.b{background:#0e0e10;color:#fff}
  .row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
  table{width:100%;border-collapse:collapse;font-size:14px}
  td,th{padding:6px;border-bottom:1px solid #2b1e4d;color:#ddd}
  .ok{color:var(--green)} .bad{color:var(--red)}
</style>
<header>SNIPER BLAZE PRO — WEB</header>

<div class="wrap">
  <div class="card">
    <div class="k">
      <b>Modo:</b>
      <button id="btnW">⚪ BRANCO</button>
      <button id="btnC">🔴⚫ CORES</button>
      <span id="modeTag" class="chip">WHITE</span>
    </div>
    <div class="k">
      <b>Bot:</b>
      <button id="btnStart" class="primary">▶ Iniciar Bot</button>
      <button id="btnStop">■ Parar Bot</button>
      <span id="botTag" class="chip">OFF</span>
    </div>

    <h3 style="margin-top:8px">Chances da rodada</h3>
    <div class="lbl">⚪ Branco <span id="pw">0%</span></div>
    <div class="bar"><span id="bw" style="background:#fff;width:0%"></span></div>
    <div class="lbl">🔴 Vermelho <span id="pr">0%</span></div>
    <div class="bar"><span id="br" style="background:#ff2e2e;width:0%"></span></div>
    <div class="lbl">⚫ Preto <span id="pb">0%</span></div>
    <div class="bar"><span id="bb" style="background:#0e0e10;width:0%"></span></div>

    <h3 style="margin-top:14px">Cor + indicada</h3>
    <div id="pick" class="sq" style="width:200px;height:110px;border:2px solid #2b1e4d;display:flex;flex-direction:column;gap:4px"></div>
    <div id="why" style="color:#c9c4de;margin-top:8px;font-size:13px">Motivos: —</div>
  </div>

  <div class="card">
    <div class="row" style="justify-content:space-between">
      <h3>Últimos giros</h3>
      <div id="detail" style="color:#c9c4de;font-size:13px">—</div>
    </div>
    <div id="boxes" class="grid" style="margin-bottom:12px"></div>

    <h3>Histórico de Sinais</h3>
    <table id="sigTbl"><thead>
      <tr><th>Hora</th><th>Modo</th><th>Alvo</th><th>Status</th><th>Gale</th><th>Saiu</th></tr>
    </thead><tbody></tbody></table>
  </div>
</div>

<script>
async function post(url, body){return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});}
document.getElementById('btnW').onclick=()=>post('/mode',{mode:'WHITE'});
document.getElementById('btnC').onclick=()=>post('/mode',{mode:'COLORS'});
document.getElementById('btnStart').onclick=()=>post('/bot',{active:true});
document.getElementById('btnStop').onclick =()=>post('/bot',{active:false});

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
  const label = (tgt==='W'?'⚪ Branco':(tgt==='R'?'🔴 Vermelho':'⚫ Preto'));
  el.innerHTML = '<div>'+label+'</div><div style="font-size:28px;font-weight:800">'+Math.round(pct*100)+'%</div>';
}
function paintSignals(sigs){
  const tb=document.querySelector('#sigTbl tbody'); tb.innerHTML='';
  sigs.forEach(s=>{
    const tr=document.createElement('tr');
    const st = s.status==='win' ? '<span class="ok">WIN</span>' : (s.status==='loss' ? '<span class="bad">LOSS</span>' : 'open');
    tr.innerHTML = `<td>${s.ts}</td><td>${s.mode}</td><td>${s.target}</td><td>${st}</td><td>${s.gale ?? '-'}</td><td>${s.came_n ?? '-'}</td>`;
    tb.appendChild(tr);
  });
}

async function tick(){
  try{
    const r=await fetch('/state'); const s=await r.json();
    document.getElementById('modeTag').textContent=s.mode;
    document.getElementById('botTag').textContent=s.bot_active?'ON':'OFF';
    paintBoxes(s.last || []);
    document.getElementById('pw').textContent=(s.probs.W*100).toFixed(1)+'%';
    document.getElementById('pr').textContent=(s.probs.R*100).toFixed(1)+'%';
    document.getElementById('pb').textContent=(s.probs.B*100).toFixed(1)+'%';
    document.getElementById('bw').style.width=(s.probs.W*100)+'%';
    document.getElementById('br').style.width=(s.probs.R*100)+'%';
    document.getElementById('bb').style.width=(s.probs.B*100)+'%';
    if(s.pick && s.pick[0]) setPick(s.pick[0], s.pick[1]);
    document.getElementById('why').textContent = 'Motivos: ' + (s.reasons && s.reasons.length ? s.reasons.join(', ') : '—');
    document.getElementById('detail').textContent = s.detail || '—';
    paintSignals(s.signals || []);
  }catch(e){}
}
setInterval(tick, 900); tick();
</script>
"""

# ================= Endpoints =================
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

@app.post("/bot")
def toggle_bot():
    global bot_active, trade
    data = request.get_json(force=True, silent=True) or {}
    active = bool(data.get("active", False))
    bot_active = active
    if not bot_active:
        trade = None  # ao parar, encerra trade aberto
    return jsonify(ok=True, bot_active=bot_active)

@app.get("/state")
def state():
    lst = list(history)[-12:]
    probs = estimate_probs(history)
    reasons=[]; detail=''
    pick = probs["rec"]
    if current_mode == "WHITE":
        sw = white_signal(history)
        reasons = sw["reasons"]; detail = sw.get("detail","")
        if sw["ok"]:
            pick = ("W", probs["W"])
    else:
        sc = color_signal(history)
        reasons = sc.get("reasons",[])
        if sc.get("ok"):
            pick = (sc.get("target"), probs[sc.get("target")])
    return jsonify(
        last=lst, size=len(history),
        mode=current_mode, bot_active=bot_active,
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
            if bot_active:
                process_new_number(n)
    return jsonify(added=added, ok=True, time=datetime.now().isoformat())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
