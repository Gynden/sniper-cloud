import os, json
from datetime import datetime
from collections import deque
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

history = deque(maxlen=2000)

HTML = """<!doctype html><meta charset=utf-8>
<title>SNIPER BLAZE PRO — Web</title>
<style>body{font-family:Arial;background:#120a28;color:#fff;padding:20px} .box{display:inline-block;width:60px;height:60px;margin:5px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-weight:bold;font-size:20px}</style>
<h2>SNIPER BLAZE PRO — WEB</h2>
<div id='boxes'></div>
<script>
async function tick(){
  try{
    const r=await fetch('/state'); const s=await r.json();
    const boxes=document.getElementById('boxes'); boxes.innerHTML='';
    (s.last||[]).forEach(n=>{
      const d=document.createElement('div');
      d.className='box';
      d.textContent=n;
      if(n===0){d.style.background='#fff';d.style.color='#000'}
      else if(n<=7){d.style.background='#ff2e2e'}
      else {d.style.background='#0e0e10'}
      boxes.appendChild(d);
    });
  }catch(e){}
}
setInterval(tick,1000); tick();
</script>"""

@app.get("/")
def index():
    return render_template_string(HTML)

@app.get("/state")
def state():
    return jsonify({"last": list(history)[-5:], "size": len(history)})

@app.post("/ingest")
def ingest():
    data = request.get_json(force=True)
    hist = data.get("history", [])
    for n in hist:
        if isinstance(n,int) and 0 <= n <= 14:
            history.append(n)
    return jsonify(ok=True, added=len(hist), time=datetime.now().isoformat())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
