"""Password and Google accounts with revocable browser sessions."""
import hashlib
import hmac
import re
import secrets
import threading
import time


class Throttled(RuntimeError):
    pass


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1, maxmem=64 * 1024**2)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Accounts:
    lifetime = 7 * 86400
    google_state_lifetime = 600

    def __init__(self, store):
        self.store = store
        self.hash_slots = threading.BoundedSemaphore(2)
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    salt TEXT NOT NULL, password_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    expires BIGINT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS session_expiry ON sessions(expires);
                CREATE TABLE IF NOT EXISTS auth_attempts (username TEXT NOT NULL, attempted BIGINT NOT NULL);
                CREATE INDEX IF NOT EXISTS auth_attempt_time ON auth_attempts(attempted);
                CREATE TABLE IF NOT EXISTS google_logins (
                    state TEXT PRIMARY KEY, nonce TEXT NOT NULL, verifier TEXT NOT NULL,
                    expires BIGINT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS google_logins_expiry ON google_logins(expires);
                CREATE TABLE IF NOT EXISTS google_identities (
                    subject TEXT PRIMARY KEY, user_id TEXT UNIQUE NOT NULL REFERENCES users(id),
                    display_name TEXT NOT NULL
                );
            """)

    def authenticate(self, username, password, register=False):
        if not isinstance(username, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{3,32}", username):
            raise ValueError("Use a username with 3–32 letters, numbers, underscores, or hyphens.")
        if not isinstance(password, str) or not 12 <= len(password) <= 128:
            raise ValueError("Use a password with 12–128 characters.")
        username = username.lower()
        now = int(time.time())
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM auth_attempts WHERE attempted<=?", (now - 600,))
            total, same_user = db.execute(
                "SELECT COUNT(*),COUNT(CASE WHEN username=? THEN 1 END) FROM auth_attempts",
                (username,),
            ).fetchone()
            if total >= 200 or same_user >= 8:
                raise Throttled("Too many sign-in attempts. Try again in a few minutes.")
            db.execute("INSERT INTO auth_attempts VALUES (?,?)", (username, now))
            user = db.execute("SELECT id,salt,password_hash FROM users WHERE username=?", (username,)).fetchone()

        if not self.hash_slots.acquire(blocking=False):
            raise Throttled("Sign-in is busy. Try again shortly.")
        try:
            salt = bytes.fromhex(user[1]) if user else secrets.token_bytes(16)
            digest = password_hash(password, salt).hex()
        finally:
            self.hash_slots.release()

        with self.store.connect() as db:
            if register:
                user_id = secrets.token_hex(16)
                created = db.execute(
                    "INSERT INTO users VALUES (?,?,?,?) ON CONFLICT(username) DO NOTHING",
                    (user_id, username, salt.hex(), digest),
                )
                if created.rowcount != 1:
                    raise ValueError("This username is already in use.")
            elif user and hmac.compare_digest(digest, user[2]):
                user_id = user[0]
            else:
                return None
            return self.create_session(db, user_id, now)

    def create_session(self, db, user_id, now):
        db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
        token = secrets.token_urlsafe(32)
        db.execute("INSERT INTO sessions VALUES (?,?,?)", (token_hash(token), user_id, now + self.lifetime))
        return token

    def start_google_login(self):
        state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        now = int(time.time())
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM google_logins WHERE expires<=?", (now,))
            if db.execute("SELECT COUNT(*) FROM google_logins").fetchone()[0] >= 500:
                raise Throttled("Google sign-in is busy. Try again shortly.")
            db.execute("INSERT INTO google_logins VALUES (?,?,?,?)",
                       (token_hash(state), nonce, verifier, now + self.google_state_lifetime))
        return state, nonce, verifier

    def consume_google_login(self, state, cookie):
        if (not isinstance(state, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", state)
                or not cookie or not cookie.isascii() or not hmac.compare_digest(state, cookie)):
            return None
        key = token_hash(state)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT nonce,verifier,expires FROM google_logins WHERE state=?", (key,)).fetchone()
            if row:
                db.execute("DELETE FROM google_logins WHERE state=?", (key,))
        return row[:2] if row and row[2] > time.time() else None

    def sign_in_with_google(self, subject, display_name):
        name = "".join(char for char in display_name if char.isprintable()).strip()[:100] if isinstance(display_name, str) else ""
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            identity = db.execute("SELECT user_id FROM google_identities WHERE subject=?", (subject,)).fetchone()
            if identity:
                user_id = identity[0]
                if name:
                    db.execute("UPDATE google_identities SET display_name=? WHERE subject=?", (name, subject))
            else:
                user_id = secrets.token_hex(16)
                # These accounts have no password credential and a distinct database identity.
                db.execute("INSERT INTO users VALUES (?,?,?,?)",
                           (user_id, "google-" + secrets.token_hex(12), secrets.token_hex(16), ""))
                db.execute("INSERT INTO google_identities VALUES (?,?,?)", (subject, user_id, name or "Google account"))
            return self.create_session(db, user_id, int(time.time()))

    def identify(self, token):
        if not token or not re.fullmatch(r"[a-zA-Z0-9_-]{43}", token):
            return None
        with self.store.connect() as db:
            row = db.execute(
                """SELECT users.id,COALESCE(google_identities.display_name,users.username)
                   FROM sessions JOIN users ON users.id=sessions.user_id
                   LEFT JOIN google_identities ON google_identities.user_id=users.id
                   WHERE token=? AND expires>?""",
                (token_hash(token), time.time()),
            ).fetchone()
        return {"id": row[0], "username": row[1]} if row else None

    def logout(self, token):
        if token:
            with self.store.connect() as db:
                db.execute("DELETE FROM sessions WHERE token=?", (token_hash(token),))
