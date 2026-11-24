#!/usr/bin/env python3
"""Flixdesk (RustDesk Edition) All-in-One Web Console & Process Supervisor.

Runs hbbs + hbbr + Web Management Console inside a single container with
zero required environment variables. Default Web Console port: 21114.
"""

import base64
import collections
import hashlib
import hmac
import http.cookies
import http.server
import json
import os
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

DATA_DIR = Path(os.environ.get("FLIXDESK_DATA_DIR", "/data"))
CONFIG_FILE = DATA_DIR / "flixdesk_config.json"
PUB_KEY_FILE = DATA_DIR / "id_ed25519.pub"
PRIV_KEY_FILE = DATA_DIR / "id_ed25519"
HBBS_DB_FILE = DATA_DIR / "db_v2.sqlite3"
PANEL_PORT = int(os.environ.get("PANEL_PORT", "21114"))
STATIC_DIR = Path(__file__).resolve().parent

SESSIONS: dict[str, float] = {}
SESSION_TTL = 86400 * 7
CONFIG_LOCK = threading.Lock()


def detect_external_ip() -> str:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://ip.sb"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                ip = resp.read().decode("utf-8", errors="ignore").strip()
                if ip and len(ip) <= 45 and " " not in ip:
                    return ip
        except Exception:
            continue
    return ""


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120000)
    return salt, dk.hex()


def verify_password(password: str, salt: str, digest: str) -> bool:
    _, calc = hash_password(password, salt)
    return hmac.compare_digest(calc, digest)


def default_config() -> dict:
    cfg = {
        "public_host": os.environ.get("PUBLIC_HOST", "").strip() or detect_external_ip(),
        "relay_port": int(os.environ.get("RELAY_PORT", "21117")),
        "force_key": os.environ.get("FORCE_KEY", "true").lower() not in ("0", "false", "no"),
        "always_use_relay": os.environ.get("ALWAYS_USE_RELAY", "false").lower() in ("1", "true", "yes"),
        "panel_password_salt": "",
        "panel_password_hash": "",
        "auto_start": True,
    }
    env_pwd = os.environ.get("PANEL_PASSWORD", "").strip()
    if env_pwd:
        salt, h = hash_password(env_pwd)
        cfg["panel_password_salt"] = salt
        cfg["panel_password_hash"] = h
    return cfg


def load_config() -> dict:
    with CONFIG_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if CONFIG_FILE.exists():
            try:
                saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                base = default_config()
                base.update(saved)
                return base
            except Exception:
                pass
        cfg = default_config()
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        return cfg


def save_config(cfg: dict) -> None:
    with CONFIG_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def read_public_key() -> str:
    if PUB_KEY_FILE.exists():
        return PUB_KEY_FILE.read_text(encoding="utf-8", errors="ignore").strip()
    return ""


def make_rustdesk_config_string(host: str, key: str) -> str:
    if not host:
        return ""
    payload = {"host": host, "relay": host, "key": key}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    b64 = base64.b64encode(raw).decode("ascii")
    return b64[::-1]


