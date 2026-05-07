import sqlite3
import time
from datetime import datetime

from flask import Flask, render_template, redirect, request, session, jsonify
from flask_socketio import SocketIO, emit
from flask_cors import CORS
from pyfingerprint.pyfingerprint import PyFingerprint

app = Flask(__name__)
app.secret_key = "supersecret"

# ✅ Enable CORS for Next.js
CORS(app)

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ---------------- DB ----------------
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

conn.commit()

ADMIN_PASSWORD = "1234"

# ---------------- SENSOR ----------------
def get_sensor():
    try:
        f = PyFingerprint('/dev/serial0', 57600, 0xFFFFFFFF, 0x00000000)
        if not f.verifyPassword():
            return None
        return f
    except:
        return None

# ---------------- ENROLL ----------------
def enroll_fingerprint(user_id):
    f = get_sensor()
    if not f:
        return "Sensor not found"

    try:
        while not f.readImage():
            pass

        f.convertImage(0x01)

        if f.searchTemplate()[0] >= 0:
            return "Already exists"

        time.sleep(2)

        while not f.readImage():
            pass

        f.convertImage(0x02)

        if f.compareCharacteristics() == 0:
            return "Mismatch"

        f.createTemplate()
        fid = f.storeTemplate()

        cursor.execute(
            "INSERT OR REPLACE INTO fingerprints VALUES (?, ?)",
            (fid, user_id)
        )
        conn.commit()

        return "Enrolled"

    except Exception as e:
        return str(e)

# ---------------- DELETE FP ----------------
def delete_all(user_id):
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

# ---------------- ATTENDANCE ----------------
def mark_attendance(fid):
    cursor.execute("""
        SELECT users.user_id, users.name, users.status
        FROM users
        JOIN fingerprints ON users.user_id = fingerprints.user_id
        WHERE fingerprints.finger_id=?
    """, (fid,))

    user = cursor.fetchone()
    if not user:
        return {"error": "User not found"}

    uid, name, status = user

    if status != "active":
        return {"error": "User inactive"}

    today = datetime.now().strftime("%Y-%m-%d")

    cursor.execute("SELECT * FROM attendance WHERE user_id=? AND date=?", (uid, today))
    if cursor.fetchone():
        return {"message": "Already marked"}

    now = datetime.now().strftime("%H:%M:%S")

    cursor.execute(
        "INSERT INTO attendance (user_id, name, date, time) VALUES (?, ?, ?, ?)",
        (uid, name, today, now)
    )

    conn.commit()
    socketio.emit("new_attendance")

    return {
        "message": f"Welcome {name}",
        "user_id": uid,
        "name": name,
        "time": now
    }

# ---------------- API ROUTES ----------------

# 🔥 Scan Fingerprint
@app.route("/api/scan", methods=["GET"])
def api_scan():
    f = get_sensor()
    if not f:
        return jsonify({"error": "Sensor not found"}), 500

    try:
        while not f.readImage():
            pass

        f.convertImage(0x01)
        fid = f.searchTemplate()[0]

        if fid < 0:
            return jsonify({"error": "Fingerprint not found"}), 404

        result = mark_attendance(fid)
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# 📊 Get Attendance
@app.route("/api/attendance", methods=["GET"])
def api_attendance():
    page = int(request.args.get("page", 1))
    limit = int(request.args.get("limit", 10))

    user_id = request.args.get("user_id")
    date = request.args.get("date")

    offset = (page - 1) * limit

    # 🔥 BASE QUERY (NO pagination yet)
    base_query = "FROM attendance WHERE 1=1"
    params = []

    # 👤 Filter by employee id
    if user_id:
        base_query += " AND user_id = ?"
        params.append(user_id)

    # 📅 Filter by date
    if date:
        base_query += " AND date = ?"
        params.append(date)

    # 🔢 TOTAL (filtered count)
    cursor.execute(f"SELECT COUNT(*) {base_query}", params)
    total = cursor.fetchone()[0]

    # 📄 FINAL DATA (filtered + paginated)
    final_query = f"""
        SELECT user_id, name, date, time 
        {base_query}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
    """

    cursor.execute(final_query, params + [limit, offset])
    rows = cursor.fetchall()

    data = []
    for r in rows:
        data.append({
            "user_id": r[0],
            "name": r[1],
            "date": r[2],
            "time": r[3]
        })

    return jsonify({
        "page": page,
        "limit": limit,
        "total": total,               # filtered total
        "total_pages": (total + limit - 1) // limit,
        "data": data
    })


# 👥 Get Users
@app.route("/api/attendance", methods=["GET"])
def api_attendance():
    page = int(request.args.get("page", 1))
    limit = int(request.args.get("limit", 10))

    user_id = request.args.get("user_id")
    date = request.args.get("date")

    offset = (page - 1) * limit

    # 🔥 BASE QUERY (NO pagination yet)
    base_query = "FROM attendance WHERE 1=1"
    params = []

    # 👤 Filter by employee id
    if user_id:
        base_query += " AND user_id = ?"
        params.append(user_id)

    # 📅 Filter by date
    if date:
        base_query += " AND date = ?"
        params.append(date)

    # 🔢 TOTAL (filtered count)
    cursor.execute(f"SELECT COUNT(*) {base_query}", params)
    total = cursor.fetchone()[0]

    # 📄 FINAL DATA (filtered + paginated)
    final_query = f"""
        SELECT user_id, name, date, time 
        {base_query}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
    """

    cursor.execute(final_query, params + [limit, offset])
    rows = cursor.fetchall()

    data = []
    for r in rows:
        data.append({
            "user_id": r[0],
            "name": r[1],
            "date": r[2],
            "time": r[3]
        })

    return jsonify({
        "page": page,
        "limit": limit,
        "total": total,               # filtered total
        "total_pages": (total + limit - 1) // limit,
        "data": data
    })

# ➕ Add User
@app.route("/api/users", methods=["POST"])
def add_user():
    data = request.json
    name = data.get("name")

    try:
        cursor.execute("INSERT INTO users (name) VALUES (?)", (name,))
        conn.commit()
        return jsonify({"message": "User added"})
    except:
        return jsonify({"error": "User already exists"}), 400


# ---------------- SOCKET ----------------
@socketio.on("connect")
def connect():
    emit("status", {"msg": "connected"})


# ---------------- RUN ----------------
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
