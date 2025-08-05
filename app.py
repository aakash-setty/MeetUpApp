from flask import Flask, render_template_string, jsonify, request
import requests, json
from icalendar import Calendar
from datetime import datetime, date, time, timedelta
import pytz
import os
import openai

# === Configuration ===
openai.api_key = os.getenv("OPENAI_API_KEY")
app = Flask(__name__)

# === 1) Load your calendars config ===
with open("calendars.json") as f:
    calendars = json.load(f)

# === 2) Fetch, normalize, split multi-day, aggregate by person/day ===
def load_events():
    eastern = pytz.timezone('US/Eastern')
    segments = []
    for cal in calendars:
        resp = requests.get(cal["url"])
        resp.raise_for_status()
        cal_obj = Calendar.from_ical(resp.text)
        for comp in cal_obj.walk():
            if comp.name != "VEVENT": continue
            start = comp.get('dtstart').dt
            end = comp.get('dtend').dt
            # Normalize date-only
            if isinstance(start, date) and not isinstance(start, datetime):
                start = datetime.combine(start, time(0,0))
            if isinstance(end, date) and not isinstance(end, datetime):
                end = datetime.combine(end, time(0,0))
            # Localize/convert to Eastern
            if start.tzinfo is None: start = eastern.localize(start)
            else: start = start.astimezone(eastern)
            if end.tzinfo is None: end = eastern.localize(end)
            else: end = end.astimezone(eastern)
            # Split multi-day
            current = start
            while current.date() < end.date():
                day_end = eastern.localize(datetime.combine(current.date(), time(23,59,59)))
                segments.append({"person":cal["name"],"start":current.isoformat(),"end":day_end.isoformat(),"title":str(comp.get('summary'))})
                current = eastern.localize(datetime.combine(current.date()+timedelta(days=1), time(0,0)))
            if current < end:
                segments.append({"person":cal["name"],"start":current.isoformat(),"end":end.isoformat(),"title":str(comp.get('summary'))})
    # Aggregate
    agg = {}
    for seg in segments:
        dt = datetime.fromisoformat(seg["start"])
        if dt.tzinfo is None: dt = eastern.localize(dt)
        else: dt = dt.astimezone(eastern)
        day = dt.date().isoformat()
        key = (seg["person"], day)
        if key not in agg:
            agg[key] = {"person":seg["person"],"date":day,"segments":[]}
        agg[key]["segments"].append({"start":seg["start"],"end":seg["end"],"title":seg.get("title","")})
    return list(agg.values())

# Preload events on startup
events = load_events()

@app.route("/events.json")
def get_events():
    return jsonify(events)

# === NLP & free-window computing ===
def format_time_py(h):
    hr = int(h) % 24
    pm = hr >= 12
    hh = hr % 12 or 12
    mm = int(round((h - int(h)) * 60))
    return f"{hh}{(':'+str(mm).zfill(2)) if mm else ''}{'pm' if pm else 'am'}"

def compute_free_windows(names, min_hours):
    eastern = pytz.timezone('US/Eastern')
    busy_by_date = {}
    for e in events:
        if e['person'] in names:
            for seg in e['segments']:
                st = datetime.fromisoformat(seg['start'])
                en = datetime.fromisoformat(seg['end'])
                st = st if st.tzinfo else eastern.localize(st)
                en = en if en.tzinfo else eastern.localize(en)
                sh = st.hour + st.minute/60
                eh = en.hour + en.minute/60
                busy_by_date.setdefault(e['date'], []).append((sh, eh))
    free_by_date = {}
    for day, intervals in busy_by_date.items():
        intervals.sort(key=lambda x: x[0])
        merged = []
        for iv in intervals:
            if merged and iv[0] <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], iv[1]))
            else:
                merged.append([iv[0], iv[1]])
        free = []
        le = 0
        for iv in merged:
            if iv[0] - le >= min_hours: free.append((le, iv[0]))
            le = iv[1]
        if 24 - le >= min_hours: free.append((le, 24))
        if free:
            free_by_date[day] = [f"{format_time_py(f[0])} - {format_time_py(f[1])}" for f in free]
    return free_by_date

@app.route('/parse-request', methods=['POST'])
def parse_request():
    data = request.get_json() or {}
    prompt = data.get('prompt','')
    functions = [{
        "name":"parse_availability_query",
        "description":"Extract list of names and minimum free duration",
        "parameters":{
            "type":"object",
            "properties":{
                "names":{"type":"array","items":{"type":"string"}},
                "min_free_hours":{"type":"number"}
            },
            "required":["names","min_free_hours"]
        }
    }]
    resp = openai.ChatCompletion.create(
        model="gpt-4-0613",
        messages=[{"role":"user","content":prompt}],
        functions=functions,
        function_call={"name":"parse_availability_query"}
    )
    msg = resp.choices[0].message
    args = json.loads(msg.function_call.arguments)
    names = args['names']
    min_h = args['min_free_hours']
    free = compute_free_windows(names, min_h)
    if not free:
        ans = f"No slots ≥ {min_h}h when {', '.join(names)} are all free."
    else:
        lines = [f"{day}: {', '.join(sl)}" for day, sl in free.items()]
        ans = f"Free slots ≥ {min_h}h for {', '.join(names)}:\n" + "\n".join(lines)
    return jsonify({"answer":ans})

