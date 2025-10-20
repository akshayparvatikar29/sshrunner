from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import sqlite3, os, datetime, paramiko
from werkzeug.utils import secure_filename

app = Flask(__name__)
CORS(app)

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

DB_FILE = 'scriptrunner.db'

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

# --- Initialize DB ---
def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS servers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        host TEXT,
        username TEXT,
        password_enc TEXT,
        keyfile_path TEXT,
        os TEXT DEFAULT 'ubuntu',
        tags TEXT,
        created_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        server_id INTEGER,
        command TEXT,
        status TEXT,
        output TEXT,
        created_at TEXT
    )''')
    conn.commit()
    conn.close()

init_db()

# --- Routes ---
@app.route('/')
def index():
    return send_from_directory('frontend', 'index.html')

@app.route('/static/<path:path>')
def serve_static(path):
    return send_from_directory('static', path)

# --- Server API ---
@app.route('/api/servers/list')
def list_servers():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM servers ORDER BY id DESC")
    servers = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(servers)

@app.route('/api/servers/add', methods=['POST'])
def add_server():
    name = request.form.get('name')
    host = request.form.get('host')
    username = request.form.get('username')
    os_type = request.form.get('os_type', 'ubuntu')
    tags = request.form.get('tags')
    password = request.form.get('password')
    keyfile = request.files.get('keyfile')
    keyfile_path = None
    if keyfile:
        filename = secure_filename(keyfile.filename)
        keyfile_path = os.path.join(UPLOAD_FOLDER, filename)
        keyfile.save(keyfile_path)

    created_at = datetime.datetime.now().isoformat()

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO servers (name, host, username, password_enc, keyfile_path, os, tags, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (name, host, username, password, keyfile_path, os_type, tags, created_at)
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/api/servers/delete/<int:id>', methods=['DELETE'])
def delete_server(id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM servers WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/api/servers/update_tags/<int:id>', methods=['POST'])
def update_tags(id):
    tags = request.json.get('tags', '')
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE servers SET tags=? WHERE id=?", (tags, id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

# --- Run Command ---
@app.route('/api/run', methods=['POST'])
def run_commands():
    data = request.json
    server_ids = data.get('server_ids', [])
    commands = data.get('commands', [])
    if not server_ids or not commands:
        return jsonify({"error": "Missing server_ids or commands"}), 400

    conn = get_db_connection()
    c = conn.cursor()
    placeholders = ','.join(['?']*len(server_ids))
    c.execute(f"SELECT * FROM servers WHERE id IN ({placeholders})", server_ids)
    servers = [dict(row) for row in c.fetchall()]
    conn.close()

    results = []
    for server in servers:
        for cmd_obj in commands:
            if cmd_obj['os'] != server['os']:
                continue
            status = "failed"
            output = ""
            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                if server['keyfile_path']:
                    ssh.connect(server['host'], username=server['username'], key_filename=server['keyfile_path'])
                else:
                    ssh.connect(server['host'], username=server['username'], password=server['password_enc'])
                stdin, stdout, stderr = ssh.exec_command(cmd_obj['cmd'])
                out = stdout.read().decode()
                err = stderr.read().decode()
                output = f"$ {cmd_obj['cmd']}\n\nOUT:\n{out}\nERR:\n{err}"
                status = "ok" if not err.strip() else "failed"
                ssh.close()
            except Exception as e:
                output = str(e)
                status = "failed"

            # Store in DB
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("INSERT INTO runs (server_id, command, status, output, created_at) VALUES (?,?,?,?,?)",
                      (server['id'], cmd_obj['cmd'], status, output, datetime.datetime.now().isoformat()))
            conn.commit()
            conn.close()

            results.append({
                "server_id": server['id'],
                "server_name": server['name'],
                "command": cmd_obj['cmd'],
                "status": status,
                "output": output
            })

    return jsonify(results)

# --- Job History ---
@app.route('/api/runs')
def get_runs():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM runs ORDER BY id DESC")
    runs = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(runs)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
