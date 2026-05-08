import sqlite3
import time
from datetime import datetime

from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
from flask_cors import CORS
from pyfingerprint.pyfingerprint import PyFingerprint

# ---------------- APP ----------------
app = Flask(__name__)
app.secret_key = "supersecret"

# ✅ Enable CORS
CORS(app)

# ✅ Socket.IO
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ---------------- DATABASE ----------------
conn = sqlite3.connect("fingerprint.db", check_same_thread=False)
cursor = conn.cursor()

# 👥 Users Table
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    status TEXT DEFAULT 'active'
)
""")

# 👆 Fingerprints Table
cursor.execute("""
CREATE TABLE IF NOT EXISTS fingerprints (
    finger_id INTEGER PRIMARY KEY,
    user_id INTEGER
)
""")

# 📊 Attendance Table
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

# ---------------- SENSOR ----------------
def get_sensor():
    try:
        f = PyFingerprint(
            '/dev/serial0',
            57600,
            0xFFFFFFFF,
            0x00000000
        )

        if not f.verifyPassword():
            return None

        return f

    except Exception:
        return None


# ---------------- ENROLL FINGERPRINT ----------------
def enroll_fingerprint(user_id):
    f = get_sensor()

    if not f:
        return "Sensor not found"

    try:
        print("Place finger...")

        while not f.readImage():
            pass

        f.convertImage(0x01)

        result = f.searchTemplate()

        if result[0] >= 0:
            return "Fingerprint already exists"

        time.sleep(2)

        print("Place same finger again...")

        while not f.readImage():
            pass

        f.convertImage(0x02)

        if f.compareCharacteristics() == 0:
            return "Fingerprints do not match"

        f.createTemplate()

        fid = f.storeTemplate()

        cursor.execute(
            "INSERT OR REPLACE INTO fingerprints (finger_id, user_id) VALUES (?, ?)",
            (fid, user_id)
        )

        conn.commit()

        return "Fingerprint enrolled successfully"

    except Exception as e:
        return str(e)


# ---------------- DELETE USER + FINGERPRINT ----------------
def delete_all(user_id):
    try:
        cursor.execute(
            "SELECT finger_id FROM fingerprints WHERE user_id=?",
            (user_id,)
        )

        rows = cursor.fetchall()

        f = get_sensor()

        for (fid,) in rows:
            try:
                if f:
                    f.deleteTemplate(fid)
            except:
                pass

        cursor.execute(
            "DELETE FROM fingerprints WHERE user_id=?",
            (user_id,)
        )

        cursor.execute(
            "DELETE FROM attendance WHERE user_id=?",
            (user_id,)
        )

        cursor.execute(
            "DELETE FROM users WHERE user_id=?",
            (user_id,)
        )

        conn.commit()

        return True

    except Exception:
        return False


# ---------------- MARK ATTENDANCE ----------------
def mark_attendance(fid):
    cursor.execute("""
        SELECT users.user_id, users.name, users.status
        FROM users
        JOIN fingerprints
        ON users.user_id = fingerprints.user_id
        WHERE fingerprints.finger_id=?
    """, (fid,))

    user = cursor.fetchone()

    if not user:
        return {"error": "User not found"}

    uid, name, status = user

    if status != "active":
        return {"error": "User inactive"}

    today = datetime.now().strftime("%Y-%m-%d")

    cursor.execute(
        "SELECT * FROM attendance WHERE user_id=? AND date=?",
        (uid, today)
    )

    if cursor.fetchone():
        return {"message": "Attendance already marked"}

    now = datetime.now().strftime("%H:%M:%S")

    cursor.execute("""
        INSERT INTO attendance (user_id, name, date, time)
        VALUES (?, ?, ?, ?)
    """, (uid, name, today, now))

    conn.commit()

    # 🔥 Realtime update
    socketio.emit("new_attendance")

    return {
        "message": f"Welcome {name}",
        "user_id": uid,
        "name": name,
        "time": now
    }


# ---------------- HOME ROUTE ----------------
@app.route("/")
def home():
    return jsonify({
        "status": True,
        "message": "Fingerprint Attendance API Running"
    })

# ---------------- GET ATTENDANCE ----------------
@app.route("/api/attendance", methods=["GET"])
def api_attendance():
    try:
        page = int(request.args.get("page", 1))
        limit = int(request.args.get("limit", 10))

        user_id = request.args.get("user_id")
        date = request.args.get("date")

        offset = (page - 1) * limit

        # 🔥 Base Query
        base_query = "FROM attendance WHERE 1=1"
        params = []

        # 👤 Filter by User ID
        if user_id:
            base_query += " AND user_id = ?"
            params.append(user_id)

        # 📅 Filter by Date
        if date:
            base_query += " AND date = ?"
            params.append(date)

        # 🔢 Total Count
        cursor.execute(
            f"SELECT COUNT(*) {base_query}",
            params
        )

        total = cursor.fetchone()[0]

        # 📄 Final Query
        final_query = f"""
            SELECT user_id, name, date, time
            {base_query}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """

        cursor.execute(
            final_query,
            params + [limit, offset]
        )

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
            "status": True,
            "page": page,
            "limit": limit,
            "total": total,
            "total_pages": (total + limit - 1) // limit,
            "data": data
        })

    except Exception as e:
        return jsonify({
            "status": False,
            "message": Internal server error
        }), 500


# ---------------- GET USERS ----------------
@app.route("/api/users", methods=["GET"])
def api_users():
    try:
        page = int(request.args.get("page", 1))
        limit = int(request.args.get("limit", 10))

        search = request.args.get("search", "")
        status = request.args.get("status", "")

        offset = (page - 1) * limit

        # 🔥 Base Query
        base_query = "FROM users WHERE 1=1"
        params = []

        # 🔍 Search
        if search:
            base_query += " AND (name LIKE ? OR user_id LIKE ?)"
            params.extend([
                f"%{search}%",
                f"%{search}%"
            ])

        # 🟢 Status Filter
        if status:
            base_query += " AND status = ?"
            params.append(status)

        # 🔢 Total Count
        cursor.execute(
            f"SELECT COUNT(*) {base_query}",
            params
        )

        total = cursor.fetchone()[0]

        # 📄 Final Query
        final_query = f"""
            SELECT user_id, name, status
            {base_query}
            ORDER BY user_id DESC
            LIMIT ? OFFSET ?
        """

        cursor.execute(
            final_query,
            params + [limit, offset]
        )

        rows = cursor.fetchall()

        users = []

        for r in rows:
            users.append({
                "user_id": r[0],
                "name": r[1],
                "status": r[2]
            })

        return jsonify({
            "status": True,
            "page": page,
            "limit": limit,
            "total": total,
            "total_pages": (total + limit - 1) // limit,
            "data": users
        })

    except Exception as e:
        return jsonify({
            "status": False,
            "message": Internal server error
        }), 500


# ---------------- Enroll User ----------------
@app.route("/api/enrollment", methods=["POST"])
def enrollment():
    try:
        data = request.json
        mode = data.get("mode")

        # ================= STEP 1: START ENROLLMENT =================
        if mode == "start":
            name = data.get("name")

            if not name:
                return jsonify({
                    "status": False,
                    "error": "Name is required"
                }), 400

            # create user first
            cursor.execute(
                "INSERT INTO users (name) VALUES (?)",
                (name,)
            )
            conn.commit()

            user_id = cursor.lastrowid

            return jsonify({
                "status": True,
                "message": "User created. Please scan fingerprint.",
                "user_id": user_id
            })

        # ================= STEP 2: SCAN + ENROLL FINGERPRINT =================
        elif mode == "scan":
            user_id = data.get("user_id")

            if not user_id:
                return jsonify({
                    "status": False,
                    "message": "user_id required",
                    "data":{}
                }), 400

            f = get_sensor()

            if not f:
                return jsonify({
                    "status": False,
                    "message": "Sensor not found",
                    "data":{}
                }), 500

            # Wait for finger
            while not f.readImage():
                pass

            f.convertImage(0x01)

            # Check duplicate fingerprint
            if f.searchTemplate()[0] >= 0:
                return jsonify({
                    "status": False,
                    "message": "Fingerprint already exists",
                    "data":{}
                }), 409

            time.sleep(2)

            # Second scan (verification)
            while not f.readImage():
                pass

            f.convertImage(0x02)

            if f.compareCharacteristics() == 0:
                return jsonify({
                    "status": False,
                    "message": "Fingerprints do not match",
                    "data":{}
                }), 400

            f.createTemplate()
            fid = f.storeTemplate()

            # Save fingerprint mapping
            cursor.execute(
                "INSERT INTO fingerprints (finger_id, user_id) VALUES (?, ?)",
                (fid, user_id)
            )

            conn.commit()

            return jsonify({
                "status": True,
                "message": "Enrollment completed successfully"
            })

        # ================= INVALID =================
        else:
            return jsonify({
                "status": False,
                "error": "Invalid mode. Use start or scan"
            }), 400

    except Exception as e:
        return jsonify({
            "status": False,
            "message":Internal Server error,
            "data":{}
        }), 500


# ---------------- SOCKET.IO ----------------
@socketio.on("connect")
def handle_connect():
    emit("status", {
        "message": "Socket connected"
    })


# ---------------- RUN SERVER ----------------
if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=5000,
        debug=True
    )