# === 3) UI with controls, NLP input, select/deselect all ===
INDEX_HTML = '''
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Miami Baddies Work Shifts</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&family=Poppins:wght@700&display=swap" rel="stylesheet">
  <style>
    :root { --bg:#fff0f6;--fg:#333;--accent:#e91e63;--shift:#ff5722;--free:#4caf50;--header-bg:#fff;--row-alt:#ffeef8;--today-bg:rgba(233,30,99,0.1);--font-body:'Inter',sans-serif;--font-heading:'Poppins',sans-serif;--row-height:32px;--font-small:0.75em; }
    body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--font-body);}
    header{position:sticky;top:0;background:var(--header-bg);padding:1rem;display:flex;align-items:center;box-shadow:0 2px 4px rgba(0,0,0,0.1);}
    header h1{font-family:var(--font-heading);color:var(--accent);margin-right:2rem;}
    .person-controls, .nl-controls, .week-selector { margin-right:1rem; }
    .person-selector{display:flex;flex-wrap:wrap;align-items:center;}
    .person-selector label{margin-right:0.5rem;font-weight:600;white-space:nowrap;}
    .person-selector input{margin-right:0.25rem;}
    .person-controls button, #nlSubmit{font-size:0.9em;padding:0.25em 0.5em;border:none;background:var(--accent);color:#fff;border-radius:4px;cursor:pointer;margin-right:0.5rem;}
    .nl-controls input{padding:0.25em;margin-right:0.5rem;flex:1;}
    .week-selector label{margin-right:0.5rem;font-weight:600;}
    #scrollArea{max-height:calc(100vh-160px);overflow-y:auto;padding:0.5rem;}
    .day-header{font-family:var(--font-heading);color:var(--accent);margin:1rem 0 0.5rem;}
    .hour-row,.row{display:flex;align-items:center;height:var(--row-height);margin-bottom:0.5rem;}
    .hour-row .label{width:150px;}
    .hour-row .timeline,.row .timeline{position:relative;flex:1;height:var(--row-height);background:#f3f3f3;border-radius:4px;background-image:repeating-linear-gradient(to right,transparent,transparent calc(100%/24 - 1px),rgba(0,0,0,0.1) calc(100%/24 - 1px),rgba(0,0,0,0.1) calc(100%/24));}
    .hour-row .hour-label{position:absolute;top:-1.2em;font-size:var(--font-small);color:var(--accent);transform:translateX(-50%);}
    .row.today{background:var(--today-bg);}
    .shift{position:absolute;top:0;height:var(--row-height);background:var(--shift);border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:var(--font-small);color:#fff;}
    .free-slot{position:absolute;top:0;height:var(--row-height);background:var(--free);opacity:0.4;border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:var(--font-small);color:#fff;}
    @media(max-width:600px){.person-selector,.person-controls,.nl-controls,.week-selector{width:100%;}}
  </style>
</head>
<body>
  <header>
    <h1>Miami Baddies Work Shifts</h1>
    <div class="person-selector" id="personSelect"></div>
    <div class="person-controls">
      <button id="clearAll">Deselect All</button>
      <button id="selectAll">Select All</button>
    </div>
    <div class="nl-controls">
      <input type="text" id="nlInput" placeholder="e.g. When are Joe, Michka, Jacob free for 3 hours?" />
      <button id="nlSubmit">Go</button>
    </div>
    <div class="week-selector"><label for="weekSelect">Week:</label><select id="weekSelect"></select></div>
  </header>
  <div id="nlAnswer" style="padding:0.5rem 1rem;font-size:0.9rem;white-space:pre-line;"></div>
  <div id="scrollArea"><div id="contentArea"></div></div>
  <script src="https://unpkg.com/clusterize.js@0.18.1/clusterize.min.js"></script>
  <script>
    const calendars = {{ calendars|tojson }};
    const persons = calendars.map(c=>c.name);
    let events=[], weekGroups={};
    const todayKey = new Date().toISOString().split('T')[0];
    function getMonday(d){const dt=new Date(d),day=dt.getDay();return new Date(dt.setDate(dt.getDate()-day+(day===0?-6:1)));}
    function formatKey(dt){return`${dt.getFullYear()}-${String(dt.getMonth()+1).padStart(2,'0')}-${String(dt.getDate()).padStart(2,'0')}`;}
    function formatDate(d){return d.toLocaleDateString(undefined,{month:'short',day:'numeric'});}    
    function formatTime(h){const hr=Math.floor(h)%24,pm=hr>=12,hh=hr%12||12,mm=Math.round((h-Math.floor(h))*60);return`${hh}${mm?':'+String(mm).padStart(2,'0'):''}${pm?'pm':'am'}`;}
    function init(){
      const pDiv=document.getElementById('personSelect');
      persons.forEach(name=>{const lbl=document.createElement('label'),chk=document.createElement('input');chk.type='checkbox';chk.value=name;chk.checked=true;chk.onchange=()=>renderWeek(sel.value);lbl.appendChild(chk);lbl.append(name);pDiv.appendChild(lbl);});
      document.getElementById('clearAll').onclick=()=>{document.querySelectorAll('#personSelect input').forEach(chk=>chk.checked=false);renderWeek(document.getElementById('weekSelect').value);};
      document.getElementById('selectAll').onclick=()=>{document.querySelectorAll('#personSelect input').forEach(chk=>chk.checked=true);renderWeek(document.getElementById('weekSelect').value);};
      document.getElementById('nlSubmit').onclick=()=>{
        const prompt=document.getElementById('nlInput').value;
        fetch('/parse-request',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt})})
          .then(r=>r.json()).then(json=>{document.getElementById('nlAnswer').textContent=json.answer;});
      };
      fetch('/events.json').then(r=>r.json()).then(data=>{
        events=data.map(e=>({person:e.person,date:e.date,segments:e.segments.map(s=>({start:new Date(s.start),end:new Date(s.end),title:s.title}))}));
        events.forEach(e=>{const dParts=e.date.split('-').map(Number),wk=formatKey(getMonday(new Date(dParts[0],dParts[1]-1,dParts[2])));(weekGroups[wk]=weekGroups[wk]||[]).push(e);});
        const weekKeys=Object.keys(weekGroups).sort(),sel=document.getElementById('weekSelect');
        weekKeys.forEach(k=>{const p=k.split('-').map(Number),s=new Date(p[0],p[1]-1,p[2]),opt=document.createElement('option');opt.value=k;opt.textContent=`${formatDate(s)} – ${formatDate(new Date(s.getTime()+6*86400000))}`;sel.appendChild(opt);});
        const today=new Date(),tKey=formatKey(getMonday(today));sel.value=weekKeys.includes(tKey)?tKey:weekKeys[0];sel.onchange=()=>renderWeek(sel.value);
        window.clusterize=new Clusterize({scrollId:'scrollArea',contentId:'contentArea',rows:[]});renderWeek(sel.value);
      });
    }
    function renderWeek(key){const sel=document.getElementById('weekSelect');const checked=Array.from(document.querySelectorAll('#personSelect input:checked')).map(i=>i.value),rows=[],we=weekGroups[key]||[];
      for(let i=0;i<7;i++){const d0=key.split('-').map(Number),d=new Date(d0[0],d0[1]-1,d0[2]+i),dKey=d.toISOString().split('T')[0],isT=dKey===todayKey;rows.push(`<div class="day-header${isT?' today':''}">${d.toLocaleDateString(undefined,{weekday:'long',month:'short',day:'numeric'})}</div>`);
        let busy=[];checked.forEach(p=>{const r=we.find(e=>e.person===p&&e.date===dKey);if(r)r.segments.forEach(s=>busy.push([s.start.getHours()+s.start.getMinutes()/60,s.end.getHours()+s.end.getMinutes()/60]));});busy.sort((a,b)=>a[0]-b[0]);const m=[];busy.forEach(iv=>{if(m.length&&iv[0]<=m[m.length-1][1])m[m.length-1][1]=Math.max(m[m.length-1][1],iv[1]);else m.push(iv);} );let free=[],le=0;m.forEach(iv=>{if(iv[0]-le>=2)free.push([le,iv[0]]);le=iv[1];});if(24-le>=2)free.push([le,24]);
        let hr=`<div class="hour-row${isT?' today':''}"><div class="label"></div><div class="timeline">`+['6','12','18'].map(h=>`<span class="hour-label" style="left:${h/24*100}%">${h%12||12}${h<12?'am':'pm'}</span>`).join('');free.forEach(f=>{const l=(f[0]/24*100).toFixed(2)+'%',w=((f[1]-f[0])/24*100).toFixed(2)+'%',lbl=`${formatTime(f[0])} - ${formatTime(f[1])}`;hr+=`<div class="free-slot" style="left:${l};width:${w}">${lbl}</div>`;});hr+=`</div></div>`;rows.push(hr);
        checked.forEach(p=>{const r=we.find(e=>e.person===p&&e.date===dKey);let html='<div class="timeline">';if(r)r.segments.forEach(s=>{const sh=s.start.getHours()+s.start.getMinutes()/60,eh=s.end.getHours()+s.end.getMinutes()/60,l=(sh/24*100).toFixed(2)+'%',w=((eh-sh)/24*100).toFixed(2)+'%';html+=`<div class="shift" style="left:${l};width:${w}">${s.title}</div>`;});html+='</div>';rows.push(`<div class="row${isT?' today':''}"><div class="label">${p}</div>${html}</div>`);});}
      clusterize.update(rows);
    }
    document.addEventListener('DOMContentLoaded', init);
  </script>
</body>
</html>
'''

@app.route('/')
def index():
    return render_template_string(INDEX_HTML, calendars=calendars)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
