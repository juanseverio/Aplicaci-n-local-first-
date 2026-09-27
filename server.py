from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import platform
import re
import secrets
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import sqlite3
import threading
import time
import uuid
import webbrowser
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

APP_VERSION = "5.3.0"
APP_NAME = "Presencialidad"
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
SOURCE_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
ROOT = RESOURCE_ROOT

def default_data_dir() -> Path:
    override = os.environ.get("PRESENCIALIDAD_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "Presencialidad"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Presencialidad"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "Presencialidad"

DATA_DIR = default_data_dir()
DB_PATH = DATA_DIR / "presencialidad.db"
BACKUP_DIR = DATA_DIR / "backups"
LOG_DIR = DATA_DIR / "logs"
UPLOAD_DIR = DATA_DIR / "uploads"
STARTED_AT = time.time()
REQUEST_COUNT = 0
REQUEST_LOCK = threading.Lock()
SESSION_COOKIE = "presencialidad_session"
DEFAULT_SESSION_HOURS = 8
PBKDF2_ITERATIONS = 310_000
GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
GMAIL_SEND_ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GOOGLE_PROFILE_SCOPES = ["openid", "email", "profile"]
GOOGLE_GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"

ROLE_LABELS = {
    "docente": "Docente",
    "preceptor": "Preceptor",
    "directivo": "Directivo",
    "administrador": "Administrador",
}
REPORT_TYPES = {"conducta", "convivencia", "academica", "inasistencias", "tardanzas", "salud", "otro"}
REPORT_PRIORITIES = {"informativo", "seguimiento", "prioritario"}
REPORT_STATUSES = {"enviado", "en_revision", "atendido", "cerrado"}
ATTENDANCE_STATUSES = {"present", "absent", "late", "justified", "early_departure"}
SCHEDULE_KINDS = {"class", "assessment", "meeting", "reminder"}
NOTICE_STATUSES = {"generated", "sent"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def increment_requests() -> int:
    global REQUEST_COUNT
    with REQUEST_LOCK:
        REQUEST_COUNT += 1
        return REQUEST_COUNT


def connect_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def password_hash(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return base64.b64encode(salt).decode("ascii"), base64.b64encode(digest).decode("ascii")


def verify_password(password: str, salt_b64: str, digest_b64: str) -> bool:
    try:
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(digest_b64.encode("ascii"))
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(actual, expected)


def table_exists(db: sqlite3.Connection, name: str) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def table_columns(db: sqlite3.Connection, name: str) -> set[str]:
    if not table_exists(db, name):
        return set()
    return {r[1] for r in db.execute(f"PRAGMA table_info({name})").fetchall()}

def ensure_column(db: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in table_columns(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

def set_data_dir(path: Path) -> None:
    global DATA_DIR, DB_PATH, BACKUP_DIR, LOG_DIR, UPLOAD_DIR
    DATA_DIR = path.expanduser().resolve()
    DB_PATH = DATA_DIR / "presencialidad.db"
    BACKUP_DIR = DATA_DIR / "backups"
    LOG_DIR = DATA_DIR / "logs"
    UPLOAD_DIR = DATA_DIR / "uploads"

def ensure_runtime_dirs() -> None:
    for folder in (DATA_DIR, BACKUP_DIR, LOG_DIR, UPLOAD_DIR):
        folder.mkdir(parents=True, exist_ok=True)

def append_log(level: str, message: str) -> None:
    try:
        ensure_runtime_dirs()
        line = f"{iso_now()} [{level.upper()}] {message}\n"
        with (LOG_DIR / "presencialidad.log").open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass

def parse_bool(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1","true","yes","si","sí","on"}

def valid_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value or ""))

def get_public_base_url(db: sqlite3.Connection, handler=None) -> str:
    configured = get_setting(db, "public_base_url", "").strip().rstrip("/")
    if configured:
        return configured
    if handler is not None:
        host = handler.headers.get("Host", "127.0.0.1:8000")
        proto = handler.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip() or "http"
        return f"{proto}://{host}"
    return "http://127.0.0.1:8765"

def backup_filename(prefix: str = "Presencialidad_Backup") -> str:
    return f"{prefix}_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.db"

def import_database_file(source: Path) -> None:
    source=source.expanduser().resolve()
    if not source.exists() or not source.is_file():
        raise ValueError(f"No existe la base indicada: {source}")
    ensure_runtime_dirs()
    if DB_PATH.exists():
        raise ValueError("La base de destino ya existe; no se importó otra base encima.")
    probe=sqlite3.connect(source)
    target=sqlite3.connect(DB_PATH)
    try:
        check=probe.execute("PRAGMA integrity_check").fetchone()
        if not check or check[0]!="ok":raise ValueError("La base anterior no supera la comprobación de integridad.")
        probe.backup(target)
    finally:
        target.close();probe.close()
    append_log("INFO",f"Base importada desde {source}")

def maybe_import_legacy_database() -> None:
    if DB_PATH.exists():return
    for candidate in (SOURCE_ROOT/"data"/"presencialidad.db",SOURCE_ROOT/"presencialidad.db"):
        if candidate.exists() and candidate.resolve()!=DB_PATH.resolve():
            try:import_database_file(candidate);return
            except Exception as exc:append_log("WARN",f"No se pudo importar automáticamente {candidate}: {exc}")

def create_database_backup(*, prefix: str = "Presencialidad_Backup") -> Path:
    ensure_runtime_dirs()
    target = BACKUP_DIR / backup_filename(prefix)
    src = connect_db()
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close(); src.close()
    return target

def list_backups() -> list[dict]:
    ensure_runtime_dirs()
    result=[]
    for f in sorted(BACKUP_DIR.glob("*.db"), key=lambda x:x.stat().st_mtime, reverse=True):
        st=f.stat(); result.append({"name":f.name,"bytes":st.st_size,"modified_at":datetime.fromtimestamp(st.st_mtime,timezone.utc).isoformat(timespec="seconds")})
    return result[:100]

def prune_backups(keep: int = 30) -> None:
    files=sorted(BACKUP_DIR.glob("*.db"), key=lambda x:x.stat().st_mtime, reverse=True)
    for f in files[max(1,keep):]:
        try:f.unlink()
        except OSError:pass

def sqlite_integrity() -> str:
    try:
        with connect_db() as db:
            row=db.execute("PRAGMA integrity_check").fetchone()
            return str(row[0] if row else "unknown")
    except Exception as exc:
        return f"error: {exc}"

def _dpapi_available() -> bool:
    return os.name == "nt"

def _fernet_instance():
    key=os.environ.get("PRESENCIALIDAD_TOKEN_KEY","").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode("ascii"))
    except Exception:
        return None

def protect_secret(value: str) -> str:
    if not value:
        return ""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class DATA_BLOB(ctypes.Structure):
            _fields_=[("cbData",wintypes.DWORD),("pbData",ctypes.POINTER(ctypes.c_byte))]
        raw=value.encode("utf-8"); buf=ctypes.create_string_buffer(raw); in_blob=DATA_BLOB(len(raw),ctypes.cast(buf,ctypes.POINTER(ctypes.c_byte))); out_blob=DATA_BLOB()
        if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(in_blob),"Presencialidad",None,None,None,0,ctypes.byref(out_blob)):
            raise RuntimeError("Windows no pudo proteger el secreto.")
        try:
            encrypted=ctypes.string_at(out_blob.pbData,out_blob.cbData)
            return "dpapi:"+base64.urlsafe_b64encode(encrypted).decode("ascii")
        finally:
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    f=_fernet_instance()
    if f is not None:
        return "fernet:"+f.encrypt(value.encode("utf-8")).decode("ascii")
    raise RuntimeError("No hay un almacén seguro para secretos. En Windows se usa DPAPI; en servidor configurá PRESENCIALIDAD_TOKEN_KEY y el paquete cryptography.")

def unprotect_secret(value: str) -> str:
    if not value:
        return ""
    if value.startswith("dpapi:") and os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class DATA_BLOB(ctypes.Structure):
            _fields_=[("cbData",wintypes.DWORD),("pbData",ctypes.POINTER(ctypes.c_byte))]
        encrypted=base64.urlsafe_b64decode(value[6:].encode("ascii")); buf=ctypes.create_string_buffer(encrypted); in_blob=DATA_BLOB(len(encrypted),ctypes.cast(buf,ctypes.POINTER(ctypes.c_byte))); out_blob=DATA_BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(in_blob),None,None,None,None,0,ctypes.byref(out_blob)):
            raise RuntimeError("No se pudo abrir el secreto protegido.")
        try:return ctypes.string_at(out_blob.pbData,out_blob.cbData).decode("utf-8")
        finally:ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    if value.startswith("fernet:"):
        f=_fernet_instance()
        if f is None:raise RuntimeError("Falta la clave segura PRESENCIALIDAD_TOKEN_KEY.")
        try:return f.decrypt(value[7:].encode("ascii")).decode("utf-8")
        except Exception as exc:raise RuntimeError("No se pudo abrir el secreto protegido del servidor.") from exc
    return ""

def google_secret_storage_available() -> bool:
    return _dpapi_available() or _fernet_instance() is not None

def http_json(url: str, *, method: str = "GET", data: dict | None = None, headers: dict | None = None, timeout: int = 20) -> dict:
    body=None; req_headers={"Accept":"application/json", **(headers or {})}
    if data is not None:
        body=urllib.parse.urlencode(data).encode("utf-8")
        req_headers.setdefault("Content-Type","application/x-www-form-urlencoded")
    req=urllib.request.Request(url,data=body,headers=req_headers,method=method)
    try:
        with urllib.request.urlopen(req,timeout=timeout) as response:
            raw=response.read()
    except urllib.error.HTTPError as exc:
        raw=exc.read()
        try: detail=json.loads(raw.decode("utf-8"))
        except Exception: detail={"error":raw.decode("utf-8",errors="replace")[:500]}
        raise ValueError(detail.get("error_description") or detail.get("error") or f"Google respondió {exc.code}")
    return json.loads(raw.decode("utf-8")) if raw else {}

def google_client_config(db: sqlite3.Connection) -> tuple[str,str]:
    client_id=os.environ.get("PRESENCIALIDAD_GOOGLE_CLIENT_ID","").strip() or get_setting(db,"google_client_id","").strip()
    secret=os.environ.get("PRESENCIALIDAD_GOOGLE_CLIENT_SECRET","").strip()
    if not secret:
        stored=get_setting(db,"google_client_secret_protected","")
        if stored:
            try:secret=unprotect_secret(stored)
            except Exception:secret=""
    return client_id,secret

def google_redirect_uri(db: sqlite3.Connection, handler=None) -> str:
    return get_public_base_url(db,handler)+"/oauth/google/callback"

def pkce_pair() -> tuple[str,str]:
    verifier=secrets.token_urlsafe(64)[:96]
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    return verifier,challenge

def google_auth_url(db: sqlite3.Connection, user_id: str, mode: str, handler=None) -> str:
    client_id,_=google_client_config(db)
    if not client_id:
        raise ValueError("Google OAuth todavía no está configurado por la institución.")
    if not google_secret_storage_available():
        raise ValueError("La conexión con Google requiere almacenamiento seguro: Windows usa DPAPI; en servidor configurá PRESENCIALIDAD_TOKEN_KEY con cryptography.")
    scopes=list(GOOGLE_PROFILE_SCOPES)
    if mode=="gmail":scopes.append(GOOGLE_GMAIL_SCOPE)
    verifier,challenge=pkce_pair(); state=secrets.token_urlsafe(32); expires=(now_utc()+timedelta(minutes=10)).isoformat(timespec="seconds")
    db.execute("DELETE FROM oauth_states WHERE expires_at<?",(iso_now(),))
    db.execute("INSERT INTO oauth_states(state,user_id,mode,code_verifier,expires_at) VALUES(?,?,?,?,?)",(state,user_id,mode,verifier,expires))
    params={"client_id":client_id,"redirect_uri":google_redirect_uri(db,handler),"response_type":"code","scope":" ".join(scopes),"access_type":"offline","include_granted_scopes":"true","prompt":"consent","state":state,"code_challenge":challenge,"code_challenge_method":"S256"}
    return GOOGLE_AUTH_ENDPOINT+"?"+urllib.parse.urlencode(params)

def google_tokens_for_user(db: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM google_accounts WHERE user_id=?",(user_id,)).fetchone()

def refresh_google_access_token(db: sqlite3.Connection, user_id: str) -> tuple[str,sqlite3.Row]:
    account=google_tokens_for_user(db,user_id)
    if not account:raise ValueError("No hay una cuenta Google conectada.")
    access=""
    try:access=unprotect_secret(account["access_token_protected"] or "")
    except Exception:access=""
    expires_at=account["access_token_expires_at"] or ""
    if access and expires_at:
        try:
            if datetime.fromisoformat(expires_at)>now_utc()+timedelta(seconds=60):return access,account
        except ValueError:pass
    refresh=unprotect_secret(account["refresh_token_protected"] or "")
    if not refresh:raise ValueError("Google requiere volver a autorizar la cuenta.")
    client_id,client_secret=google_client_config(db)
    payload={"client_id":client_id,"refresh_token":refresh,"grant_type":"refresh_token"}
    if client_secret:payload["client_secret"]=client_secret
    token=http_json(GOOGLE_TOKEN_ENDPOINT,method="POST",data=payload)
    access=token.get("access_token","")
    if not access:raise ValueError("Google no devolvió un token de acceso.")
    expiry=(now_utc()+timedelta(seconds=int(token.get("expires_in",3600)))).isoformat(timespec="seconds")
    db.execute("UPDATE google_accounts SET access_token_protected=?,access_token_expires_at=?,updated_at=? WHERE user_id=?",(protect_secret(access),expiry,iso_now(),user_id))
    account=db.execute("SELECT * FROM google_accounts WHERE user_id=?",(user_id,)).fetchone()
    return access,account

def send_gmail(db: sqlite3.Connection, user_id: str, *, to_email: str, subject: str, body_text: str) -> dict:
    if not valid_email(to_email):raise ValueError("El correo destinatario no es válido.")
    access,account=refresh_google_access_token(db,user_id)
    scopes=set((account["scopes"] or "").split())
    if GOOGLE_GMAIL_SCOPE not in scopes:raise ValueError("La cuenta Google no autorizó el envío mediante Gmail.")
    import email.message
    msg=email.message.EmailMessage();msg["To"]=to_email;msg["From"]=account["email"] or "me";msg["Subject"]=subject;msg.set_content(body_text)
    raw=base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")
    req=urllib.request.Request(GMAIL_SEND_ENDPOINT,data=json.dumps({"raw":raw}).encode("utf-8"),headers={"Authorization":f"Bearer {access}","Content-Type":"application/json","Accept":"application/json"},method="POST")
    try:
        with urllib.request.urlopen(req,timeout=20) as response:payload=json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail=exc.read().decode("utf-8",errors="replace")[:500];raise ValueError(f"Gmail rechazó el envío ({exc.code}): {detail}")
    return payload


def ensure_attendance_schema(db: sqlite3.Connection) -> None:
    """Upgrade the old v3 attendance table while preserving records."""
    if not table_exists(db, "attendance"):
        db.execute(
            """CREATE TABLE attendance (
                id TEXT PRIMARY KEY,
                date TEXT NOT NULL,
                student_id TEXT NOT NULL REFERENCES students(id),
                classroom_id TEXT NOT NULL REFERENCES classrooms(id),
                status TEXT NOT NULL CHECK(status IN ('present','absent','late','justified','early_departure')),
                reason TEXT NOT NULL DEFAULT '',
                arrival_time TEXT NOT NULL DEFAULT '',
                departure_time TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL REFERENCES users(id),
                updated_by TEXT NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(date, student_id)
            )"""
        )
        return

    columns = table_columns(db, "attendance")
    table_sql_row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='attendance'").fetchone()
    table_sql = (table_sql_row[0] or "") if table_sql_row else ""
    needed = {"reason", "arrival_time", "departure_time", "created_by", "created_at"}
    if needed.issubset(columns) and "justified" in table_sql and "early_departure" in table_sql:
        return

    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("ALTER TABLE attendance RENAME TO attendance_v3_backup")
    db.execute(
        """CREATE TABLE attendance (
            id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            student_id TEXT NOT NULL REFERENCES students(id),
            classroom_id TEXT NOT NULL REFERENCES classrooms(id),
            status TEXT NOT NULL CHECK(status IN ('present','absent','late','justified','early_departure')),
            reason TEXT NOT NULL DEFAULT '',
            arrival_time TEXT NOT NULL DEFAULT '',
            departure_time TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL REFERENCES users(id),
            updated_by TEXT NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(date, student_id)
        )"""
    )
    old_cols = table_columns(db, "attendance_v3_backup")
    rows = db.execute("SELECT * FROM attendance_v3_backup").fetchall()
    for r in rows:
        updated_by = r["updated_by"] if "updated_by" in old_cols else None
        if not updated_by:
            continue
        updated_at = r["updated_at"] if "updated_at" in old_cols else iso_now()
        db.execute(
            """INSERT OR REPLACE INTO attendance(
                id,date,student_id,classroom_id,status,reason,arrival_time,departure_time,
                created_by,updated_by,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                r["id"], r["date"], r["student_id"], r["classroom_id"], r["status"],
                "", "", "", updated_by, updated_by, updated_at, updated_at,
            ),
        )
    db.execute("DROP TABLE attendance_v3_backup")
    db.execute("PRAGMA foreign_keys=ON")


def init_db() -> None:
    with connect_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('docente','preceptor','directivo','administrador')),
                institution TEXT NOT NULL DEFAULT '',
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                last_login TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS classrooms (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_classrooms (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                classroom_id TEXT NOT NULL REFERENCES classrooms(id) ON DELETE CASCADE,
                PRIMARY KEY(user_id, classroom_id)
            );

            CREATE TABLE IF NOT EXISTS students (
                id TEXT PRIMARY KEY,
                record_number TEXT NOT NULL UNIQUE COLLATE NOCASE,
                name TEXT NOT NULL,
                classroom_id TEXT NOT NULL REFERENCES classrooms(id),
                dni TEXT NOT NULL DEFAULT '',
                birth_date TEXT NOT NULL DEFAULT '',
                guardian_name TEXT NOT NULL DEFAULT '',
                guardian_phone TEXT NOT NULL DEFAULT '',
                emergency_name TEXT NOT NULL DEFAULT '',
                emergency_phone TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                student_id TEXT NOT NULL REFERENCES students(id),
                classroom_id TEXT NOT NULL REFERENCES classrooms(id),
                reporter_id TEXT NOT NULL REFERENCES users(id),
                type TEXT NOT NULL,
                description TEXT NOT NULL,
                priority TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'enviado',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS report_comments (
                id TEXT PRIMARY KEY,
                report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
                author_id TEXT NOT NULL REFERENCES users(id),
                comment TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS teacher_notes (
                id TEXT PRIMARY KEY,
                student_id TEXT NOT NULL REFERENCES students(id),
                classroom_id TEXT NOT NULL REFERENCES classrooms(id),
                author_id TEXT NOT NULL REFERENCES users(id),
                note TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS assessments (
                id TEXT PRIMARY KEY,
                classroom_id TEXT NOT NULL REFERENCES classrooms(id),
                creator_id TEXT NOT NULL REFERENCES users(id),
                title TEXT NOT NULL,
                assessment_date TEXT NOT NULL,
                max_score REAL NOT NULL DEFAULT 10,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS grades (
                id TEXT PRIMARY KEY,
                assessment_id TEXT NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
                student_id TEXT NOT NULL REFERENCES students(id),
                score REAL,
                comment TEXT NOT NULL DEFAULT '',
                updated_by TEXT NOT NULL REFERENCES users(id),
                updated_at TEXT NOT NULL,
                UNIQUE(assessment_id, student_id)
            );

            CREATE TABLE IF NOT EXISTS schedule_items (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                classroom_id TEXT REFERENCES classrooms(id) ON DELETE SET NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                event_date TEXT NOT NULL,
                start_time TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                repeat_weekly INTEGER NOT NULL DEFAULT 0,
                weekday INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS family_notices (
                id TEXT PRIMARY KEY,
                student_id TEXT NOT NULL REFERENCES students(id),
                classroom_id TEXT NOT NULL REFERENCES classrooms(id),
                author_id TEXT NOT NULL REFERENCES users(id),
                reason TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'generated',
                created_at TEXT NOT NULL,
                sent_at TEXT
            );

            CREATE TABLE IF NOT EXISTS attendance_drafts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                classroom_id TEXT NOT NULL REFERENCES classrooms(id) ON DELETE CASCADE,
                date TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, classroom_id, date)
            );

            CREATE TABLE IF NOT EXISTS attendance_audit (
                id TEXT PRIMARY KEY,
                attendance_id TEXT,
                date TEXT NOT NULL,
                student_id TEXT NOT NULL REFERENCES students(id),
                classroom_id TEXT NOT NULL REFERENCES classrooms(id),
                old_status TEXT,
                new_status TEXT NOT NULL,
                old_details TEXT NOT NULL DEFAULT '{}',
                new_details TEXT NOT NULL DEFAULT '{}',
                changed_by TEXT NOT NULL REFERENCES users(id),
                changed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                user_agent TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS activity (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                action TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        ensure_attendance_schema(db)
        db.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_attendance_date_classroom ON attendance(date, classroom_id);
            CREATE INDEX IF NOT EXISTS idx_attendance_student ON attendance(student_id, date);
            CREATE INDEX IF NOT EXISTS idx_students_classroom ON students(classroom_id);
            CREATE INDEX IF NOT EXISTS idx_reports_reporter ON reports(reporter_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_reports_classroom ON reports(classroom_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_notes_student ON teacher_notes(student_id, author_id);
            CREATE INDEX IF NOT EXISTS idx_assessments_classroom ON assessments(classroom_id, assessment_date);
            CREATE INDEX IF NOT EXISTS idx_schedule_owner ON schedule_items(owner_id, event_date);
            CREATE INDEX IF NOT EXISTS idx_notices_student ON family_notices(student_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_audit_student ON attendance_audit(student_id, changed_at);
            CREATE INDEX IF NOT EXISTS idx_activity_user ON activity(user_id, created_at);
            """
        )
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('attendance_threshold','75')")
        # v5 starts with no predefined users. The first-run wizard creates the first administrator.
        ensure_column(db, "users", "avatar_url TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "users", "preferred_language TEXT NOT NULL DEFAULT 'es'")
        ensure_column(db, "students", "guardian_email TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "classrooms", "archived INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "sessions", "csrf_token TEXT NOT NULL DEFAULT ''")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS google_accounts (
                user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                email TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                picture_url TEXT NOT NULL DEFAULT '',
                scopes TEXT NOT NULL DEFAULT '',
                refresh_token_protected TEXT NOT NULL DEFAULT '',
                access_token_protected TEXT NOT NULL DEFAULT '',
                access_token_expires_at TEXT NOT NULL DEFAULT '',
                connected_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_states (
                state TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                mode TEXT NOT NULL,
                code_verifier TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS login_attempts (
                key TEXT PRIMARY KEY,
                attempts INTEGER NOT NULL DEFAULT 0,
                first_at TEXT NOT NULL,
                blocked_until TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
        """)
        db.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(520,?)", (iso_now(),))
        defaults={
            "institution_name":"", "academic_year":str(datetime.now().year), "education_level":"", "shifts":"",
            "attendance_threshold":"75", "absence_alert_count":"3", "late_alert_count":"5",
            "auto_backup_enabled":"1", "backup_interval_hours":"24", "backup_keep":"30", "session_hours":"8",
            "public_base_url":"", "google_client_id":"", "google_client_secret_protected":"",
            "default_language":"es", "setup_complete":"0"
        }
        for key,value in defaults.items():db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",(key,value))


def create_user(db: sqlite3.Connection, *, email: str, name: str, role: str, password: str,
                institution: str = "", active: bool = True, user_id: str | None = None, preferred_language: str = "es") -> str:
    salt, digest = password_hash(password)
    uid = user_id or new_id("user")
    db.execute(
        "INSERT INTO users(id,email,name,role,institution,password_salt,password_hash,active,created_at,preferred_language) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (uid, email.strip().lower(), name.strip(), role, institution.strip(), salt, digest, int(active), iso_now(), preferred_language if preferred_language in {"es","en","ja","ko","zh","ru","ar"} else "es"),
    )
    return uid


def seed_test_users(db: sqlite3.Connection) -> None:
    """Compatibility no-op. v5.3.0 never creates default users."""
    return


def log_activity(db: sqlite3.Connection, user_id: str, action: str, details: str) -> None:
    db.execute(
        "INSERT INTO activity(id,user_id,action,details,created_at) VALUES(?,?,?,?,?)",
        (new_id("activity"), user_id, action, details, iso_now()),
    )


def role_can_admin(user: sqlite3.Row | dict) -> bool:
    return user["role"] in {"directivo", "administrador"}


def role_can_review_reports(user: sqlite3.Row | dict) -> bool:
    return user["role"] in {"preceptor", "directivo", "administrador"}


def role_can_view_contacts(user: sqlite3.Row | dict) -> bool:
    return user["role"] in {"preceptor", "directivo", "administrador"}


def role_can_edit_grades(user: sqlite3.Row | dict) -> bool:
    return user["role"] in {"docente", "directivo", "administrador"}


def user_has_classroom(db: sqlite3.Connection, user: sqlite3.Row | dict, classroom_id: str) -> bool:
    if user["role"] in {"directivo", "administrador"}:
        return bool(db.execute("SELECT 1 FROM classrooms WHERE id=? AND archived=0", (classroom_id,)).fetchone())
    return bool(db.execute("SELECT 1 FROM user_classrooms uc JOIN classrooms c ON c.id=uc.classroom_id WHERE uc.user_id=? AND uc.classroom_id=? AND c.archived=0", (user["id"], classroom_id)).fetchone())


def classroom_ids_for_user(db: sqlite3.Connection, user: sqlite3.Row | dict) -> list[str]:
    if user["role"] in {"directivo", "administrador"}:
        return [r[0] for r in db.execute("SELECT id FROM classrooms WHERE archived=0 ORDER BY name COLLATE NOCASE")]
    return [r[0] for r in db.execute("SELECT uc.classroom_id FROM user_classrooms uc JOIN classrooms c ON c.id=uc.classroom_id WHERE uc.user_id=? AND c.archived=0", (user["id"],))]


def user_public(db: sqlite3.Connection, user: sqlite3.Row | dict) -> dict:
    if user["role"] in {"directivo", "administrador"}:
        classroom_rows = db.execute("SELECT id,name FROM classrooms WHERE archived=0 ORDER BY name COLLATE NOCASE").fetchall()
    else:
        classroom_rows = db.execute(
            """SELECT c.id,c.name FROM classrooms c JOIN user_classrooms uc ON uc.classroom_id=c.id
               WHERE uc.user_id=? AND c.archived=0 ORDER BY c.name COLLATE NOCASE""", (user["id"],)
        ).fetchall()
    return {
        "id": user["id"], "email": user["email"], "name": user["name"], "role": user["role"],
        "institution": user["institution"], "last_login": user["last_login"], "active": bool(user["active"]),
        "avatar_url": user["avatar_url"] if "avatar_url" in user.keys() else "",
        "preferred_language": user["preferred_language"] if "preferred_language" in user.keys() else "es",
        "classrooms": [{"id": r["id"], "name": r["name"]} for r in classroom_rows],
        "permissions": {
            "admin": role_can_admin(user),
            "review_reports": role_can_review_reports(user),
            "view_contacts": role_can_view_contacts(user),
            "clear_history": role_can_admin(user),
            "edit_grades": role_can_edit_grades(user),
        },
    }


def student_protection_counts(db: sqlite3.Connection, student_id: str) -> tuple[int, int]:
    reports = db.execute("SELECT COUNT(*) FROM reports WHERE student_id=?", (student_id,)).fetchone()[0]
    notes = db.execute("SELECT COUNT(*) FROM teacher_notes WHERE student_id=?", (student_id,)).fetchone()[0]
    return reports, notes


def student_public(db: sqlite3.Connection, row: sqlite3.Row, user: sqlite3.Row | dict) -> dict:
    report_count, note_count = student_protection_counts(db, row["id"])
    data = {
        "id": row["id"], "record_number": row["record_number"], "name": row["name"],
        "classroom_id": row["classroom_id"], "classroom_name": row["classroom_name"],
        "birth_date": row["birth_date"], "active": bool(row["active"]), "report_count": report_count,
        "note_count": note_count if role_can_admin(user) else None,
        "protected": bool(report_count or note_count),
    }
    if role_can_view_contacts(user):
        data.update({
            "dni": row["dni"], "guardian_name": row["guardian_name"], "guardian_phone": row["guardian_phone"],
            "guardian_email": row["guardian_email"] if "guardian_email" in row.keys() else "",
            "emergency_name": row["emergency_name"], "emergency_phone": row["emergency_phone"],
        })
    else:
        data.update({"dni": "", "guardian_name": "", "guardian_phone": "", "guardian_email": "", "emergency_name": "", "emergency_phone": ""})
    return data


def session_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def cleanup_sessions(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (iso_now(),))


def normalize_time(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("La hora debe usar el formato HH:MM.")
    return value


def validate_student_payload(data: dict) -> dict:
    payload = {
        "record_number": str(data.get("record_number", "")).strip(),
        "name": str(data.get("name", "")).strip(),
        "classroom_id": str(data.get("classroom_id", "")).strip(),
        "dni": str(data.get("dni", "")).strip(),
        "birth_date": str(data.get("birth_date", "")).strip(),
        "guardian_name": str(data.get("guardian_name", "")).strip(),
        "guardian_phone": str(data.get("guardian_phone", "")).strip(),
        "guardian_email": str(data.get("guardian_email", "")).strip().lower(),
        "emergency_name": str(data.get("emergency_name", "")).strip(),
        "emergency_phone": str(data.get("emergency_phone", "")).strip(),
    }
    if not payload["record_number"] or not payload["name"] or not payload["classroom_id"]:
        raise ValueError("Legajo, nombre y aula son obligatorios.")
    if payload["dni"] and not re.fullmatch(r"[0-9]{7,9}", payload["dni"]):
        raise ValueError("El DNI debe contener únicamente entre 7 y 9 números.")
    phone_pattern = re.compile(r"[0-9+() .-]{6,30}")
    for field in ("guardian_phone", "emergency_phone"):
        if payload[field] and not phone_pattern.fullmatch(payload[field]):
            raise ValueError("Los teléfonos solo pueden contener números y los símbolos + ( ) . -")
    if payload["guardian_email"] and not valid_email(payload["guardian_email"]):
        raise ValueError("El correo del contacto familiar no es válido.")
    return payload


def set_user_classrooms(db: sqlite3.Connection, user_id: str, classroom_ids) -> None:
    if not isinstance(classroom_ids, list):
        classroom_ids = []
    db.execute("DELETE FROM user_classrooms WHERE user_id=?", (user_id,))
    for classroom_id in dict.fromkeys(str(x) for x in classroom_ids):
        if db.execute("SELECT 1 FROM classrooms WHERE id=? AND archived=0", (classroom_id,)).fetchone():
            db.execute("INSERT INTO user_classrooms(user_id,classroom_id) VALUES(?,?)", (user_id, classroom_id))


def report_visible_to_user(db: sqlite3.Connection, user: sqlite3.Row | dict, report: sqlite3.Row | dict) -> bool:
    if user["role"] == "docente":
        return report["reporter_id"] == user["id"]
    if user["role"] == "preceptor":
        return user_has_classroom(db, user, report["classroom_id"])
    return user["role"] in {"directivo", "administrador"}


def get_setting(db: sqlite3.Connection, key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def attendance_alerts(db: sqlite3.Connection, user: sqlite3.Row | dict, *, limit: int = 100) -> list[dict]:
    allowed = classroom_ids_for_user(db, user)
    if not allowed:
        return []
    placeholders = ",".join("?" for _ in allowed)
    students = db.execute(
        f"""SELECT s.id,s.name,s.record_number,s.classroom_id,c.name AS classroom_name
            FROM students s JOIN classrooms c ON c.id=s.classroom_id
            WHERE s.active=1 AND s.classroom_id IN ({placeholders}) ORDER BY s.name COLLATE NOCASE""", allowed
    ).fetchall()
    threshold = float(get_setting(db, "attendance_threshold", "75") or 75)
    absence_alert_count = max(1, int(float(get_setting(db, "absence_alert_count", "3") or 3)))
    late_alert_count = max(1, int(float(get_setting(db, "late_alert_count", "5") or 5)))
    current_month = datetime.now().strftime("%Y-%m")
    alerts: list[dict] = []
    absence_like = {"absent", "justified"}
    attended_like = {"present", "late", "early_departure"}
    for student in students:
        records = db.execute(
            "SELECT date,status FROM attendance WHERE student_id=? ORDER BY date DESC LIMIT 120", (student["id"],)
        ).fetchall()
        if not records:
            continue
        consecutive = 0
        for r in records:
            if r["status"] in absence_like:
                consecutive += 1
            else:
                break
        late_count = sum(1 for r in records if r["date"].startswith(current_month) and r["status"] == "late")
        total = len(records)
        attended = sum(1 for r in records if r["status"] in attended_like)
        rate = round(attended * 100 / total, 1) if total else 100.0
        base = {"student_id": student["id"], "student_name": student["name"], "record_number": student["record_number"],
                "classroom_id": student["classroom_id"], "classroom_name": student["classroom_name"]}
        if consecutive >= absence_alert_count:
            alerts.append({**base, "type": "consecutive_absences", "value": consecutive, "threshold": absence_alert_count})
        if late_count >= late_alert_count:
            alerts.append({**base, "type": "monthly_lates", "value": late_count, "threshold": late_alert_count})
        if total >= 4 and rate < threshold:
            alerts.append({**base, "type": "low_attendance", "value": rate, "threshold": threshold})
        if len(alerts) >= limit:
            break
    return alerts[:limit]


def attendance_summary(db: sqlite3.Connection, student_id: str) -> dict:
    rows = db.execute("SELECT status FROM attendance WHERE student_id=? ORDER BY date DESC LIMIT 120", (student_id,)).fetchall()
    counts = {k: 0 for k in ATTENDANCE_STATUSES}
    for r in rows:
        if r["status"] in counts:
            counts[r["status"]] += 1
    total = len(rows)
    attended = counts["present"] + counts["late"] + counts["early_departure"]
    return {"total": total, "attendance_rate": round(attended * 100 / total, 1) if total else None, **counts}


def parse_csv_bytes(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8-sig")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [dict(row) for row in csv.DictReader(io.StringIO(text), dialect=dialect)]


def xlsx_col_index(cell_ref: str) -> int:
    letters = re.match(r"[A-Z]+", cell_ref or "A")
    n = 0
    for ch in (letters.group(0) if letters else "A"):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def parse_xlsx_bytes(raw: bytes) -> list[dict]:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            for si in root.findall("a:si", ns):
                shared.append("".join((t.text or "") for t in si.findall(".//a:t", ns)))
        workbook = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
        rel_ns = {"p": "http://schemas.openxmlformats.org/package/2006/relationships"}
        rel_map = {r.attrib["Id"]: r.attrib["Target"] for r in rels.findall("p:Relationship", rel_ns)}
        sheet = workbook.find("a:sheets/a:sheet", ns)
        if sheet is None:
            return []
        target = rel_map.get(sheet.attrib.get(f"{{{ns['r']}}}id", ""), "worksheets/sheet1.xml").lstrip("/")
        sheet_path = target if target.startswith("xl/") else "xl/" + target
        root = ET.fromstring(z.read(sheet_path))
        rows: list[list[str]] = []
        for row in root.findall(".//a:sheetData/a:row", ns):
            values: dict[int, str] = {}
            for c in row.findall("a:c", ns):
                idx = xlsx_col_index(c.attrib.get("r", "A1"))
                ctype = c.attrib.get("t", "")
                if ctype == "inlineStr":
                    value = "".join((t.text or "") for t in c.findall(".//a:t", ns))
                else:
                    v = c.find("a:v", ns)
                    value = v.text if v is not None and v.text is not None else ""
                    if ctype == "s" and value.isdigit():
                        pos = int(value)
                        value = shared[pos] if pos < len(shared) else ""
                values[idx] = value
            if values:
                max_idx = max(values)
                rows.append([values.get(i, "") for i in range(max_idx + 1)])
        if not rows:
            return []
        headers = [str(x).strip() for x in rows[0]]
        result = []
        for row in rows[1:]:
            result.append({headers[i]: (row[i] if i < len(row) else "") for i in range(len(headers))})
        return result


def normalize_import_key(key: str) -> str:
    s = str(key or "").strip().lower()
    trans = str.maketrans("áéíóúüñ", "aeiouun")
    s = s.translate(trans)
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    aliases = {
        "nombre": "name", "nombre_y_apellido": "name", "alumno": "name", "name": "name",
        "legajo": "record_number", "record_number": "record_number", "registro": "record_number",
        "aula": "classroom", "curso": "classroom", "grado": "classroom", "classroom": "classroom",
        "dni": "dni", "documento": "dni",
        "fecha_de_nacimiento": "birth_date", "nacimiento": "birth_date", "birth_date": "birth_date",
        "tutor": "guardian_name", "padre_madre_tutor": "guardian_name", "guardian_name": "guardian_name",
        "telefono": "guardian_phone", "telefono_tutor": "guardian_phone", "guardian_phone": "guardian_phone",
        "correo_tutor": "guardian_email", "email_tutor": "guardian_email", "correo_familiar": "guardian_email", "guardian_email": "guardian_email", "family_email": "guardian_email",
        "contacto_emergencia": "emergency_name", "emergency_name": "emergency_name",
        "telefono_emergencia": "emergency_phone", "emergency_phone": "emergency_phone",
    }
    return aliases.get(s, s)


def parse_import_payload(data: dict) -> list[dict]:
    filename = str(data.get("file_name", "")).lower()
    encoded = str(data.get("data_base64", ""))
    if not filename or not encoded:
        raise ValueError("Seleccioná un archivo CSV o XLSX.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError("El archivo no pudo decodificarse.") from exc
    if len(raw) > 8_000_000:
        raise ValueError("El archivo supera el límite de 8 MB.")
    if filename.endswith(".csv"):
        rows = parse_csv_bytes(raw)
    elif filename.endswith(".xlsx"):
        rows = parse_xlsx_bytes(raw)
    else:
        raise ValueError("Formato no compatible. Usá CSV o XLSX.")
    normalized: list[dict] = []
    for index, row in enumerate(rows, start=2):
        item = {normalize_import_key(k): str(v or "").strip() for k, v in row.items() if k}
        item["row_number"] = index
        normalized.append(item)
    return normalized


def preview_import_rows(db: sqlite3.Connection, rows: list[dict]) -> dict:
    preview = []
    valid = 0
    for item in rows[:2000]:
        errors = []
        name = item.get("name", "")
        record = item.get("record_number", "")
        classroom = item.get("classroom", "")
        dni = item.get("dni", "")
        guardian_email = item.get("guardian_email", "")
        if not name:
            errors.append("Falta nombre")
        if not record:
            errors.append("Falta legajo")
        if not classroom:
            errors.append("Falta aula")
        if dni and not re.fullmatch(r"[0-9]{7,9}", dni):
            errors.append("DNI inválido")
        if guardian_email and not valid_email(guardian_email):
            errors.append("Correo familiar inválido")
        if record and db.execute("SELECT 1 FROM students WHERE record_number=? COLLATE NOCASE", (record,)).fetchone():
            errors.append("Legajo ya existente")
        if not errors:
            valid += 1
        preview.append({"row_number": item.get("row_number"), "name": name, "record_number": record, "classroom": classroom, "dni": dni, "errors": errors})
    return {"rows": preview[:300], "total": len(rows), "valid": valid, "invalid": len(rows) - valid}


def commit_import_rows(db: sqlite3.Connection, admin: sqlite3.Row, rows: list[dict]) -> dict:
    imported = 0
    skipped = 0
    created_classrooms = 0
    classroom_cache = {r["name"].casefold(): r["id"] for r in db.execute("SELECT id,name FROM classrooms")}
    for item in rows:
        name = item.get("name", "").strip()
        record = item.get("record_number", "").strip()
        classroom_name = item.get("classroom", "").strip()
        dni = item.get("dni", "").strip()
        guardian_email=item.get("guardian_email","").strip().lower()
        if not name or not record or not classroom_name or (dni and not re.fullmatch(r"[0-9]{7,9}", dni)) or (guardian_email and not valid_email(guardian_email)):
            skipped += 1
            continue
        if db.execute("SELECT 1 FROM students WHERE record_number=? COLLATE NOCASE", (record,)).fetchone():
            skipped += 1
            continue
        cid = classroom_cache.get(classroom_name.casefold())
        if not cid:
            cid = new_id("class")
            db.execute("INSERT INTO classrooms(id,name,created_at) VALUES(?,?,?)", (cid, classroom_name, iso_now()))
            classroom_cache[classroom_name.casefold()] = cid
            created_classrooms += 1
        phone_pattern = re.compile(r"[0-9+() .-]{6,30}")
        guardian_phone = item.get("guardian_phone", "").strip()
        emergency_phone = item.get("emergency_phone", "").strip()
        if guardian_phone and not phone_pattern.fullmatch(guardian_phone):
            guardian_phone = ""
        if emergency_phone and not phone_pattern.fullmatch(emergency_phone):
            emergency_phone = ""
        sid = new_id("student")
        db.execute(
            """INSERT INTO students(id,record_number,name,classroom_id,dni,birth_date,guardian_name,guardian_phone,guardian_email,
               emergency_name,emergency_phone,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, record, name, cid, dni, item.get("birth_date", ""), item.get("guardian_name", ""), guardian_phone,
             guardian_email, item.get("emergency_name", ""), emergency_phone, iso_now(), iso_now()),
        )
        imported += 1
    log_activity(db, admin["id"], "student_import", f"Importó {imported} alumnos; {skipped} filas omitidas; {created_classrooms} aulas nuevas.")
    return {"imported": imported, "skipped": skipped, "created_classrooms": created_classrooms}


def pdf_escape(text: str) -> bytes:
    text = str(text).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return text.encode("cp1252", errors="replace")


def simple_pdf(title: str, lines: list[str]) -> bytes:
    # Tiny dependency-free PDF writer using built-in Helvetica/WinAnsi.
    wrapped: list[str] = []
    for line in [title, ""] + lines:
        s = str(line)
        while len(s) > 105:
            cut = s.rfind(" ", 0, 105)
            if cut < 40:
                cut = 105
            wrapped.append(s[:cut])
            s = s[cut:].lstrip()
        wrapped.append(s)
    pages = [wrapped[i:i+48] for i in range(0, len(wrapped), 48)] or [[]]
    objects: list[bytes] = []
    # 1 catalog, 2 pages, 3 font; page/content pairs follow.
    page_ids = []
    for _ in pages:
        page_ids.append((len(objects) + 4, len(objects) + 5))
        objects.extend([b"", b""])
    kids = " ".join(f"{pid} 0 R" for pid, _ in page_ids)
    catalog = b"<< /Type /Catalog /Pages 2 0 R >>"
    pages_obj = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
    font = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    final_objects: list[bytes] = [catalog, pages_obj, font]
    for (page_id, content_id), page_lines in zip(page_ids, pages):
        content = bytearray(b"BT /F1 10 Tf 50 790 Td 14 TL\n")
        for i, line in enumerate(page_lines):
            if i:
                content.extend(b"T* ")
            content.extend(b"(" + pdf_escape(line) + b") Tj\n")
        content.extend(b"ET")
        page_obj = f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>".encode()
        content_obj = b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + bytes(content) + b"\nendstream"
        final_objects.extend([page_obj, content_obj])
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, obj in enumerate(final_objects, start=1):
        offsets.append(len(out))
        out.extend(f"{i} 0 obj\n".encode())
        out.extend(obj)
        out.extend(b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(final_objects)+1}\n".encode())
    out.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.extend(f"{offset:010d} 00000 n \n".encode())
    out.extend(f"trailer << /Size {len(final_objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return bytes(out)


def migrate_local_state(db: sqlite3.Connection, admin: sqlite3.Row, state) -> dict:
    if not isinstance(state, dict):
        raise ValueError("No se encontraron datos locales válidos.")
    classrooms = state.get("classrooms") if isinstance(state.get("classrooms"), list) else []
    students = state.get("students") if isinstance(state.get("students"), list) else []
    attendance = state.get("attendance") if isinstance(state.get("attendance"), list) else []
    class_map: dict[str, str] = {}
    student_map: dict[str, str] = {}
    imported = {"classrooms": 0, "students": 0, "attendance": 0}
    for item in classrooms:
        old_id = str(item.get("id", "")); name = str(item.get("name", "")).strip()
        if not name:
            continue
        existing = db.execute("SELECT id FROM classrooms WHERE name=? COLLATE NOCASE", (name,)).fetchone()
        if existing:
            cid = existing["id"]
        else:
            cid = old_id if old_id and not db.execute("SELECT 1 FROM classrooms WHERE id=?", (old_id,)).fetchone() else new_id("class")
            db.execute("INSERT INTO classrooms(id,name,created_at) VALUES(?,?,?)", (cid, name, iso_now())); imported["classrooms"] += 1
        class_map[old_id] = cid
    for item in students:
        old_id = str(item.get("id", "")); name = str(item.get("name", "")).strip()
        record = str(item.get("recordNumber", item.get("record_number", ""))).strip() or f"MIG-{secrets.token_hex(3).upper()}"
        old_class = str(item.get("classroomId", item.get("classroom_id", ""))); cid = class_map.get(old_class, old_class)
        if not name or not db.execute("SELECT 1 FROM classrooms WHERE id=?", (cid,)).fetchone():
            continue
        existing = db.execute("SELECT id FROM students WHERE record_number=? COLLATE NOCASE", (record,)).fetchone()
        if existing:
            sid = existing["id"]
        else:
            sid = old_id if old_id and not db.execute("SELECT 1 FROM students WHERE id=?", (old_id,)).fetchone() else new_id("student")
            db.execute(
                """INSERT INTO students(id,record_number,name,classroom_id,dni,birth_date,guardian_name,guardian_phone,guardian_email,
                   emergency_name,emergency_phone,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sid, record, name, cid, str(item.get("dni", "")), str(item.get("birthDate", item.get("birth_date", ""))),
                 str(item.get("guardianName", item.get("guardian_name", ""))), str(item.get("guardianPhone", item.get("guardian_phone", ""))),
                 str(item.get("guardianEmail", item.get("guardian_email", ""))), str(item.get("emergencyName", item.get("emergency_name", ""))), str(item.get("emergencyPhone", item.get("emergency_phone", ""))),
                 iso_now(), iso_now()),
            ); imported["students"] += 1
        student_map[old_id] = sid
    for item in attendance:
        old_sid = str(item.get("studentId", item.get("student_id", ""))); old_cid = str(item.get("classroomId", item.get("classroom_id", "")))
        sid = student_map.get(old_sid, old_sid); cid = class_map.get(old_cid, old_cid); date = str(item.get("date", "")); status = str(item.get("status", ""))
        if status not in ATTENDANCE_STATUSES or not date or not db.execute("SELECT 1 FROM students WHERE id=?", (sid,)).fetchone():
            continue
        stamp = iso_now()
        db.execute(
            """INSERT INTO attendance(id,date,student_id,classroom_id,status,reason,arrival_time,departure_time,created_by,updated_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(date,student_id) DO UPDATE SET
               classroom_id=excluded.classroom_id,status=excluded.status,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
            (new_id("att"), date, sid, cid, status, "", "", "", admin["id"], admin["id"], stamp, stamp),
        ); imported["attendance"] += 1
    log_activity(db, admin["id"], "migration", f"Importó datos locales de v3: {imported}.")
    return {"ok": True, "imported": imported}


def setup_required(db: sqlite3.Connection) -> bool:
    return db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def session_hours(db: sqlite3.Connection) -> int:
    try:return max(1,min(168,int(float(get_setting(db,"session_hours",str(DEFAULT_SESSION_HOURS)) or DEFAULT_SESSION_HOURS))))
    except Exception:return DEFAULT_SESSION_HOURS


def create_session(db: sqlite3.Connection, user_id: str, user_agent: str = "") -> tuple[str,str,str]:
    token=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(32); hours=session_hours(db); expires=now_utc()+timedelta(hours=hours)
    db.execute("INSERT INTO sessions(id,token_hash,user_id,created_at,expires_at,last_seen,user_agent,csrf_token) VALUES(?,?,?,?,?,?,?,?)",
               (new_id("session"),session_token_hash(token),user_id,iso_now(),expires.isoformat(timespec="seconds"),iso_now(),user_agent[:300],csrf))
    return token,csrf,expires.isoformat(timespec="seconds")


def clear_login_attempt(db: sqlite3.Connection, key: str) -> None:
    db.execute("DELETE FROM login_attempts WHERE key=?",(key,))


def login_attempt_key(email: str, ip: str) -> str:
    return hashlib.sha256(f"{email.casefold()}|{ip}".encode("utf-8")).hexdigest()


def login_is_blocked(db: sqlite3.Connection, key: str) -> tuple[bool,str]:
    row=db.execute("SELECT * FROM login_attempts WHERE key=?",(key,)).fetchone()
    if not row:return False,""
    blocked=row["blocked_until"] or ""
    if blocked:
        try:
            if datetime.fromisoformat(blocked)>now_utc():return True,blocked
        except ValueError:pass
    return False,""


def register_login_failure(db: sqlite3.Connection, key: str) -> str:
    row=db.execute("SELECT * FROM login_attempts WHERE key=?",(key,)).fetchone(); now=now_utc()
    attempts=1; first=iso_now()
    if row:
        try:first_dt=datetime.fromisoformat(row["first_at"])
        except ValueError:first_dt=now
        if now-first_dt<=timedelta(minutes=15):attempts=int(row["attempts"])+1;first=row["first_at"]
    blocked=""
    if attempts>=5:blocked=(now+timedelta(minutes=5)).isoformat(timespec="seconds")
    db.execute("INSERT INTO login_attempts(key,attempts,first_at,blocked_until) VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET attempts=excluded.attempts,first_at=excluded.first_at,blocked_until=excluded.blocked_until",(key,attempts,first,blocked))
    return blocked


def get_google_status(db: sqlite3.Connection, user_id: str) -> dict:
    row=google_tokens_for_user(db,user_id); client_id,_=google_client_config(db)
    if not row:
        return {"configured":bool(client_id),"storage_available":google_secret_storage_available(),"connected":False,"gmail_enabled":False}
    scopes=set((row["scopes"] or "").split())
    return {"configured":bool(client_id),"storage_available":google_secret_storage_available(),"connected":True,"gmail_enabled":GOOGLE_GMAIL_SCOPE in scopes,"email":row["email"],"display_name":row["display_name"],"picture_url":row["picture_url"],"scopes":sorted(scopes),"connected_at":row["connected_at"]}


def restore_database_backup(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.db",name or ""):raise ValueError("Nombre de copia inválido.")
    source=(BACKUP_DIR/name).resolve()
    if source.parent!=BACKUP_DIR.resolve() or not source.exists():raise ValueError("La copia no existe.")
    probe=sqlite3.connect(source)
    try:
        integrity=probe.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity!="ok":raise ValueError("La copia seleccionada no supera la comprobación de integridad.")
        target=connect_db()
        try:probe.backup(target);target.execute("DELETE FROM sessions");target.commit()
        finally:target.close()
    finally:probe.close()


def auto_backup_if_due(force: bool=False) -> Path | None:
    if not DB_PATH.exists():return None
    try:
        with connect_db() as db:
            if setup_required(db):return None
            enabled=parse_bool(get_setting(db,"auto_backup_enabled","1"),True)
            interval=max(1,int(float(get_setting(db,"backup_interval_hours","24") or 24)))
            keep=max(3,int(float(get_setting(db,"backup_keep","30") or 30)))
        if not enabled and not force:return None
        latest=max(BACKUP_DIR.glob("*.db"),key=lambda p:p.stat().st_mtime,default=None)
        if not force and latest and time.time()-latest.stat().st_mtime<interval*3600:return None
        path=create_database_backup(prefix="AutoBackup");prune_backups(keep);append_log("INFO",f"Copia automática creada: {path.name}");return path
    except Exception as exc:append_log("ERROR",f"Error al crear copia automática: {exc}");return None


def backup_worker(stop_event: threading.Event) -> None:
    while not stop_event.wait(600):auto_backup_if_due(False)


class AppHandler(SimpleHTTPRequestHandler):
    server_version = "PresencialidadHTTP/5.3.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(RESOURCE_ROOT), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def end_headers(self) -> None:
        self.send_header("X-Presencialidad-Version", APP_VERSION)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self' https://oauth2.googleapis.com https://openidconnect.googleapis.com https://gmail.googleapis.com; frame-ancestors 'none'; base-uri 'self'")
        super().end_headers()

    def send_json(self, status: int, payload: dict | list, *, cookie: str | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, status: int, body: bytes, content_type: str, filename: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > 12_000_000:
            raise ValueError("Payload demasiado grande")
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(data, dict):
            raise ValueError("Se esperaba un objeto JSON")
        return data

    def get_cookie_token(self) -> str | None:
        header = self.headers.get("Cookie")
        if not header:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(header)
        except Exception:
            return None
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def current_user(self, db: sqlite3.Connection, *, touch: bool = True) -> sqlite3.Row | None:
        token = self.get_cookie_token()
        if not token:
            return None
        cleanup_sessions(db)
        token_hash = session_token_hash(token)
        row = db.execute(
            """SELECT u.*,s.id AS session_id,s.expires_at,s.csrf_token,s.created_at AS session_created_at,s.last_seen AS session_last_seen,s.user_agent AS session_user_agent FROM sessions s JOIN users u ON u.id=s.user_id
               WHERE s.token_hash=? AND u.active=1""", (token_hash,)
        ).fetchone()
        if row and touch:
            db.execute("UPDATE sessions SET last_seen=? WHERE id=?", (iso_now(), row["session_id"]))
        return row

    def require_user(self, db: sqlite3.Connection) -> sqlite3.Row | None:
        user = self.current_user(db)
        if not user:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Necesitás iniciar sesión."})
            return None
        return user

    def require_admin(self, db: sqlite3.Connection) -> sqlite3.Row | None:
        user = self.require_user(db)
        if not user:
            return None
        if not role_can_admin(user):
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "No tenés permisos de administración."})
            return None
        return user

    def require_csrf(self, user: sqlite3.Row | dict) -> bool:
        expected = str(user["csrf_token"] if "csrf_token" in user.keys() else "")
        provided = self.headers.get("X-CSRF-Token", "")
        if not expected or not provided or not hmac.compare_digest(expected, provided):
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "La sesión no pudo validar esta operación. Recargá la aplicación e intentá nuevamente."})
            return False
        return True

    def session_cookie(self, token: str, max_age: int) -> str:
        secure = self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower() == "https"
        parts=[f"{SESSION_COOKIE}={token}","HttpOnly","SameSite=Lax","Path=/",f"Max-Age={max_age}"]
        if secure:parts.append("Secure")
        return "; ".join(parts)

    def route(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urlparse(self.path)
        return parsed.path.rstrip("/") or "/", parse_qs(parsed.query)

    def do_GET(self) -> None:
        increment_requests(); path, query = self.route()
        if path == "/oauth/google/callback":
            try:self.handle_google_callback(query)
            except Exception as exc:
                append_log("ERROR",f"Google OAuth callback: {exc}");self.send_bytes(400,f"<html><body><h2>Google</h2><p>{str(exc)}</p><script>setTimeout(()=>window.close(),5000)</script></body></html>".encode("utf-8"),"text/html; charset=utf-8")
            return
        if path.startswith("/api/"):
            try:
                self.handle_api_get(path, query)
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            except Exception as exc:
                print("API GET error:", repr(exc)); self.send_json(500, {"error": "Error interno del servidor."})
            return
        allowed_static = {"/", "/index.html", "/styles.css", "/app.js", "/i18n.js", "/manifest.webmanifest"}
        if path not in allowed_static:
            self.send_error(404); return
        super().do_GET()

    def do_POST(self) -> None:
        increment_requests(); path, query = self.route()
        if not path.startswith("/api/"):
            self.send_error(405); return
        try:
            self.handle_api_post(path, query)
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except sqlite3.IntegrityError as exc:
            self.send_json(409, {"error": "El dato ya existe o entra en conflicto con otro registro.", "detail": str(exc)})
        except Exception as exc:
            print("API POST error:", repr(exc)); self.send_json(500, {"error": "Error interno del servidor."})

    def do_PATCH(self) -> None:
        increment_requests(); path, query = self.route()
        try:
            self.handle_api_patch(path, query)
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except sqlite3.IntegrityError as exc:
            self.send_json(409, {"error": "El dato ya existe o entra en conflicto con otro registro.", "detail": str(exc)})
        except Exception as exc:
            print("API PATCH error:", repr(exc)); self.send_json(500, {"error": "Error interno del servidor."})

    def do_DELETE(self) -> None:
        increment_requests(); path, query = self.route()
        try:
            self.handle_api_delete(path, query)
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            print("API DELETE error:", repr(exc)); self.send_json(500, {"error": "Error interno del servidor."})

    def handle_google_callback(self, query: dict[str,list[str]]) -> None:
        state=(query.get("state") or [""])[0]; code=(query.get("code") or [""])[0]; error=(query.get("error") or [""])[0]
        if error:raise ValueError(f"Google canceló la autorización: {error}")
        if not state or not code:raise ValueError("Respuesta OAuth incompleta.")
        with connect_db() as db:
            oauth=db.execute("SELECT * FROM oauth_states WHERE state=?",(state,)).fetchone()
            if not oauth:raise ValueError("La solicitud de conexión expiró o no es válida.")
            try:
                if datetime.fromisoformat(oauth["expires_at"])<now_utc():raise ValueError("La solicitud de conexión expiró.")
            except ValueError:raise ValueError("La solicitud de conexión expiró.")
            client_id,client_secret=google_client_config(db)
            payload={"client_id":client_id,"code":code,"code_verifier":oauth["code_verifier"],"grant_type":"authorization_code","redirect_uri":google_redirect_uri(db,self)}
            if client_secret:payload["client_secret"]=client_secret
            tokens=http_json(GOOGLE_TOKEN_ENDPOINT,method="POST",data=payload)
            access=tokens.get("access_token","");refresh=tokens.get("refresh_token","")
            if not access:raise ValueError("Google no devolvió un token de acceso.")
            userinfo=http_json(GOOGLE_USERINFO_ENDPOINT,headers={"Authorization":f"Bearer {access}"})
            existing=db.execute("SELECT * FROM google_accounts WHERE user_id=?",(oauth["user_id"],)).fetchone()
            if not refresh and existing:
                try:refresh=unprotect_secret(existing["refresh_token_protected"] or "")
                except Exception:refresh=""
            if not refresh:raise ValueError("Google no devolvió un token de actualización. Volvé a autorizar la cuenta.")
            scopes=str(tokens.get("scope","")).strip() or " ".join(GOOGLE_PROFILE_SCOPES+([GOOGLE_GMAIL_SCOPE] if oauth["mode"]=="gmail" else []))
            expiry=(now_utc()+timedelta(seconds=int(tokens.get("expires_in",3600)))).isoformat(timespec="seconds");stamp=iso_now()
            db.execute("""INSERT INTO google_accounts(user_id,email,display_name,picture_url,scopes,refresh_token_protected,access_token_protected,access_token_expires_at,connected_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET email=excluded.email,display_name=excluded.display_name,picture_url=excluded.picture_url,scopes=excluded.scopes,refresh_token_protected=excluded.refresh_token_protected,access_token_protected=excluded.access_token_protected,access_token_expires_at=excluded.access_token_expires_at,updated_at=excluded.updated_at""",
                       (oauth["user_id"],userinfo.get("email",""),userinfo.get("name",""),userinfo.get("picture",""),scopes,protect_secret(refresh),protect_secret(access),expiry,existing["connected_at"] if existing else stamp,stamp))
            # Keep the app profile independent, but use Google picture when the user has no avatar.
            user=db.execute("SELECT * FROM users WHERE id=?",(oauth["user_id"],)).fetchone()
            if user and not (user["avatar_url"] or "") and userinfo.get("picture"):
                db.execute("UPDATE users SET avatar_url=? WHERE id=?",(userinfo["picture"],oauth["user_id"]))
            db.execute("DELETE FROM oauth_states WHERE state=?",(state,));log_activity(db,oauth["user_id"],"google_connect",f"Conectó la cuenta Google {userinfo.get('email','')} ({oauth['mode']}).")
        html="""<!doctype html><meta charset='utf-8'><title>Presencialidad · Google</title><style>body{font-family:system-ui;padding:40px;max-width:620px;margin:auto}h2{color:#168c86}</style><h2>Cuenta Google conectada</h2><p>Podés cerrar esta ventana y volver a Presencialidad.</p><script>try{window.opener&&window.opener.postMessage({type:'presencialidad-google-connected'},location.origin)}catch(e){}setTimeout(()=>window.close(),1200)</script>"""
        self.send_bytes(200,html.encode("utf-8"),"text/html; charset=utf-8")

    def handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/setup/status":
            with connect_db() as db:
                self.send_json(200,{"needs_setup":setup_required(db),"version":APP_VERSION,"data_dir":str(DATA_DIR),"institution":get_setting(db,"institution_name","")})
            return
        if path == "/api/status":
            with connect_db() as db:
                stats = {
                    "users": db.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0],
                    "classrooms": db.execute("SELECT COUNT(*) FROM classrooms WHERE archived=0").fetchone()[0],
                    "students": db.execute("SELECT COUNT(*) FROM students WHERE active=1").fetchone()[0],
                    "attendance": db.execute("SELECT COUNT(*) FROM attendance").fetchone()[0],
                    "reports": db.execute("SELECT COUNT(*) FROM reports").fetchone()[0],
                    "notes": db.execute("SELECT COUNT(*) FROM teacher_notes").fetchone()[0],
                    "assessments": db.execute("SELECT COUNT(*) FROM assessments").fetchone()[0],
                    "schedule": db.execute("SELECT COUNT(*) FROM schedule_items").fetchone()[0],
                    "notices": db.execute("SELECT COUNT(*) FROM family_notices").fetchone()[0],
                    "sessions": db.execute("SELECT COUNT(*) FROM sessions WHERE expires_at>=?", (iso_now(),)).fetchone()[0],
                }
            self.send_json(200, {"status":"ok","version":APP_VERSION,"uptime_seconds":round(time.time()-STARTED_AT,3),
                                 "request_count":REQUEST_COUNT,"python_version":platform.python_version(),"platform":platform.system(),
                                 "database":DB_PATH.name,"database_bytes":DB_PATH.stat().st_size if DB_PATH.exists() else 0,
                                 "stats":stats,"server_time":iso_now(),"data_dir":str(DATA_DIR),"backup_dir":str(BACKUP_DIR),"integrity":sqlite_integrity(),"setup_required":stats["users"]==0}); return
        if path == "/api/specs":
            self.send_json(200, {
                "name": APP_NAME, "version": APP_VERSION, "frontend": ["HTML5","CSS","JavaScript"],
                "backend": "Python 3 + biblioteca estándar + sqlite3", "database": "SQLite (local-first)",
                "authentication": f"PBKDF2-HMAC-SHA256 ({PBKDF2_ITERATIONS:,} iteraciones) + sesiones HttpOnly + CSRF",
                "roles": list(ROLE_LABELS.values()), "languages": ["Español","English","日本語","한국어","中文（普通话）","Русский","العربية"],
                "attendance": ["Presente","Ausente","Tarde","Justificada","Retiro anticipado","Autoguardado cada 15 s","Borrador offline"],
                "modules": ["Inicio","Asistencia","Historial e informes","Calificaciones","Calendario","Reportes","Notas privadas","Avisos a familias","Importación CSV/XLSX","Perfil","Google/Gmail opcional","Backups","Diagnóstico"],
                "network": "Local 127.0.0.1 por defecto; modo servidor disponible mediante --host y proxy HTTPS",
            }); return

        with connect_db() as db:
            if path == "/api/auth/me":
                user = self.current_user(db)
                if not user:
                    self.send_json(401, {"authenticated":False}); return
                self.send_json(200, {"authenticated":True,"user":user_public(db,user),"csrf_token":user["csrf_token"],"google":get_google_status(db,user["id"])}); return
            user = self.require_user(db)
            if not user:
                return

            if path == "/api/profile":
                self.send_json(200,{"user":user_public(db,user),"google":get_google_status(db,user["id"])});return

            if path == "/api/auth/sessions":
                rows=db.execute("SELECT id,created_at,expires_at,last_seen,user_agent FROM sessions WHERE user_id=? AND expires_at>=? ORDER BY last_seen DESC",(user["id"],iso_now())).fetchall()
                self.send_json(200,{"sessions":[{**dict(r),"current":r["id"]==user["session_id"]} for r in rows]});return

            if path == "/api/google/status":
                self.send_json(200,get_google_status(db,user["id"]));return

            if path == "/api/google/auth-url":
                mode=(query.get("mode") or ["profile"])[0]
                if mode not in {"profile","gmail"}:raise ValueError("Modo de Google inválido.")
                self.send_json(200,{"url":google_auth_url(db,user["id"],mode,self)});return

            if path == "/api/backups":
                admin=self.require_admin(db)
                if not admin:return
                self.send_json(200,{"backups":list_backups(),"directory":str(BACKUP_DIR),"integrity":sqlite_integrity()});return

            if path == "/api/backups/download":
                admin=self.require_admin(db)
                if not admin:return
                name=(query.get("name") or [""])[0]
                if not re.fullmatch(r"[A-Za-z0-9_.-]+\.db",name):raise ValueError("Nombre de copia inválido.")
                file=(BACKUP_DIR/name).resolve()
                if file.parent!=BACKUP_DIR.resolve() or not file.exists():self.send_json(404,{"error":"Copia no encontrada."});return
                self.send_bytes(200,file.read_bytes(),"application/x-sqlite3",name);return

            if path == "/api/classrooms":
                include_archived=(query.get("include_archived") or ["0"])[0]=="1" and role_can_admin(user)
                if user["role"] in {"directivo","administrador"}:
                    where="" if include_archived else "WHERE c.archived=0"
                    rows = db.execute(f"""SELECT c.id,c.name,c.archived,COUNT(s.id) AS student_count FROM classrooms c
                                       LEFT JOIN students s ON s.classroom_id=c.id AND s.active=1
                                       {where} GROUP BY c.id,c.name,c.archived ORDER BY c.archived,c.name COLLATE NOCASE""").fetchall()
                else:
                    rows = db.execute("""SELECT c.id,c.name,c.archived,COUNT(s.id) AS student_count FROM classrooms c
                                       JOIN user_classrooms uc ON uc.classroom_id=c.id AND uc.user_id=?
                                       LEFT JOIN students s ON s.classroom_id=c.id AND s.active=1
                                       WHERE c.archived=0 GROUP BY c.id,c.name,c.archived ORDER BY c.name COLLATE NOCASE""", (user["id"],)).fetchall()
                self.send_json(200, {"classrooms":[dict(r) for r in rows]}); return

            if path == "/api/students":
                classroom_id = (query.get("classroom_id") or [""])[0]
                include_inactive=(query.get("include_inactive") or ["0"])[0]=="1" and role_can_admin(user)
                allowed = classroom_ids_for_user(db, user); params=[]; where=[] if include_inactive else ["s.active=1"]
                if classroom_id:
                    if classroom_id not in allowed:
                        self.send_json(403,{"error":"No tenés acceso a esa aula."}); return
                    where.append("s.classroom_id=?"); params.append(classroom_id)
                elif user["role"] not in {"directivo","administrador"}:
                    if not allowed:
                        self.send_json(200,{"students":[]}); return
                    where.append("s.classroom_id IN (%s)" % ",".join("?" for _ in allowed)); params.extend(allowed)
                where_sql=(' WHERE '+ ' AND '.join(where)) if where else ''
                rows = db.execute(f"""SELECT s.*,c.name AS classroom_name FROM students s JOIN classrooms c ON c.id=s.classroom_id
                                    {where_sql} ORDER BY s.active DESC,s.name COLLATE NOCASE""", params).fetchall()
                self.send_json(200,{"students":[student_public(db,r,user) for r in rows]}); return

            if path == "/api/notes":
                q=(query.get("q") or [""])[0].strip()
                classroom_id=(query.get("classroom_id") or ["all"])[0].strip() or "all"
                where=["n.author_id=?"]; params=[user["id"]]
                if classroom_id != "all":
                    where.append("n.classroom_id=?"); params.append(classroom_id)
                if q:
                    like=f"%{q}%"
                    where.append("(n.note LIKE ? COLLATE NOCASE OR s.name LIKE ? COLLATE NOCASE OR s.record_number LIKE ? COLLATE NOCASE OR c.name LIKE ? COLLATE NOCASE)")
                    params.extend([like,like,like,like])
                rows=db.execute(f"""SELECT n.id,n.student_id,n.classroom_id,n.note,n.created_at,n.updated_at,
                                           s.name AS student_name,s.record_number,c.name AS classroom_name
                                    FROM teacher_notes n
                                    JOIN students s ON s.id=n.student_id
                                    JOIN classrooms c ON c.id=n.classroom_id
                                    WHERE {' AND '.join(where)}
                                    ORDER BY n.updated_at DESC,n.created_at DESC LIMIT 500""",params).fetchall()
                self.send_json(200,{"notes":[dict(r) for r in rows]});return

            if path == "/api/search":
                q = (query.get("q") or [""])[0].strip()
                if len(q) < 2:
                    self.send_json(200,{"results":[]}); return
                allowed = classroom_ids_for_user(db,user)
                if not allowed:
                    self.send_json(200,{"results":[]}); return
                placeholders = ",".join("?" for _ in allowed)
                like = f"%{q}%"
                conditions = ["s.name LIKE ? COLLATE NOCASE", "s.record_number LIKE ? COLLATE NOCASE"]
                params: list = [like, like]
                if role_can_view_contacts(user):
                    conditions.append("s.dni LIKE ?"); params.append(like)
                params.extend(allowed)
                rows = db.execute(f"""SELECT s.id,s.name,s.record_number,s.classroom_id,c.name AS classroom_name
                                    FROM students s JOIN classrooms c ON c.id=s.classroom_id
                                    WHERE s.active=1 AND ({' OR '.join(conditions)}) AND s.classroom_id IN ({placeholders})
                                    ORDER BY s.name COLLATE NOCASE LIMIT 15""", params).fetchall()
                self.send_json(200,{"results":[dict(r) for r in rows]}); return

            if path.startswith("/api/students/"):
                parts = path.split("/"); student_id = parts[3] if len(parts)>3 else ""
                row = db.execute("SELECT s.*,c.name AS classroom_name FROM students s JOIN classrooms c ON c.id=s.classroom_id WHERE s.id=? AND s.active=1", (student_id,)).fetchone()
                if not row or not user_has_classroom(db,user,row["classroom_id"]):
                    self.send_json(404,{"error":"Alumno no encontrado."}); return
                if len(parts)==4:
                    data = student_public(db,row,user); data["attendance_summary"] = attendance_summary(db, student_id)
                    self.send_json(200,{"student":data}); return
                if len(parts)==5 and parts[4]=="attendance":
                    records = db.execute("""SELECT a.date,a.status,a.reason,a.arrival_time,a.departure_time,a.updated_at,u.name AS updated_by_name
                                          FROM attendance a JOIN users u ON u.id=a.updated_by WHERE a.student_id=? ORDER BY a.date DESC LIMIT 180""", (student_id,)).fetchall()
                    self.send_json(200,{"attendance":[dict(r) for r in records],"summary":attendance_summary(db,student_id)}); return
                if len(parts)==5 and parts[4]=="audit":
                    rows=db.execute("""SELECT aa.*,u.name AS changed_by_name FROM attendance_audit aa
                                       JOIN users u ON u.id=aa.changed_by WHERE aa.student_id=?
                                       ORDER BY aa.changed_at DESC LIMIT 100""",(student_id,)).fetchall()
                    self.send_json(200,{"audit":[dict(r) for r in rows]}); return
                if len(parts)==5 and parts[4]=="reports":
                    if user["role"]=="docente":
                        rows=db.execute("""SELECT r.*,u.name AS reporter_name,c.name AS classroom_name FROM reports r
                                         JOIN users u ON u.id=r.reporter_id JOIN classrooms c ON c.id=r.classroom_id
                                         WHERE r.student_id=? AND r.reporter_id=? ORDER BY r.created_at DESC""",(student_id,user["id"])).fetchall()
                    else:
                        rows=db.execute("""SELECT r.*,u.name AS reporter_name,c.name AS classroom_name FROM reports r
                                         JOIN users u ON u.id=r.reporter_id JOIN classrooms c ON c.id=r.classroom_id
                                         WHERE r.student_id=? ORDER BY r.created_at DESC""",(student_id,)).fetchall()
                    self.send_json(200,{"reports":[dict(r) for r in rows]}); return
                if len(parts)==5 and parts[4]=="notes":
                    rows=db.execute("SELECT id,note,created_at,updated_at FROM teacher_notes WHERE student_id=? AND author_id=? ORDER BY created_at DESC",(student_id,user["id"])).fetchall()
                    self.send_json(200,{"notes":[dict(r) for r in rows]}); return
                if len(parts)==5 and parts[4]=="grades":
                    rows=db.execute("""SELECT a.id AS assessment_id,a.title,a.assessment_date,a.max_score,g.score,g.comment,u.name AS updated_by_name,g.updated_at
                                     FROM assessments a LEFT JOIN grades g ON g.assessment_id=a.id AND g.student_id=?
                                     LEFT JOIN users u ON u.id=g.updated_by WHERE a.classroom_id=? ORDER BY a.assessment_date DESC,a.title COLLATE NOCASE""",(student_id,row["classroom_id"])).fetchall()
                    self.send_json(200,{"grades":[dict(r) for r in rows]}); return
                if len(parts)==5 and parts[4]=="notices":
                    if user["role"]=="docente":
                        rows=db.execute("""SELECT n.*,u.name AS author_name FROM family_notices n JOIN users u ON u.id=n.author_id
                                         WHERE n.student_id=? AND n.author_id=? ORDER BY n.created_at DESC""",(student_id,user["id"])).fetchall()
                    else:
                        rows=db.execute("""SELECT n.*,u.name AS author_name FROM family_notices n JOIN users u ON u.id=n.author_id
                                         WHERE n.student_id=? ORDER BY n.created_at DESC""",(student_id,)).fetchall()
                    self.send_json(200,{"notices":[dict(r) for r in rows]}); return

            if path == "/api/attendance":
                date=(query.get("date") or [""])[0]; classroom_id=(query.get("classroom_id") or [""])[0]
                if not date or not classroom_id: raise ValueError("Falta fecha o aula.")
                if not user_has_classroom(db,user,classroom_id): self.send_json(403,{"error":"No tenés acceso a esa aula."}); return
                rows=db.execute("""SELECT a.*,s.name AS student_name,s.record_number,c.name AS classroom_name,u.name AS updated_by_name
                                 FROM attendance a JOIN students s ON s.id=a.student_id JOIN classrooms c ON c.id=a.classroom_id
                                 JOIN users u ON u.id=a.updated_by WHERE a.date=? AND a.classroom_id=? ORDER BY s.name COLLATE NOCASE""",(date,classroom_id)).fetchall()
                self.send_json(200,{"attendance":[dict(r) for r in rows]}); return

            if path == "/api/attendance/draft":
                date=(query.get("date") or [""])[0]; classroom_id=(query.get("classroom_id") or [""])[0]
                if not date or not classroom_id: raise ValueError("Falta fecha o aula.")
                if not user_has_classroom(db,user,classroom_id): self.send_json(403,{"error":"No tenés acceso a esa aula."}); return
                row=db.execute("SELECT payload_json,updated_at FROM attendance_drafts WHERE user_id=? AND classroom_id=? AND date=?",(user["id"],classroom_id,date)).fetchone()
                self.send_json(200,{"draft": json.loads(row["payload_json"]) if row else None, "updated_at":row["updated_at"] if row else None}); return

            if path == "/api/history":
                date=(query.get("date") or [""])[0]; classroom_id=(query.get("classroom_id") or ["all"])[0]
                allowed=classroom_ids_for_user(db,user)
                if not date: raise ValueError("Falta la fecha.")
                if classroom_id!="all" and classroom_id not in allowed: self.send_json(403,{"error":"No tenés acceso a esa aula."}); return
                params=[date]; where=["a.date=?"]
                if classroom_id!="all": where.append("a.classroom_id=?"); params.append(classroom_id)
                elif user["role"] not in {"directivo","administrador"}:
                    if not allowed: self.send_json(200,{"attendance":[]}); return
                    where.append("a.classroom_id IN (%s)" % ",".join("?" for _ in allowed)); params.extend(allowed)
                rows=db.execute(f"""SELECT a.*,s.name AS student_name,s.record_number,c.name AS classroom_name,u.name AS updated_by_name
                                  FROM attendance a JOIN students s ON s.id=a.student_id JOIN classrooms c ON c.id=a.classroom_id
                                  JOIN users u ON u.id=a.updated_by WHERE {' AND '.join(where)}
                                  ORDER BY c.name COLLATE NOCASE,s.name COLLATE NOCASE""",params).fetchall()
                self.send_json(200,{"attendance":[dict(r) for r in rows]}); return

            if path == "/api/alerts":
                self.send_json(200,{"alerts":attendance_alerts(db,user)}); return

            if path == "/api/schedule/today":
                date=(query.get("date") or [datetime.now().strftime("%Y-%m-%d")])[0]
                try: weekday=datetime.strptime(date,"%Y-%m-%d").weekday()
                except ValueError: raise ValueError("Fecha inválida.")
                rows=db.execute("""SELECT si.*,c.name AS classroom_name FROM schedule_items si LEFT JOIN classrooms c ON c.id=si.classroom_id
                                  WHERE si.owner_id=? AND (si.event_date=? OR (si.repeat_weekly=1 AND si.weekday=? AND si.event_date<=?))
                                  ORDER BY CASE WHEN si.start_time='' THEN 1 ELSE 0 END,si.start_time,si.title COLLATE NOCASE""",(user["id"],date,weekday,date)).fetchall()
                self.send_json(200,{"items":[dict(r) for r in rows]}); return

            if path == "/api/schedule":
                start=(query.get("from") or [datetime.now().strftime("%Y-%m-%d")])[0]
                end=(query.get("to") or [(datetime.now()+timedelta(days=60)).strftime("%Y-%m-%d")])[0]
                rows=db.execute("""SELECT si.*,c.name AS classroom_name FROM schedule_items si LEFT JOIN classrooms c ON c.id=si.classroom_id
                                  WHERE si.owner_id=? AND (si.event_date BETWEEN ? AND ? OR si.repeat_weekly=1)
                                  ORDER BY si.event_date,si.start_time,si.title COLLATE NOCASE""",(user["id"],start,end)).fetchall()
                self.send_json(200,{"items":[dict(r) for r in rows]}); return

            if path == "/api/assessments":
                classroom_id=(query.get("classroom_id") or [""])[0]
                if not classroom_id or not user_has_classroom(db,user,classroom_id): self.send_json(403,{"error":"No tenés acceso a esa aula."}); return
                rows=db.execute("""SELECT a.*,u.name AS creator_name,COUNT(g.id) AS graded_count FROM assessments a
                                  JOIN users u ON u.id=a.creator_id LEFT JOIN grades g ON g.assessment_id=a.id
                                  WHERE a.classroom_id=? GROUP BY a.id ORDER BY a.assessment_date DESC,a.title COLLATE NOCASE""",(classroom_id,)).fetchall()
                self.send_json(200,{"assessments":[dict(r) for r in rows]}); return

            if path.startswith("/api/assessments/") and path.endswith("/grades"):
                assessment_id=path.split("/")[3]
                assessment=db.execute("SELECT a.*,c.name AS classroom_name FROM assessments a JOIN classrooms c ON c.id=a.classroom_id WHERE a.id=?",(assessment_id,)).fetchone()
                if not assessment or not user_has_classroom(db,user,assessment["classroom_id"]): self.send_json(404,{"error":"Evaluación no encontrada."}); return
                students=db.execute("""SELECT s.id,s.name,s.record_number,g.score,g.comment,g.updated_at,u.name AS updated_by_name
                                     FROM students s LEFT JOIN grades g ON g.student_id=s.id AND g.assessment_id=?
                                     LEFT JOIN users u ON u.id=g.updated_by WHERE s.classroom_id=? AND s.active=1 ORDER BY s.name COLLATE NOCASE""",(assessment_id,assessment["classroom_id"])).fetchall()
                self.send_json(200,{"assessment":dict(assessment),"students":[dict(r) for r in students],"can_edit":role_can_edit_grades(user)}); return

            if path == "/api/reports/mine":
                rows=db.execute("""SELECT r.*,s.name AS student_name,s.record_number,c.name AS classroom_name FROM reports r
                                  JOIN students s ON s.id=r.student_id JOIN classrooms c ON c.id=r.classroom_id
                                  WHERE r.reporter_id=? ORDER BY r.created_at DESC""",(user["id"],)).fetchall()
                self.send_json(200,{"reports":[dict(r) for r in rows]}); return

            if path == "/api/reports":
                if user["role"]=="docente":
                    rows=db.execute("""SELECT r.*,s.name AS student_name,c.name AS classroom_name,u.name AS reporter_name FROM reports r
                                      JOIN students s ON s.id=r.student_id JOIN classrooms c ON c.id=r.classroom_id JOIN users u ON u.id=r.reporter_id
                                      WHERE r.reporter_id=? ORDER BY r.created_at DESC""",(user["id"],)).fetchall()
                elif user["role"]=="preceptor":
                    allowed=classroom_ids_for_user(db,user)
                    if not allowed: rows=[]
                    else:
                        rows=db.execute(f"""SELECT r.*,s.name AS student_name,c.name AS classroom_name,u.name AS reporter_name FROM reports r
                                           JOIN students s ON s.id=r.student_id JOIN classrooms c ON c.id=r.classroom_id JOIN users u ON u.id=r.reporter_id
                                           WHERE r.classroom_id IN ({','.join('?' for _ in allowed)}) ORDER BY r.created_at DESC""",allowed).fetchall()
                else:
                    rows=db.execute("""SELECT r.*,s.name AS student_name,c.name AS classroom_name,u.name AS reporter_name FROM reports r
                                      JOIN students s ON s.id=r.student_id JOIN classrooms c ON c.id=r.classroom_id JOIN users u ON u.id=r.reporter_id
                                      ORDER BY r.created_at DESC""").fetchall()
                self.send_json(200,{"reports":[dict(r) for r in rows]}); return

            if path.startswith("/api/reports/") and path.endswith("/comments"):
                report_id=path.split("/")[3]
                report=db.execute("SELECT * FROM reports WHERE id=?",(report_id,)).fetchone()
                if not report or not report_visible_to_user(db,user,report): self.send_json(404,{"error":"Reporte no encontrado."}); return
                rows=db.execute("""SELECT rc.*,u.name AS author_name,u.role AS author_role FROM report_comments rc JOIN users u ON u.id=rc.author_id
                                  WHERE rc.report_id=? ORDER BY rc.created_at""",(report_id,)).fetchall()
                self.send_json(200,{"comments":[dict(r) for r in rows]}); return

            if path == "/api/activity":
                scope=(query.get("scope") or ["mine"])[0]
                try: limit=max(1,min(200,int((query.get("limit") or ["50"])[0])))
                except ValueError: limit=50
                if scope=="all" and role_can_admin(user):
                    rows=db.execute("""SELECT a.*,u.name AS user_name,u.role AS user_role FROM activity a JOIN users u ON u.id=a.user_id
                                      ORDER BY a.created_at DESC LIMIT ?""",(limit,)).fetchall()
                else:
                    rows=db.execute("""SELECT a.*,u.name AS user_name,u.role AS user_role FROM activity a JOIN users u ON u.id=a.user_id
                                      WHERE a.user_id=? ORDER BY a.created_at DESC LIMIT ?""",(user["id"],limit)).fetchall()
                self.send_json(200,{"activity":[dict(r) for r in rows]}); return

            if path == "/api/settings":
                base={
                    "institution_name":get_setting(db,"institution_name",""),"academic_year":get_setting(db,"academic_year",str(datetime.now().year)),
                    "education_level":get_setting(db,"education_level",""),"shifts":get_setting(db,"shifts",""),
                    "attendance_threshold":float(get_setting(db,"attendance_threshold","75")),
                    "absence_alert_count":int(float(get_setting(db,"absence_alert_count","3") or 3)),
                    "late_alert_count":int(float(get_setting(db,"late_alert_count","5") or 5)),
                    "default_language":get_setting(db,"default_language","es")
                }
                if role_can_admin(user):
                    client_id,_=google_client_config(db)
                    base.update({"auto_backup_enabled":parse_bool(get_setting(db,"auto_backup_enabled","1"),True),"backup_interval_hours":int(float(get_setting(db,"backup_interval_hours","24") or 24)),"backup_keep":int(float(get_setting(db,"backup_keep","30") or 30)),"session_hours":session_hours(db),"public_base_url":get_setting(db,"public_base_url",""),"google_client_id":client_id,"google_client_secret_configured":bool(get_setting(db,"google_client_secret_protected","") or os.environ.get("PRESENCIALIDAD_GOOGLE_CLIENT_SECRET")),"google_storage_available":google_secret_storage_available(),"data_dir":str(DATA_DIR)})
                self.send_json(200,base); return

            if path == "/api/admin/users":
                admin=self.require_admin(db)
                if not admin: return
                rows=db.execute("SELECT * FROM users ORDER BY name COLLATE NOCASE").fetchall(); users=[]
                for r in rows:
                    d=user_public(db,r); d.pop("permissions",None); users.append(d)
                self.send_json(200,{"users":users}); return

            if path.startswith("/api/export/"):
                self.handle_export_get(db,user,path,query); return

            self.send_json(404,{"error":"Endpoint no encontrado."})

    def handle_export_get(self, db: sqlite3.Connection, user: sqlite3.Row, path: str, query: dict[str,list[str]]) -> None:
        kind=path.split("/")[-1]
        classroom_id=(query.get("classroom_id") or ["all"])[0]
        month=(query.get("month") or [datetime.now().strftime("%Y-%m")])[0]
        student_id=(query.get("student_id") or [""])[0]
        allowed=classroom_ids_for_user(db,user)
        if classroom_id!="all" and classroom_id not in allowed:
            self.send_json(403,{"error":"No tenés acceso a esa aula."}); return

        def attendance_rows():
            clauses=["a.date LIKE ?"]; params=[month+"%"]
            if classroom_id!="all":
                clauses.append("a.classroom_id=?"); params.append(classroom_id)
            elif user["role"] not in {"directivo","administrador"}:
                if not allowed: return []
                clauses.append("a.classroom_id IN (%s)" % ",".join("?" for _ in allowed)); params.extend(allowed)
            return db.execute(f"""SELECT a.date,s.name AS student_name,s.record_number,c.name AS classroom_name,a.status,a.reason,a.arrival_time,a.departure_time,u.name AS updated_by_name
                              FROM attendance a JOIN students s ON s.id=a.student_id JOIN classrooms c ON c.id=a.classroom_id JOIN users u ON u.id=a.updated_by
                              WHERE {' AND '.join(clauses)} ORDER BY a.date,c.name COLLATE NOCASE,s.name COLLATE NOCASE""",params).fetchall()

        def student_rows():
            if classroom_id=="all":
                if not allowed: return []
                return db.execute(f"SELECT s.record_number,s.name,c.name AS classroom_name,s.dni,s.birth_date FROM students s JOIN classrooms c ON c.id=s.classroom_id WHERE s.active=1 AND s.classroom_id IN ({','.join('?' for _ in allowed)}) ORDER BY c.name,s.name",allowed).fetchall()
            return db.execute("SELECT s.record_number,s.name,c.name AS classroom_name,s.dni,s.birth_date FROM students s JOIN classrooms c ON c.id=s.classroom_id WHERE s.active=1 AND s.classroom_id=? ORDER BY s.name",(classroom_id,)).fetchall()

        def report_rows():
            if user["role"]=="docente":
                return db.execute("""SELECT r.created_at,s.name,c.name AS classroom_name,r.type,r.priority,r.status,r.description,u.name AS reporter_name FROM reports r JOIN students s ON s.id=r.student_id JOIN classrooms c ON c.id=r.classroom_id JOIN users u ON u.id=r.reporter_id WHERE r.reporter_id=? ORDER BY r.created_at DESC""",(user["id"],)).fetchall()
            if not allowed: return []
            return db.execute(f"""SELECT r.created_at,s.name,c.name AS classroom_name,r.type,r.priority,r.status,r.description,u.name AS reporter_name FROM reports r JOIN students s ON s.id=r.student_id JOIN classrooms c ON c.id=r.classroom_id JOIN users u ON u.id=r.reporter_id WHERE r.classroom_id IN ({','.join('?' for _ in allowed)}) ORDER BY r.created_at DESC""",allowed).fetchall()

        if kind in {"attendance.csv","attendance.pdf"}:
            rows=attendance_rows()
            if kind.endswith(".csv"):
                out=io.StringIO(); w=csv.writer(out); w.writerow(["Fecha","Alumno","Legajo","Aula","Estado","Motivo","Hora llegada","Hora retiro","Actualizado por"])
                for r in rows: w.writerow([r["date"],r["student_name"],r["record_number"],r["classroom_name"],r["status"],r["reason"],r["arrival_time"],r["departure_time"],r["updated_by_name"]])
                self.send_bytes(200,out.getvalue().encode("utf-8-sig"),"text/csv; charset=utf-8",f"asistencia-{month}.csv"); return
            counts={s:0 for s in ATTENDANCE_STATUSES}
            for r in rows: counts[r["status"]]=counts.get(r["status"],0)+1
            total=len(rows); attended=counts["present"]+counts["late"]+counts["early_departure"]; pct=(attended/total*100) if total else 0
            lines=[f"Periodo: {month}",f"Registros: {total}",f"Asistencia: {pct:.1f}%",f"Presentes: {counts['present']} | Ausentes: {counts['absent']} | Tarde: {counts['late']} | Justificadas: {counts['justified']} | Retiros: {counts['early_departure']}",""]
            lines += [f"{r['date']} | {r['classroom_name']} | {r['student_name']} | {r['status']}" + (f" | {r['reason']}" if r['reason'] else "") for r in rows]
            self.send_bytes(200,simple_pdf(f"Resumen de asistencia - {month}",lines),"application/pdf",f"asistencia-{month}.pdf"); return

        if kind in {"students.csv","students.pdf"}:
            rows=student_rows()
            if kind.endswith(".csv"):
                out=io.StringIO(); w=csv.writer(out); w.writerow(["Legajo","Nombre","Aula","DNI","Fecha de nacimiento"])
                for r in rows: w.writerow([r["record_number"],r["name"],r["classroom_name"],r["dni"] if role_can_view_contacts(user) else "",r["birth_date"]])
                self.send_bytes(200,out.getvalue().encode("utf-8-sig"),"text/csv; charset=utf-8","alumnos.csv"); return
            lines=[]
            for r in rows:
                extra=f" | DNI {r['dni']}" if role_can_view_contacts(user) and r['dni'] else ""
                lines.append(f"{r['record_number']} | {r['name']} | {r['classroom_name']}{extra}")
            self.send_bytes(200,simple_pdf("Listado de alumnos",lines or ["Sin alumnos para la selección."]),"application/pdf","alumnos.pdf"); return

        if kind in {"reports.csv","reports.pdf"}:
            rows=report_rows()
            if kind.endswith(".csv"):
                out=io.StringIO(); w=csv.writer(out); w.writerow(["Fecha","Alumno","Aula","Tipo","Nivel","Estado","Descripción","Autor"])
                for r in rows: w.writerow([r["created_at"],r["name"],r["classroom_name"],r["type"],r["priority"],r["status"],r["description"],r["reporter_name"]])
                self.send_bytes(200,out.getvalue().encode("utf-8-sig"),"text/csv; charset=utf-8","reportes.csv"); return
            lines=[f"{r['created_at'][:16]} | {r['name']} | {r['classroom_name']} | {r['type']} | {r['status']} | {r['reporter_name']}\n{r['description']}" for r in rows]
            self.send_bytes(200,simple_pdf("Reportes de alumnos",lines or ["Sin reportes para la selección."]),"application/pdf","reportes.pdf"); return

        if kind in {"student-history.csv","student-history.pdf"}:
            if not student_id:
                raise ValueError("Falta el alumno.")
            student=db.execute("SELECT s.*,c.name AS classroom_name FROM students s JOIN classrooms c ON c.id=s.classroom_id WHERE s.id=?",(student_id,)).fetchone()
            if not student or not user_has_classroom(db,user,student["classroom_id"]):
                self.send_json(404,{"error":"Alumno no encontrado."}); return
            rows=db.execute("""SELECT a.date,a.status,a.reason,a.arrival_time,a.departure_time,u.name AS updated_by_name FROM attendance a JOIN users u ON u.id=a.updated_by WHERE a.student_id=? ORDER BY a.date DESC""",(student_id,)).fetchall()
            if kind.endswith(".csv"):
                out=io.StringIO(); w=csv.writer(out); w.writerow(["Fecha","Estado","Motivo","Hora llegada","Hora retiro","Actualizado por"])
                for r in rows: w.writerow([r["date"],r["status"],r["reason"],r["arrival_time"],r["departure_time"],r["updated_by_name"]])
                self.send_bytes(200,out.getvalue().encode("utf-8-sig"),"text/csv; charset=utf-8",f"historial-{student['record_number']}.csv"); return
            summary=attendance_summary(db,student_id)
            lines=[f"Alumno: {student['name']}",f"Legajo: {student['record_number']} | Aula: {student['classroom_name']}",f"Asistencia: {summary.get('attendance_rate') or 0:.1f}%",""]
            lines += [f"{r['date']} | {r['status']}" + (f" | {r['reason']}" if r['reason'] else "") for r in rows]
            self.send_bytes(200,simple_pdf("Historial individual de asistencia",lines),"application/pdf",f"historial-{student['record_number']}.pdf"); return

        self.send_json(404,{"error":"Exportación no encontrada."})

    def handle_api_post(self, path: str, query: dict[str,list[str]]) -> None:
        data=self.read_json()
        if path=="/api/setup":
            institution=str(data.get("institution_name","")).strip();name=str(data.get("name","")).strip();email=str(data.get("email","")).strip().lower();password=str(data.get("password",""));year=str(data.get("academic_year",datetime.now().year)).strip();language=str(data.get("language","es"))
            if not institution or not name or not email:raise ValueError("Institución, nombre y correo son obligatorios.")
            if not valid_email(email):raise ValueError("El correo no es válido.")
            if len(password)<10:raise ValueError("La contraseña inicial debe tener al menos 10 caracteres.")
            if language not in {"es","en","ja","ko","zh","ru","ar"}:language="es"
            with connect_db() as db:
                if not setup_required(db):self.send_json(409,{"error":"Esta instalación ya fue configurada."});return
                uid=create_user(db,email=email,name=name,role="administrador",password=password,institution=institution,preferred_language=language)
                for key,value in {"institution_name":institution,"academic_year":year,"default_language":language,"setup_complete":"1"}.items():db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,value))
                token,csrf,expires=create_session(db,uid,self.headers.get("User-Agent",""));db.execute("UPDATE users SET last_login=? WHERE id=?",(iso_now(),uid));log_activity(db,uid,"setup","Completó la configuración inicial y creó la primera cuenta administradora.")
                user=db.execute("SELECT u.*,s.id AS session_id,s.expires_at,s.csrf_token,s.created_at AS session_created_at,s.last_seen AS session_last_seen,s.user_agent AS session_user_agent FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",(session_token_hash(token),)).fetchone();hours=session_hours(db)
                self.send_json(201,{"ok":True,"user":user_public(db,user),"csrf_token":csrf},cookie=self.session_cookie(token,hours*3600));return
        if path=="/api/auth/login":
            email=str(data.get("email","")).strip().lower(); password=str(data.get("password",""));ip=self.client_address[0] if self.client_address else "local"
            with connect_db() as db:
                if setup_required(db):self.send_json(409,{"error":"Primero completá la configuración inicial.","setup_required":True});return
                key=login_attempt_key(email,ip);blocked,until=login_is_blocked(db,key)
                if blocked:self.send_json(429,{"error":"Demasiados intentos. Esperá unos minutos antes de volver a intentar.","blocked_until":until});return
                user=db.execute("SELECT * FROM users WHERE email=? AND active=1",(email,)).fetchone()
                if not user or not verify_password(password,user["password_salt"],user["password_hash"]):
                    blocked_until=register_login_failure(db,key);time.sleep(.18);self.send_json(401,{"error":"Correo o contraseña incorrectos.","blocked_until":blocked_until});return
                clear_login_attempt(db,key);token,csrf,expires=create_session(db,user["id"],self.headers.get("User-Agent",""));db.execute("UPDATE users SET last_login=? WHERE id=?",(iso_now(),user["id"]));log_activity(db,user["id"],"login","Inició sesión.")
                updated=db.execute("SELECT * FROM users WHERE id=?",(user["id"],)).fetchone();hours=session_hours(db)
                self.send_json(200,{"user":user_public(db,updated),"csrf_token":csrf,"google":get_google_status(db,user["id"])},cookie=self.session_cookie(token,hours*3600)); return
        with connect_db() as db:
            user=self.require_user(db)
            if not user: return
            if not self.require_csrf(user): return
            if path=="/api/auth/logout":
                token=self.get_cookie_token()
                if token: db.execute("DELETE FROM sessions WHERE token_hash=?",(session_token_hash(token),))
                log_activity(db,user["id"],"logout","Cerró sesión."); self.send_json(200,{"ok":True},cookie=self.session_cookie("",0)); return
            if path=="/api/auth/revoke-others":
                deleted=db.execute("DELETE FROM sessions WHERE user_id=? AND id<>?",(user["id"],user["session_id"])).rowcount;log_activity(db,user["id"],"sessions_revoke",f"Cerró {deleted} sesiones adicionales.");self.send_json(200,{"ok":True,"deleted":deleted});return
            if path=="/api/auth/change-password":
                current=str(data.get("current_password","")); new=str(data.get("new_password",""))
                if len(new)<10: raise ValueError("La nueva contraseña debe tener al menos 10 caracteres.")
                if not verify_password(current,user["password_salt"],user["password_hash"]): self.send_json(403,{"error":"La contraseña actual no coincide."}); return
                salt,digest=password_hash(new); db.execute("UPDATE users SET password_salt=?,password_hash=? WHERE id=?",(salt,digest,user["id"])); log_activity(db,user["id"],"password_change","Cambió su contraseña."); self.send_json(200,{"ok":True}); return
            if path=="/api/backups":
                admin=self.require_admin(db)
                if not admin:return
                path_obj=create_database_backup(prefix="ManualBackup");prune_backups(max(3,int(float(get_setting(db,"backup_keep","30") or 30))));log_activity(db,admin["id"],"backup_create",f"Creó la copia {path_obj.name}.");self.send_json(201,{"ok":True,"backup":{"name":path_obj.name,"bytes":path_obj.stat().st_size}});return
            if path=="/api/backups/restore":
                admin=self.require_admin(db)
                if not admin:return
                name=str(data.get("name","")).strip();confirm=str(data.get("confirm",""))
                if confirm!="RESTAURAR":raise ValueError("Escribí RESTAURAR para confirmar la restauración.")
                if not re.fullmatch(r"[A-Za-z0-9_.-]+\.db",name):raise ValueError("Nombre de copia inválido.")
                source=(BACKUP_DIR/name).resolve()
                if source.parent!=BACKUP_DIR.resolve() or not source.exists():raise ValueError("La copia no existe.")
                safety=create_database_backup(prefix="PreRestore");db.commit();probe=sqlite3.connect(source)
                try:
                    if probe.execute("PRAGMA integrity_check").fetchone()[0]!="ok":raise ValueError("La copia seleccionada no supera la comprobación de integridad.")
                    probe.backup(db);db.execute("DELETE FROM sessions");db.commit()
                finally:probe.close()
                append_log("WARN",f"Base restaurada desde {name}; copia previa {safety.name}");self.send_json(200,{"ok":True,"restored":name,"safety_backup":safety.name},cookie=self.session_cookie("",0));return
            if path.startswith("/api/notices/") and path.endswith("/send-gmail"):
                nid=path.split("/")[3];notice=db.execute("""SELECT fn.*,s.name AS student_name,s.guardian_email FROM family_notices fn JOIN students s ON s.id=fn.student_id WHERE fn.id=?""",(nid,)).fetchone()
                if not notice or not user_has_classroom(db,user,notice["classroom_id"]):self.send_json(404,{"error":"Aviso no encontrado."});return
                if notice["author_id"]!=user["id"] and not role_can_review_reports(user):self.send_json(403,{"error":"No tenés permisos para enviar ese aviso."});return
                if not notice["guardian_email"]:raise ValueError("El alumno no tiene un correo familiar registrado.")
                institution=get_setting(db,"institution_name",user["institution"] or APP_NAME);subject=f"{institution} · {notice['reason']} · {notice['student_name']}";result=send_gmail(db,user["id"],to_email=notice["guardian_email"],subject=subject,body_text=notice["message"]);stamp=iso_now();db.execute("UPDATE family_notices SET status='sent',sent_at=? WHERE id=?",(stamp,nid));log_activity(db,user["id"],"family_notice_gmail",f"Envió por Gmail un aviso a {notice['guardian_email']} para {notice['student_name']}.");self.send_json(200,{"ok":True,"message_id":result.get("id",""),"sent_at":stamp});return
            if path.startswith("/api/students/") and path.endswith("/restore"):
                admin=self.require_admin(db)
                if not admin:return
                sid=path.split("/")[3];row=db.execute("SELECT s.name,s.classroom_id,c.archived FROM students s JOIN classrooms c ON c.id=s.classroom_id WHERE s.id=?",(sid,)).fetchone()
                if not row:self.send_json(404,{"error":"Alumno no encontrado."});return
                if row["archived"]:self.send_json(409,{"error":"Restaurá primero el aula del alumno."});return
                db.execute("UPDATE students SET active=1,updated_at=? WHERE id=?",(iso_now(),sid));log_activity(db,admin["id"],"student_restore",f"Restauró a {row['name']}.");self.send_json(200,{"ok":True});return
            if path=="/api/classrooms":
                admin=self.require_admin(db)
                if not admin: return
                name=str(data.get("name","")).strip()
                if not name: raise ValueError("El nombre del aula es obligatorio.")
                archived=db.execute("SELECT id FROM classrooms WHERE name=? COLLATE NOCASE AND archived=1",(name,)).fetchone()
                if archived:
                    cid=archived["id"];db.execute("UPDATE classrooms SET archived=0 WHERE id=?",(cid,));log_activity(db,admin["id"],"classroom_restore",f"Restauró el aula {name}.")
                else:
                    cid=new_id("class");db.execute("INSERT INTO classrooms(id,name,created_at,archived) VALUES(?,?,?,0)",(cid,name,iso_now()));log_activity(db,admin["id"],"classroom_create",f"Creó el aula {name}.")
                self.send_json(201,{"id":cid,"name":name}); return
            if path=="/api/students":
                admin=self.require_admin(db)
                if not admin:return
                payload=validate_student_payload(data)
                if not db.execute("SELECT 1 FROM classrooms WHERE id=?",(payload["classroom_id"],)).fetchone(): raise ValueError("El aula seleccionada no existe.")
                sid=new_id("student"); db.execute("""INSERT INTO students(id,record_number,name,classroom_id,dni,birth_date,guardian_name,guardian_phone,guardian_email,emergency_name,emergency_phone,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (sid,payload["record_number"],payload["name"],payload["classroom_id"],payload["dni"],payload["birth_date"],payload["guardian_name"],payload["guardian_phone"],payload["guardian_email"],payload["emergency_name"],payload["emergency_phone"],iso_now(),iso_now()))
                log_activity(db,admin["id"],"student_create",f"Registró a {payload['name']} (legajo {payload['record_number']})."); self.send_json(201,{"id":sid}); return
            if path=="/api/google/test-gmail":
                status=get_google_status(db,user["id"])
                if not status.get("connected") or not status.get("gmail_enabled"):
                    raise ValueError("Primero conectá Google y habilitá el permiso de envío con Gmail.")
                target=status.get("email","")
                institution=get_setting(db,"institution_name",user["institution"] or APP_NAME)
                subject=f"{institution} · Prueba de Gmail"
                body_text=(f"Este es un correo de prueba enviado por {APP_NAME} {APP_VERSION}.\n\n"
                           "La conexión con Gmail está funcionando y solo tiene permiso para enviar mensajes.")
                result=send_gmail(db,user["id"],to_email=target,subject=subject,body_text=body_text)
                log_activity(db,user["id"],"gmail_test",f"Envió un correo de prueba a su cuenta Google {target}.")
                self.send_json(200,{"ok":True,"message_id":result.get("id",""),"to":target});return

            if path=="/api/attendance/draft":
                date=str(data.get("date","")).strip(); classroom_id=str(data.get("classroom_id","")).strip(); records=data.get("records",[])
                if not date or not classroom_id or not isinstance(records,list): raise ValueError("Borrador de asistencia inválido.")
                if not user_has_classroom(db,user,classroom_id): self.send_json(403,{"error":"No tenés acceso a esa aula."}); return
                payload=json.dumps({"records":records},ensure_ascii=False); stamp=iso_now()
                db.execute("""INSERT INTO attendance_drafts(id,user_id,classroom_id,date,payload_json,updated_at) VALUES(?,?,?,?,?,?)
                            ON CONFLICT(user_id,classroom_id,date) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                           (new_id("draft"),user["id"],classroom_id,date,payload,stamp)); self.send_json(200,{"ok":True,"updated_at":stamp}); return
            if path=="/api/attendance/save":
                date=str(data.get("date","")).strip(); classroom_id=str(data.get("classroom_id","")).strip(); records=data.get("records")
                if not date or not classroom_id or not isinstance(records,list): raise ValueError("La asistencia está incompleta.")
                if not user_has_classroom(db,user,classroom_id): self.send_json(403,{"error":"No tenés acceso a esa aula."}); return
                classroom=db.execute("SELECT name FROM classrooms WHERE id=?",(classroom_id,)).fetchone()
                valid_students={r["id"]:r for r in db.execute("SELECT id,name FROM students WHERE classroom_id=? AND active=1",(classroom_id,)).fetchall()}
                if len(records)!=len(valid_students): raise ValueError("Debés registrar el estado de todos los alumnos del aula.")
                seen=set(); changes=0; stamp=iso_now()
                for item in records:
                    sid=str(item.get("student_id","")); status=str(item.get("status","")); reason=str(item.get("reason","")).strip()[:240]
                    arrival=normalize_time(item.get("arrival_time","")); departure=normalize_time(item.get("departure_time",""))
                    if sid not in valid_students or sid in seen or status not in ATTENDANCE_STATUSES: raise ValueError("Hay un registro de asistencia inválido.")
                    seen.add(sid)
                    if status!="late": arrival=""
                    if status!="early_departure": departure=""
                    if status not in {"absent","justified","early_departure"}: reason=""
                    previous=db.execute("SELECT * FROM attendance WHERE date=? AND student_id=?",(date,sid)).fetchone()
                    new_details={"reason":reason,"arrival_time":arrival,"departure_time":departure}
                    if previous:
                        old_details={"reason":previous["reason"],"arrival_time":previous["arrival_time"],"departure_time":previous["departure_time"]}
                        changed=previous["status"]!=status or old_details!=new_details
                        if changed:
                            db.execute("UPDATE attendance SET status=?,reason=?,arrival_time=?,departure_time=?,classroom_id=?,updated_by=?,updated_at=? WHERE id=?",
                                       (status,reason,arrival,departure,classroom_id,user["id"],stamp,previous["id"]))
                            db.execute("INSERT INTO attendance_audit(id,attendance_id,date,student_id,classroom_id,old_status,new_status,old_details,new_details,changed_by,changed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                       (new_id("audit"),previous["id"],date,sid,classroom_id,previous["status"],status,json.dumps(old_details,ensure_ascii=False),json.dumps(new_details,ensure_ascii=False),user["id"],stamp))
                            log_activity(db,user["id"],"attendance_change",f"Modificó a {valid_students[sid]['name']}: {previous['status']} → {status} ({classroom['name']}, {date})."); changes+=1
                    else:
                        aid=new_id("att"); db.execute("INSERT INTO attendance(id,date,student_id,classroom_id,status,reason,arrival_time,departure_time,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (aid,date,sid,classroom_id,status,reason,arrival,departure,user["id"],user["id"],stamp,stamp))
                        db.execute("INSERT INTO attendance_audit(id,attendance_id,date,student_id,classroom_id,old_status,new_status,old_details,new_details,changed_by,changed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (new_id("audit"),aid,date,sid,classroom_id,None,status,"{}",json.dumps(new_details,ensure_ascii=False),user["id"],stamp)); changes+=1
                db.execute("DELETE FROM attendance_drafts WHERE user_id=? AND classroom_id=? AND date=?",(user["id"],classroom_id,date))
                log_activity(db,user["id"],"attendance_save",f"Guardó asistencia de {classroom['name']} para {date}. {changes} cambios.")
                self.send_json(200,{"ok":True,"changes":changes,"saved_at":stamp}); return
            if path=="/api/reports":
                student_id=str(data.get("student_id","")).strip(); report_type=str(data.get("type","")).strip(); description=str(data.get("description","")).strip(); priority=str(data.get("priority","informativo")).strip()
                if report_type not in REPORT_TYPES: raise ValueError("Tipo de reporte inválido.")
                if priority not in REPORT_PRIORITIES: raise ValueError("Nivel de reporte inválido.")
                if not description: raise ValueError("La descripción es obligatoria.")
                student=db.execute("SELECT s.id,s.name,s.classroom_id,c.name AS classroom_name FROM students s JOIN classrooms c ON c.id=s.classroom_id WHERE s.id=? AND s.active=1",(student_id,)).fetchone()
                if not student or not user_has_classroom(db,user,student["classroom_id"]): self.send_json(403,{"error":"No tenés acceso a ese alumno."}); return
                rid=new_id("report"); stamp=iso_now(); db.execute("INSERT INTO reports(id,student_id,classroom_id,reporter_id,type,description,priority,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (rid,student_id,student["classroom_id"],user["id"],report_type,description,priority,"enviado",stamp,stamp)); log_activity(db,user["id"],"report_create",f"Registró una situación de {student['name']} ({student['classroom_name']})."); self.send_json(201,{"id":rid,"status":"enviado"}); return
            if path.startswith("/api/reports/") and path.endswith("/comments"):
                report_id=path.split("/")[3]; comment=str(data.get("comment","")).strip()
                if not comment: raise ValueError("El comentario no puede quedar vacío.")
                report=db.execute("SELECT * FROM reports WHERE id=?",(report_id,)).fetchone()
                if not report or not report_visible_to_user(db,user,report): self.send_json(404,{"error":"Reporte no encontrado."}); return
                db.execute("INSERT INTO report_comments(id,report_id,author_id,comment,created_at) VALUES(?,?,?,?,?)",(new_id("comment"),report_id,user["id"],comment,iso_now())); log_activity(db,user["id"],"report_comment","Agregó un seguimiento a un reporte."); self.send_json(201,{"ok":True}); return
            if path=="/api/notes":
                student_id=str(data.get("student_id","")).strip(); note=str(data.get("note","")).strip()
                if not note: raise ValueError("La nota no puede quedar vacía.")
                student=db.execute("SELECT id,name,classroom_id FROM students WHERE id=? AND active=1",(student_id,)).fetchone()
                if not student or not user_has_classroom(db,user,student["classroom_id"]): self.send_json(403,{"error":"No tenés acceso a ese alumno."}); return
                nid=new_id("note"); stamp=iso_now(); db.execute("INSERT INTO teacher_notes(id,student_id,classroom_id,author_id,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(nid,student_id,student["classroom_id"],user["id"],note,stamp,stamp)); log_activity(db,user["id"],"teacher_note_create",f"Agregó una nota privada sobre {student['name']}."); self.send_json(201,{"id":nid}); return
            if path=="/api/notices":
                student_id=str(data.get("student_id","")).strip(); reason=str(data.get("reason","")).strip(); message=str(data.get("message","")).strip(); mark_sent=bool(data.get("mark_sent",False))
                if not reason or not message: raise ValueError("Motivo y mensaje son obligatorios.")
                student=db.execute("SELECT id,name,classroom_id FROM students WHERE id=? AND active=1",(student_id,)).fetchone()
                if not student or not user_has_classroom(db,user,student["classroom_id"]): self.send_json(403,{"error":"No tenés acceso a ese alumno."}); return
                status="sent" if mark_sent else "generated"; stamp=iso_now(); nid=new_id("notice")
                db.execute("INSERT INTO family_notices(id,student_id,classroom_id,author_id,reason,message,status,created_at,sent_at) VALUES(?,?,?,?,?,?,?,?,?)",(nid,student_id,student["classroom_id"],user["id"],reason,message,status,stamp,stamp if mark_sent else None)); log_activity(db,user["id"],"family_notice",f"Generó un aviso para la familia de {student['name']}."); self.send_json(201,{"id":nid,"status":status}); return
            if path=="/api/assessments":
                classroom_id=str(data.get("classroom_id","")).strip(); title=str(data.get("title","")).strip(); assessment_date=str(data.get("assessment_date","")).strip()
                try: max_score=float(data.get("max_score",10))
                except (ValueError,TypeError): raise ValueError("Puntaje máximo inválido.")
                if not role_can_edit_grades(user): self.send_json(403,{"error":"Tu rol no puede crear evaluaciones."}); return
                if not classroom_id or not user_has_classroom(db,user,classroom_id): self.send_json(403,{"error":"No tenés acceso a esa aula."}); return
                if not title or not assessment_date or max_score<=0: raise ValueError("Completá título, fecha y puntaje máximo.")
                aid=new_id("assessment"); db.execute("INSERT INTO assessments(id,classroom_id,creator_id,title,assessment_date,max_score,created_at) VALUES(?,?,?,?,?,?,?)",(aid,classroom_id,user["id"],title,assessment_date,max_score,iso_now())); log_activity(db,user["id"],"assessment_create",f"Creó la evaluación {title}."); self.send_json(201,{"id":aid}); return
            if path.startswith("/api/assessments/") and path.endswith("/grades"):
                if not role_can_edit_grades(user): self.send_json(403,{"error":"Tu rol no puede modificar calificaciones."}); return
                aid=path.split("/")[3]; assessment=db.execute("SELECT * FROM assessments WHERE id=?",(aid,)).fetchone()
                if not assessment or not user_has_classroom(db,user,assessment["classroom_id"]): self.send_json(404,{"error":"Evaluación no encontrada."}); return
                records=data.get("records",[])
                if not isinstance(records,list): raise ValueError("Calificaciones inválidas.")
                valid={r["id"] for r in db.execute("SELECT id FROM students WHERE classroom_id=? AND active=1",(assessment["classroom_id"],)).fetchall()}; updated=0
                for item in records:
                    sid=str(item.get("student_id","")); score=item.get("score",None); comment=str(item.get("comment","")).strip()[:300]
                    if sid not in valid: continue
                    if score in ("",None): score_value=None
                    else:
                        try: score_value=float(score)
                        except (ValueError,TypeError): raise ValueError("Hay una nota inválida.")
                        if score_value<0 or score_value>assessment["max_score"]: raise ValueError("Una nota está fuera del rango de la evaluación.")
                    existing=db.execute("SELECT id FROM grades WHERE assessment_id=? AND student_id=?",(aid,sid)).fetchone(); stamp=iso_now()
                    if existing: db.execute("UPDATE grades SET score=?,comment=?,updated_by=?,updated_at=? WHERE id=?",(score_value,comment,user["id"],stamp,existing["id"]))
                    else: db.execute("INSERT INTO grades(id,assessment_id,student_id,score,comment,updated_by,updated_at) VALUES(?,?,?,?,?,?,?)",(new_id("grade"),aid,sid,score_value,comment,user["id"],stamp))
                    updated+=1
                log_activity(db,user["id"],"grades_save",f"Actualizó {updated} calificaciones de {assessment['title']}."); self.send_json(200,{"ok":True,"updated":updated}); return
            if path=="/api/schedule":
                kind=str(data.get("kind","reminder")); title=str(data.get("title","")).strip(); event_date=str(data.get("event_date","")).strip(); start_time=normalize_time(data.get("start_time","")); note=str(data.get("note","")).strip()[:1000]; repeat_weekly=bool(data.get("repeat_weekly",False)); classroom_id=str(data.get("classroom_id","")).strip() or None
                if kind not in SCHEDULE_KINDS or not title or not event_date: raise ValueError("Completá tipo, título y fecha.")
                try: weekday=datetime.strptime(event_date,"%Y-%m-%d").weekday()
                except ValueError: raise ValueError("Fecha inválida.")
                if classroom_id and not user_has_classroom(db,user,classroom_id): self.send_json(403,{"error":"No tenés acceso a esa aula."}); return
                sid=new_id("schedule"); db.execute("INSERT INTO schedule_items(id,owner_id,classroom_id,kind,title,event_date,start_time,note,repeat_weekly,weekday,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(sid,user["id"],classroom_id,kind,title,event_date,start_time,note,int(repeat_weekly),weekday,iso_now())); log_activity(db,user["id"],"schedule_create",f"Agendó {title} para {event_date}."); self.send_json(201,{"id":sid}); return
            if path=="/api/import/students/preview":
                admin=self.require_admin(db)
                if not admin:return
                rows=parse_import_payload(data); self.send_json(200,preview_import_rows(db,rows)); return
            if path=="/api/import/students/commit":
                admin=self.require_admin(db)
                if not admin:return
                rows=parse_import_payload(data); result=commit_import_rows(db,admin,rows); self.send_json(200,result); return
            if path=="/api/admin/users":
                admin=self.require_admin(db)
                if not admin:return
                email=str(data.get("email","")).strip().lower(); name=str(data.get("name","")).strip(); role=str(data.get("role","docente")); institution=str(data.get("institution","")).strip(); password=str(data.get("password","")); classroom_ids=data.get("classroom_ids",[])
                if not email or "@" not in email or not name: raise ValueError("Nombre y correo son obligatorios.")
                if role not in ROLE_LABELS: raise ValueError("Rol inválido.")
                if admin["role"]!="administrador" and role=="administrador": self.send_json(403,{"error":"Solo un administrador puede crear otro administrador."}); return
                if len(password)<10: raise ValueError("La contraseña inicial debe tener al menos 10 caracteres.")
                uid=create_user(db,email=email,name=name,role=role,password=password,institution=institution); set_user_classrooms(db,uid,classroom_ids); log_activity(db,admin["id"],"user_create",f"Creó la cuenta {name} ({ROLE_LABELS[role]})."); self.send_json(201,{"id":uid}); return
            if path=="/api/migrate/local":
                admin=self.require_admin(db)
                if not admin:return
                self.send_json(200,migrate_local_state(db,admin,data.get("state"))); return
            self.send_json(404,{"error":"Endpoint no encontrado."})

    def handle_api_patch(self, path: str, query: dict[str,list[str]]) -> None:
        data=self.read_json()
        with connect_db() as db:
            user=self.require_user(db)
            if not user:return
            if not self.require_csrf(user):return
            if path=="/api/profile":
                name=str(data.get("name",user["name"])).strip();email=str(data.get("email",user["email"])).strip().lower();language=str(data.get("preferred_language",user["preferred_language"] if "preferred_language" in user.keys() else "es"));avatar=str(data.get("avatar_url",user["avatar_url"] if "avatar_url" in user.keys() else "")).strip()
                if not name:raise ValueError("El nombre no puede quedar vacío.")
                if not valid_email(email):raise ValueError("El correo no es válido.")
                if language not in {"es","en","ja","ko","zh","ru","ar"}:raise ValueError("Idioma inválido.")
                if avatar and not (avatar.startswith("https://") or avatar.startswith("data:image/")):raise ValueError("El avatar debe usar HTTPS o una imagen local válida.")
                if parse_bool(data.get("use_google_picture"),False):
                    ga=google_tokens_for_user(db,user["id"]);avatar=ga["picture_url"] if ga and ga["picture_url"] else avatar
                db.execute("UPDATE users SET name=?,email=?,preferred_language=?,avatar_url=? WHERE id=?",(name,email,language,avatar,user["id"]));log_activity(db,user["id"],"profile_update","Actualizó su perfil.");updated=db.execute("SELECT * FROM users WHERE id=?",(user["id"],)).fetchone();self.send_json(200,{"ok":True,"user":user_public(db,updated)});return
            if path.startswith("/api/classrooms/"):
                admin=self.require_admin(db)
                if not admin:return
                cid=path.split("/")[3]; old=db.execute("SELECT name,archived FROM classrooms WHERE id=?",(cid,)).fetchone()
                if not old:self.send_json(404,{"error":"Aula no encontrada."});return
                if "archived" in data and not bool(data.get("archived")):
                    db.execute("UPDATE classrooms SET archived=0 WHERE id=?",(cid,));log_activity(db,admin["id"],"classroom_restore",f"Restauró el aula {old['name']}.");self.send_json(200,{"ok":True});return
                name=str(data.get("name",old["name"])).strip()
                if not name: raise ValueError("El nombre del aula es obligatorio.")
                db.execute("UPDATE classrooms SET name=? WHERE id=?",(name,cid)); log_activity(db,admin["id"],"classroom_rename",f"Renombró {old['name']} como {name}."); self.send_json(200,{"ok":True});return
            if path.startswith("/api/students/") and len(path.split("/"))==4:
                admin=self.require_admin(db)
                if not admin:return
                sid=path.split("/")[3]; payload=validate_student_payload(data); old=db.execute("SELECT name FROM students WHERE id=? AND active=1",(sid,)).fetchone()
                if not old:self.send_json(404,{"error":"Alumno no encontrado."});return
                db.execute("""UPDATE students SET record_number=?,name=?,classroom_id=?,dni=?,birth_date=?,guardian_name=?,guardian_phone=?,guardian_email=?,emergency_name=?,emergency_phone=?,updated_at=? WHERE id=?""",
                           (payload["record_number"],payload["name"],payload["classroom_id"],payload["dni"],payload["birth_date"],payload["guardian_name"],payload["guardian_phone"],payload["guardian_email"],payload["emergency_name"],payload["emergency_phone"],iso_now(),sid)); log_activity(db,admin["id"],"student_update",f"Actualizó el legajo de {old['name']} → {payload['name']}."); self.send_json(200,{"ok":True});return
            if path.startswith("/api/reports/") and path.endswith("/status"):
                if not role_can_review_reports(user):self.send_json(403,{"error":"No tenés permisos para cambiar el estado de reportes."});return
                rid=path.split("/")[3]; status=str(data.get("status","")).strip()
                if status not in REPORT_STATUSES:raise ValueError("Estado de reporte inválido.")
                report=db.execute("SELECT r.*,s.name AS student_name FROM reports r JOIN students s ON s.id=r.student_id WHERE r.id=?",(rid,)).fetchone()
                if not report or (user["role"]=="preceptor" and not user_has_classroom(db,user,report["classroom_id"])):self.send_json(404,{"error":"Reporte no encontrado."});return
                db.execute("UPDATE reports SET status=?,updated_at=? WHERE id=?",(status,iso_now(),rid));log_activity(db,user["id"],"report_status",f"Cambió el reporte de {report['student_name']} a {status}.");self.send_json(200,{"ok":True});return
            if path.startswith("/api/notes/") and len(path.split("/"))==4:
                nid=path.split("/")[3];note=str(data.get("note","")).strip()
                if not note:raise ValueError("La nota no puede quedar vacía.")
                row=db.execute("SELECT * FROM teacher_notes WHERE id=? AND author_id=?",(nid,user["id"])).fetchone()
                if not row:self.send_json(404,{"error":"Nota no encontrada."});return
                stamp=iso_now();db.execute("UPDATE teacher_notes SET note=?,updated_at=? WHERE id=?",(note,stamp,nid))
                log_activity(db,user["id"],"teacher_note_update","Actualizó una nota privada docente.")
                self.send_json(200,{"ok":True,"updated_at":stamp});return

            if path.startswith("/api/notices/") and path.endswith("/sent"):
                nid=path.split("/")[3]; notice=db.execute("SELECT * FROM family_notices WHERE id=?",(nid,)).fetchone()
                if not notice or not user_has_classroom(db,user,notice["classroom_id"]):self.send_json(404,{"error":"Aviso no encontrado."});return
                if notice["author_id"]!=user["id"] and not role_can_review_reports(user):self.send_json(403,{"error":"No tenés permisos para modificar ese aviso."});return
                db.execute("UPDATE family_notices SET status='sent',sent_at=? WHERE id=?",(iso_now(),nid));log_activity(db,user["id"],"family_notice_sent","Marcó un aviso familiar como enviado.");self.send_json(200,{"ok":True});return
            if path=="/api/settings":
                admin=self.require_admin(db)
                if not admin:return
                try:threshold=float(data.get("attendance_threshold",get_setting(db,"attendance_threshold","75")));absence_count=int(data.get("absence_alert_count",get_setting(db,"absence_alert_count","3")));late_count=int(data.get("late_alert_count",get_setting(db,"late_alert_count","5")));backup_hours=int(data.get("backup_interval_hours",get_setting(db,"backup_interval_hours","24")));backup_keep=int(data.get("backup_keep",get_setting(db,"backup_keep","30")));session_hours_value=int(data.get("session_hours",session_hours(db)))
                except (ValueError,TypeError):raise ValueError("Hay una configuración numérica inválida.")
                if threshold<1 or threshold>100:raise ValueError("El umbral debe estar entre 1 y 100.")
                if not 1<=absence_count<=30 or not 1<=late_count<=50:raise ValueError("Los umbrales de alertas están fuera de rango.")
                if not 1<=backup_hours<=720 or not 3<=backup_keep<=365:raise ValueError("La configuración de copias de seguridad está fuera de rango.")
                if not 1<=session_hours_value<=168:raise ValueError("La duración de sesión debe estar entre 1 y 168 horas.")
                updates={"attendance_threshold":str(threshold),"absence_alert_count":str(absence_count),"late_alert_count":str(late_count),"auto_backup_enabled":"1" if parse_bool(data.get("auto_backup_enabled"),True) else "0","backup_interval_hours":str(backup_hours),"backup_keep":str(backup_keep),"session_hours":str(session_hours_value),"institution_name":str(data.get("institution_name",get_setting(db,"institution_name",""))).strip(),"academic_year":str(data.get("academic_year",get_setting(db,"academic_year",str(datetime.now().year)))).strip(),"education_level":str(data.get("education_level",get_setting(db,"education_level",""))).strip(),"shifts":str(data.get("shifts",get_setting(db,"shifts",""))).strip(),"default_language":str(data.get("default_language",get_setting(db,"default_language","es"))).strip(),"public_base_url":str(data.get("public_base_url",get_setting(db,"public_base_url",""))).strip().rstrip("/"),"google_client_id":str(data.get("google_client_id",get_setting(db,"google_client_id",""))).strip()}
                if updates["default_language"] not in {"es","en","ja","ko","zh","ru","ar"}:raise ValueError("Idioma predeterminado inválido.")
                if updates["public_base_url"] and not re.fullmatch(r"https?://[^\s/]+(?::\d+)?(?:/[^\s]*)?",updates["public_base_url"]):raise ValueError("La URL pública no es válida.")
                for k,v in updates.items():db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(k,v))
                secret=data.get("google_client_secret")
                if secret is not None and str(secret).strip():
                    if not google_secret_storage_available():raise ValueError("Este sistema no dispone de almacenamiento seguro local para el secreto de Google.")
                    db.execute("INSERT INTO settings(key,value) VALUES('google_client_secret_protected',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(protect_secret(str(secret).strip()),))
                if updates["institution_name"]:db.execute("UPDATE users SET institution=?",(updates["institution_name"],))
                log_activity(db,admin["id"],"settings_update","Actualizó la configuración institucional y del sistema.");self.send_json(200,{"ok":True});return
            if path.startswith("/api/admin/users/") and path.endswith("/reset-password"):
                admin=self.require_admin(db)
                if not admin:return
                target_id=path.split("/")[4]; password=str(data.get("password",""))
                if len(password)<10:raise ValueError("La nueva contraseña debe tener al menos 10 caracteres.")
                target=db.execute("SELECT * FROM users WHERE id=?",(target_id,)).fetchone()
                if not target:self.send_json(404,{"error":"Usuario no encontrado."});return
                if target["role"]=="administrador" and admin["role"]!="administrador":self.send_json(403,{"error":"No podés restablecer la contraseña de un administrador."});return
                salt,digest=password_hash(password);db.execute("UPDATE users SET password_salt=?,password_hash=? WHERE id=?",(salt,digest,target_id));db.execute("DELETE FROM sessions WHERE user_id=?",(target_id,));log_activity(db,admin["id"],"user_password_reset",f"Restableció la contraseña de {target['name']}.");self.send_json(200,{"ok":True});return
            if path.startswith("/api/admin/users/"):
                admin=self.require_admin(db)
                if not admin:return
                target_id=path.split("/")[4]; target=db.execute("SELECT * FROM users WHERE id=?",(target_id,)).fetchone()
                if not target:self.send_json(404,{"error":"Usuario no encontrado."});return
                name=str(data.get("name",target["name"])).strip();email=str(data.get("email",target["email"])).strip().lower();role=str(data.get("role",target["role"]));institution=str(data.get("institution",target["institution"])).strip();active=bool(data.get("active",bool(target["active"])));classroom_ids=data.get("classroom_ids",None)
                if role not in ROLE_LABELS:raise ValueError("Rol inválido.")
                if admin["role"]!="administrador" and (target["role"]=="administrador" or role=="administrador"):self.send_json(403,{"error":"Solo un administrador puede modificar cuentas de administrador."});return
                if target_id==admin["id"] and not active:raise ValueError("No podés desactivar tu propia cuenta.")
                if target["role"]=="administrador" and (role!="administrador" or not active):
                    other_admins=db.execute("SELECT COUNT(*) FROM users WHERE role='administrador' AND active=1 AND id<>?",(target_id,)).fetchone()[0]
                    if other_admins==0:raise ValueError("No podés dejar el sistema sin ningún administrador activo.")
                db.execute("UPDATE users SET name=?,email=?,role=?,institution=?,active=? WHERE id=?",(name,email,role,institution,int(active),target_id))
                if isinstance(classroom_ids,list):set_user_classrooms(db,target_id,classroom_ids)
                if not active:db.execute("DELETE FROM sessions WHERE user_id=?",(target_id,))
                log_activity(db,admin["id"],"user_update",f"Actualizó la cuenta {name}.");self.send_json(200,{"ok":True});return
            self.send_json(404,{"error":"Endpoint no encontrado."})

    def handle_api_delete(self, path: str, query: dict[str,list[str]]) -> None:
        with connect_db() as db:
            user=self.require_user(db)
            if not user:return
            if not self.require_csrf(user):return
            if path.startswith("/api/auth/sessions/"):
                session_id=path.split("/")[4]
                if session_id==user["session_id"]:self.send_json(409,{"error":"Usá Cerrar sesión para cerrar la sesión actual."});return
                deleted=db.execute("DELETE FROM sessions WHERE id=? AND user_id=?",(session_id,user["id"])).rowcount;self.send_json(200,{"ok":True,"deleted":deleted});return
            if path=="/api/google":
                deleted=db.execute("DELETE FROM google_accounts WHERE user_id=?",(user["id"],)).rowcount;log_activity(db,user["id"],"google_disconnect","Desconectó su cuenta Google.");self.send_json(200,{"ok":True,"deleted":deleted});return
            if path=="/api/attendance/history":
                admin=self.require_admin(db)
                if not admin:return
                count=db.execute("SELECT COUNT(*) FROM attendance").fetchone()[0];db.execute("DELETE FROM attendance_audit");db.execute("DELETE FROM attendance");db.execute("DELETE FROM attendance_drafts");log_activity(db,admin["id"],"attendance_clear",f"Borró el historial completo de asistencia ({count} registros).");self.send_json(200,{"ok":True,"deleted":count});return
            if path=="/api/attendance/draft":
                date=(query.get("date") or [""])[0];classroom_id=(query.get("classroom_id") or [""])[0]
                db.execute("DELETE FROM attendance_drafts WHERE user_id=? AND classroom_id=? AND date=?",(user["id"],classroom_id,date));self.send_json(200,{"ok":True});return
            if path.startswith("/api/classrooms/"):
                admin=self.require_admin(db)
                if not admin:return
                cid=path.split("/")[3];row=db.execute("SELECT name FROM classrooms WHERE id=?",(cid,)).fetchone()
                if not row:self.send_json(404,{"error":"Aula no encontrada."});return
                if db.execute("SELECT 1 FROM students WHERE classroom_id=? AND active=1 LIMIT 1",(cid,)).fetchone():self.send_json(409,{"error":"No podés eliminar un aula que todavía tiene alumnos."});return
                db.execute("UPDATE classrooms SET archived=1 WHERE id=?",(cid,));db.execute("DELETE FROM user_classrooms WHERE classroom_id=?",(cid,));log_activity(db,admin["id"],"classroom_archive",f"Archivó el aula {row['name']}.");self.send_json(200,{"ok":True});return
            if path.startswith("/api/students/"):
                admin=self.require_admin(db)
                if not admin:return
                sid=path.split("/")[3];row=db.execute("SELECT name FROM students WHERE id=? AND active=1",(sid,)).fetchone()
                if not row:self.send_json(404,{"error":"Alumno no encontrado."});return
                reports,notes=student_protection_counts(db,sid)
                if reports or notes:self.send_json(409,{"error":"No se puede dar de baja: el alumno tiene reportes o notas del profesor.","report_count":reports,"note_count":notes});return
                db.execute("UPDATE students SET active=0,updated_at=? WHERE id=?",(iso_now(),sid));log_activity(db,admin["id"],"student_delete",f"Dio de baja a {row['name']}.");self.send_json(200,{"ok":True});return
            if path.startswith("/api/notes/"):
                nid=path.split("/")[3];row=db.execute("SELECT * FROM teacher_notes WHERE id=? AND author_id=?",(nid,user["id"])).fetchone()
                if not row:self.send_json(404,{"error":"Nota no encontrada."});return
                db.execute("DELETE FROM teacher_notes WHERE id=?",(nid,));log_activity(db,user["id"],"teacher_note_delete","Eliminó una nota privada.");self.send_json(200,{"ok":True});return
            if path.startswith("/api/schedule/"):
                sid=path.split("/")[3];row=db.execute("SELECT * FROM schedule_items WHERE id=?",(sid,)).fetchone()
                if not row or (row["owner_id"]!=user["id"] and not role_can_admin(user)):self.send_json(404,{"error":"Evento no encontrado."});return
                db.execute("DELETE FROM schedule_items WHERE id=?",(sid,));log_activity(db,user["id"],"schedule_delete",f"Eliminó el evento {row['title']}.");self.send_json(200,{"ok":True});return
            self.send_json(404,{"error":"Endpoint no encontrado."})


def main() -> None:
    parser=argparse.ArgumentParser(description=f"Servidor local de {APP_NAME} v{APP_VERSION}")
    parser.add_argument("--port",type=int,default=int(os.environ.get("PORT","8765")));parser.add_argument("--host",default=os.environ.get("PRESENCIALIDAD_HOST","127.0.0.1"));parser.add_argument("--no-browser",action="store_true");parser.add_argument("--portable",action="store_true",help="Guarda los datos junto a la aplicación en ./runtime");parser.add_argument("--data-dir",default="",help="Carpeta personalizada para datos, copias y logs");parser.add_argument("--import-db",default="",help="Importa una base SQLite anterior si todavía no existe la base de destino");parser.add_argument("--public-url",default=os.environ.get("PRESENCIALIDAD_PUBLIC_URL",""))
    args=parser.parse_args()
    if args.portable:set_data_dir(SOURCE_ROOT/"runtime")
    elif args.data_dir:set_data_dir(Path(args.data_dir))
    ensure_runtime_dirs()
    if args.import_db and not DB_PATH.exists():import_database_file(Path(args.import_db))
    else:maybe_import_legacy_database()
    init_db()
    if args.public_url:
        with connect_db() as db:db.execute("INSERT INTO settings(key,value) VALUES('public_base_url',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(args.public_url.rstrip('/'),))
    auto_backup_if_due(False);stop_event=threading.Event();threading.Thread(target=backup_worker,args=(stop_event,),daemon=True,name="PresencialidadBackup").start()
    server=ThreadingHTTPServer((args.host,args.port),AppHandler);display_host="127.0.0.1" if args.host in {"0.0.0.0","::"} else args.host;url=f"http://{display_host}:{args.port}/"
    append_log("INFO",f"Inicio {APP_NAME} v{APP_VERSION} en {args.host}:{args.port}; datos={DATA_DIR}")
    print(f"{APP_NAME} v{APP_VERSION}\nServidor: {url}\nDatos: {DATA_DIR}\nBase de datos: {DB_PATH}\nPara detenerlo, presioná Ctrl+C.")
    if not args.no_browser and args.host in {"127.0.0.1","localhost","::1"}:threading.Timer(.65,lambda:webbrowser.open(url)).start()
    try:server.serve_forever()
    except KeyboardInterrupt:print("\nServidor detenido.")
    finally:stop_event.set();server.server_close();append_log("INFO","Servidor detenido.")


if __name__=="__main__":
    main()
