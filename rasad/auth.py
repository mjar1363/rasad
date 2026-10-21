"""Users, sessions and role-based access control (admin / analyst / viewer).

* Passwords: PBKDF2-HMAC-SHA256 (200k iterations, per-user salt).
* Sessions: random 32-byte token in an HttpOnly cookie (or `Authorization: Bearer`); only its SHA-256 is stored.
* First start: when no user exists the UI shows a one-time "create admin" screen (or set RASAD_ADMIN_USER / RASAD_ADMIN_PASSWORD).
* Legacy `X-Admin-Token` (RASAD_ADMIN_TOKEN) is still accepted as an admin credential for scripts.
"""
import contextvars
import hashlib
import hmac
import os
import secrets
import time

from . import db
from .config import settings

ROLES = {"viewer": 1, "analyst": 2, "admin": 3}
ROLES_FA = {"viewer": "بیننده", "analyst": "تحلیلگر", "admin": "مدیر"}
COOKIE = "rasad_session"
SESSION_TTL = 14 * 24 * 3600
ITER = 200_000

current_user = contextvars.ContextVar("current_user", default=None)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, name TEXT, role TEXT NOT NULL DEFAULT 'viewer',
  pw_hash TEXT NOT NULL, active INTEGER DEFAULT 1, created REAL, last_login REAL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER, created REAL, expires REAL, ua TEXT, ip TEXT);
