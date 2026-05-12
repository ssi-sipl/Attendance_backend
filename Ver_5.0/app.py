import sqlite3
import time
import re
from datetime import datetime, timedelta

from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
from flask_cors import CORS
from pyfingerprint.pyfingerprint import PyFingerprint

# Sentence Transformers + fuzzy matching
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np
from thefuzz import process as fuzz_process

# ---------------- APP ----------------
app = Flask(__name__)
app.secret_key = "supersecret"

CORS(app)

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ---------------- DATABASE ----------------
conn = sqlite3.connect("fingerprint.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    status TEXT DEFAULT 'active'
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS fingerprints (
    finger_id INTEGER PRIMARY KEY,
    user_id INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    name TEXT,
    date TEXT,
    time TEXT
)
""")

# ---------------- INDEXES (speeds up all date/user queries) ----------------
cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_attendance_date
    ON attendance(date)
""")

cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_attendance_user
    ON attendance(user_id)
""")

cursor.execute("""
    CREATE INDEX IF NOT EXISTS idx_attendance_date_time
    ON attendance(date, time)
""")

conn.commit()


# ---------------- SENTENCE TRANSFORMERS SETUP ----------------
# Loaded ONCE at startup — not per request
print("Loading Sentence Transformer model...")
st_model = SentenceTransformer('all-MiniLM-L6-v2')

# Intent examples — more examples = better accuracy
# Each list represents natural ways to express that intent
INTENT_EXAMPLES = {
    "absent": [
        "who was absent today",
        "who did not come",
        "who was missing",
        "who didn't show up",
        "who skipped work",
        "who was not present",
        "list absentees",
        "who bunked today",
        "missing employees",
        "who didn't attend",
        "who was out today",
        "absentees list",
    ],
    "late_attendance": [
        "who came late",
        "who arrived late",
        "late arrivals",
        "who was tardy",
        "who came after 10",
        "late employees",
        "who showed up late",
        "who came after time",
        "employees who arrived late",
        "who was delayed",
    ],
    "early_attendance": [
        "who came early",
        "who arrived before time",
        "early arrivals",
        "who showed up early",
        "who was early today",
        "employees who came before 9",
        "who reached early",
        "early birds",
    ],
    "specific_presence": [
        "was john present",
        "did rahul come today",
        "is priya in office",
        "did the employee attend",
        "was he present",
        "did she come",
        "check if someone attended",
        "was anyone present",
    ],
    "attendance_list": [
        "who came today",
        "who attended today",
        "who was present",
        "show today's attendance",
        "list of present employees",
        "who is in office today",
        "attendance for today",
        "who turned up today",
        "present employees",
    ],
    "employee_records": [
        "show records for rahul",
        "attendance history of john",
        "fetch records for employee",
        "display attendance of priya",
        "show me someone's history",
        "past attendance of employee",
        "attendance log for rahul",
        "get records for id 5",
    ],
    "count_attendance": [
        "how many came today",
        "count of present employees",
        "total employees today",
        "how many attended",
        "number of people present",
        "headcount today",
        "how many showed up",
        "total attendance count",
    ],
    "weekly_attendance": [
        "attendance this week",
        "who came this week",
        "weekly attendance report",
        "this week's records",
        "attendance for the week",
        "who attended this week",
        "weekly summary",
    ],
    "monthly_attendance": [
        "attendance this month",
        "monthly report",
        "who came this month",
        "this month's attendance",
        "monthly summary",
        "attendance for the month",
    ],
    "most_absent": [
        "who missed the most days",
        "who is absent the most",
        "most frequent absentee",
        "who has most absences",
        "least attending employee",
        "who skipped most",
        "highest absenteeism",
        "who missed most this month",
    ],
    "most_punctual": [
        "who is most punctual",
        "who comes earliest always",
        "best attendance employee",
        "most regular employee",
        "who arrives on time most",
        "most consistent employee",
        "who has best attendance",
        "top punctual employee",
    ],
    "average_arrival": [
        "what is average arrival time",
        "average time employees come",
        "when do employees usually arrive",
        "average check in time",
        "mean arrival time",
        "what time do people usually come",
    ],
    "list_users": [
        "list all employees",
        "show all users",
        "all staff members",
        "show registered employees",
        "who are all the employees",
        "complete employee list",
        "all workers",
    ],
    "help": [
        "help",
        "what can you do",
        "show commands",
        "what are your features",
        "how do i use this",
        "guide me",
        "what queries can i ask",
    ],
}

# Pre-encode all intent examples at startup — O(1) at query time
print("Encoding intent examples...")
INTENT_EMBEDDINGS = {}
for intent, examples in INTENT_EXAMPLES.items():
    INTENT_EMBEDDINGS[intent] = st_model.encode(examples)

print("Chatbot ready.")


# ---------------- SENSOR ----------------
def get_sensor():
    try:
        f = PyFingerprint('/dev/serial0', 57600, 0xFFFFFFFF, 0x00000000)
        if not f.verifyPassword():
            return None
        return f
    except Exception:
        return None


# ---------------- DELETE ALL ----------------
def delete_all(user_id):
    try:
        cursor.execute("SELECT finger_id FROM fingerprints WHERE user_id=?", (user_id,))
        rows = cursor.fetchall()

        f = get_sensor()

        for (fid,) in rows:
            try:
                if f:
                    f.deleteTemplate(fid)
            except:
                pass

        cursor.execute("DELETE FROM fingerprints WHERE user_id=?", (user_id,))
        cursor.execute("DELETE FROM attendance WHERE user_id=?", (user_id,))
        cursor.execute("DELETE FROM users WHERE user_id=?", (user_id,))

        conn.commit()
        return True
    except Exception:
        return False


# ---------------- HOME ----------------
@app.route("/")
def home():
    return jsonify({
        "status": True,
        "message": "Fingerprint Attendance API Running",
        "data": {}
    }), 200


# ---------------- GET ATTENDANCE ----------------
@app.route("/api/attendance", methods=["GET"])
def api_attendance():
    try:
        page = int(request.args.get("page", 1))
        limit = int(request.args.get("limit", 10))
        user_id = request.args.get("user_id")
        date = request.args.get("date")

        offset = (page - 1) * limit

        base_query = "FROM attendance WHERE 1=1"
        params = []

        if user_id:
            base_query += " AND user_id=?"
            params.append(user_id)

        if date:
            base_query += " AND date=?"
            params.append(date)

        cursor.execute(f"SELECT COUNT(*) {base_query}", params)
        total = cursor.fetchone()[0]

        cursor.execute(f"""
            SELECT user_id, name, date, time
            {base_query}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """, params + [limit, offset])

        rows = cursor.fetchall()

        data = [{"user_id": r[0], "name": r[1], "date": r[2], "time": r[3]} for r in rows]

        return jsonify({
            "status": True,
            "message": "Attendance fetched successfully",
            "data": {
                "records": data,
                "page": page,
                "limit": limit,
                "total": total,
                "total_pages": (total + limit - 1) // limit
            }
        }), 200

    except Exception:
        return jsonify({"status": False, "message": "Internal server error", "data": {}}), 500


# ---------------- GET USERS ----------------
@app.route("/api/users", methods=["GET"])
def api_users():
    try:
        page = int(request.args.get("page", 1))
        limit = int(request.args.get("limit", 10))
        search = request.args.get("search", "")
        status = request.args.get("status", "")

        offset = (page - 1) * limit

        base_query = "FROM users WHERE 1=1"
        params = []

        if search:
            base_query += " AND (name LIKE ? OR user_id LIKE ?)"
            params += [f"%{search}%", f"%{search}%"]

        if status:
            base_query += " AND status=?"
            params.append(status)

        cursor.execute(f"SELECT COUNT(*) {base_query}", params)
        total = cursor.fetchone()[0]

        cursor.execute(f"""
            SELECT user_id, name, status
            {base_query}
            ORDER BY user_id DESC
            LIMIT ? OFFSET ?
        """, params + [limit, offset])

        rows = cursor.fetchall()
        users = [{"user_id": r[0], "name": r[1], "status": r[2]} for r in rows]

        return jsonify({
            "status": True,
            "message": "Users fetched successfully",
            "data": {
                "users": users,
                "page": page,
                "limit": limit,
                "total": total,
                "total_pages": (total + limit - 1) // limit
            }
        }), 200

    except Exception:
        return jsonify({"status": False, "message": "Internal server error", "data": {}}), 500


# ---------------- ENROLL ----------------
@app.route("/api/enroll", methods=["POST"])
def enroll():
    try:
        data = request.json
        name = data.get("name")

        if not name:
            return jsonify({"status": False, "message": "Name is required", "data": {}}), 400

        f = get_sensor()
        if not f:
            return jsonify({"status": False, "message": "Sensor not found", "data": {}}), 500

        while not f.readImage():
            pass

        f.convertImage(0x01)
        result = f.searchTemplate()

        if result[0] >= 0:
            return jsonify({"status": False, "message": "Fingerprint already exists", "data": {}}), 409

        time.sleep(2)

        while not f.readImage():
            pass

        f.convertImage(0x02)

        if f.compareCharacteristics() == 0:
            return jsonify({"status": False, "message": "Fingerprint mismatch", "data": {}}), 400

        f.createTemplate()
        finger_id = f.storeTemplate()

        cursor.execute("INSERT INTO users (name) VALUES (?)", (name,))
        user_id = cursor.lastrowid

        cursor.execute(
            "INSERT INTO fingerprints (finger_id, user_id) VALUES (?, ?)",
            (finger_id, user_id)
        )

        conn.commit()

        return jsonify({
            "status": True,
            "message": "Enrollment completed successfully",
            "data": {"user_id": user_id, "finger_id": finger_id}
        }), 200

    except Exception as e:
        return jsonify({"status": False, "message": str(e), "data": {}}), 500


# ---------------- DELETE USER ----------------
@app.route("/api/users/<int:user_id>", methods=["DELETE"])
def delete_user(user_id):
    try:
        cursor.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
        user = cursor.fetchone()

        if not user:
            return jsonify({"status": False, "message": "User not found", "data": {}}), 404

        success = delete_all(user_id)

        if not success:
            return jsonify({"status": False, "message": "Failed to delete user", "data": {}}), 500

        return jsonify({
            "status": True,
            "message": "User deleted successfully",
            "data": {"user_id": user_id}
        }), 200

    except Exception:
        return jsonify({"status": False, "message": "Internal server error", "data": {}}), 500


# ---------------- SOCKET ----------------
@socketio.on("connect")
def handle_connect():
    emit("status", {"message": "Socket connected"})


# ============================================================
#                        CHATBOT
# ============================================================

# ---------------- INTENT DETECTION (Sentence Transformers) ----------------
CONFIDENCE_THRESHOLD = 0.35  # below this → unknown intent

def detect_intent(query: str) -> str:
    """
    Uses Sentence Transformers cosine similarity to detect intent.
    TC: O(I) where I = number of intents (fixed, small)
    SC: O(1) additional space
    """
    query_embedding = st_model.encode([query])  # shape (1, 384)

    best_intent = "unknown"
    best_score = 0.0

    for intent, embeddings in INTENT_EMBEDDINGS.items():
        # cosine similarity between query and all examples for this intent
        sims = cosine_similarity(query_embedding, embeddings)  # shape (1, N)
        score = float(np.max(sims))  # best matching example

        if score > best_score:
            best_score = score
            best_intent = intent

    # If confidence too low, return unknown rather than wrong answer
    if best_score < CONFIDENCE_THRESHOLD:
        return "unknown"

    return best_intent


# ---------------- NAME FUZZY MATCHING (thefuzz) ----------------
def get_all_names() -> list[str]:
    """Fetch all registered employee names from DB."""
    cursor.execute("SELECT name FROM users")
    return [r[0] for r in cursor.fetchall()]


def fuzzy_match_name(extracted: str, threshold: int = 75) -> str | None:
    """
    Uses Levenshtein distance to fix typos in employee names.
    'Dhirj' → 'Dhiraj', 'Rahull' → 'Rahul'
    TC: O(N * L) where N = employees, L = name length
    SC: O(N)
    Returns matched name or None if below threshold.
    """
    all_names = get_all_names()
    if not all_names:
        return None

    result = fuzz_process.extractOne(extracted, all_names)

    if result and result[1] >= threshold:
        return result[0]  # corrected name

    return None


# ---------------- DATE EXTRACTION (Regex — unchanged) ----------------
def extract_date(text: str) -> str | None:
    text = text.lower()
    today = datetime.now().strftime("%Y-%m-%d")

    if 'yesterday' in text or 'yday' in text or 'ytd' in text:
        return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    if 'today' in text or '2day' in text or 'tday' in text or 'todays' in text:
        return today

    # format: 2026-05-07
    m = re.search(r'(\d{4}-\d{2}-\d{2})', text)
    if m:
        return m.group(1)

    months = {
        'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4,
        'may': 5, 'jun': 6, 'jul': 7, 'aug': 8,
        'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12
    }

    # format: 7 may
    m = re.search(
        r'(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)',
        text, re.IGNORECASE
    )
    if m:
        return (
            f"{datetime.now().year}-"
            f"{months[m.group(2).lower()]:02d}-"
            f"{int(m.group(1)):02d}"
        )

    # format: may 7
    m = re.search(
        r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+(\d{1,2})',
        text, re.IGNORECASE
    )
    if m:
        return (
            f"{datetime.now().year}-"
            f"{months[m.group(1).lower()]:02d}-"
            f"{int(m.group(2)):02d}"
        )

    return None


# ---------------- TIME EXTRACTION (Regex — unchanged) ----------------
def extract_time(text: str) -> str | None:
    m = re.search(r'(\d{1,2}):(\d{2})\s*(am|pm)?', text, re.IGNORECASE)
    if m:
        h, mn, period = int(m.group(1)), int(m.group(2)), m.group(3)
        if period and period.lower() == 'pm' and h != 12:
            h += 12
        if period and period.lower() == 'am' and h == 12:
            h = 0
        return f"{h:02d}:{mn:02d}:00"
    return None


# ---------------- NAME EXTRACTION (Regex + thefuzz) ----------------
def extract_name(text: str) -> str | None:
    """
    Extract name from query text, then fuzzy-correct it against DB.
    'Show Dhirj records' → extracts 'Dhirj' → corrects to 'Dhiraj'
    """
    raw_name = None

    # "did X come", "was X present" → grab only single word after verb
    verb_match = re.search(r"(?:did|was|is)\s+([a-zA-Z]+)", text, re.IGNORECASE)
    if verb_match:
        candidate = verb_match.group(1).strip()
        stopwords = {
            'the', 'all', 'today', 'this', 'who', 'how',
            'many', 'employee', 'yesterday', 'week', 'month',
            'he', 'she', 'they', 'not', 'no', 'any'
        }
        if candidate.lower() not in stopwords:
            raw_name = candidate

    # Fallback: "show/fetch/for X" or "X attendance/records"
    if not raw_name:
        for pattern in [
            r"(?:of|for|show|employee|check)\s+([a-zA-Z]+)",
            r"([a-zA-Z]+)\s+(?:attendance|records|present|late|absent)"
        ]:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                candidate = m.group(1).strip()
                stopwords = {
                    'the', 'all', 'today', 'this', 'who', 'how',
                    'many', 'employee', 'yesterday', 'week', 'month'
                }
                if candidate.lower() not in stopwords:
                    raw_name = candidate
                    break

    if not raw_name:
        return None

    # Try to fuzzy-correct the extracted name against actual DB names
    corrected = fuzzy_match_name(raw_name)
    return corrected if corrected else raw_name


# ---------------- HELPERS ----------------
def row_to_dict(r) -> dict:
    return {"user_id": r[0], "name": r[1], "date": r[2], "time": r[3]}


def format_records(rows: list, empty_msg: str) -> str:
    if not rows:
        return empty_msg
    lines = [
        f"• {r['name']} (ID {r['user_id']}) — {r['date']} at {r['time']}"
        for r in rows
    ]
    return "\n".join(lines)


# ---------------- MAIN CHAT QUERY ----------------
def chat_query(q: str) -> str:

    q_lower = q.lower().strip()
    intent = detect_intent(q_lower)
    today = datetime.now().strftime("%Y-%m-%d")

    # ── ABSENT EMPLOYEES ──
    if intent == "absent":
        date = extract_date(q_lower) or today

        cursor.execute("""
            SELECT user_id, name
            FROM users
            WHERE user_id NOT IN (
                SELECT DISTINCT user_id
                FROM attendance
                WHERE date=?
            )
            ORDER BY name
        """, (date,))

        rows = cursor.fetchall()

        if not rows:
            return f"Everyone was present on {date}."

        lines = [f"• {r[1]} (ID {r[0]})" for r in rows]
        return f"**{len(rows)} employees were absent on {date}:**\n" + "\n".join(lines)

    # ── SPECIFIC EMPLOYEE PRESENCE ──
    if intent == "specific_presence":
        name = extract_name(q_lower)
        date = extract_date(q_lower) or today

        if name:
            cursor.execute("""
                SELECT user_id, name, date, time
                FROM attendance
                WHERE LOWER(name) LIKE ? AND date=?
            """, (f"%{name.lower()}%", date))

            row = cursor.fetchone()

            if row:
                return f"Yes, {row[1]} was present on {row[2]} at {row[3]}."
            return f"No, {name.title()} was absent on {date}."

        return "Please specify an employee name."

    # ── WHO CAME TODAY / ON DATE ──
    if intent == "attendance_list":
        date = extract_date(q_lower) or today

        cursor.execute("""
            SELECT user_id, name, date, time
            FROM attendance
            WHERE date=?
            ORDER BY time
        """, (date,))

        rows = [row_to_dict(r) for r in cursor.fetchall()]

        if not rows:
            return f"No attendance records found for {date}."

        return (
            f"**{len(rows)} employees present on {date}:**\n"
            + format_records(rows, "")
        )

    # ── LATE ARRIVALS ──
    if intent == "late_attendance":
        t = extract_time(q_lower) or "10:00:00"
        date = extract_date(q_lower) or today

        cursor.execute("""
            SELECT user_id, name, date, time
            FROM attendance
            WHERE date=? AND time > ?
            ORDER BY time
        """, (date, t))

        rows = [row_to_dict(r) for r in cursor.fetchall()]

        if not rows:
            return f"No one came late on {date} (after {t[:5]})."

        return (
            f"**{len(rows)} employees came late on {date} (after {t[:5]}):**\n"
            + format_records(rows, "")
        )

    # ── EARLY ARRIVALS ──
    if intent == "early_attendance":
        t = extract_time(q_lower) or "09:30:00"
        date = extract_date(q_lower) or today

        cursor.execute("""
            SELECT user_id, name, date, time
            FROM attendance
            WHERE date=? AND time < ?
            ORDER BY time
        """, (date, t))

        rows = [row_to_dict(r) for r in cursor.fetchall()]

        if not rows:
            return f"No one arrived before {t[:5]} on {date}."

        return (
            f"**{len(rows)} employees arrived before {t[:5]} on {date}:**\n"
            + format_records(rows, "")
        )

    # ── EMPLOYEE RECORDS ──
    if intent == "employee_records":
        name = extract_name(q_lower)

        id_match = re.search(r'\b(?:id|employee|emp)?\s*#?(\d+)\b', q_lower)

        if name:
            cursor.execute("""
                SELECT user_id, name, date, time
                FROM attendance
                WHERE LOWER(name) LIKE ?
                ORDER BY date DESC
                LIMIT 30
            """, (f"%{name.lower()}%",))

            rows = [row_to_dict(r) for r in cursor.fetchall()]

            if not rows:
                return f"No records found for '{name}'."

            return (
                f"**{len(rows)} records for {rows[0]['name']}:**\n"
                + format_records(rows, "")
            )

        elif id_match:
            uid = id_match.group(1)

            cursor.execute("""
                SELECT user_id, name, date, time
                FROM attendance
                WHERE user_id=?
                ORDER BY date DESC
                LIMIT 30
            """, (uid,))

            rows = [row_to_dict(r) for r in cursor.fetchall()]

            if not rows:
                return f"No records found for employee ID {uid}."

            return (
                f"**{len(rows)} records for {rows[0]['name']}:**\n"
                + format_records(rows, "")
            )

        return "Please specify an employee name or ID."

    # ── COUNT / HOW MANY ──
    if intent == "count_attendance":
        date = extract_date(q_lower) or today

        cursor.execute("SELECT COUNT(*) FROM attendance WHERE date=?", (date,))
        count = cursor.fetchone()[0]

        return f"**{count} employees** were present on {date}."

    # ── WEEKLY ATTENDANCE ──
    if intent == "weekly_attendance":
        week_start = (
            datetime.now() - timedelta(days=datetime.now().weekday())
        ).strftime("%Y-%m-%d")

        cursor.execute("""
            SELECT user_id, name, date, time
            FROM attendance
            WHERE date >= ?
            ORDER BY date DESC, time
        """, (week_start,))

        rows = [row_to_dict(r) for r in cursor.fetchall()]

        if not rows:
            return f"No attendance records found this week (from {week_start})."

        return (
            f"**{len(rows)} records this week (from {week_start}):**\n"
            + format_records(rows, "")
        )

    # ── MONTHLY ATTENDANCE ──
    if intent == "monthly_attendance":
        month_start = datetime.now().strftime("%Y-%m-01")

        cursor.execute("""
            SELECT user_id, name, date, time
            FROM attendance
            WHERE date >= ?
            ORDER BY date DESC, time
        """, (month_start,))

        rows = [row_to_dict(r) for r in cursor.fetchall()]

        if not rows:
            return f"No attendance records found this month (from {month_start})."

        return (
            f"**{len(rows)} records this month (from {month_start}):**\n"
            + format_records(rows, "")
        )

    # ── MOST ABSENT EMPLOYEES ──
    if intent == "most_absent":
        month_start = datetime.now().strftime("%Y-%m-01")

        # TC: O(U + A) with index, SC: O(U)
        cursor.execute("""
            SELECT
                u.user_id,
                u.name,
                COUNT(DISTINCT a.date) AS present_days,
                (
                    SELECT COUNT(DISTINCT date)
                    FROM attendance
                    WHERE date >= ?
                ) - COUNT(DISTINCT a.date) AS absent_days
            FROM users u
            LEFT JOIN attendance a
                ON u.user_id = a.user_id
                AND a.date >= ?
            GROUP BY u.user_id
            ORDER BY absent_days DESC
            LIMIT 5
        """, (month_start, month_start))

        rows = cursor.fetchall()

        if not rows:
            return "No data available for this month."

        lines = [
            f"• {r[1]} (ID {r[0]}) — absent {r[3]} day(s), present {r[2]} day(s)"
            for r in rows
        ]

        return f"**Most absent employees this month:**\n" + "\n".join(lines)

    # ── MOST PUNCTUAL EMPLOYEES ──
    if intent == "most_punctual":
        # Average arrival time in minutes since midnight
        # TC: O(A log A) with index, SC: O(U)
        cursor.execute("""
            SELECT
                user_id,
                name,
                AVG(
                    CAST(substr(time, 1, 2) AS INTEGER) * 60 +
                    CAST(substr(time, 4, 2) AS INTEGER)
                ) AS avg_minutes
            FROM attendance
            GROUP BY user_id
            ORDER BY avg_minutes ASC
            LIMIT 5
        """)

        rows = cursor.fetchall()

        if not rows:
            return "No attendance data available."

        lines = []
        for r in rows:
            avg_min = int(r[2])
            h, m = divmod(avg_min, 60)
            lines.append(f"• {r[1]} (ID {r[0]}) — avg arrival {h:02d}:{m:02d}")

        return f"**Most punctual employees (by avg arrival time):**\n" + "\n".join(lines)

    # ── AVERAGE ARRIVAL TIME ──
    if intent == "average_arrival":
        date = extract_date(q_lower)

        if date:
            # Average for a specific date
            cursor.execute("""
                SELECT
                    user_id,
                    name,
                    AVG(
                        CAST(substr(time, 1, 2) AS INTEGER) * 60 +
                        CAST(substr(time, 4, 2) AS INTEGER)
                    ) AS avg_minutes
                FROM attendance
                WHERE date = ?
                GROUP BY user_id
                ORDER BY avg_minutes ASC
            """, (date,))
        else:
            # Overall average
            cursor.execute("""
                SELECT
                    AVG(
                        CAST(substr(time, 1, 2) AS INTEGER) * 60 +
                        CAST(substr(time, 4, 2) AS INTEGER)
                    ) AS avg_minutes
                FROM attendance
            """)

            row = cursor.fetchone()

            if not row or row[0] is None:
                return "No attendance data available."

            avg_min = int(row[0])
            h, m = divmod(avg_min, 60)
            return f"**Overall average arrival time: {h:02d}:{m:02d}**"

        rows = cursor.fetchall()

        if not rows:
            return f"No data for {date}."

        lines = []
        for r in rows:
            avg_min = int(r[2])
            h, m = divmod(avg_min, 60)
            lines.append(f"• {r[1]} (ID {r[0]}) — {h:02d}:{m:02d}")

        return f"**Average arrival times on {date}:**\n" + "\n".join(lines)

    # ── LIST ALL USERS ──
    if intent == "list_users":
        cursor.execute("SELECT user_id, name, status FROM users ORDER BY user_id")
        rows = cursor.fetchall()

        if not rows:
            return "No users registered."

        lines = [f"• {r[1]} (ID {r[0]}) — {r[2]}" for r in rows]
        return f"**{len(rows)} registered employees:**\n" + "\n".join(lines)

    # ── HELP ──
    if intent == "help":
        return (
            "Here's what I can help with:\n"
            "• **Who came today** — today's attendance\n"
            "• **Who came on 5 May** — attendance on a specific date\n"
            "• **Who was absent today** — absentees list\n"
            "• **Late arrivals after 10:15** — everyone after a time\n"
            "• **Who came early today** — early arrivals\n"
            "• **Show Dhiraj's records** — attendance history (typos auto-corrected)\n"
            "• **How many came today** — count of present employees\n"
            "• **Attendance this week** — weekly records\n"
            "• **Attendance this month** — monthly records\n"
            "• **Who missed the most this month** — top absentees\n"
            "• **Who is most punctual** — top punctual employees\n"
            "• **Average arrival time** — overall or by date\n"
            "• **List all employees** — all registered users"
        )

    # ── UNKNOWN ──
    return (
        "I didn't understand that. "
        "Type **help** to see what I can do."
    )


# ---------------- CHAT API ----------------
@app.route("/api/chat", methods=["POST"])
def api_chat():
    try:
        data = request.json
        query = data.get("query", "").strip()

        if not query:
            return jsonify({
                "status": False,
                "message": "Query is required",
                "data": {}
            }), 400

        answer = chat_query(query)

        return jsonify({
            "status": True,
            "message": "OK",
            "data": {
                "query": query,
                "answer": answer
            }
        }), 200

    except Exception as e:
        return jsonify({
            "status": False,
            "message": str(e),
            "data": {}
        }), 500


# ---------------- RUN ----------------
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)