#!/usr/bin/env python3
"""Flixdesk All-in-One Web Configuration Panel & Process Supervisor.

Manages crossdesk_server and coturn inside a single container with zero
required environment variables, exposing a Web UI on PANEL_PORT (default 9098).
"""

import collections
import hashlib
import hmac
import http.cookies
import http.server
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

DATA_DIR = Path(os.environ.get("CROSSDESK_DATA_DIR", "/var/lib/crossdesk"))
LOG_DIR = Path(os.environ.get("CROSSDESK_LOG_DIR", "/var/log/crossdesk"))
CERT_DIR = DATA_DIR / "certs"
DB_DIR = DATA_DIR / "db"
CONFIG_FILE = DATA_DIR / "panel_config.json"
PANEL_PORT = int(os.environ.get("PANEL_PORT", "9098"))
STATIC_DIR = Path(__file__).resolve().parent

CERT_KEY = CERT_DIR / "api.crossdesk.cn.key"
CERT_BUNDLE = CERT_DIR / "api.crossdesk.cn_bundle.crt"
CERT_ROOT = CERT_DIR / "api.crossdesk.cn_root.crt"

SESSIONS: dict[str, float] = {}
SESSION_TTL = 86400 * 7
CONFIG_LOCK = threading.Lock()


def detect_internal_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def detect_external_ip() -> str:
    urls = [
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://ip.sb",
    ]
    for url in urls:
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
    ext_ip = os.environ.get("EXTERNAL_IP", "").strip() or detect_external_ip()
    int_ip = os.environ.get("INTERNAL_IP", "").strip() or detect_internal_ip()
    cfg = {
        "external_ip": ext_ip,
        "internal_ip": int_ip,
        "public_host": os.environ.get("COTURN_PUBLIC_HOST", "").strip(),
        "server_port": int(os.environ.get("CROSSDESK_SERVER_PORT", "9099")),
        "coturn_port": int(os.environ.get("COTURN_PORT", "3478")),
        "min_port": int(os.environ.get("MIN_PORT", "50000")),
        "max_port": int(os.environ.get("MAX_PORT", "60000")),
        "coturn_realm": os.environ.get("COTURN_REALM", "flixdesk").strip() or "flixdesk",
        "coturn_auth_secret": os.environ.get("COTURN_AUTH_SECRET", "").strip() or secrets.token_hex(32),
        "coturn_stateless_nonce_secret": os.environ.get("COTURN_STATELESS_NONCE_SECRET", "").strip() or secrets.token_hex(32),
        "coturn_credential_ttl": int(os.environ.get("COTURN_CREDENTIAL_TTL_SECONDS", "3600")),
        "ice_servers": os.environ.get("CROSSDESK_ICE_SERVERS", "").strip(),
        "admin_username": os.environ.get("ADMIN_USERNAME", "").strip(),
        "admin_password": os.environ.get("ADMIN_PASSWORD", "").strip(),
        "max_connections": int(os.environ.get("CROSSDESK_MAX_CONNECTIONS", "8192")),
        "panel_password_salt": "",
        "panel_password_hash": "",
        "auto_start": True,
    }
    env_panel_pwd = os.environ.get("PANEL_PASSWORD", "").strip()
    if env_panel_pwd:
        salt, h = hash_password(env_panel_pwd)
        cfg["panel_password_salt"] = salt
        cfg["panel_password_hash"] = h
    return cfg


def load_config() -> dict:
    with CONFIG_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CERT_DIR.mkdir(parents=True, exist_ok=True)
        DB_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / "coturn").mkdir(parents=True, exist_ok=True)
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