CREATE TABLE IF NOT EXISTS login_attempts(key TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS api_tokens(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT, token_hash TEXT UNIQUE, prefix TEXT,
  created REAL, last_used REAL, expires REAL);
"""


def init():
    with db.tx() as c:
        c.executescript(SCHEMA)
    u, p = os.environ.get("RASAD_ADMIN_USER"), os.environ.get("RASAD_ADMIN_PASSWORD")
    if u and p and not user_count():
        create_user(u, p, "admin", name="مدیر سامانه")


def enabled():
    return settings.auth_enabled


def hash_pw(pw: str, salt: bytes = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, ITER)
    return f"pbkdf2_sha256${ITER}${salt.hex()}${dk.hex()}"


def verify_pw(pw: str, stored: str) -> bool:
    try:
        _, it, salt, h = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), h)
    except Exception:
        return False


def _check_pw_policy(pw):
    if not pw or len(pw) < 8:
        raise ValueError("رمز عبور باید حداقل ۸ نویسه باشد")


def user_count():
    return db.q1("SELECT COUNT(*) n FROM users")["n"]


def public(u):
    if not u:
        return None
    return {"id": u["id"], "username": u["username"], "name": u.get("name") or u["username"], "role": u["role"],
            "role_fa": ROLES_FA.get(u["role"], u["role"]), "active": bool(u.get("active", 1)), "created": u.get("created"),
            "last_login": u.get("last_login")}


def create_user(username, password, role="viewer", name=None):
    username = (username or "").strip().lower()
    if not username or not username.replace("_", "").replace(".", "").replace("-", "").isalnum():
        raise ValueError("نام کاربری نامعتبر است (حروف لاتین، عدد، . _ -)")
    if role not in ROLES:
        raise ValueError("نقش نامعتبر")
    _check_pw_policy(password)
    if db.q1("SELECT id FROM users WHERE username=?", (username,)):
        raise ValueError("این نام کاربری وجود دارد")
    with db.tx() as c:
        cur = c.execute("INSERT INTO users(username,name,role,pw_hash,active,created) VALUES(?,?,?,?,1,?)",
                        (username, name or username, role, hash_pw(password), time.time()))
        return cur.lastrowid


def update_user(uid, body):
    u = db.q1("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        raise KeyError(uid)
    sets, args = [], []
    if "role" in body:
        if body["role"] not in ROLES:
            raise ValueError("نقش نامعتبر")
        if u["role"] == "admin" and body["role"] != "admin" and _admins() <= 1:
            raise ValueError("دست‌کم یک مدیر فعال باید باقی بماند")
        sets.append("role=?"); args.append(body["role"])
    if "active" in body:
        if u["role"] == "admin" and not body["active"] and _admins() <= 1:
            raise ValueError("دست‌کم یک مدیر فعال باید باقی بماند")
        sets.append("active=?"); args.append(1 if body["active"] else 0)
    if "name" in body:
        sets.append("name=?"); args.append(str(body["name"])[:80])
    if body.get("password"):
        _check_pw_policy(body["password"])
        sets.append("pw_hash=?"); args.append(hash_pw(body["password"]))
    if sets:
        with db.tx() as c:
            c.execute(f"UPDATE users SET {','.join(sets)} WHERE id=?", (*args, uid))
            if body.get("password") or body.get("active") is False:
                c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            if body.get("password") or body.get("active") is False or "role" in body:
                c.execute("DELETE FROM api_tokens WHERE user_id=?", (uid,))
    return public(db.q1("SELECT * FROM users WHERE id=?", (uid,)))


def delete_user(uid):
    u = db.q1("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        raise KeyError(uid)
    if u["role"] == "admin" and _admins() <= 1:
        raise ValueError("آخرین مدیر قابل حذف نیست")
    with db.tx() as c:
        c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        c.execute("DELETE FROM api_tokens WHERE user_id=?", (uid,))
        c.execute("DELETE FROM users WHERE id=?", (uid,))


def _admins():
    return db.q1("SELECT COUNT(*) n FROM users WHERE role='admin' AND active=1")["n"]


def _throttled(key):
    since = time.time() - 300
    with db.tx() as c:
        c.execute("DELETE FROM login_attempts WHERE ts<?", (since,))
    return db.q1("SELECT COUNT(*) n FROM login_attempts WHERE key=? AND ts>?", (key, since))["n"] >= 5


def login(username, password, ua="", ip=""):
    username = (username or "").strip().lower()
    key = f"{username}|{ip}"
    if _throttled(key):
        raise PermissionError("تلاش‌های ناموفق زیاد؛ ۵ دقیقه دیگر دوباره امتحان کنید")
    u = db.q1("SELECT * FROM users WHERE username=?", (username,))
    if not u or not u["active"] or not verify_pw(password or "", u["pw_hash"]):
        with db.tx() as c:
            c.execute("INSERT INTO login_attempts(key,ts) VALUES(?,?)", (key, time.time()))
        raise PermissionError("نام کاربری یا رمز عبور نادرست است")
    tok = secrets.token_urlsafe(32)
    now = time.time()
    with db.tx() as c:
        c.execute("INSERT INTO sessions(token_hash,user_id,created,expires,ua,ip) VALUES(?,?,?,?,?,?)",
                  (_th(tok), u["id"], now, now + SESSION_TTL, ua[:200], ip))
        c.execute("UPDATE users SET last_login=? WHERE id=?", (now, u["id"]))
        c.execute("DELETE FROM sessions WHERE expires<?", (now,))
    return tok, public(u)


def logout(tok):
    if tok:
        with db.tx() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (_th(tok),))


def _th(tok):
    return hashlib.sha256(tok.encode()).hexdigest()


API_PREFIX = "rsd_"


def create_api_token(uid, name, days=None):
    import secrets
    tok = API_PREFIX + secrets.token_urlsafe(30)
    exp = time.time() + int(days) * 86400 if days else None
    with db.tx() as c:
        tid = c.execute("INSERT INTO api_tokens(user_id,name,token_hash,prefix,created,expires) VALUES(?,?,?,?,?,?)",
                        (uid, (name or "token")[:60], _th(tok), tok[:10], time.time(), exp)).lastrowid
    return tok, tid


def list_api_tokens(uid):
    return db.q("SELECT id,name,prefix,created,last_used,expires FROM api_tokens WHERE user_id=? ORDER BY id DESC", (uid,))


def revoke_api_token(uid, tid):
    with db.tx() as c:
        return c.execute("DELETE FROM api_tokens WHERE id=? AND user_id=?", (tid, uid)).rowcount


def user_from_token(tok):
    if not tok:
        return None
    if tok.startswith(API_PREFIX):
        r = db.q1("""SELECT u.*, t.id tid, t.last_used FROM api_tokens t JOIN users u ON u.id=t.user_id
                     WHERE t.token_hash=? AND (t.expires IS NULL OR t.expires>?) AND u.active=1""", (_th(tok), time.time()))
        if r and time.time() - (r["last_used"] or 0) > 60:
            with db.tx() as c:
                c.execute("UPDATE api_tokens SET last_used=? WHERE id=?", (time.time(), r["tid"]))
        return public(r)
    r = db.q1("""SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>? AND u.active=1""",
              (_th(tok), time.time()))
    return public(r)


SYSTEM_ADMIN = {"id": 0, "username": "token-admin", "name": "توکن مدیریت", "role": "admin", "role_fa": "مدیر", "active": True}
OPEN_USER = {"id": 0, "username": "local", "name": "کاربر محلی", "role": "admin", "role_fa": "مدیر", "active": True}

# ---------- route policy ----------
PUBLIC = ("/api/auth/", "/healthz", "/metrics")
ADMIN_PREFIX = ("/api/control", "/api/ai/config", "/api/ai/test", "/api/users", "/api/sources", "/api/rerun", "/api/notify",
                "/api/system", "/api/audit")
VIEWER_POST_OK = ("/api/ask", "/api/me/")


def required_role(method: str, path: str):
    """Minimum role for an API call (None = public)."""
    if not path.startswith("/api/") and path != "/metrics":
        return None
    if path.startswith(PUBLIC):
        return None
    write = method not in ("GET", "HEAD", "OPTIONS")
    if path == "/api/audit" or (path.startswith("/api/system") and not path.startswith("/api/system/info")):
        return "admin"
    if write:
        if path.startswith(ADMIN_PREFIX):
            return "admin"
        if path.startswith(VIEWER_POST_OK):
            return "viewer"
        return "analyst"
    if path.startswith(("/api/users", "/api/notify")):
        return "admin"
    return "viewer"


def allowed(user, role):
    return role is None or (user is not None and ROLES.get(user["role"], 0) >= ROLES[role])


def actor():
    u = current_user.get()
    return u["username"] if u else "system"
