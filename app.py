import os, re, json, hashlib, sqlite3, time, hmac
from datetime import date, timedelta, datetime
from functools import wraps

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, Response
from werkzeug.security import generate_password_hash, check_password_hash
from youtube_transcript_api import YouTubeTranscriptApi
from google import genai
from google.genai import types
import requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY")
CODE_SECRET  = os.getenv("CODE_SECRET")

COW_ACCOUNTS = "https://theorangecow.org"
COW_CLIENT_ID = "brain-wave"
COW_CLIENT_SECRET = "dev-secret-brainwave" #os.getenv("COW_CLIENT_SECRET")
COW_REDIRECT_URI = "https://brainwave.theorangecow.org/cow/callback"

DB_PATH = os.path.join(os.path.dirname(__file__), "brainwave.db")
MODEL = "gemini-2.5-flash"


# database
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_join_code():
    hour_bucket = int(time.time()) // 3600
    raw = hmac.new(CODE_SECRET.encode(), str(hour_bucket).encode(), hashlib.sha256).hexdigest()
    return raw[:6].upper()

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            api_key TEXT DEFAULT '',
            streak INTEGER DEFAULT 0,
            last_active TEXT DEFAULT '',
            timetable_json TEXT DEFAULT '',
            timetable_updated TEXT DEFAULT ''
        )
    """)
    conn.commit()

    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "timetable_json" not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN timetable_json TEXT DEFAULT ''")
    if "timetable_updated" not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN timetable_updated TEXT DEFAULT ''")
    conn.commit()
    conn.close()


init_db()


# auth
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def current_user():
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    conn.close()
    return user


def update_streak(user):
    today = date.today().isoformat()
    last = user["last_active"]
    streak = user["streak"]

    if last == today:
        pass
    elif last == (date.today() - timedelta(days=1)).isoformat():
        streak += 1
    else:
        streak = 1

    conn = get_db()
    conn.execute("UPDATE users SET streak = ?, last_active = ? WHERE id = ?",
                 (streak, today, user["id"]))
    conn.commit()
    conn.close()
    return streak


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        joincode = request.form.get("joincode", "")
        api_key = request.form.get("api_key", "").strip()

        if not username or not password:
            return render_template("signup.html", error="Username and password are required.")

        if joincode != get_join_code():
            return render_template("signup.html", error="Invalid join code.")

        conn = get_db()
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            conn.close()
            return render_template("signup.html", error="That username is already taken.")

        conn.execute(
            "INSERT INTO users (username, password_hash, api_key) VALUES (?, ?, ?)",
            (username, generate_password_hash(password), api_key),
        )
        conn.commit()
        user_id = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]
        conn.close()

        session["user_id"] = user_id
        return redirect(url_for("home"))

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            return redirect(url_for("home"))
        return render_template("login.html", error="Invalid username or password.")

    return render_template("login.html")

@app.route("/cow/login")
def cow_login():
    return redirect(
        f"{COW_ACCOUNTS}/sso/authorize"
        f"?client_id={COW_CLIENT_ID}"
        f"&redirect_uri={COW_REDIRECT_URI}"
    )
@app.route("/cow/callback")
def cow_callback():
    token = request.args.get("token")

    if not token:
        return redirect(url_for("login"))

    try:
        resp = requests.post(
            f"{COW_ACCOUNTS}/sso/verify",
            json={
                "client_id": COW_CLIENT_ID,
                "client_secret": COW_CLIENT_SECRET,
                "token": token,
            },
            timeout=10,
        )

        result = resp.json()

    except Exception:
        return render_template(
            "login.html",
            error="Could not connect to Cow Accounts."
        )

    if resp.status_code != 200 or not result.get("ok"):
        return render_template(
            "login.html",
            error="Cow sign-in failed."
        )

    username = result["username"]

    conn = get_db()

    user = conn.execute(
        "SELECT * FROM users WHERE username=?",
        (username,)
    ).fetchone()

    if not user:
        conn.execute(
            """
            INSERT INTO users
            (username, password_hash)
            VALUES (?, ?)
            """,
            (username, "")
        )

        conn.commit()

        user = conn.execute(
            "SELECT * FROM users WHERE username=?",
            (username,)
        ).fetchone()

    session["user_id"] = user["id"]

    conn.close()

    return redirect(url_for("home"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# pages
@app.route("/")
@login_required
def home():
    user = current_user()
    streak = update_streak(user)

    upcoming_days = []
    next_exam = None
    timetable_updated = user["timetable_updated"]

    if user["timetable_json"]:
        try:
            tt = json.loads(user["timetable_json"])
            today_iso = date.today().isoformat()

            upcoming_days = [d for d in tt.get("days", []) if d["date"] >= today_iso][:3]

            future_exams = [e for e in tt.get("exams", []) if e["date"] >= today_iso]
            future_exams.sort(key=lambda e: e["date"])
            if future_exams:
                next_exam = future_exams[0]
                next_exam = dict(next_exam)
                days_left = (date.fromisoformat(next_exam["date"]) - date.today()).days
                next_exam["days_left"] = days_left
        except Exception:
            upcoming_days = []
            next_exam = None

    return render_template(
        "home.html",
        username=user["username"],
        streak=streak,
        has_key=bool(user["api_key"]),
        has_timetable=bool(user["timetable_json"]),
        upcoming_days=upcoming_days,
        next_exam=next_exam,
        timetable_updated=timetable_updated,
        joincode=get_join_code(),
    )


@app.route("/video")
@login_required
def video_page():
    user = current_user()
    return render_template("video.html", username=user["username"], has_key=bool(user["api_key"]))


@app.route("/exams")
@login_required
def exams_page():
    user = current_user()
    return render_template("exams.html", username=user["username"], has_key=bool(user["api_key"]))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    user = current_user()
    message = None
    if request.method == "POST":
        api_key = request.form.get("api_key", "").strip()
        conn = get_db()
        conn.execute("UPDATE users SET api_key = ? WHERE id = ?", (api_key, user["id"]))
        conn.commit()
        conn.close()
        message = "API key saved."
        user = current_user()
    return render_template("settings.html", username=user["username"], api_key=user["api_key"], message=message)


# ai logic
def extract_video_id(url):
    patterns = [r"(?:v=|\/)([0-9A-Za-z_-]{11}).*", r"youtu\.be\/([0-9A-Za-z_-]{11})"]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def get_transcript_text(video_id):
    transcript = YouTubeTranscriptApi().fetch(video_id)
    return " ".join(snippet.text for snippet in transcript)


COMBINED_PROMPT = """You are helping a student study a YouTube video from its transcript.
Using the transcript below, produce ALL of the following in a SINGLE valid JSON object
(no markdown fences, no preamble, JSON only):