class ServiceSupervisor:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.server_proc: subprocess.Popen | None = None
        self.coturn_proc: subprocess.Popen | None = None
        self.logs: collections.deque[str] = collections.deque(maxlen=400)
        self.last_error = ""
        self.started_at: float | None = None
        self._turn_help_cache: str | None = None

    def log(self, source: str, message: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] [{source}] {message.rstrip()}"
        self.logs.append(line)
        print(line, flush=True)

    def _pipe_reader(self, name: str, stream) -> None:
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                self.log(name, line)
        except Exception:
            pass

    def _get_turn_help(self) -> str:
        if self._turn_help_cache is None:
            try:
                res = subprocess.run(
                    ["turnserver", "--help"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self._turn_help_cache = (res.stdout or "") + (res.stderr or "")
            except Exception:
                self._turn_help_cache = ""
        return self._turn_help_cache

    def ensure_dirs(self) -> None:
        CERT_DIR.mkdir(parents=True, exist_ok=True)
        DB_DIR.mkdir(parents=True, exist_ok=True)
        coturn_log = LOG_DIR / "coturn"
        coturn_log.mkdir(parents=True, exist_ok=True)
        try:
            os.chown(coturn_log, 65534, 65534)
            os.chmod(coturn_log, 0o750)
        except Exception:
            pass

    def generate_certs(self, external_ip: str, force: bool = False) -> tuple[bool, str]:
        self.ensure_dirs()
        if not force and CERT_KEY.exists() and CERT_BUNDLE.exists():
            return True, "Certificates already exist"
        if not external_ip:
            return False, "请先填写公网 IP（EXTERNAL_IP）后再生成证书"
        if force:
            for f in (CERT_KEY, CERT_BUNDLE, CERT_ROOT):
                if f.exists():
                    f.unlink()
        try:
            res = subprocess.run(
                ["bash", "/docker/generate_certs.sh", external_ip, str(CERT_DIR)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if res.returncode != 0:
                msg = (res.stderr or res.stdout or "证书生成失败").strip()
                self.log("cert", f"FAILED: {msg}")
                return False, msg
            self.log("cert", f"Generated self-signed certificates for {external_ip}")
            return True, "证书生成成功"
        except Exception as e:
            self.log("cert", f"ERROR: {e}")
            return False, str(e)

    def _terminate(self, proc: subprocess.Popen | None, name: str) -> None:
        if proc is None or proc.poll() is not None:
            return
        self.log("supervisor", f"Stopping {name} (pid={proc.pid})...")
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass

    def stop(self) -> None:
        with self.lock:
            self._terminate(self.coturn_proc, "coturn")
            self._terminate(self.server_proc, "crossdesk_server")
            self.coturn_proc = None
            self.server_proc = None
            self.started_at = None

    def start(self, cfg: dict) -> tuple[bool, str]:
        with self.lock:
            self._terminate(self.coturn_proc, "coturn")
            self._terminate(self.server_proc, "crossdesk_server")
            self.coturn_proc = None
            self.server_proc = None
            self.last_error = ""

            ext_ip = str(cfg.get("external_ip", "")).strip()
            int_ip = str(cfg.get("internal_ip", "")).strip() or detect_internal_ip()
            if not ext_ip:
                self.last_error = "未配置公网 IP（EXTERNAL_IP），请在 Web 面板中填写后启动。"
                self.log("supervisor", self.last_error)
                return False, self.last_error

            ok, msg = self.generate_certs(ext_ip, force=False)
            if not ok:
                self.last_error = msg
                return False, msg

            env = os.environ.copy()
            env.update({
                "EXTERNAL_IP": ext_ip,
                "CROSSDESK_SERVER_PORT": str(cfg.get("server_port", 9099)),
                "COTURN_PUBLIC_HOST": str(cfg.get("public_host", "")).strip(),
                "COTURN_PORT": str(cfg.get("coturn_port", 3478)),
                "COTURN_AUTH_SECRET": str(cfg.get("coturn_auth_secret", "")),
                "COTURN_CREDENTIAL_TTL_SECONDS": str(cfg.get("coturn_credential_ttl", 3600)),
                "CROSSDESK_ICE_SERVERS": str(cfg.get("ice_servers", "")).strip(),
                "ADMIN_USERNAME": str(cfg.get("admin_username", "")).strip(),
                "ADMIN_PASSWORD": str(cfg.get("admin_password", "")).strip(),
                "CROSSDESK_MAX_CONNECTIONS": str(cfg.get("max_connections", 8192)),
            })

            server_bin = "/crossdesk-server/crossdesk_server"
            if not os.path.exists(server_bin):
                self.last_error = f"未找到服务端二进制: {server_bin}"
                self.log("supervisor", self.last_error)
                return False, self.last_error

            try:
                self.server_proc = subprocess.Popen(
                    [server_bin, str(cfg.get("server_port", 9099))],
                    cwd="/",
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                threading.Thread(
                    target=self._pipe_reader,
                    args=("server", self.server_proc.stdout),
                    daemon=True,
                ).start()
                self.log("supervisor", f"Started crossdesk_server on port {cfg.get('server_port', 9099)} (pid={self.server_proc.pid})")
            except Exception as e:
                self.last_error = f"启动 crossdesk_server 失败: {e}"
                self.log("supervisor", self.last_error)
                return False, self.last_error

            turn_help = self._get_turn_help()
            turn_cmd = [
                "turnserver",
                "-n",
                f"--listening-port={cfg.get('coturn_port', 3478)}",
                f"--listening-ip={int_ip}",
                f"--relay-ip={int_ip}",
                f"--external-ip={ext_ip}/{int_ip}",
                f"--min-port={cfg.get('min_port', 50000)}",
                f"--max-port={cfg.get('max_port', 60000)}",
                "--fingerprint",
                "--use-auth-secret",
                f"--static-auth-secret={cfg.get('coturn_auth_secret', '')}",
                f"--realm={cfg.get('coturn_realm', 'flixdesk')}",
                f"--cert={CERT_BUNDLE}",
                f"--pkey={CERT_KEY}",
                f"--log-file={LOG_DIR / 'coturn' / 'turn.log'}",
                "--no-stdout-log",
                "--new-log-timestamp",
                "--no-multicast-peers",
                "--proc-user=nobody",
                "--proc-group=nogroup",
            ]
            if "--dtls" in turn_help:
                turn_cmd.append("--dtls")
            if "--stateless-nonce-secret" in turn_help and cfg.get("coturn_stateless_nonce_secret"):
                turn_cmd.append(f"--stateless-nonce-secret={cfg['coturn_stateless_nonce_secret']}")
            if "--cli" in turn_help:
                turn_cmd.append("--cli=false")

            try:
                self.coturn_proc = subprocess.Popen(
                    turn_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                threading.Thread(
                    target=self._pipe_reader,
                    args=("coturn", self.coturn_proc.stdout),
                    daemon=True,
                ).start()
                self.log("supervisor", f"Started coturn on port {cfg.get('coturn_port', 3478)} (pid={self.coturn_proc.pid})")
            except Exception as e:
                self.log("supervisor", f"启动 coturn 失败: {e}")

            self.started_at = time.time()
            return True, "服务已启动"

    def status(self) -> dict:
        with self.lock:
            srv_running = self.server_proc is not None and self.server_proc.poll() is None
            turn_running = self.coturn_proc is not None and self.coturn_proc.poll() is None
            return {
                "server_running": srv_running,
                "server_pid": self.server_proc.pid if srv_running else None,
                "coturn_running": turn_running,
                "coturn_pid": self.coturn_proc.pid if turn_running else None,
                "uptime_seconds": int(time.time() - self.started_at) if (self.started_at and srv_running) else 0,
                "last_error": self.last_error,
                "has_certs": CERT_KEY.exists() and CERT_BUNDLE.exists(),
                "has_root_cert": CERT_ROOT.exists(),
            }


SUPERVISOR = ServiceSupervisor()


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
        if length <= 0 or length > 2 * 1024 * 1024:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _is_authed(self, cfg: dict) -> bool:
        if not cfg.get("panel_password_hash"):
            return False
        cookie_hdr = self.headers.get("Cookie", "")
        c = http.cookies.SimpleCookie()
        try:
            c.load(cookie_hdr)
        except Exception:
            return False
        morsel = c.get("flixdesk_panel_sid")
        if not morsel:
            return False
        exp = SESSIONS.get(morsel.value)
        if not exp or exp < time.time():
            SESSIONS.pop(morsel.value, None)
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
            html_file = STATIC_DIR / "index.html"
            body = html_file.read_bytes()
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
            st["detected_internal_ip"] = detect_internal_ip()
            self._send_json(st)
            return

        if self.path == "/api/config":
            safe = {k: v for k, v in cfg.items() if not k.startswith("panel_password_")}
            self._send_json(safe)
            return

        if self.path == "/api/logs":
            self._send_json({"lines": list(SUPERVISOR.logs)})
            return

        if self.path == "/api/certs/root.crt":
            if not CERT_ROOT.exists():
                self._send_json({"error": "根证书不存在（如使用自有域名证书则无需根证书）"}, status=404)
                return
            data = CERT_ROOT.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-x509-ca-cert")
            self.send_header("Content-Disposition", 'attachment; filename="api.crossdesk.cn_root.crt"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
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
                self._send_json({"error": "面板密码已初始化"}, status=400)
                return
            pwd = str(body.get("password", "")).strip()
            if len(pwd) < 6:
                self._send_json({"error": "密码长度至少 6 位"}, status=400)
                return
            salt, h = hash_password(pwd)
            cfg["panel_password_salt"] = salt
            cfg["panel_password_hash"] = h
            save_config(cfg)
            sid = secrets.token_hex(32)
            SESSIONS[sid] = time.time() + SESSION_TTL
            self._send_json(
                {"ok": True},
                extra_headers={"Set-Cookie": f"flixdesk_panel_sid={sid}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}"},
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
                extra_headers={"Set-Cookie": f"flixdesk_panel_sid={sid}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}"},
            )
            return

        if self.path == "/api/auth/logout":
            self._send_json(
                {"ok": True},
                extra_headers={"Set-Cookie": "flixdesk_panel_sid=; Path=/; HttpOnly; Max-Age=0"},
            )
            return

        if not self._require_auth(cfg):
            return

        if self.path == "/api/config":
            editable_str = [
                "external_ip", "internal_ip", "public_host", "coturn_realm",
                "coturn_auth_secret", "coturn_stateless_nonce_secret",
                "ice_servers", "admin_username", "admin_password",
            ]
            editable_int = [
                "server_port", "coturn_port", "min_port", "max_port",
                "coturn_credential_ttl", "max_connections",
            ]
            for k in editable_str:
                if k in body:
                    cfg[k] = str(body[k]).strip()
            for k in editable_int:
                if k in body and str(body[k]).strip() != "":
                    cfg[k] = int(body[k])
            if not cfg["coturn_auth_secret"]:
                cfg["coturn_auth_secret"] = secrets.token_hex(32)
            if not cfg["coturn_stateless_nonce_secret"]:
                cfg["coturn_stateless_nonce_secret"] = secrets.token_hex(32)
            save_config(cfg)
            if body.get("restart", True):
                ok, msg = SUPERVISOR.start(cfg)
                self._send_json({"ok": ok, "message": msg})
            else:
                self._send_json({"ok": True, "message": "配置已保存"})
            return

        if self.path == "/api/certs/generate":
            ok, msg = SUPERVISOR.generate_certs(cfg.get("external_ip", ""), force=True)
            if ok:
                SUPERVISOR.start(cfg)
            self._send_json({"ok": ok, "message": msg}, status=200 if ok else 400)
            return

        if self.path == "/api/certs/upload":
            cert_pem = str(body.get("cert", "")).strip()
            key_pem = str(body.get("key", "")).strip()
            if "BEGIN CERTIFICATE" not in cert_pem or "PRIVATE KEY" not in key_pem:
                self._send_json({"error": "证书或私钥格式无效（需 PEM 格式）"}, status=400)
                return
            SUPERVISOR.ensure_dirs()
            CERT_BUNDLE.write_text(cert_pem + "\n", encoding="utf-8")
            CERT_KEY.write_text(key_pem + "\n", encoding="utf-8")
            os.chmod(CERT_KEY, 0o600)
            ok, msg = SUPERVISOR.start(cfg)
            self._send_json({"ok": ok, "message": "自定义证书已保存并重载服务" if ok else msg})
            return

        if self.path == "/api/service/restart":
            ok, msg = SUPERVISOR.start(cfg)
            self._send_json({"ok": ok, "message": msg}, status=200 if ok else 400)
            return

        if self.path == "/api/service/stop":
            SUPERVISOR.stop()
            self._send_json({"ok": True, "message": "服务已停止"})
            return

        if self.path == "/api/detect-ip":
            self._send_json({
                "external_ip": detect_external_ip(),
                "internal_ip": detect_internal_ip(),
            })
            return

        self._send_json({"error": "not_found"}, status=404)


def main() -> None:
    cfg = load_config()
    if cfg.get("auto_start", True) and cfg.get("external_ip"):
        SUPERVISOR.start(cfg)
    else:
        SUPERVISOR.log("panel", f"Waiting for configuration on http://0.0.0.0:{PANEL_PORT}")

    server = http.server.ThreadingHTTPServer(("0.0.0.0", PANEL_PORT), PanelHandler)

    def handle_sig(_signum, _frame):
        SUPERVISOR.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    SUPERVISOR.log("panel", f"Flixdesk Web Config Panel listening on http://0.0.0.0:{PANEL_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
