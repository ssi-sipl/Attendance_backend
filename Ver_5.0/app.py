import sqlite3
import time
from datetime import datetime

from flask import Flask, render_template, redirect, request, session, jsonify
from flask_socketio import SocketIO, emit
from pyfingerprint.pyfingerprint import PyFingerprint

app = Flask(__name__)
app.secret_key = "supersecret"
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
        return "User not found"

    uid, name, status = user

    if status != "active":
        return "User inactive"

    today = datetime.now().strftime("%Y-%m-%d")

    cursor.execute("SELECT * FROM attendance WHERE user_id=? AND date=?", (uid, today))
    if cursor.fetchone():
        return "Already marked"

    now = datetime.now().strftime("%H:%M:%S")

    cursor.execute(
        "INSERT INTO attendance (user_id, name, date, time) VALUES (?, ?, ?, ?)",
        (uid, name, today, now)
    )

    conn.commit()
    socketio.emit("new_attendance")

    return f"Welcome {name}"

# ---------------- ROUTES ----------------

@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect("/dashboard")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/dashboard")
def dashboard():
    if not session.get("admin"):
        return redirect("/")
    return render_template("dashboard.html")

@app.route("/live")
def live():
    cursor.execute("SELECT * FROM attendance ORDER BY id DESC")
    return jsonify(cursor.fetchall())

# 🔥 FIXED SCAN
@app.route("/scan", methods=["GET"])
@app.route("/scan/", methods=["GET"])
def scan():
    f = get_sensor()
    if not f:
        return "Sensor not found"

    try:
        print("Waiting for finger...")
        while not f.readImage():
            pass

        f.convertImage(0x01)
        fid = f.searchTemplate()[0]

        if fid < 0:
            return "Fingerprint not found"

        return mark_attendance(fid)

    except Exception as e:
        return str(e)

# ---------------- SETTINGS ----------------
@app.route("/settings", methods=["GET", "POST"])
def settings():
    if not session.get("admin"):
        return redirect("/")

    if request.method == "POST":
        action = request.form.get("action")
        uid = request.form.get("user_id")
        name = request.form.get("name")

        if action == "add":
            try:
                cursor.execute("INSERT INTO users (name) VALUES (?)", (name,))
            except:
                pass

        elif action == "activate":
            cursor.execute("UPDATE users SET status='active' WHERE user_id=?", (uid,))

        elif action == "deactivate":
            cursor.execute("UPDATE users SET status='inactive' WHERE user_id=?", (uid,))

        elif action == "enroll":
            print(enroll_fingerprint(int(uid)))

        elif action == "delete_all":
            delete_all(uid)

        conn.commit()
        socketio.emit("update")

    cursor.execute("SELECT user_id, name FROM users WHERE status='active'")
    active = cursor.fetchall()

    cursor.execute("SELECT user_id, name FROM users WHERE status='inactive'")
    inactive = cursor.fetchall()

    return render_template("settings.html", active=active, inactive=inactive)

# ---------------- SOCKET ----------------
@socketio.on("connect")
def connect():
    emit("status", {"msg": "connected"})

# ---------------- RUN ----------------
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
