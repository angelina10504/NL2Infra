"""GitHub sign-in, revocable sessions and project membership.

Only numeric GitHub IDs grant membership. Roles and Slack identities are always
looked up on the server; they are never accepted from a browser or Slack label.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from html import escape
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import Request as URLRequest, urlopen

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

router = APIRouter()
SESSION_COOKIE = "nl2infra_session"
STATE_COOKIE = "nl2infra_oauth"
SESSION_TTL = 12 * 60 * 60
ROLES = {"admin", "developer", "viewer"}


@contextmanager
def database():
    path = Path(os.environ.get("NL2INFRA_AUTH_DB", "data/auth.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    path.chmod(0o600)
    db.row_factory = sqlite3.Row
    try:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS members (
                github_id TEXT PRIMARY KEY, login TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL CHECK(role IN ('admin','developer','viewer')),
                active INTEGER NOT NULL DEFAULT 1,
                slack_team_id TEXT, slack_user_id TEXT,
                UNIQUE(slack_team_id, slack_user_id)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, github_id TEXT NOT NULL,
                csrf TEXT NOT NULL, expires REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_states (
                state_hash TEXT PRIMARY KEY, verifier TEXT NOT NULL, expires REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_owners (
                request_id TEXT PRIMARY KEY, github_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS membership_audit (
                id INTEGER PRIMARY KEY, actor_id TEXT NOT NULL,
                github_id TEXT NOT NULL, details TEXT NOT NULL, created REAL NOT NULL
            );
        """)
        # Bootstrap only an empty database. Once membership is managed in the UI,
        # changing an environment variable must not restore a revoked permission.
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM members LIMIT 1").fetchone():
            for user_id in os.environ.get("NL2INFRA_BOOTSTRAP_ADMIN_IDS", "").split(","):
                user_id = user_id.strip()
                if re.fullmatch(r"[1-9][0-9]{0,19}", user_id):
                    db.execute("INSERT OR IGNORE INTO members(github_id,role) VALUES (?,'admin')", (user_id,))
        db.commit()
        yield db
        db.commit()
    finally:
        db.close()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def member(github_id: str) -> dict | None:
    with database() as db:
        row = db.execute("SELECT * FROM members WHERE github_id=? AND active=1", (github_id,)).fetchone()
        return dict(row) if row else None


def slack_admin(team_id: str, user_id: str) -> dict | None:
    if not team_id or not user_id:
        return None
    with database() as db:
        row = db.execute("SELECT * FROM members WHERE slack_team_id=? AND slack_user_id=? "
                         "AND role='admin' AND active=1", (team_id, user_id)).fetchone()
        return dict(row) if row else None


def session(token: str) -> dict | None:
    if not token or len(token) > 256:
        return None
    with database() as db:
        row = db.execute("SELECT m.*,s.csrf FROM sessions s JOIN members m USING(github_id) "
                         "WHERE s.token_hash=? AND s.expires>? AND m.active=1", (_hash(token), time.time())).fetchone()
        return dict(row) if row else None


def create_session(github_id: str) -> tuple[str, str]:
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with database() as db:
        db.execute("DELETE FROM sessions WHERE expires<=?", (time.time(),))
        db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (_hash(token), github_id, csrf, time.time()+SESSION_TTL))
    return token, csrf


def public_member(user: dict) -> dict:
    return {key: user.get(key) for key in ("github_id", "login", "role", "active", "slack_team_id", "slack_user_id")}


def require_role(request: Request, *roles: str) -> dict:
    user = getattr(request.state, "member", None)
    if not user:
        raise HTTPException(401, "Sign in with an invited GitHub account")
    if roles and user["role"] not in roles:
        raise HTTPException(403, "Your project role does not allow this action")
    return user


def record_owner(request_id: str, github_id: str) -> None:
    with database() as db:
        db.execute("INSERT INTO run_owners VALUES (?,?)", (request_id, github_id))


def owner(request_id: str) -> str | None:
    with database() as db:
        row = db.execute("SELECT github_id FROM run_owners WHERE request_id=?", (request_id,)).fetchone()
        return row[0] if row else None


def base_url() -> str:
    value = (os.environ.get("NL2INFRA_AUTH_BASE_URL") or os.environ.get("PUBLIC_BASE_URL", "")).rstrip("/")
    parsed = urlparse(value)
    local = parsed.hostname in ("localhost", "127.0.0.1")
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path or (parsed.scheme != "https" and not (local and parsed.scheme == "http"))):
        raise HTTPException(503, "Set NL2INFRA_AUTH_BASE_URL or PUBLIC_BASE_URL to this deployment's HTTPS origin")
    return value