{{
  "summary": "a clear, well-organized summary using short paragraphs",
  "quiz": [
    {{"question": "...", "options": ["A","B","C","D"], "correct_index": 0, "explanation": "..."}}
  ],
  "flashcards": [
    {{"front": "term or question", "back": "definition or answer"}}
  ],
  "mindmap": {{
    "topic": "central topic",
    "branches": [
      {{"label": "branch name", "points": ["sub point", "sub point"]}}
    ]
  }},
  "exam_questions": [
    {{"question": "a longer, exam-style open-ended question", "model_answer": "a strong sample answer"}}
  ]
}}

Make 5 quiz questions, 8 flashcards, 4-6 mindmap branches with 2-4 points each, and 4 exam questions.

Transcript:
{transcript}
"""


def generate_all(api_key, transcript_text):
    client = genai.Client(api_key=api_key)
    prompt = COMBINED_PROMPT.format(transcript=transcript_text[:18000])
    resp = client.models.generate_content(model=MODEL, contents=prompt)
    raw = resp.text.strip()
    raw = re.sub(r"^```json|```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)


EXAM_EXTRACT_PROMPT = """This image shows an exam timetable. Extract every exam you can find (only the onces that are highlighted).
Respond ONLY with valid JSON, no markdown fences, no preamble, in this exact format:

{
  "exams": [
    {"subject": "Subject name", "date": "YYYY-MM-DD", "time": "e.g. 9:00am (or empty string if not visible)"}
  ]
}

