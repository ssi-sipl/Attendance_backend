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

conn.commit()


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
        return jsonify({
            "status": False,
            "message": "Internal server error",
            "data": {}
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

        users = []
        for r in rows:
            users.append({
                "user_id": r[0],
                "name": r[1],
                "status": r[2]
            })

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
        return jsonify({
            "status": False,
            "message": "Internal server error",
            "data": {}
        }), 500


# ---------------- ENROLL ----------------
@app.route("/api/enroll", methods=["POST"])
def enroll():
    try:
        data = request.json
        name = data.get("name")

        if not name:
            return jsonify({
                "status": False,
                "message": "Name is required",
                "data": {}
            }), 400

        f = get_sensor()
        if not f:
            return jsonify({
                "status": False,
                "message": "Sensor not found",
                "data": {}
            }), 500

        while not f.readImage():
            pass

        f.convertImage(0x01)
        result = f.searchTemplate()

        if result[0] >= 0:
            return jsonify({
                "status": False,
                "message": "Fingerprint already exists",
                "data": {}
            }), 409

        time.sleep(2)

        while not f.readImage():
            pass

        f.convertImage(0x02)

        if f.compareCharacteristics() == 0:
            return jsonify({
                "status": False,
                "message": "Fingerprint mismatch",
                "data": {}
            }), 400

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
            "data": {
                "user_id": user_id,
                "finger_id": finger_id
            }
        }), 200

    except Exception as e:
        return jsonify({
            "status": False,
            "message": str(e),
            "data": {}
        }), 500


# ---------------- DELETE USER ----------------
@app.route("/api/users/<int:user_id>", methods=["DELETE"])
def delete_user(user_id):
    try:
        cursor.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
        user = cursor.fetchone()

        if not user:
            return jsonify({
                "status": False,
                "message": "User not found",
                "data": {}
            }), 404

        success = delete_all(user_id)

        if not success:
            return jsonify({
                "status": False,
                "message": "Failed to delete user",
                "data": {}
            }), 500

        return jsonify({
            "status": True,
            "message": "User deleted successfully",
            "data": {
                "user_id": user_id
            }
        }), 200

    except Exception:
        return jsonify({
            "status": False,
            "message": "Internal server error",
            "data": {}
        }), 500


# ---------------- SOCKET ----------------
@socketio.on("connect")
def handle_connect():
    emit("status", {
        "message": "Socket connected"
    })


# ---------------- RUN ----------------
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
