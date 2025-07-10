from flask import Flask, render_template_string, jsonify
import requests, json
from icalendar import Calendar
from datetime import datetime, date, time, timedelta
import pytz

app = Flask(__name__)

# === 1) Load your calendars config ===
with open("calendars.json") as f:
    calendars = json.load(f)

# === 2) Fetch, normalize to Eastern, split multi-day, and aggregate by person/day ===
def load_events():
    eastern = pytz.timezone('US/Eastern')
    segments = []
    for cal in calendars:
        resp = requests.get(cal["url"])
        resp.raise_for_status()
        cal_obj = Calendar.from_ical(resp.text)
        for comp in cal_obj.walk():
            if comp.name != "VEVENT":
                continue
            start = comp.get('dtstart').dt
            end = comp.get('dtend').dt
            if isinstance(start, date) and not isinstance(start, datetime):
                start = datetime.combine(start, time(0,0))
            if isinstance(end, date) and not isinstance(end, datetime):
                end = datetime.combine(end, time(0,0))
            if start.tzinfo is None:
                start = eastern.localize(start)
            else:
                start = start.astimezone(eastern)
            if end.tzinfo is None:
                end = eastern.localize(end)
            else:
                end = end.astimezone(eastern)
            current = start
            while current.date() < end.date():
                day_end = eastern.localize(datetime.combine(current.date(), time(23,59,59)))
                segments.append({"person": cal["name"], "start": current.isoformat(), "end": day_end.isoformat(), "title": str(comp.get('summary'))})
                current = eastern.localize(datetime.combine(current.date() + timedelta(days=1), time(0,0)))
            if current < end:
                segments.append({"person": cal["name"], "start": current.isoformat(), "end": end.isoformat(), "title": str(comp.get('summary'))})
    agg = {}
    for seg in segments:
        dt = datetime.fromisoformat(seg["start"])
        if dt.tzinfo is None:
            dt = eastern.localize(dt)
        else:
            dt = dt.astimezone(eastern)
        day_str = dt.date().isoformat()
        key = (seg["person"], day_str)
        if key not in agg:
            agg[key] = {"person": seg["person"], "date": day_str, "segments": []}
        agg[key]["segments"].append({"start": seg["start"], "end": seg["end"], "title": seg.get("title", "")})
    return list(agg.values())

events = load_events()

@app.route("/events.json")
def get_events():
    return jsonify(events)