If a date's year isn't shown, assume the current year. If you genuinely cannot read a field, use an empty string."""


def extract_exams_from_image(api_key, image_bytes, mime_type):
    client = genai.Client(api_key=api_key)
    image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
    resp = client.models.generate_content(
        model=MODEL,
        contents=[EXAM_EXTRACT_PROMPT, image_part],
    )
    raw = resp.text.strip()
    raw = re.sub(r"^```json|```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)


@app.route("/api/extract-exams", methods=["POST"])
@login_required
def extract_exams():
    user = current_user()
    if not user["api_key"]:
        return jsonify({"error": "Add your Gemini API key in Settings first."}), 400

    if "image" not in request.files:
        return jsonify({"error": "No image uploaded."}), 400

    file = request.files["image"]
    image_bytes = file.read()
    mime_type = file.mimetype or "image/png"

    try:
        result = extract_exams_from_image(user["api_key"], image_bytes, mime_type)
    except Exception as e:
        return jsonify({"error": f"Could not read the timetable image: {e}"}), 500

    return jsonify(result)


def compute_session_times(start_time_str, session_minutes, break_minutes, count):
    try:
        h, m = map(int, start_time_str.split(":"))
    except Exception:
        h, m = 16, 0

    cur = datetime(2000, 1, 1, h, m)
    times = []
    for _ in range(max(count, 0)):
        end = cur + timedelta(minutes=session_minutes)
        times.append((cur.strftime("%H:%M"), end.strftime("%H:%M")))
        cur = end + timedelta(minutes=break_minutes)
    return times


def build_revision_timetable(exams, comfort, sessions_per_day=2, start_time="16:00",
                              session_minutes=45, break_minutes=15):
    from datetime import datetime as dt

    DATE_FORMATS = ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"]

    def parse_date(s):
        s = (s or "").strip()
        for fmt in DATE_FORMATS:
            try:
                return dt.strptime(s, fmt).date()
            except Exception:
                continue
        return None

    today = date.today()
    parsed = []
    for e in exams:
        d = parse_date(e.get("date", ""))
        if d is None:
            continue
        parsed.append({"subject": e["subject"], "date": d, "time": e.get("time", "")})

    if not parsed:
        return {"days": [], "exams": []}

    parsed.sort(key=lambda x: x["date"])
    last_day = max(p["date"] for p in parsed)
    start_day = today if today < last_day else last_day - timedelta(days=1)
    if start_day < today:
        start_day = today

    weight = {}
    for p in parsed:
        c = comfort.get(p["subject"], 3)
        c = max(1, min(5, int(c)))
        weight[p["subject"]] = 6 - c

    allocated = {s: 0 for s in weight}
    total_days = (last_day - start_day).days
    if total_days < 1:
        total_days = 1

    days_out = []
    cur = start_day
    while cur <= last_day:
        available = [p["subject"] for p in parsed if p["date"] >= cur]
        slots = []
        if available:
            chosen_today = set()
            for _ in range(sessions_per_day):
                avail_now = [s for s in available if s not in chosen_today]
                if not avail_now:
                    avail_now = available
                best = min(avail_now, key=lambda s: allocated[s] / weight[s])
                slots.append(best)
                allocated[best] += 1
                chosen_today.add(best)

        session_times = compute_session_times(start_time, session_minutes, break_minutes, len(slots))
        sessions_detailed = [
            {"subject": subj, "start": st, "end": en}
            for subj, (st, en) in zip(slots, session_times)
        ]

        exam_today = [p["subject"] for p in parsed if p["date"] == cur]
        days_out.append({
            "date": cur.isoformat(),
            "weekday": cur.strftime("%A"),
            "sessions": sessions_detailed,
            "exam_today": exam_today,
        })
        cur += timedelta(days=1)

    return {
        "days": days_out,
        "exams": [{"subject": p["subject"], "date": p["date"].isoformat(), "time": p["time"]} for p in parsed],
    }


@app.route("/api/generate-timetable", methods=["POST"])
@login_required
def generate_timetable():
    user = current_user()
    data = request.get_json() or {}
    exams = data.get("exams", [])
    comfort = data.get("comfort", {})

    if not exams:
        return jsonify({"error": "No exams provided."}), 400

    start_time = data.get("start_time") or "16:00"
    try:
        session_minutes = int(data.get("session_minutes", 45))
    except Exception:
        session_minutes = 45
    try:
        break_minutes = int(data.get("break_minutes", 15))
    except Exception:
        break_minutes = 15
    try:
        sessions_per_day = int(data.get("sessions_per_day", 2))
    except Exception:
        sessions_per_day = 2
    sessions_per_day = max(1, min(sessions_per_day, 6))

    result = build_revision_timetable(
        exams, comfort,
        sessions_per_day=sessions_per_day,
        start_time=start_time,
        session_minutes=session_minutes,
        break_minutes=break_minutes,
    )
    if not result["days"]:
        return jsonify({"error": "Could not understand the exam dates. Please check they're filled in correctly and try again."}), 400

    result["settings"] = {
        "start_time": start_time,
        "session_minutes": session_minutes,
        "break_minutes": break_minutes,
        "sessions_per_day": sessions_per_day,
    }

    conn = get_db()
    conn.execute(
        "UPDATE users SET timetable_json = ?, timetable_updated = ? WHERE id = ?",
        (json.dumps(result), date.today().isoformat(), user["id"]),
    )
    conn.commit()
    conn.close()

    return jsonify(result)


@app.route("/api/my-timetable")
@login_required
def my_timetable():
    user = current_user()
    if not user["timetable_json"]:
        return jsonify({"timetable": None})
    try:
        data = json.loads(user["timetable_json"])
    except Exception:
        return jsonify({"timetable": None})
    return jsonify({"timetable": data, "updated": user["timetable_updated"]})


def build_ics(timetable):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Brain Wave//Revision Timetable//EN",
        "CALSCALE:GREGORIAN",
    ]
    uid_counter = 0

    def esc(s):
        return str(s).replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")

    def add_event(uid, dt_start, dt_end, summary, all_day=False):
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{uid}@brainwave")
        lines.append(f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}")
        if all_day:
            lines.append(f"DTSTART;VALUE=DATE:{dt_start}")
            lines.append(f"DTEND;VALUE=DATE:{dt_end}")
        else:
            lines.append(f"DTSTART:{dt_start}")
            lines.append(f"DTEND:{dt_end}")
        lines.append(f"SUMMARY:{esc(summary)}")
        lines.append("END:VEVENT")

    for day in timetable.get("days", []):
        date_str = day["date"].replace("-", "")
        for s in day.get("sessions", []):
            if isinstance(s, dict) and s.get("start") and s.get("end"):
                start_h, start_m = s["start"].split(":")
                end_h, end_m = s["end"].split(":")
                dtstart = f"{date_str}T{start_h}{start_m}00"
                dtend = f"{date_str}T{end_h}{end_m}00"
                uid_counter += 1
                add_event(f"session-{date_str}-{uid_counter}", dtstart, dtend, f"Revise: {s['subject']}")

    for exam in timetable.get("exams", []):
        try:
            exam_date = datetime.strptime(exam["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        date_str = exam_date.strftime("%Y%m%d")
        next_day_str = (exam_date + timedelta(days=1)).strftime("%Y%m%d")
        uid_counter += 1
        label = f"EXAM: {exam['subject']}"
        if exam.get("time"):
            label += f" ({exam['time']})"
        add_event(f"exam-{date_str}-{uid_counter}", date_str, next_day_str, label, all_day=True)

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


@app.route("/api/download-timetable.ics")
@login_required
def download_timetable_ics():
    user = current_user()
    if not user["timetable_json"]:
        return jsonify({"error": "No timetable found. Generate one first."}), 400
    try:
        tt = json.loads(user["timetable_json"])
    except Exception:
        return jsonify({"error": "Could not read your saved timetable."}), 500

    ics_content = build_ics(tt)
    return Response(
        ics_content,
        mimetype="text/calendar",
        headers={"Content-Disposition": "attachment; filename=brainwave-revision-timetable.ics"},
    )


@app.route("/api/process", methods=["POST"])
@login_required
def process():
    user = current_user()
    if not user["api_key"]:
        return jsonify({"error": "Add your Gemini API key in Settings first."}), 400

    data = request.get_json()
    url = data.get("url", "")
    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({"error": "Could not extract a video ID from that URL."}), 400

    try:
        transcript_text = get_transcript_text(video_id)
    except Exception as e:
        return jsonify({"error": f"Could not fetch transcript: {e}"}), 400

    if not transcript_text.strip():
        return jsonify({"error": "This video has no available transcript/captions."}), 400

    try:
        result = generate_all(user["api_key"], transcript_text)
    except Exception as e:
        return jsonify({"error": f"Error generating content: {e}"}), 500

    result["video_id"] = video_id
    result["transcript"] = transcript_text[:6000]
    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
