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
  <script src="https://unpkg.com/clusterize.js@0.18.1/clusterize.min.js"></script>
  <script>
    const calendars = {{ calendars|tojson }};
    const persons = calendars.map(c => c.name);
    let events = [], weekGroups = {};
    const todayDateKey = new Date().toISOString().split('T')[0];

    function getMonday(d) {
      const dt = new Date(d);
      const day = dt.getDay();
      return new Date(dt.setDate(dt.getDate() - day + (day === 0 ? -6 : 1)));
    }

    function formatKey(dt) {
      return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')}`;
    }

    function formatDate(d) {
      return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    }

    function formatTime(h) {
      const hr = Math.floor(h) % 24;
      const pm = hr >= 12;
      const hh = hr % 12 || 12;
      const mm = Math.round((h - Math.floor(h)) * 60);
      return `${hh}${mm ? ':' + String(mm).padStart(2, '0') : ''}${pm ? 'pm' : 'am'}`;
    }

    function init() {
      // Person checkboxes
      const personDiv = document.getElementById('personSelect');
      persons.forEach(name => {
        const lbl = document.createElement('label');
        const chk = document.createElement('input');
        chk.type = 'checkbox'; chk.value = name; chk.checked = true;
        chk.onchange = () => renderWeek(document.getElementById('weekSelect').value);
        lbl.appendChild(chk);
        lbl.appendChild(document.createTextNode(name));
        personDiv.appendChild(lbl);
      });

      // Select/Deselect all
      document.getElementById('clearAll').onclick = () => {
        document.querySelectorAll('#personSelect input').forEach(chk => chk.checked = false);
        renderWeek(document.getElementById('weekSelect').value);
      };
      document.getElementById('selectAll').onclick = () => {
        document.querySelectorAll('#personSelect input').forEach(chk => chk.checked = true);
        renderWeek(document.getElementById('weekSelect').value);
      };

      // NLP submit
      document.getElementById('nlSubmit').onclick = () => {
        const prompt = document.getElementById('nlInput').value;
        if (!prompt) return;
        fetch('/parse-request', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ prompt })
        })
        .then(r => r.json())
        .then(json => {
          document.getElementById('nlAnswer').textContent = json.answer;
        });
      };

      // Load events and prepare weeks
      fetch('/events.json')
        .then(r => r.json())
        .then(data => {
          events = data.map(e => ({
            person: e.person,
            date: e.date,
            segments: e.segments.map(s => ({
              start: new Date(s.start),
              end: new Date(s.end),
              title: s.title
            }))
          }));

          events.forEach(e => {
            const [y, mo, da] = e.date.split('-').map(Number);
            const wkKey = formatKey(getMonday(new Date(y, mo - 1, da)));
            (weekGroups[wkKey] = weekGroups[wkKey] || []).push(e);
          });

          const weekKeys = Object.keys(weekGroups).sort();
          const select = document.getElementById('weekSelect');
          weekKeys.forEach(key => {
            const [y, mo, da] = key.split('-').map(Number);
            const start = new Date(y, mo - 1, da);
            const opt = document.createElement('option');
            opt.value = key;
            opt.textContent = `${formatDate(start)} – ${formatDate(new Date(start.getTime() + 6 * 86400000))}`;
            select.appendChild(opt);
          });

          const today = new Date();
          const todayWeekKey = formatKey(getMonday(today));
          select.value = weekKeys.includes(todayWeekKey) ? todayWeekKey : weekKeys[0];
          select.onchange = () => renderWeek(select.value);

          window.clusterize = new Clusterize({
            scrollId: 'scrollArea',
            contentId: 'contentArea',
            rows: []
          });

          renderWeek(select.value);
        });
    }

    function renderWeek(key) {
      const checked = Array.from(document.querySelectorAll('#personSelect input:checked')).map(i => i.value);
      const rows = [];
      const weekEvents = weekGroups[key] || [];

      for (let i = 0; i < 7; i++) {
        const [y, mo, da] = key.split('-').map(Number);
        const d = new Date(y, mo - 1, da + i);
        const dateKey = d.toISOString().split('T')[0];
        const isToday = dateKey === todayDateKey;

        // Day header
        rows.push(
          `<div class="day-header${isToday ? ' today' : ''}">${d.toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' })}</div>`
        );

        // Compute busy/free
        let busy = [];
        checked.forEach(person => {
          const rec = weekEvents.find(e => e.person === person && e.date === dateKey);
          if (rec) rec.segments.forEach(s => {
            busy.push([s.start.getHours() + s.start.getMinutes() / 60, s.end.getHours() + s.end.getMinutes() / 60]);
          });
        });
        busy.sort((a, b) => a[0] - b[0]);
        const merged = [];
        busy.forEach(iv => {
          if (merged.length && iv[0] <= merged[merged.length - 1][1]) {
            merged[merged.length - 1][1] = Math.max(merged[merged.length - 1][1], iv[1]);
          } else {
            merged.push([iv[0], iv[1]]);
          }
        });
        const free = [];
        let le = 0;
        merged.forEach(iv => {
          if (iv[0] - le >= 2) free.push([le, iv[0]]);
          le = iv[1];
        });
        if (24 - le >= 2) free.push([le, 24]);

        // Hour row with free slots and markers
        let hr = `<div class="hour-row${isToday ? ' today' : ''}"><div class="label"></div><div class="timeline">`;
        [6, 12, 18].forEach(h => {
          hr += `<span class="hour-label" style="left:${(h/24*100).toFixed(2)}%">${h % 12 || 12}${h < 12 ? 'am' : 'pm'}</span>`;
        });
        free.forEach(f => {
          const left = (f[0] / 24 * 100).toFixed(2) + '%';
          const width = ((f[1] - f[0]) / 24 * 100).toFixed(2) + '%';
          const label = `${formatTime(f[0])} - ${formatTime(f[1])}`;
          hr += `<div class="free-slot" style="left:${left};width:${width}">${label}</div>`;
        });
        hr += `</div></div>`;
        rows.push(hr);

        // Person rows
        checked.forEach(person => {
          const rec = weekEvents.find(e => e.person === person && e.date === dateKey);
          let html = '<div class="timeline">';
          if (rec) rec.segments.forEach(s => {
            const sh = s.start.getHours() + s.start.getMinutes() / 60;
            const eh = s.end.getHours() + s.end.getMinutes() / 60;
            const left = (sh / 24 * 100).toFixed(2) + '%';
            const width = ((eh - sh) / 24 * 100).toFixed(2) + '%';
            html += `<div class="shift" style="left:${left};width:${width}">${s.title}</div>`;
          });
          html += '</div>';
          rows.push(
            `<div class="row${isToday ? ' today' : ''}"><div class="label">${person}</div>${html}</div>`
          );
        });
      }

      window.clusterize.update(rows);
    }

    document.addEventListener('DOMContentLoaded', init);
  </script>
</body></html>
'''

@app.route('/')
def index():
    return render_template_string(INDEX_HTML, calendars=calendars)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
