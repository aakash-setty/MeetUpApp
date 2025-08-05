from flask import Flask, render_template_string, jsonify, request
import requests, json
from icalendar import Calendar
from datetime import datetime, date, time, timedelta
import pytz
import os

# Attempt to import OpenAI SDK
try:
    import openai
except ImportError:
    openai = None  # OpenAI SDK not installed

# === Configuration ===
if openai is not None:
    openai.api_key = os.getenv("OPENAI_API_KEY")

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
            # Normalize date-only to datetime
            if isinstance(start, date) and not isinstance(start, datetime):
                start = datetime.combine(start, time(0,0))
            if isinstance(end, date) and not isinstance(end, datetime):
                end = datetime.combine(end, time(0,0))
            # Convert to Eastern
            if start.tzinfo is None:
                start = eastern.localize(start)
            else:
                start = start.astimezone(eastern)
            if end.tzinfo is None:
                end = eastern.localize(end)
            else:
                end = end.astimezone(eastern)
            # Split multi-day events
            current = start
            while current.date() < end.date():
                day_end = eastern.localize(datetime.combine(current.date(), time(23,59,59)))
                segments.append({
                    "person": cal["name"],
                    "start": current.isoformat(),
                    "end":   day_end.isoformat(),
                    "title": str(comp.get('summary'))
                })
                current = eastern.localize(datetime.combine(current.date() + timedelta(days=1), time(0,0)))
            if current < end:
                segments.append({
                    "person": cal["name"],
                    "start": current.isoformat(),
                    "end":   end.isoformat(),
                    "title": str(comp.get('summary'))
                })
    # Aggregate by person & date
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
        agg[key]["segments"].append({
            "start": seg["start"],
            "end":   seg["end"],
            "title": seg.get("title", "")
        })
    return list(agg.values())

# Preload events on startup
events = load_events()

@app.route("/events.json")
def get_events():
    return jsonify(events)

# === NLP parsing & free-window computation ===
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
                busy_by_date.setdefault(e['date'], []).append((st.hour + st.minute/60, en.hour + en.minute/60))
    free_by_date = {}
    for day, intervals in busy_by_date.items():
        intervals.sort(key=lambda x: x[0])
        merged = []
        for iv in intervals:
            if merged and iv[0] <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], iv[1])
            else:
                merged.append([iv[0], iv[1]])
        free_slots = []
        le = 0
        for iv in merged:
            if iv[0] - le >= min_hours:
                free_slots.append((le, iv[0]))
            le = iv[1]
        if 24 - le >= min_hours:
            free_slots.append((le, 24))
        if free_slots:
            free_by_date[day] = [f"{format_time_py(f[0])} - {format_time_py(f[1])}" for f in free_slots]
    return free_by_date

@app.route('/parse-request', methods=['POST'])
def parse_request():
    if openai is None:
        return jsonify({"answer":"Error: OpenAI SDK not installed. Install via 'pip install openai'."}), 500
    data = request.get_json() or {}
    prompt = data.get('prompt', '')
    functions = [{
        "name": "parse_availability_query",
        "description": "Extract list of names and minimum free duration",
        "parameters": {
            "type": "object",
            "properties": {
                "names": {"type": "array", "items": {"type": "string"}},
                "min_free_hours": {"type": "number"}
            },
            "required": ["names", "min_free_hours"]
        }
    }]
    resp = openai.ChatCompletion.create(
        model="gpt-4-0613",
        messages=[{"role": "user", "content": prompt}],
        functions=functions,
        function_call={"name": "parse_availability_query"}
    )
    msg = resp.choices[0].message
    args = json.loads(msg.function_call.arguments)
    names = args['names']
    min_h = args['min_free_hours']
    free = compute_free_windows(names, min_h)
    if not free:
        answer = f"No slots ≥ {min_h}h when {', '.join(names)} are all free."
    else:
        lines = [f"{day}: {', '.join(sl)}" for day, sl in free.items()]
        answer = f"Free slots ≥ {min_h}h for {', '.join(names)}:\n" + "\n".join(lines)
    return jsonify({"answer": answer})

# === 3) UI with controls, NLP input, select/deselect all ===
INDEX_HTML = '''
<!doctype html>
<html><head><meta charset="utf-8"><title>Miami Baddies Work Shifts</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&family=Poppins:wght@700&display=swap" rel="stylesheet">
  <style>/* ...styles same as before... */</style></head>
<body>
  <header>
    <h1>Miami Baddies Work Shifts</h1>
    <div class="person-selector" id="personSelect"></div>
    <div class="person-controls">
      <button id="clearAll">Deselect All</button>
      <button id="selectAll">Select All</button>
    </div>
    <div class="nl-controls">
      <input type="text" id="nlInput" placeholder="When are Joe, Michka, Jacob free for 3 hours?" />
      <button id="nlSubmit">Go</button>
    </div>
    <div class="week-selector"><label for="weekSelect">Week:</label><select id="weekSelect"></select></div>
  </header>
  <div id="nlAnswer" style="padding:0.5rem 1rem;font-size:0.9rem;white-space:pre-line;"></div>
  <div id="scrollArea"><div id="contentArea"></div></div>
  <script src="https://unpkg.com/clusterize.js@0.18.1/clusterize.min.js"></script>
  <script>/* ...JS same as before... */</script>
</body></html>
'''

@app.route('/')
def index():
    return render_template_string(INDEX_HTML, calendars=calendars)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