def _cookie(response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(name, value, max_age=max_age, httponly=True,
                        secure=base_url().startswith("https://"), samesite="lax", path="/")


def _github_json(url: str, data=None, token=None) -> dict:
    headers = {"Accept": "application/json", "User-Agent": "NL2Infra"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = "Bearer " + token
    with urlopen(URLRequest(url, data=urlencode(data).encode() if data is not None else None,
                            headers=headers), timeout=10) as response:
        return json.load(response)


@router.get("/auth/login")
def login():
    client_id = os.environ.get("GITHUB_OAUTH_CLIENT_ID")
    if not client_id or not os.environ.get("GITHUB_OAUTH_CLIENT_SECRET"):
        template = Path(__file__).parent / "templates" / "auth_setup.html"
        try:
            origin = base_url()
        except HTTPException:
            origin = "https://YOUR_APP_HOST"
        markup = template.read_text(encoding="utf-8")
        markup = markup.replace("{{ORIGIN}}", escape(origin)).replace("{{CALLBACK}}", escape(origin + "/auth/callback"))
        return HTMLResponse(markup, status_code=503, headers={"Cache-Control": "no-store"})
    callback = base_url() + "/auth/callback"
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    with database() as db:
        db.execute("DELETE FROM oauth_states WHERE expires<=?", (time.time(),))
        db.execute("INSERT INTO oauth_states VALUES (?,?,?)", (_hash(state), verifier, time.time()+600))
    # No repo scope: the sign-in token identifies the user only. GitOps uses the
    # server credential independently, after authorization.
    params = {"client_id": client_id, "redirect_uri": callback, "state": state,
              "code_challenge": challenge, "code_challenge_method": "S256", "scope": ""}
    response = RedirectResponse("https://github.com/login/oauth/authorize?" + urlencode(params), status_code=303)
    _cookie(response, STATE_COOKIE, state, 600)
    return response


@router.get("/auth/callback")
def callback(request: Request, code: str = "", state: str = ""):
    cookie = request.cookies.get(STATE_COOKIE, "")
    if not state or not cookie or len(state) > 256 or not hmac.compare_digest(state.encode(), cookie.encode()) or not code:
        raise HTTPException(401, "Invalid or expired sign-in. Please sign in again.")
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("DELETE FROM oauth_states WHERE state_hash=? AND expires>? RETURNING verifier",
                         (_hash(state), time.time())).fetchone()
    if not row:
        raise HTTPException(401, "Invalid or expired sign-in. Please sign in again.")
    try:
        result = _github_json("https://github.com/login/oauth/access_token", data={
            "client_id": os.environ.get("GITHUB_OAUTH_CLIENT_ID", ""),
            "client_secret": os.environ.get("GITHUB_OAUTH_CLIENT_SECRET", ""),
            "code": code, "redirect_uri": base_url()+"/auth/callback", "code_verifier": row[0]})
        token = result.get("access_token")
        if not token:
            raise ValueError("No OAuth token")
        identity = _github_json("https://api.github.com/user", token=token)
        user_id = str(identity["id"])
        user_login = str(identity["login"])
    except Exception:
        # Neither codes, tokens nor upstream responses belong in a log or error.
        raise HTTPException(502, "GitHub sign-in could not be completed. Try again.") from None
    user = member(user_id)
    if not user:
        raise HTTPException(403, "You are not an active project member. Ask an admin to invite your GitHub ID: " + user_id)
    with database() as db:
        db.execute("UPDATE members SET login=? WHERE github_id=?", (user_login, user_id))
    token, _ = create_session(user_id)
    response = RedirectResponse(base_url()+"/", status_code=303)
    _cookie(response, SESSION_COOKIE, token, SESSION_TTL)
    response.delete_cookie(STATE_COOKIE, path="/", secure=base_url().startswith("https://"), httponly=True, samesite="lax")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/api/session")
def current_session(request: Request):
    user = session(request.cookies.get(SESSION_COOKIE, ""))
    return JSONResponse({"user": public_member(user) if user else None,
                         "csrf_token": user["csrf"] if user else None}, headers={"Cache-Control": "no-store"})


@router.post("/api/logout")
def logout(request: Request):
    with database() as db:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (_hash(request.cookies.get(SESSION_COOKIE, "")),))
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


class MemberUpdate(BaseModel):
    github_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
    role: str = Field(pattern=r"^(admin|developer|viewer)$")
    active: bool = True
    slack_team_id: str | None = Field(default=None, pattern=r"^T[A-Z0-9]+$")
    slack_user_id: str | None = Field(default=None, pattern=r"^[UW][A-Z0-9]+$")


@router.get("/api/team")
def team(request: Request):
    require_role(request, "admin")
    with database() as db:
        return [dict(row) for row in db.execute("SELECT * FROM members ORDER BY login, github_id")]


@router.post("/api/team")
def update_member(change: MemberUpdate, request: Request):
    actor = require_role(request, "admin")
    if bool(change.slack_team_id) != bool(change.slack_user_id):
        raise HTTPException(422, "Set both Slack workspace ID and Slack user ID, or neither")
    resolved_login = ""
    if not change.github_id.isdigit():
        try:
            identity = _github_json("https://api.github.com/users/" + change.github_id)
            change.github_id, resolved_login = str(identity["id"]), str(identity["login"])
        except Exception:
            raise HTTPException(422, "Could not resolve that GitHub username. Check the spelling or use its numeric GitHub ID.") from None
    if not re.fullmatch(r"[1-9][0-9]{0,19}", change.github_id):
        raise HTTPException(422, "Invalid GitHub user ID")
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT * FROM members WHERE github_id=?", (change.github_id,)).fetchone()
        if old and old["role"] == "admin" and old["active"] and (change.role != "admin" or not change.active):
            count = db.execute("SELECT COUNT(*) FROM members WHERE role='admin' AND active=1").fetchone()[0]
            if count <= 1:
                raise HTTPException(409, "Keep at least one active admin")
        try:
            db.execute("INSERT INTO members(github_id,role,active,slack_team_id,slack_user_id) VALUES (?,?,?,?,?) "
                       "ON CONFLICT(github_id) DO UPDATE SET role=excluded.role, active=excluded.active, "
                       "slack_team_id=excluded.slack_team_id, slack_user_id=excluded.slack_user_id",
                       (change.github_id, change.role, int(change.active), change.slack_team_id, change.slack_user_id))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "This Slack identity is already linked to a project member") from None
        if resolved_login:
            db.execute("UPDATE members SET login=? WHERE github_id=?", (resolved_login, change.github_id))
        db.execute("INSERT INTO membership_audit(actor_id,github_id,details,created) VALUES (?,?,?,?)",
                   (actor["github_id"], change.github_id, change.model_dump_json(), time.time()))
    return {"ok": True}