def list_registered_peers() -> list[dict]:
    if not HBBS_DB_FILE.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{HBBS_DB_FILE}?mode=ro", uri=True, timeout=2)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='peer'")
        if not cur.fetchone():
            conn.close()
            return []
        cur.execute("SELECT id, created_at FROM peer ORDER BY created_at DESC LIMIT 200")
        rows = [{"id": r[0], "created_at": str(r[1] or "")} for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


class Supervisor:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.hbbs: subprocess.Popen | None = None
        self.hbbr: subprocess.Popen | None = None
        self.logs: collections.deque[str] = collections.deque(maxlen=400)
        self.last_error = ""
        self.started_at: float | None = None

    def log(self, src: str, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] [{src}] {msg.rstrip()}"
        self.logs.append(line)
        print(line, flush=True)

    def _pipe(self, name: str, stream) -> None:
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                self.log(name, line)
        except Exception:
            pass

    def _stop_proc(self, proc: subprocess.Popen | None, name: str) -> None:
        if proc is None or proc.poll() is not None:
            return
        self.log("supervisor", f"Stopping {name} (pid={proc.pid})")
        try:
            proc.terminate()
            proc.wait(timeout=6)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass

    def stop(self) -> None:
        with self.lock:
            self._stop_proc(self.hbbs, "hbbs")
            self._stop_proc(self.hbbr, "hbbr")
            self.hbbs = None
            self.hbbr = None
            self.started_at = None

    def start(self, cfg: dict) -> tuple[bool, str]:
        with self.lock:
            self._stop_proc(self.hbbs, "hbbs")
            self._stop_proc(self.hbbr, "hbbr")
            self.hbbs = None
            self.hbbr = None
            self.last_error = ""

            DATA_DIR.mkdir(parents=True, exist_ok=True)
            host = str(cfg.get("public_host", "")).strip()
            relay_port = int(cfg.get("relay_port", 21117))
            force_key = bool(cfg.get("force_key", True))
            always_relay = bool(cfg.get("always_use_relay", False))

            env = os.environ.copy()
            env["ALWAYS_USE_RELAY"] = "Y" if always_relay else "N"

            hbbs_cmd = ["/usr/local/bin/hbbs"]
            if host:
                hbbs_cmd += ["-r", f"{host}:{relay_port}"]
            if force_key:
                hbbs_cmd += ["-k", "_"]

            hbbr_cmd = ["/usr/local/bin/hbbr"]
            if force_key:
                hbbr_cmd += ["-k", "_"]

            try:
                self.hbbs = subprocess.Popen(
                    hbbs_cmd,
                    cwd=str(DATA_DIR),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                threading.Thread(target=self._pipe, args=("hbbs", self.hbbs.stdout), daemon=True).start()
                self.log("supervisor", f"Started hbbs (pid={self.hbbs.pid}): {' '.join(hbbs_cmd)}")

                self.hbbr = subprocess.Popen(
                    hbbr_cmd,
                    cwd=str(DATA_DIR),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                threading.Thread(target=self._pipe, args=("hbbr", self.hbbr.stdout), daemon=True).start()
                self.log("supervisor", f"Started hbbr (pid={self.hbbr.pid}): {' '.join(hbbr_cmd)}")
                self.started_at = time.time()
                return True, "hbbs 与 hbbr 已启动"
            except Exception as e:
                self.last_error = str(e)
                self.log("supervisor", f"Failed to start services: {e}")
                return False, str(e)

    def status(self) -> dict:
        with self.lock:
            hbbs_up = self.hbbs is not None and self.hbbs.poll() is None
            hbbr_up = self.hbbr is not None and self.hbbr.poll() is None
            return {
                "hbbs_running": hbbs_up,
                "hbbs_pid": self.hbbs.pid if hbbs_up else None,
                "hbbr_running": hbbr_up,
                "hbbr_pid": self.hbbr.pid if hbbr_up else None,
                "uptime_seconds": int(time.time() - self.started_at) if (self.started_at and hbbs_up) else 0,
                "last_error": self.last_error,
                "public_key": read_public_key(),
            }


SUPERVISOR = Supervisor()


class PanelHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return

    def _send_json(self, data: dict, status: int = 200, extra_headers: dict | None = None) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 1024 * 1024:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _is_authed(self, cfg: dict) -> bool:
        if not cfg.get("panel_password_hash"):
            return False
        c = http.cookies.SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except Exception:
            return False
        m = c.get("flixdesk_rd_sid")
        if not m:
            return False
        exp = SESSIONS.get(m.value)
        if not exp or exp < time.time():
            SESSIONS.pop(m.value, None)
            return False
        return True

    def _require_auth(self, cfg: dict) -> bool:
        if not cfg.get("panel_password_hash"):
            self._send_json({"error": "need_setup"}, status=401)
            return False
        if not self._is_authed(cfg):
            self._send_json({"error": "unauthorized"}, status=401)
            return False
        return True

    def do_GET(self) -> None:
        cfg = load_config()
        if self.path == "/" or self.path.startswith("/?"):
            body = (STATIC_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/auth/state":
            self._send_json({
                "initialized": bool(cfg.get("panel_password_hash")),
                "authenticated": self._is_authed(cfg),
            })
            return

        if not self._require_auth(cfg):
            return

        if self.path == "/api/status":
            st = SUPERVISOR.status()
            host = cfg.get("public_host", "")
            st["public_host"] = host
            st["client_config_string"] = make_rustdesk_config_string(host, st["public_key"])
            st["peers"] = list_registered_peers()
            self._send_json(st)
            return

        if self.path == "/api/config":
            safe = {k: v for k, v in cfg.items() if not k.startswith("panel_password_")}
            self._send_json(safe)
            return

        if self.path == "/api/logs":
            self._send_json({"lines": list(SUPERVISOR.logs)})
            return

        self._send_json({"error": "not_found"}, status=404)

    def do_POST(self) -> None:
        cfg = load_config()
        try:
            body = self._read_json()
        except Exception:
            self._send_json({"error": "invalid_json"}, status=400)
            return

        if self.path == "/api/auth/setup":
            if cfg.get("panel_password_hash"):
                self._send_json({"error": "密码已初始化"}, status=400)
                return
            pwd = str(body.get("password", "")).strip()
            if len(pwd) < 6:
                self._send_json({"error": "密码至少 6 位"}, status=400)
                return
            salt, h = hash_password(pwd)
            cfg["panel_password_salt"] = salt
            cfg["panel_password_hash"] = h
            save_config(cfg)
            sid = secrets.token_hex(32)
            SESSIONS[sid] = time.time() + SESSION_TTL
            self._send_json(
                {"ok": True},
                extra_headers={"Set-Cookie": f"flixdesk_rd_sid={sid}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}"},
            )
            return

        if self.path == "/api/auth/login":
            pwd = str(body.get("password", ""))
            if not cfg.get("panel_password_hash") or not verify_password(pwd, cfg["panel_password_salt"], cfg["panel_password_hash"]):
                self._send_json({"error": "密码错误"}, status=403)
                return
            sid = secrets.token_hex(32)
            SESSIONS[sid] = time.time() + SESSION_TTL
            self._send_json(
                {"ok": True},
                extra_headers={"Set-Cookie": f"flixdesk_rd_sid={sid}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}"},
            )
            return

        if self.path == "/api/auth/logout":
            self._send_json(
                {"ok": True},
                extra_headers={"Set-Cookie": "flixdesk_rd_sid=; Path=/; HttpOnly; Max-Age=0"},
            )
            return

        if not self._require_auth(cfg):
            return

        if self.path == "/api/config":
            if "public_host" in body:
                cfg["public_host"] = str(body["public_host"]).strip()
            if "relay_port" in body and str(body["relay_port"]).strip():
                cfg["relay_port"] = int(body["relay_port"])
            if "force_key" in body:
                cfg["force_key"] = bool(body["force_key"])
            if "always_use_relay" in body:
                cfg["always_use_relay"] = bool(body["always_use_relay"])
            save_config(cfg)
            ok, msg = SUPERVISOR.start(cfg)
            self._send_json({"ok": ok, "message": msg})
            return

        if self.path == "/api/service/restart":
            ok, msg = SUPERVISOR.start(cfg)
            self._send_json({"ok": ok, "message": msg}, status=200 if ok else 400)
            return

        if self.path == "/api/service/stop":
            SUPERVISOR.stop()
            self._send_json({"ok": True, "message": "服务已停止"})
            return

        if self.path == "/api/key/regenerate":
            SUPERVISOR.stop()
            for f in (PUB_KEY_FILE, PRIV_KEY_FILE):
                if f.exists():
                    f.unlink()
            ok, msg = SUPERVISOR.start(cfg)
            time.sleep(1)
            self._send_json({"ok": ok, "message": "已重新生成密钥对并重启服务", "public_key": read_public_key()})
            return

        if self.path == "/api/detect-ip":
            self._send_json({"public_host": detect_external_ip()})
            return

        self._send_json({"error": "not_found"}, status=404)


def main() -> None:
    cfg = load_config()
    if cfg.get("auto_start", True):
        SUPERVISOR.start(cfg)

    server = http.server.ThreadingHTTPServer(("0.0.0.0", PANEL_PORT), PanelHandler)

    def handle_sig(_signum, _frame):
        SUPERVISOR.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    SUPERVISOR.log("panel", f"Flixdesk RustDesk Console listening on http://0.0.0.0:{PANEL_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