INDEX_HTML = '''
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Miami Baddies Work Shifts</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&family=Poppins:wght@700&display=swap" rel="stylesheet">
  <style>
    :root { --bg: #fff0f6; --fg: #333; --accent: #e91e63; --shift: #ff5722; --free: #4caf50; --header-bg: #ffffff; --row-alt: #ffeef8; --today-bg: rgba(233,30,99,0.1); --font-body: 'Inter',sans-serif; --font-heading: 'Poppins',sans-serif; --row-height: 32px; --font-small: 0.75em; }
    body { margin:0; background:var(--bg); color:var(--fg); font-family:var(--font-body); }
    header { position:sticky; top:0; background:var(--header-bg); padding:1rem; display:flex; align-items:center; box-shadow:0 2px 4px rgba(0,0,0,0.1); }
    header h1 { flex:none; font-family:var(--font-heading); color:var(--accent); }
    .person-selector, .week-selector { margin-left:1rem; }
    .person-selector label, .week-selector label { margin-right:0.5rem; font-weight:600; }
    .person-selector input { margin-right:0.25rem; }
    .label { width:150px; flex:none; margin-right:1rem; }
    #scrollArea { max-height:calc(100vh - 100px); overflow-y:auto; padding:0.5rem; }
    .day-header { font-family:var(--font-heading); color:var(--accent); margin:1rem 0 0.5rem; }
    .hour-row, .row { display:flex; align-items:center; height:var(--row-height); margin-bottom:0.5rem; }
    .hour-row .label { width:150px; }
    .hour-row .timeline, .row .timeline {
    position: relative;
    flex: 1;
    height: var(--row-height);
    background: #f3f3f3;
    border-radius: 4px;
    background-image: repeating-linear-gradient(
      to right,
      transparent,
      transparent calc(100%/24 - 1px),
      rgba(0, 0, 0, 0.1) calc(100%/24 - 1px),
      rgba(0, 0, 0, 0.1) calc(100%/24)
    );
}
    .hour-row .hour-label { position:absolute; top:-1.2em; font-size:var(--font-small); color:var(--accent); transform:translateX(-50%); }
    .row.today { background:var(--today-bg); }
    .shift { position:absolute; top:0; height:var(--row-height); background:var(--shift); border-radius:4px; display:flex; align-items:center; justify-content:center; font-size:var(--font-small); color:#fff; }
    .free-slot { position:absolute; top:0; height:var(--row-height); background:var(--free); opacity:0.4; border-radius:4px; display:flex; align-items:center; justify-content:center; font-size:var(--font-small); color:#fff; }
    @media (max-width:600px) { .person-selector, .week-selector { width:100%; } }
  </style>
</head>
<body>
  <header>
    <h1>Miami Baddies Work Shifts</h1>
    <div class="person-selector" id="personSelect"></div>
    <div class="week-selector"><label for="weekSelect">Week:</label><select id="weekSelect"></select></div>
    <div style="margin-left:2rem; font-size:0.9rem;"><p>🔎 Use the checkboxes to toggle team members’ shifts.<br>📆 Select the week from the dropdown.<br>📊 Scroll down to view schedules and highlighted free windows.</p></div>
  </header>
  <div id="scrollArea"><div id="contentArea"></div></div>
  <script src="https://unpkg.com/clusterize.js@0.18.1/clusterize.min.js"></script>
  <script>
    const calendars = {{ calendars|tojson }};
    const persons = calendars.map(c=>c.name);
    let events = [], weekGroups = {};
    const todayDateKey = new Date().toISOString().split('T')[0];

    function getMonday(d){ const dt=new Date(d),day=dt.getDay(); return new Date(dt.setDate(dt.getDate()-day+(day===0?-6:1))); }
    function formatKey(dt){ return `${dt.getFullYear()}-${String(dt.getMonth()+1).padStart(2,'0')}-${String(dt.getDate()).padStart(2,'0')}`; }
    function formatDate(d){ return d.toLocaleDateString(undefined,{month:'short',day:'numeric'}); }
    function formatTime(h){ const hr=Math.floor(h)%24,pm=hr>=12,hh=hr%12||12,mm=Math.round((h-Math.floor(h))*60); return `${hh}${mm?':'+String(mm).padStart(2,'0'):''}${pm?'pm':'am'}`; }
    function init(){
      const personDiv=document.getElementById('personSelect');
      persons.forEach(name=>{const lbl=document.createElement('label'),chk=document.createElement('input');chk.type='checkbox';chk.value=name;chk.checked=true;chk.onchange = () => renderWeek(document.getElementById('weekSelect').value);lbl.appendChild(chk);lbl.append(name);personDiv.appendChild(lbl);});
      fetch('/events.json').then(r=>r.json()).then(data=>{events=data.map(e=>({person:e.person,date:e.date,segments:e.segments.map(s=>({start:new Date(s.start),end:new Date(s.end),title:s.title}))}));events.forEach(e=>{const [y,mo,da]=e.date.split('-').map(Number),d=new Date(y,mo-1,da),wk=formatKey(getMonday(d));(weekGroups[wk]=weekGroups[wk]||[]).push(e);});const weekKeys=Object.keys(weekGroups).sort(),select=document.getElementById('weekSelect');weekKeys.forEach(key=>{const [y,mo,da]=key.split('-').map(Number),s=new Date(y,mo-1,da),opt=document.createElement('option');opt.value=key;opt.textContent=`${formatDate(s)} – ${formatDate(new Date(s.getTime()+6*86400000))}`;select.appendChild(opt);});const today = new Date();
        const todayWeekKey = formatKey(getMonday(today));
        select.value = weekKeys.includes(todayWeekKey) ? todayWeekKey : weekKeys[0];select.onchange=()=>renderWeek(select.value);window.clusterize=new Clusterize({scrollId:'scrollArea',contentId:'contentArea',rows:[]});renderWeek(select.value);});}
    function renderWeek(key){const checked=Array.from(document.querySelectorAll('#personSelect input:checked')).map(i=>i.value),rows=[],weekEvents=weekGroups[key]||[];for(let i=0;i<7;i++){const [y,mo,da]=key.split('-').map(Number),d=new Date(y,mo-1,da+i),dateKey=d.toISOString().split('T')[0],isToday=dateKey===todayDateKey;rows.push(`<div class="day-header${isToday?' today':''}">${d.toLocaleDateString(undefined,{weekday:'long',month:'short',day:'numeric'})}</div>`);
        // hour row with free slots
        let busy=[];checked.forEach(person=>{const rec=weekEvents.find(e=>e.person===person&&e.date===dateKey);if(rec)rec.segments.forEach(s=>{busy.push([s.start.getHours()+s.start.getMinutes()/60,s.end.getHours()+s.end.getMinutes()/60]);});});busy.sort((a,b)=>a[0]-b[0]);let merged=[];busy.forEach(iv=>{if(merged.length&&iv[0]<=merged[merged.length-1][1])merged[merged.length-1][1]=Math.max(merged[merged.length-1][1],iv[1]);else merged.push(iv);});let free=[];let le=0;merged.forEach(iv=>{if(iv[0]-le>=2)free.push([le,iv[0]]);le=iv[1];});if(24-le>=2)free.push([le,24]);
        let hrhtml=`<div class="hour-row${isToday?' today':''}"><div class="label"></div><div class="timeline">`+`<span class="hour-label" style="left:${6/24*100}%">6am</span>`+`<span class="hour-label" style="left:${12/24*100}%">12pm</span>`+`<span class="hour-label" style="left:${18/24*100}%">6pm</span>`;
        free.forEach(f=>{const left=(f[0]/24*100).toFixed(2)+'%',w=((f[1]-f[0])/24*100).toFixed(2)+'%',label=`${formatTime(f[0])} - ${formatTime(f[1])}`;hrhtml+=`<div class="free-slot" style="left:${left};width:${w}">${label}</div>`;});hrhtml+=`</div></div>`;rows.push(hrhtml);
        // person rows
        checked.forEach(person=>{const rec=weekEvents.find(e=>e.person===person&&e.date===dateKey);let html='<div class="timeline">';if(rec)rec.segments.forEach(s=>{const sh=s.start.getHours()+s.start.getMinutes()/60,eh=s.end.getHours()+s.end.getMinutes()/60,left=(sh/24*100).toFixed(2)+'%',w=((eh-sh)/24*100).toFixed(2)+'%';html+=`<div class="shift" style="left:${left};width:${w}">${s.title}</div>`;});html+='</div>';rows.push(`<div class="row${isToday?' today':''}"><div class="label">${person}</div>${html}</div>`);});}
      clusterize.update(rows);}document.addEventListener('DOMContentLoaded',init);
  </script>
</body>
</html>
'''

@app.route('/')
def index():
    return render_template_string(INDEX_HTML, calendars=calendars)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5002, debug=True)
