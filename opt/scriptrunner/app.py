import os
import sqlite3
import json
import datetime
from typing import List
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import paramiko
from cryptography.fernet import Fernet

APP_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(APP_DIR, "scriptrunner.db")
KEY_DIR = os.path.join(APP_DIR, "keys")
os.makedirs(KEY_DIR, exist_ok=True)

# Encryption key
KEY_FILE = os.path.join(APP_DIR, "secret.key")
if not os.path.exists(KEY_FILE):
    key = Fernet.generate_key()
    with open(KEY_FILE, "wb") as f:
        f.write(key)
else:
    key = open(KEY_FILE, "rb").read()
fernet = Fernet(key)

# Initialize DB
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS servers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    host TEXT UNIQUE,
                    username TEXT,
                    password_enc TEXT,
                    keyfile_path TEXT,
                    created_at TEXT,
                    os TEXT DEFAULT 'ubuntu',
                    tags TEXT DEFAULT ''
                )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_id INTEGER,
                    command TEXT,
                    output TEXT,
                    status TEXT,
                    created_at TEXT
                )""")
    conn.commit()
    conn.close()

init_db()

app = FastAPI(title="ScriptRunner API")

# Mount frontend & static
app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "frontend")), name="static")

@app.get("/")
def serve_index():
    return FileResponse(os.path.join(APP_DIR, "frontend/index.html"))

# --- Pydantic Models
class ServerAdd(BaseModel):
    name: str
    host: str
    username: str
    password: str = None
    keyfile_path: str = None
    os: str = "ubuntu"
    tags: str = ""

class RunCommand(BaseModel):
    server_ids: List[int]
    commands: List[dict]  # [{"os":"ubuntu","cmd":"ls"}]
    timeout: int = 30

# --- Helper to run SSH command
def run_ssh_command(host, username, password=None, keyfile=None, command="uptime", timeout=30):
    output = ""
    status = "ok"
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        if keyfile:
            pkey = paramiko.RSAKey.from_private_key_file(keyfile)
            client.connect(hostname=host, username=username, pkey=pkey, timeout=timeout)
        else:
            client.connect(hostname=host, username=username, password=password, timeout=timeout)
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode(errors="ignore")
        err = stderr.read().decode(errors="ignore")
        output = out + ("\nERR:\n" + err if err else "")
        client.close()
    except Exception as e:
        output = str(e)
        status = "error"
    return status, output

# --- API Endpoints ---

# ✅ Add new server
@app.post("/api/servers/add")
async def add_server(
    name: str = Form(...),
    host: str = Form(...),
    username: str = Form(...),
    os_type: str = Form("ubuntu"),
    tags: str = Form(""),
    password: str = Form(None),
    keyfile: UploadFile = File(None)
):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Check duplicate
    cur.execute("SELECT id FROM servers WHERE host=? OR name=?", (host, name))
    existing = cur.fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=400, detail="Server with same host or name already exists.")

    keyfile_path = None
    if keyfile:
        filename = f"{name}_{int(datetime.datetime.utcnow().timestamp())}.pem"
        keyfile_path = os.path.join(KEY_DIR, filename)
        with open(keyfile_path, "wb") as f:
            f.write(await keyfile.read())
        os.chmod(keyfile_path, 0o600)

    pwd_enc = fernet.encrypt(password.encode()).decode() if password else None

    cur.execute("""INSERT INTO servers (name, host, username, password_enc, keyfile_path, tags, os, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, host, username, pwd_enc, keyfile_path, tags, os_type, datetime.datetime.utcnow().isoformat()))
    conn.commit()
    server_id = cur.lastrowid
    conn.close()

    return {"status": "ok", "server_id": server_id}

# ✅ List all servers
@app.get("/api/servers/list")
def list_servers(tag: str = None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if tag:
        cur.execute("SELECT id, name, host, username, keyfile_path, created_at, os, tags FROM servers WHERE tags LIKE ? ORDER BY id DESC", ('%' + tag + '%',))
    else:
        cur.execute("SELECT id, name, host, username, keyfile_path, created_at, os, tags FROM servers ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    res = []
    for r in rows:
        res.append({
            "id": r[0], "name": r[1], "host": r[2], "username": r[3],
            "keyfile_path": r[4], "created_at": r[5],
            "os": r[6], "tags": r[7]
        })
    return res

# ✅ Delete a server
@app.delete("/api/servers/delete/{server_id}")
def delete_server(server_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT keyfile_path FROM servers WHERE id=?", (server_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Server not found")

    keyfile_path = row[0]
    if keyfile_path and os.path.exists(keyfile_path):
        try: os.remove(keyfile_path)
        except Exception: pass

    cur.execute("DELETE FROM runs WHERE server_id=?", (server_id,))
    cur.execute("DELETE FROM servers WHERE id=?", (server_id,))
    conn.commit()
    conn.close()
    return {"status": "ok", "message": f"Server {server_id} deleted successfully."}

# ✅ Run commands
@app.post("/api/run")
def run_command(payload: RunCommand):
    results = []
    for sid in payload.server_ids:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT host, username, password_enc, keyfile_path, os FROM servers WHERE id=?", (sid,))
        row = cur.fetchone()
        conn.close()
        if not row:
            results.append({"server_id": sid, "status": "error", "output": "Server not found"})
            continue

        host, username, password_enc, keyfile_path, server_os = row
        password = fernet.decrypt(password_enc.encode()).decode() if password_enc else None

        server_output = ""
        server_status = "ok"
        for cmd_obj in payload.commands:
            if not isinstance(cmd_obj, dict) or "os" not in cmd_obj or "cmd" not in cmd_obj:
                continue
            if cmd_obj["os"] != server_os:
                continue
            status, output = run_ssh_command(host, username, password=password, keyfile=keyfile_path, command=cmd_obj["cmd"], timeout=payload.timeout)
            server_output += f"$ {cmd_obj['cmd']}\n{output}\n"
            if status != "ok": server_status = status

        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""INSERT INTO runs (server_id, command, output, status, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (sid, json.dumps(payload.commands), server_output, server_status, datetime.datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()

        results.append({"server_id": sid, "status": server_status, "output": server_output})
    return results

# ✅ List run history
@app.get("/api/runs")
def list_runs(limit: int = 50):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, server_id, command, status, created_at FROM runs ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    res = []
    for r in rows:
        res.append({"id": r[0], "server_id": r[1], "command": r[2], "status": r[3], "created_at": r[4]})
    return res
