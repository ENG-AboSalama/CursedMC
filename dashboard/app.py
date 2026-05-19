#!/usr/bin/env python3
"""
CursedMC - Web Dashboard
Full control from browser - no terminal commands needed!

© 2026 CursedMC. All rights reserved.
"""

import os
import sys
import sqlite3
import subprocess
import threading
import time
import hashlib
import secrets
import re
import json
import shutil
import math
from pathlib import Path
from datetime import datetime, timedelta
from functools import wraps
from collections import deque

from flask import Flask, render_template, jsonify, request, redirect, url_for, session, send_file
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename
from urllib.parse import urlparse
import psutil
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import bcrypt

# ============ PATHS ============
BASE_DIR = Path("/opt/cursedmc")
DB_FILE = BASE_DIR / "cursedmc.db"
SERVER_DIR = BASE_DIR / "server"
MODS_DIR = SERVER_DIR / "mods"
LOG_FILE = BASE_DIR / "logs" / "server.log"
PID_FILE = BASE_DIR / "server.pid"
PROPERTIES_FILE = SERVER_DIR / "server.properties"
PLAYIT_LOG_FILE = BASE_DIR / "logs" / "playit.log"
PLAYIT_PID_FILE = BASE_DIR / "playit.pid"

# ============ APP CONFIG ============
app = Flask(__name__)
app.config['SECRET_KEY'] = secrets.token_hex(32)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB max upload
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# HTTP session with retry logic for external APIs
def get_http_session():
    """Create HTTP session with retry logic"""
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

# ============ GLOBAL STATE ============
ngrok_url = None
ngrok_error = None
ngrok_status = "not_configured"  # not_configured, starting, active, error
server_process = None
server_stdin = None
auto_restart_enabled = False
performance_history = []

MIN_RAM_GB = 4
STATUS_EMIT_INTERVAL_SEC = 3
PERF_SAMPLE_INTERVAL_SEC = 15
PLAYIT_STATUS_CACHE_SEC = 15
PERF_MAX_ROWS = 1000
PERF_CLEANUP_INTERVAL_SEC = 300
LOG_ROTATE_BYTES = 50 * 1024 * 1024
LOG_ROTATE_KEEP = 3

_last_perf_cleanup = 0


def parse_ram_to_gb(ram_value):
    """Parse a RAM string like 4G/4096M into GB (float)."""
    try:
        ram_str = str(ram_value).strip().upper()
        if ram_str.endswith('G'):
            return float(ram_str[:-1])
        if ram_str.endswith('M'):
            return float(ram_str[:-1]) / 1024
        return float(ram_str)
    except Exception:
        return float(MIN_RAM_GB)


def normalize_ram_setting(ram_value):
    """Normalize RAM to an integer GB string and clamp to MIN_RAM_GB."""
    ram_gb = parse_ram_to_gb(ram_value)
    if not ram_gb or ram_gb < MIN_RAM_GB:
        ram_gb = float(MIN_RAM_GB)
    ram_gb = int(math.ceil(ram_gb))
    return ram_gb, f"{ram_gb}G"


def cap_max_ram_to_system(requested_gb):
    """Cap max RAM so the OS and dashboard keep headroom."""
    try:
        total_bytes = psutil.virtual_memory().total
        total_gb = max(1, int(math.floor(total_bytes / (1024 ** 3))))
        reserve_gb = max(2, int(math.ceil(total_gb * 0.25)))
        if total_gb - reserve_gb < MIN_RAM_GB:
            reserve_gb = max(0, total_gb - MIN_RAM_GB)
        max_allowed_gb = max(MIN_RAM_GB, total_gb - reserve_gb)
        capped_gb = min(int(requested_gb), int(max_allowed_gb))
        return capped_gb, int(max_allowed_gb), int(total_gb), int(reserve_gb)
    except Exception:
        return int(requested_gb), int(requested_gb), 0, 0


def get_effective_max_ram(ram_value):
    """Return requested and capped RAM values for safe runtime use."""
    requested_gb, _ = normalize_ram_setting(ram_value)
    capped_gb, max_allowed_gb, total_gb, reserve_gb = cap_max_ram_to_system(requested_gb)
    return requested_gb, capped_gb, f"{capped_gb}G", max_allowed_gb, total_gb, reserve_gb


def get_g1_region_size(ram_gb):
    """Choose a G1 region size based on heap size."""
    return "16M" if ram_gb >= 12 else "8M"


def rotate_log_file():
    """Rotate server.log when it gets large to reduce I/O pressure."""
    try:
        if not LOG_FILE.exists() or LOG_FILE.stat().st_size < LOG_ROTATE_BYTES:
            return

        oldest = Path(f"{LOG_FILE}.{LOG_ROTATE_KEEP}")
        if oldest.exists():
            oldest.unlink()

        for i in range(LOG_ROTATE_KEEP - 1, 0, -1):
            src = Path(f"{LOG_FILE}.{i}")
            dst = Path(f"{LOG_FILE}.{i + 1}")
            if src.exists():
                src.replace(dst)

        LOG_FILE.replace(Path(f"{LOG_FILE}.1"))
    except Exception:
        pass


# ============ DATABASE ============

def get_db_connection():
    """Get thread-safe database connection"""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize SQLite database"""
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    
    # Settings table
    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    
    # Server history table
    c.execute('''
        CREATE TABLE IF NOT EXISTS server_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Performance history table
    c.execute('''
        CREATE TABLE IF NOT EXISTS performance_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cpu REAL,
            ram REAL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Default settings - Note: password_hash is empty by default, enforced in setup
    defaults = {
        'setup_complete': 'false',
        'server_name': 'My Server',
        'server_type': 'paper',
        'server_version': '1.21.4',
        'server_ram': '4G',
        'server_port': '25565',
        'playit_address': '',
        'ngrok_token': '',
        'password_hash': '',
        'auto_restart': 'false',
        'auto_playit': 'true'
    }
    
    for key, value in defaults.items():
        c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))

    # Ensure server RAM setting respects the minimum
    try:
        c.execute('SELECT value FROM settings WHERE key = "server_ram"')
        row = c.fetchone()
        if row and row[0]:
            _, normalized_ram = normalize_ram_setting(row[0])
            c.execute('UPDATE settings SET value = ? WHERE key = "server_ram"', (normalized_ram,))
    except Exception:
        pass
    
    conn.commit()
    conn.close()


def get_setting(key, default=''):
    """Get a setting from database (thread-safe)"""
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
        c = conn.cursor()
        c.execute('SELECT value FROM settings WHERE key = ?', (key,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else default
    except Exception as e:
        print(f"[CursedMC] DB read error: {e}")
        return default


def set_setting(key, value):
    """Set a setting in database (thread-safe)"""
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, str(value)))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[CursedMC] DB write error: {e}")


def get_all_settings():
    """Get all settings as dict (thread-safe)"""
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
        c = conn.cursor()
        c.execute('SELECT key, value FROM settings')
        rows = c.fetchall()
        conn.close()
        return {row[0]: row[1] for row in rows}
    except Exception as e:
        print(f"[CursedMC] DB read error in get_all_settings: {e}")
        return {}


def log_action(action):
    """Log server action to history (thread-safe)"""
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
        c = conn.cursor()
        c.execute('INSERT INTO server_history (action) VALUES (?)', (str(action)[:500],))  # Limit length
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[CursedMC] Failed to log action: {e}")


def get_history(limit=50):
    """Get server history (thread-safe)"""
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
        c = conn.cursor()
        c.execute('SELECT action, timestamp FROM server_history ORDER BY id DESC LIMIT ?', (min(limit, 500),))
        rows = c.fetchall()
        conn.close()
        return [{'action': r[0], 'time': r[1]} for r in rows]
    except Exception as e:
        print(f"[CursedMC] Failed to get history: {e}")
        return []


def save_performance_data(cpu, ram):
    """Save performance data point (thread-safe)"""
    global _last_perf_cleanup
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
        c = conn.cursor()
        c.execute('INSERT INTO performance_history (cpu, ram) VALUES (?, ?)', (cpu, ram))
        now = time.time()
        if now - _last_perf_cleanup >= PERF_CLEANUP_INTERVAL_SEC:
            c.execute(
                f'DELETE FROM performance_history WHERE id NOT IN (SELECT id FROM performance_history ORDER BY id DESC LIMIT {PERF_MAX_ROWS})'
            )
            _last_perf_cleanup = now
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[CursedMC] Failed to save performance data: {e}")


def get_performance_history(limit=60):
    """Get performance history for charts (thread-safe)"""
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
        c = conn.cursor()
        c.execute('SELECT cpu, ram, timestamp FROM performance_history ORDER BY id DESC LIMIT ?', (min(limit, 500),))
        rows = c.fetchall()
        conn.close()
        return [{'cpu': r[0], 'ram': r[1], 'time': r[2]} for r in reversed(rows)]
    except Exception as e:
        print(f"[CursedMC] Failed to get performance history: {e}")
        return []


# ============ PASSWORD & AUTH ============

def hash_password(password):
    """
    Hash password using bcrypt with automatic salt generation.
    
    bcrypt is a secure password hashing algorithm that:
    - Automatically generates a unique salt per password
    - Uses a work factor for computational cost (default 12 rounds)
    - Is resistant to rainbow table and brute-force attacks
    """
    password_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt(rounds=12)
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode('utf-8')


def verify_password(password, stored_hash):
    """
    Verify password against stored bcrypt hash.
    Also supports legacy SHA256 hashes for migration.
    """
    if not stored_hash:
        return False
    
    # Check if this is a bcrypt hash (starts with $2b$ or $2a$)
    if stored_hash.startswith('$2b$') or stored_hash.startswith('$2a$'):
        try:
            return bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8'))
        except Exception:
            return False
    
    # Legacy SHA256 hash migration support
    # If valid, the user should update their password
    if ':' in stored_hash:
        salt, hashed = stored_hash.split(':', 1)
        legacy_valid = hashlib.sha256((salt + password).encode()).hexdigest() == hashed
        if legacy_valid:
            # Log that migration is needed (password should be updated)
            print("[CursedMC] ⚠️ Legacy SHA256 hash detected - password should be updated")
        return legacy_valid
    
    # Very old legacy hash (plain SHA256 without salt) - insecure
    legacy_valid = hashlib.sha256(password.encode()).hexdigest() == stored_hash
    if legacy_valid:
        print("[CursedMC] ⚠️ Very old insecure hash detected - password must be updated")
    return legacy_valid


def check_password(password):
    """Check if password is correct"""
    stored_hash = get_setting('password_hash', '')
    if not stored_hash:
        return False  # No password set means not accessible
    return verify_password(password, stored_hash)


def is_password_set():
    """Check if password is configured"""
    return bool(get_setting('password_hash', ''))


def login_required(f):
    """Decorator to require login"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Password is always required
        if not is_password_set():
            if request.is_json:
                return jsonify({'error': 'Setup not complete - password required'}), 401
            return redirect(url_for('setup'))
        if not session.get('logged_in'):
            if request.is_json:
                return jsonify({'error': 'Unauthorized'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


# ============ SECURITY ============

login_attempts = {}

def check_rate_limit(ip_address):
    """Check if IP is rate limited (5 failed attempts in 15 mins)"""
    now = time.time()
    
    # Clean up old entries
    if ip_address in login_attempts:
        attempts, first_time = login_attempts[ip_address]
        if now - first_time > 900:  # 15 minutes
            del login_attempts[ip_address]
            
    if ip_address in login_attempts:
        attempts, first_time = login_attempts[ip_address]
        if attempts >= 5:
            wait_time = int(900 - (now - first_time))
            return False, f"Too many failed attempts. Try again in {wait_time // 60} minutes."
            
    return True, ""


def record_failed_attempt(ip_address):
    """Record a failed login attempt"""
    now = time.time()
    if ip_address not in login_attempts:
        login_attempts[ip_address] = (1, now)
    else:
        attempts, first_time = login_attempts[ip_address]
        login_attempts[ip_address] = (attempts + 1, first_time)


# ============ SERVER PROPERTIES ============

def read_server_properties():
    """Read server.properties file"""
    properties = {}
    if PROPERTIES_FILE.exists():
        with open(PROPERTIES_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    properties[key.strip()] = value.strip()
    return properties


def write_server_properties(properties):
    """Write server.properties file"""
    lines = []
    existing_keys = set()
    
    if PROPERTIES_FILE.exists():
        with open(PROPERTIES_FILE, 'r') as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith('#') and '=' in stripped:
                    key = stripped.split('=', 1)[0].strip()
                    if key in properties:
                        lines.append(f"{key}={properties[key]}\n")
                        existing_keys.add(key)
                    else:
                        lines.append(line)
                else:
                    lines.append(line)
    
    for key, value in properties.items():
        if key not in existing_keys:
            lines.append(f"{key}={value}\n")
    
    with open(PROPERTIES_FILE, 'w') as f:
        f.writelines(lines)


# ============ VERSION APIS ============

def fetch_vanilla_versions():
    """Fetch available Vanilla Minecraft versions from Mojang API"""
    try:
        http = get_http_session()
        response = http.get(
            "https://launchermeta.mojang.com/mc/game/version_manifest.json",
            timeout=15
        )
        response.raise_for_status()
        data = response.json()
        
        versions = []
        for v in data['versions']:
            if v['type'] == 'release':
                versions.append({
                    'id': v['id'],
                    'type': 'release',
                    'url': v['url'],
                    'releaseTime': v['releaseTime']
                })
        return versions[:30]  # Return last 30 releases
    except requests.exceptions.Timeout:
        print("[CursedMC] Vanilla API timeout")
        return []
    except Exception as e:
        print(f"[CursedMC] Error fetching Vanilla versions: {e}")
        return []


def fetch_paper_versions():
    """Fetch available Paper versions from PaperMC API"""
    try:
        http = get_http_session()
        response = http.get(
            "https://api.papermc.io/v2/projects/paper",
            timeout=15
        )
        response.raise_for_status()
        data = response.json()
        
        versions = []
        for version in reversed(data.get('versions', [])):
            versions.append({
                'id': version,
                'type': 'paper'
            })
        return versions[:30]
    except requests.exceptions.Timeout:
        print("[CursedMC] Paper API timeout")
        return []
    except Exception as e:
        print(f"[CursedMC] Error fetching Paper versions: {e}")
        return []


def fetch_paper_builds(version):
    """Fetch available builds for a Paper version"""
    try:
        http = get_http_session()
        response = http.get(
            f"https://api.papermc.io/v2/projects/paper/versions/{version}/builds",
            timeout=15
        )
        response.raise_for_status()
        data = response.json()
        
        builds = []
        for build in reversed(data.get('builds', [])):
            builds.append({
                'build': build['build'],
                'channel': build.get('channel', 'default'),
                'downloads': build.get('downloads', {})
            })
        return builds[:20]  # Return last 20 builds
    except requests.exceptions.Timeout:
        print(f"[CursedMC] Paper builds API timeout for {version}")
        return []
    except Exception as e:
        print(f"[CursedMC] Error fetching Paper builds: {e}")
        return []


# Global cache for Forge promotions data
_forge_promos_cache = None
_forge_promos_cache_time = 0

def fetch_forge_promos():
    """Fetch and cache the full Forge promotions data."""
    global _forge_promos_cache, _forge_promos_cache_time
    import time
    
    # Cache for 5 minutes
    if _forge_promos_cache and (time.time() - _forge_promos_cache_time) < 300:
        return _forge_promos_cache
    
    try:
        http = get_http_session()
        response = http.get(
            "https://files.minecraftforge.net/maven/net/minecraftforge/forge/promotions_slim.json",
            timeout=15
        )
        response.raise_for_status()
        data = response.json()
        _forge_promos_cache = data.get('promos', {})
        _forge_promos_cache_time = time.time()
        return _forge_promos_cache
    except Exception as e:
        print(f"[CursedMC] Error fetching Forge promos: {e}")
        return _forge_promos_cache or {}


def fetch_forge_versions():
    """Fetch available Forge versions from Forge Maven Promotions endpoint.
    
    Note: This uses the official Forge promotions_slim.json from the Maven repository,
    which provides recommended and latest builds for each Minecraft version.
    """
    try:
        promos = fetch_forge_promos()
        
        versions = {}
        for key, forge_version in promos.items():
            # Parse keys like "1.20.4-recommended" or "1.20.4-latest"
            if '-' in key:
                mc_version, build_type = key.rsplit('-', 1)
                if mc_version not in versions:
                    versions[mc_version] = {'id': mc_version}
                versions[mc_version][build_type] = forge_version
        
        # Convert to list and sort by version (semantic version sorting)
        result = list(versions.values())
        
        def version_key(v):
            """Sort versions properly (1.21.10 > 1.21.2)"""
            try:
                parts = v['id'].split('.')
                return tuple(int(p) for p in parts)
            except:
                return (0,)
        
        result.sort(key=version_key, reverse=True)
        return result[:30]
    except Exception as e:
        print(f"[CursedMC] Error fetching Forge versions: {e}")
        return []


def get_forge_version_for_mc(mc_version, prefer_recommended=True):
    """Get Forge version for a specific Minecraft version.
    
    Args:
        mc_version: Minecraft version (e.g., "1.20.1")
        prefer_recommended: If True, prefer recommended over latest
    
    Returns:
        Forge version string or None if not found
    """
    try:
        promos = fetch_forge_promos()
        
        if prefer_recommended:
            recommended = promos.get(f"{mc_version}-recommended")
            if recommended:
                return recommended
        
        latest = promos.get(f"{mc_version}-latest")
        return latest
    except Exception as e:
        print(f"[CursedMC] Error getting Forge version for MC {mc_version}: {e}")
        return None


def get_forge_versions_for_mc(mc_version):
    """Get all available Forge builds for a specific Minecraft version.
    
    Args:
        mc_version: Minecraft version (e.g., "1.20.1")
    
    Returns:
        List of dicts with forge version info
    """
    try:
        promos = fetch_forge_promos()
        
        result = []
        recommended = promos.get(f"{mc_version}-recommended")
        latest = promos.get(f"{mc_version}-latest")
        
        if recommended:
            result.append({
                'forge_version': recommended,
                'type': 'recommended',
                'full_version': f"{mc_version}-{recommended}",
                'display': f"{recommended} (Recommended)"
            })
        
        if latest and latest != recommended:
            result.append({
                'forge_version': latest,
                'type': 'latest',
                'full_version': f"{mc_version}-{latest}",
                'display': f"{latest} (Latest)"
            })
        
        return result
    except Exception as e:
        print(f"[CursedMC] Error getting Forge versions for MC {mc_version}: {e}")
        return []


def install_forge_server(mc_version, forge_version):
    """Download and install Forge server automatically.
    
    This downloads the Forge installer JAR and runs it with --installServer
    to create a fully functional Forge server.
    
    Args:
        mc_version: Minecraft version (e.g., "1.20.1")
        forge_version: Forge version (e.g., "47.4.16")
    
    Returns:
        (success: bool, message: str)
    """
    import shutil
    
    SERVER_DIR.mkdir(parents=True, exist_ok=True)
    full_version = f"{mc_version}-{forge_version}"
    installer_url = f"https://maven.minecraftforge.net/net/minecraftforge/forge/{full_version}/forge-{full_version}-installer.jar"
    installer_path = SERVER_DIR / f"forge-{full_version}-installer.jar"
    
    try:
        print(f"[CursedMC] Downloading Forge installer: {installer_url}")
        
        # Download the installer
        http = get_http_session()
        response = http.get(installer_url, timeout=300, stream=True)
        response.raise_for_status()
        
        with open(installer_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        print(f"[CursedMC] Running Forge installer with --installServer")
        
        # Run the installer with --installServer
        result = subprocess.run(
            ['java', '-jar', str(installer_path), '--installServer'],
            cwd=SERVER_DIR,
            capture_output=True,
            text=True,
            timeout=600  # 10 minutes timeout for installation
        )
        
        if result.returncode != 0:
            print(f"[CursedMC] Forge installer error: {result.stderr}")
            return False, f"Forge installer failed: {result.stderr[:200]}"
        
        # Find the correct jar to use
        # Modern Forge (1.17+) creates run scripts and uses different jar naming
        # Older Forge creates forge-<version>.jar directly
        
        # Check for modern Forge (creates run.sh/run.bat)
        run_sh = SERVER_DIR / "run.sh"
        run_bat = SERVER_DIR / "run.bat"
        
        jar_path = SERVER_DIR / "server.jar"
        
        if run_sh.exists() or run_bat.exists():
            # Modern Forge - we need to use the run script or find the correct jar
            # Look for the minecraft server jar that forge downloads
            
            # For modern Forge, the server is started differently
            # We need to create a wrapper or use the forge jar directly
            
            # Look for forge-<version>-server.jar or similar
            forge_patterns = [
                f"forge-{full_version}-server.jar",
                f"forge-{full_version}-shim.jar", 
                f"forge-{full_version}.jar",
            ]
            
            forge_jar = None
            for pattern in forge_patterns:
                potential = SERVER_DIR / pattern
                if potential.exists():
                    forge_jar = potential
                    break
            
            # If no forge jar found, look for any forge*.jar
            if not forge_jar:
                for f in SERVER_DIR.glob("forge-*.jar"):
                    if "installer" not in f.name:
                        forge_jar = f
                        break
            
            if forge_jar:
                # Create symlink or copy to server.jar for compatibility
                if jar_path.exists():
                    jar_path.unlink()
                shutil.copy(forge_jar, jar_path)
                print(f"[CursedMC] Using Forge jar: {forge_jar.name}")
            else:
                # No jar found, user needs to use run script
                # Create a notice file
                notice = SERVER_DIR / "FORGE_NOTICE.txt"
                notice.write_text(
                    f"Forge {full_version} has been installed.\n\n"
                    "Modern Forge uses a different startup method.\n"
                    "The server should work normally - if not, check the logs.\n"
                )
        else:
            # Older Forge - look for the universal/server jar
            forge_jar = None
            patterns = [
                f"forge-{full_version}.jar",
                f"forge-{full_version}-universal.jar",
                f"forge-{full_version}-server.jar",
            ]
            
            for pattern in patterns:
                potential = SERVER_DIR / pattern
                if potential.exists():
                    forge_jar = potential
                    break
            
            if forge_jar:
                if jar_path.exists():
                    jar_path.unlink()
                shutil.copy(forge_jar, jar_path)
            elif not jar_path.exists():
                # Check if minecraft_server.jar was downloaded
                mc_jar = SERVER_DIR / f"minecraft_server.{mc_version}.jar"
                if mc_jar.exists():
                    shutil.copy(mc_jar, jar_path)
        
        # Clean up installer
        try:
            installer_path.unlink()
        except:
            pass
        
        # Verify installation
        if jar_path.exists():
            # Save Forge-specific settings
            set_setting('forge_version', forge_version)
            set_setting('forge_full_version', full_version)
            return True, f"Forge {full_version} installed successfully!"
        else:
            # Installation might still be OK, check for other files
            for f in SERVER_DIR.iterdir():
                if f.suffix == '.jar' and 'forge' in f.name.lower():
                    set_setting('forge_version', forge_version)
                    set_setting('forge_full_version', full_version)
                    return True, f"Forge {full_version} installed! Server jar: {f.name}"
            
            return False, "Forge installer completed but no server jar found. Check server directory."
    
    except subprocess.TimeoutExpired:
        return False, "Forge installation timed out (10 minutes). Try again or install manually."
    except requests.exceptions.RequestException as e:
        return False, f"Failed to download Forge installer: {str(e)}"
    except Exception as e:
        print(f"[CursedMC] Forge installation error: {e}")
        return False, f"Forge installation failed: {str(e)}"


# ============ SERVER FUNCTIONS ============

def is_setup_complete():
    """Check if initial setup is done"""
    # Setup is only complete if password is set
    return get_setting('setup_complete', 'false') == 'true' and is_password_set()


def get_server_pid():
    """Get server PID if running"""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            if psutil.pid_exists(pid):
                return pid
        except:
            pass
    return None


def is_server_running():
    """Check if Minecraft server is running"""
    return get_server_pid() is not None


def download_server_jar(server_type, version, build=None, forge_version=None):
    """Download the server JAR
    
    Args:
        server_type: 'vanilla', 'paper', or 'forge'
        version: Minecraft version
        build: Paper build number (optional)
        forge_version: Forge version (optional, auto-detected if not provided)
    """
    SERVER_DIR.mkdir(parents=True, exist_ok=True)
    jar_path = SERVER_DIR / "server.jar"
    
    try:
        if server_type == "vanilla":
            manifest = requests.get(
                "https://launchermeta.mojang.com/mc/game/version_manifest.json",
                timeout=10
            ).json()
            version_data = next((v for v in manifest['versions'] if v['id'] == version), None)
            if version_data:
                version_info = requests.get(version_data['url'], timeout=10).json()
                jar_url = version_info['downloads']['server']['url']
                response = requests.get(jar_url, timeout=300)
                jar_path.write_bytes(response.content)
                return True, f"Vanilla {version} downloaded!"
            return False, f"Version {version} not found"
        
        elif server_type == "paper":
            # Get latest build if not specified
            if not build:
                builds_data = requests.get(
                    f"https://api.papermc.io/v2/projects/paper/versions/{version}/builds",
                    timeout=10
                ).json()
                if builds_data.get('builds'):
                    build = builds_data['builds'][-1]['build']
                else:
                    return False, f"No builds found for Paper {version}"
            
            # Get download info
            build_info = requests.get(
                f"https://api.papermc.io/v2/projects/paper/versions/{version}/builds/{build}",
                timeout=10
            ).json()
            
            jar_name = build_info['downloads']['application']['name']
            jar_url = f"https://api.papermc.io/v2/projects/paper/versions/{version}/builds/{build}/downloads/{jar_name}"
            response = requests.get(jar_url, timeout=300)
            jar_path.write_bytes(response.content)
            return True, f"Paper {version} (build {build}) downloaded!"
        
        elif server_type == "forge":
            # Get Forge version if not specified
            if not forge_version:
                forge_version = get_forge_version_for_mc(version, prefer_recommended=True)
            
            if not forge_version:
                return False, f"No Forge builds found for Minecraft {version}"
            
            # Use the new automatic Forge installation
            return install_forge_server(version, forge_version)
        
        return False, f"Unknown server type: {server_type}"
    
    except requests.Timeout:
        return False, "Download timed out. Please try again."
    except Exception as e:
        return False, f"Download failed: {str(e)}"


def accept_eula():
    """Accept Minecraft EULA"""
    eula_file = SERVER_DIR / "eula.txt"
    eula_file.write_text("eula=true\n")


def get_optimized_jvm_flags(ram, server_type='paper'):
    """Get optimized JVM flags for Minecraft servers.
    
    Based on research from:
    - https://github.com/brucethemoose/Minecraft-Performance-Flags-Benchmarks
    - https://docs.papermc.io/paper/aikars-flags
    - https://aikar.co/2018/07/02/tuning-the-jvm-g1gc-garbage-collector-flags-for-minecraft/
    
    Key findings:
    - Minimum heap (Xms) is fixed at 4G; maximum heap (Xmx) comes from settings
    - Modded servers need more metaspace and code cache
    - G1GC settings need tuning for Minecraft's high allocation rate
    
    Args:
        ram: RAM string like '4G' or '8G' (this is the MAXIMUM)
        server_type: 'paper', 'vanilla', or 'forge'
        
    Returns:
        list: Optimized JVM arguments
    """
    # Parse RAM to get numeric value in GB, clamp to minimum
    ram_gb, max_ram = normalize_ram_setting(ram)
    min_ram = f"{MIN_RAM_GB}G"
    g1_region_size = get_g1_region_size(ram_gb)
    
    if server_type == 'forge':
        # FORGE/MODDED SERVER FLAGS
        # Based on brucethemoose benchmarks + Forge-specific needs
        # Mods require: more metaspace, larger code cache, string deduplication
        metaspace_size = '256M'
        max_metaspace = '512M'
        code_cache = '400M'
        non_nmethod_cache = '12M'
        profiled_cache = '194M'
        non_profiled_cache = '194M'
        if ram_gb <= 8:
            metaspace_size = '128M'
            max_metaspace = '256M'
            code_cache = '240M'
            non_nmethod_cache = '8M'
            profiled_cache = '116M'
            non_profiled_cache = '116M'

        flags = [
            f'-Xms{min_ram}',
            f'-Xmx{max_ram}',
            
            # G1GC - best for modded with proper tuning
            '-XX:+UseG1GC',
            '-XX:+ParallelRefProcEnabled',
            '-XX:MaxGCPauseMillis=130',  # Server can tolerate longer pauses
            '-XX:+UnlockExperimentalVMOptions',
            '-XX:+UnlockDiagnosticVMOptions',
            '-XX:+DisableExplicitGC',
            '-XX:+AlwaysPreTouch',
            '-XX:+AlwaysActAsServerClassMachine',
            
            # CRITICAL FOR MODS - Metaspace (mods load MANY classes)
            f'-XX:MetaspaceSize={metaspace_size}',
            f'-XX:MaxMetaspaceSize={max_metaspace}',
            
            # Code cache for mod class compilation
            f'-XX:ReservedCodeCacheSize={code_cache}',
            f'-XX:NonNMethodCodeHeapSize={non_nmethod_cache}',
            f'-XX:ProfiledCodeHeapSize={profiled_cache}',
            f'-XX:NonProfiledCodeHeapSize={non_profiled_cache}',
            
            # G1GC tuning for modded (from brucethemoose)
            f'-XX:G1HeapRegionSize={g1_region_size}',
            '-XX:G1ReservePercent=20',
            '-XX:G1HeapWastePercent=5',
            '-XX:G1MixedGCCountTarget=4',
            '-XX:G1MixedGCLiveThresholdPercent=90',
            '-XX:G1RSetUpdatingPauseTimePercent=5',
            '-XX:InitiatingHeapOccupancyPercent=15',
            '-XX:SurvivorRatio=32',
            '-XX:MaxTenuringThreshold=1',
            '-XX:+PerfDisableSharedMem',
            
            # Additional optimizations
            '-XX:+UseStringDeduplication',  # Mods create many duplicate strings
            '-XX:-DontCompileHugeMethods',  # Modded has huge methods
            '-XX:MaxNodeLimit=240000',
            '-XX:NodeLimitFudgeFactor=8000',
            
            # Prevent crashes, exit cleanly on OOM
            '-XX:+ExitOnOutOfMemoryError',
            '-XX:+HeapDumpOnOutOfMemoryError',
            
            # Mod compatibility flags
            '-Dfml.ignorePatchDiscrepancies=true',
            '-Dfml.ignoreInvalidMinecraftCertificates=true',
            
            # Better performance
            '-XX:+UseFastUnorderedTimeStamps',
        ]
        
        # Adjust G1 new size based on RAM
        if ram_gb >= 12:
            flags.extend([
                '-XX:G1NewSizePercent=40',
                '-XX:G1MaxNewSizePercent=50',
            ])
        elif ram_gb >= 8:
            flags.extend([
                '-XX:G1NewSizePercent=35',
                '-XX:G1MaxNewSizePercent=45',
            ])
        else:
            flags.extend([
                '-XX:G1NewSizePercent=28',
                '-XX:G1MaxNewSizePercent=40',
            ])
    
    else:
        # VANILLA/PAPER SERVER FLAGS (Aikar's flags)
        flags = [
            f'-Xms{min_ram}',
            f'-Xmx{max_ram}',
            '-XX:+UseG1GC',
            '-XX:+ParallelRefProcEnabled',
            '-XX:MaxGCPauseMillis=200',
            '-XX:+UnlockExperimentalVMOptions',
            '-XX:+DisableExplicitGC',
            '-XX:+AlwaysPreTouch',
            '-XX:G1HeapWastePercent=5',
            '-XX:G1MixedGCCountTarget=4',
            '-XX:G1MixedGCLiveThresholdPercent=90',
            '-XX:G1RSetUpdatingPauseTimePercent=5',
            '-XX:SurvivorRatio=32',
            '-XX:+PerfDisableSharedMem',
            '-XX:MaxTenuringThreshold=1',
            '-Dusing.aikars.flags=https://mcflags.emc.gs',
            '-Daikars.new.flags=true',
            
            # Stability additions
            '-XX:MetaspaceSize=128M',
            '-XX:MaxMetaspaceSize=256M',
        ]
        
        # Adjust flags based on RAM amount
        if ram_gb >= 12:
            flags.extend([
                '-XX:G1NewSizePercent=40',
                '-XX:G1MaxNewSizePercent=50',
                f'-XX:G1HeapRegionSize={g1_region_size}',
                '-XX:G1ReservePercent=15',
                '-XX:InitiatingHeapOccupancyPercent=20',
            ])
        else:
            flags.extend([
                '-XX:G1NewSizePercent=30',
                '-XX:G1MaxNewSizePercent=40',
                f'-XX:G1HeapRegionSize={g1_region_size}',
                '-XX:G1ReservePercent=20',
                '-XX:InitiatingHeapOccupancyPercent=15',
            ])
    
    return flags


def get_forge_start_command(ram, use_optimized=True):
    """Get the correct command to start a Forge server.
    
    Modern Forge (1.17+) uses different startup methods.
    We need to run Java DIRECTLY (not through run.sh) to maintain stdin pipe.
    
    Args:
        ram: RAM allocation string (e.g., '4G')
        use_optimized: Whether to use Aikar's optimized JVM flags
    
    Returns:
        list: Command arguments for subprocess
    """
    # Get JVM flags optimized for Forge/modded servers
    if use_optimized:
        jvm_flags = get_optimized_jvm_flags(ram, server_type='forge')
    else:
        # Basic flags: fixed minimum and configured maximum
        _, max_ram = normalize_ram_setting(ram)
        min_ram = f"{MIN_RAM_GB}G"
        jvm_flags = [f'-Xms{min_ram}', f'-Xmx{max_ram}']
    
    # First, try to find and parse unix_args.txt for the proper Java arguments
    for args_file in SERVER_DIR.glob("libraries/net/minecraftforge/forge/*/unix_args.txt"):
        try:
            args_content = args_file.read_text().strip()
            # Build command - the args file contains classpath and main class
            # Format: java @user_jvm_args.txt @libraries/.../unix_args.txt
            cmd = ['java'] + jvm_flags
            
            # Check for user_jvm_args.txt
            user_args = SERVER_DIR / "user_jvm_args.txt"
            if user_args.exists():
                user_content = user_args.read_text().strip()
                # Parse user args (each line is an argument, # is comment)
                for line in user_content.split('\n'):
                    line = line.strip()
                    if line and not line.startswith('#'):
                        # Skip memory args since we set our own
                        if not line.startswith('-Xmx') and not line.startswith('-Xms') and not line.startswith('-XX:'):
                            cmd.append(line)
            
            # Add the args from unix_args.txt
            # These are typically: -cp <classpath> <main_class> args...
            cmd.append(f'@{args_file}')
            
            print(f"[CursedMC] Forge start command with {'optimized' if use_optimized else 'basic'} flags")
            return cmd
        except Exception as e:
            print(f"[CursedMC] Error parsing unix_args.txt: {e}")
    
    # Fallback: Try to parse run.sh to extract the Java command
    run_sh = SERVER_DIR / "run.sh"
    if run_sh.exists():
        try:
            content = run_sh.read_text()
            # Look for the java command line
            import re
            # Match java command with @args syntax
            match = re.search(r'java\s+(.+?)(?:\n|$)', content)
            if match:
                java_args = match.group(1).strip()
                # Remove existing memory/GC args
                java_args = re.sub(r'-Xmx\S+', '', java_args)
                java_args = re.sub(r'-Xms\S+', '', java_args)
                java_args = re.sub(r'-XX:\S+', '', java_args)
                java_args = java_args.strip()
                
                cmd = ['java'] + jvm_flags + java_args.split()
                print(f"[CursedMC] Forge command from run.sh with {'optimized' if use_optimized else 'basic'} flags")
                return cmd
        except Exception as e:
            print(f"[CursedMC] Error parsing run.sh: {e}")
    
    # Check for forge JAR files (older Forge or fallback)
    forge_jars = []
    for f in SERVER_DIR.glob("forge-*.jar"):
        if "installer" not in f.name.lower():
            forge_jars.append(f)
    
    # Sort to get the most recent/relevant one
    if forge_jars:
        # Prefer server jar, then universal, then any other
        for jar in forge_jars:
            if 'server' in jar.name.lower() or 'shim' in jar.name.lower():
                return ['java'] + jvm_flags + ['-jar', jar.name, 'nogui']
        # Use the first one found
        return ['java'] + jvm_flags + ['-jar', forge_jars[0].name, 'nogui']
    
    # Fallback to server.jar
    return ['java'] + jvm_flags + ['-jar', 'server.jar', 'nogui']


def is_forge_installed():
    """Check if Forge is properly installed."""
    # Check for various Forge installation indicators
    indicators = [
        SERVER_DIR / "run.sh",
        SERVER_DIR / "run.bat",
    ]
    
    for ind in indicators:
        if ind.exists():
            return True
    
    # Check for forge jars
    for f in SERVER_DIR.glob("forge-*.jar"):
        if "installer" not in f.name.lower():
            return True
    
    # Check for libraries folder with forge
    forge_libs = SERVER_DIR / "libraries" / "net" / "minecraftforge"
    if forge_libs.exists():
        return True
    
    return False


def start_minecraft():
    """Start Minecraft server"""
    global server_process, server_stdin
    
    if is_server_running():
        return False, "Server is already running!"
    
    ram_setting = get_setting('server_ram', '4G')
    requested_gb, capped_gb, ram, _, total_gb, reserve_gb = get_effective_max_ram(ram_setting)
    if capped_gb < requested_gb:
        log_action(f'RAM capped to {ram} (system {total_gb}G, reserved {reserve_gb}G)')
    server_type = get_setting('server_type', 'paper')
    jar_path = SERVER_DIR / "server.jar"
    
    # Check if server files exist
    if server_type == 'forge':
        # For Forge, check if Forge is installed (not just server.jar)
        if not is_forge_installed() and not jar_path.exists():
            log_action('Forge not found, attempting auto-install...')
            version = get_setting('server_version', '1.21.4')
            forge_version = get_setting('forge_version', '')
            success, msg = download_server_jar(server_type, version, forge_version=forge_version)
            if not success:
                return False, f"Auto-install failed: {msg}"
            log_action(f'Auto-installed Forge for MC {version}')
    else:
        # For Vanilla/Paper, check server.jar
        if not jar_path.exists():
            log_action('Server JAR not found, attempting auto-download...')
            version = get_setting('server_version', '1.21.4')
            success, msg = download_server_jar(server_type, version)
            if not success:
                return False, f"Auto-download failed: {msg}"
            log_action(f'Auto-downloaded {server_type} {version}')
    
    # Auto-start Playit if enabled
    if get_setting('auto_playit', 'true') == 'true' and not check_playit_status():
        start_playit()
        log_action('Playit auto-started with server')
    
    accept_eula()
    
    # Check if optimized flags are enabled
    use_optimized = get_setting('optimized_jvm', 'true') == 'true'
    
    # Get the appropriate start command
    if server_type == 'forge':
        cmd = get_forge_start_command(ram, use_optimized)
        print(f"[CursedMC] Starting Forge with optimized modded flags")
        print(f"[CursedMC] Command: {' '.join(str(c) for c in cmd[:8])}...")
    else:
        if use_optimized:
            jvm_flags = get_optimized_jvm_flags(ram, server_type=server_type)
            cmd = ['java'] + jvm_flags + ['-jar', 'server.jar', 'nogui']
            print(f"[CursedMC] Starting {server_type} with optimized flags")
        else:
            min_ram = f"{MIN_RAM_GB}G"
            cmd = ['java', f'-Xms{min_ram}', f'-Xmx{ram}', '-jar', 'server.jar', 'nogui']
    
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    rotate_log_file()
    log_handle = open(LOG_FILE, 'a')
    
    # Always run without shell to maintain stdin pipe
    server_process = subprocess.Popen(
        cmd,
        cwd=SERVER_DIR,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE,
        text=True,
        bufsize=1
    )
    
    server_stdin = server_process.stdin
    PID_FILE.write_text(str(server_process.pid))
    log_action('Server Started')
    
    # Start auto-restart monitor
    if get_setting('auto_restart', 'false') == 'true':
        threading.Thread(target=monitor_server_crash, daemon=True).start()
    
    return True, "Server started!"


def stop_minecraft():
    """Stop Minecraft server gracefully"""
    global server_process, server_stdin
    
    pid = get_server_pid()
    if not pid:
        return False, "Server is not running"
    
    try:
        if server_stdin:
            try:
                server_stdin.write("stop\n")
                server_stdin.flush()
                time.sleep(5)
            except:
                pass
        
        proc = psutil.Process(pid)
        if proc.is_running():
            proc.terminate()
            proc.wait(timeout=30)
    except psutil.TimeoutExpired:
        proc.kill()
    except Exception as e:
        return False, f"Error stopping server: {e}"
    
    if PID_FILE.exists():
        PID_FILE.unlink()
    
    server_stdin = None
    log_action('Server Stopped')
    
    return True, "Server stopped!"


def send_console_command(command):
    """Send command to server console with sanitization"""
    global server_stdin
    
    if not is_server_running():
        return False, "Server is not running"
    
    if not server_stdin:
        return False, "Console not available"
    
    # Sanitize command - remove dangerous characters
    command = command.strip()
    if not command:
        return False, "Empty command"
    
    # Block shell escape attempts
    dangerous = ['$(', '`', '|', '&&', '||', ';', '>', '<', '\n', '\r']
    for d in dangerous:
        if d in command:
            return False, "Invalid command characters"
    
    # Limit command length
    if len(command) > 1000:
        return False, "Command too long (max 1000 chars)"
    
    try:
        server_stdin.write(command + "\n")
        server_stdin.flush()
        log_action(f'Command: {command[:100]}')
        return True, f"Sent: {command}"
    except Exception as e:
        return False, f"Failed to send command: {e}"


def monitor_server_crash():
    """Monitor server and restart if crashed"""
    global server_process
    
    while True:
        time.sleep(10)
        
        if get_setting('auto_restart', 'false') != 'true':
            break
        
        if server_process and server_process.poll() is not None:
            if PID_FILE.exists():
                PID_FILE.unlink()
            
            log_action('Server Crashed - Auto Restarting')
            time.sleep(5)
            start_minecraft()
            break


def get_system_stats():
    """Get system resource usage"""
    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory().percent
    
    return {
        "cpu": cpu,
        "ram_percent": ram,
        "ram_used": round(psutil.virtual_memory().used / (1024**3), 1),
        "ram_total": round(psutil.virtual_memory().total / (1024**3), 1),
        "disk_percent": psutil.disk_usage('/').percent,
        "disk_used": round(psutil.disk_usage('/').used / (1024**3), 1),
        "disk_total": round(psutil.disk_usage('/').total / (1024**3), 1)
    }


def get_logs(lines=100):
    """Get server logs efficiently"""
    if LOG_FILE.exists():
        try:
            with open(LOG_FILE, 'r', errors='replace') as f:
                return ''.join(deque(f, maxlen=lines))
        except:
            pass
    return "No logs yet. Start the server to see logs here."


# ============ PLAYIT FUNCTIONS ============

def check_playit_status():
    """Check if playit is running via screen"""
    try:
        cmd = ["screen", "-list"]
        env = os.environ.copy()
        env['TERM'] = 'xterm'
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5, env=env)
        return "playit" in result.stdout
    except:
        return False


def get_playit_pid():
    """Get Playit PID if running"""
    if PLAYIT_PID_FILE.exists():
        try:
            pid = int(PLAYIT_PID_FILE.read_text().strip())
            if psutil.pid_exists(pid):
                return pid
        except:
            pass
    
    for proc in psutil.process_iter(['name', 'pid']):
        try:
            if 'playit' in proc.info['name'].lower():
                return proc.info['pid']
        except:
            pass
    return None


def start_playit():
    """Start Playit tunnel in background"""
    if check_playit_status():
        return False, "Playit is already running!"
    
    if not check_playit_installed():
        return False, "Playit is not installed. Run the installer first."
    
    PLAYIT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        cmd = ["screen", "-L", "-Logfile", str(PLAYIT_LOG_FILE), "-dmS", "playit", "bash", "-c", "playit; echo Playit exited; sleep 30"]
        env = os.environ.copy()
        env['TERM'] = 'xterm'
        subprocess.run(cmd, check=True, timeout=10, env=env)
        
        log_action('Playit Started')
        return True, "Playit tunnel started!"
    except Exception as e:
        return False, f"Failed to start Playit: {e}"


def strip_ansi(text):
    """Strip ANSI escape codes and TUI box characters from text"""
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    text = ansi_escape.sub('', text)
    text = re.sub(r'[\u2500-\u257F]', '', text)
    lines = [line.strip() for line in text.splitlines()]
    clean_lines = [l for l in lines if l and not l.isspace() and len(l) > 2]
    return '\n'.join(clean_lines)


def get_playit_claim_link():
    """Extract claim link from Playit logs"""
    if not PLAYIT_LOG_FILE.exists():
        return None
        
    try:
        with open(PLAYIT_LOG_FILE, 'r', errors='replace') as f:
            content = strip_ansi(f.read())
            match = re.search(r'https://playit\.gg/claim/[a-zA-Z0-9]+', content)
            if match:
                return match.group(0)
    except:
        pass
    return None


def get_playit_address_from_logs():
    """Extract assigned address from Playit logs"""
    if not PLAYIT_LOG_FILE.exists():
        return None
        
    try:
        with open(PLAYIT_LOG_FILE, 'r', errors='replace') as f:
            content = strip_ansi(f.read())
            match = re.search(r'([a-zA-Z0-9-]+\.(?:gl\.joinmc\.link|playit\.gg)) =>', content)
            if match:
                address = match.group(1)
                current = get_setting('playit_address', '')
                if current != address:
                    set_setting('playit_address', address)
                return address
    except:
        pass
    return None


def stop_playit():
    """Stop Playit screen session"""
    if not check_playit_status():
        return False, "Playit is not running"
    
    try:
        env = os.environ.copy()
        env['TERM'] = 'xterm'
        subprocess.run(["screen", "-S", "playit", "-X", "quit"], check=True, timeout=5, env=env)
        time.sleep(1)
        log_action('Playit Stopped')
        return True, "Playit stopped!"
    except Exception as e:
        return False, f"Error stopping Playit: {e}"


def get_playit_logs(lines=50):
    """Get Playit logs"""
    if PLAYIT_LOG_FILE.exists():
        try:
            with open(PLAYIT_LOG_FILE, 'r', errors='replace') as f:
                content = ''.join(deque(f, maxlen=lines))
                return strip_ansi(content)
        except:
            pass
    return "No Playit logs yet. Service might be starting..."


def check_java_installed():
    """Check if Java is installed and return version info"""
    try:
        result = subprocess.run(['java', '-version'], capture_output=True, text=True)
        if result.returncode == 0:
            # Java version is printed to stderr
            version_output = result.stderr or result.stdout
            return True
        return False
    except:
        return False


def get_java_version():
    """Get detailed Java version information.
    
    Returns:
        dict: {installed: bool, version: str, major: int, recommended: bool, compatible: bool}
        
    Minecraft version requirements:
    - Java 21: Recommended (Minecraft 1.20.5+)
    - Java 17: Minimum (Minecraft 1.18+)
    - Java 8-16: Legacy versions only
    """
    result = {
        'installed': False,
        'version': None,
        'major': 0,
        'recommended': False,
        'compatible': False,
        'message': 'Java not found'
    }
    
    try:
        proc = subprocess.run(['java', '-version'], capture_output=True, text=True)
        if proc.returncode != 0:
            return result
        
        # Java version is printed to stderr
        output = proc.stderr or proc.stdout
        result['installed'] = True
        
        # Parse version string (e.g., "openjdk version "21.0.1"" or "java version "1.8.0_202"")
        import re
        version_match = re.search(r'version "([^"]+)"', output)
        if version_match:
            version_str = version_match.group(1)
            result['version'] = version_str
            
            # Parse major version
            if version_str.startswith('1.'):
                # Old format: 1.8.0_xxx -> major version 8
                major = int(version_str.split('.')[1])
            else:
                # New format: 21.0.1 -> major version 21
                major = int(version_str.split('.')[0])
            
            result['major'] = major
            result['recommended'] = major >= 21
            result['compatible'] = major >= 17
            
            if major >= 21:
                result['message'] = f'Java {major} (Recommended)'
            elif major >= 17:
                result['message'] = f'Java {major} (Compatible - Java 21 recommended)'
            else:
                result['message'] = f'Java {major} (Outdated - Java 17+ required for modern Minecraft)'
        
        return result
    except Exception as e:
        result['message'] = f'Error checking Java: {str(e)}'
        return result


def check_playit_installed():
    """Check if playit is installed"""
    try:
        result = subprocess.run(['which', 'playit'], capture_output=True, text=True)
        return result.returncode == 0
    except:
        return False


# ============ NGROK FUNCTIONS ============

def check_ngrok_installed():
    """Check if ngrok is available via pyngrok"""
    try:
        from pyngrok import ngrok
        return True
    except ImportError:
        return False


def setup_ngrok():
    """Setup ngrok tunnel with proper error handling"""
    global ngrok_url, ngrok_error, ngrok_status
    
    token = get_setting('ngrok_token', '')
    
    if not token:
        ngrok_status = "not_configured"
        ngrok_url = None
        ngrok_error = None
        return
    
    ngrok_status = "starting"
    ngrok_error = None
    
    try:
        from pyngrok import ngrok, conf
        from pyngrok.exception import PyngrokNgrokError
        
        # Kill existing tunnels first
        try:
            for tunnel in ngrok.get_tunnels():
                ngrok.disconnect(tunnel.public_url)
        except:
            pass
        
        # Set auth token
        conf.get_default().auth_token = token
        
        # Create tunnel
        tunnel = ngrok.connect(8080, "http")
        ngrok_url = tunnel.public_url
        ngrok_status = "active"
        ngrok_error = None
        print(f"[CursedMC] 🌐 Remote access: {ngrok_url}")
        log_action(f'Ngrok tunnel active: {ngrok_url}')
        
    except ImportError:
        ngrok_status = "error"
        ngrok_error = "Ngrok package (pyngrok) is not installed. Run: pip install pyngrok"
        ngrok_url = None
        print(f"[CursedMC] Ngrok error: {ngrok_error}")
        
    except Exception as e:
        ngrok_status = "error"
        error_str = str(e)
        
        if "authentication failed" in error_str.lower() or "invalid" in error_str.lower():
            ngrok_error = "Invalid ngrok auth token. Please check your token at dashboard.ngrok.com"
        elif "tunnel session" in error_str.lower():
            ngrok_error = "Ngrok session limit reached. Free accounts allow 1 tunnel at a time."
        elif "connection refused" in error_str.lower():
            ngrok_error = "Cannot connect to ngrok service. Check your internet connection."
        else:
            ngrok_error = f"Ngrok failed to start: {error_str}"
        
        ngrok_url = None
        print(f"[CursedMC] Ngrok error: {ngrok_error}")


def stop_ngrok():
    """Stop ngrok tunnel"""
    global ngrok_url, ngrok_status, ngrok_error
    
    try:
        from pyngrok import ngrok
        ngrok.kill()
    except:
        pass
    
    ngrok_url = None
    ngrok_status = "not_configured" if not get_setting('ngrok_token', '') else "stopped"
    ngrok_error = None


# ============ FILE MANAGER ============

def secure_path(path_str):
    """
    Resolve and secure a path to prevent directory traversal.
    Returns None if path is invalid or outside allowed directory.
    Uses strict validation to prevent path traversal attacks.
    """
    if not path_str:
        return SERVER_DIR.resolve()
    
    # Reject paths with dangerous patterns immediately
    dangerous_patterns = ['..', '\x00', '~', '$', '|', ';', '&', '`', '\n', '\r']
    for pattern in dangerous_patterns:
        if pattern in path_str:
            return None
    
    # Clean and normalize the path
    path_str = path_str.strip().strip('/\\').replace('\\', '/')
    
    # Reject absolute paths and paths starting with /
    if path_str.startswith('/') or (len(path_str) > 1 and path_str[1] == ':'):
        return None
    
    base_dir = SERVER_DIR.resolve()
    
    try:
        # Join and resolve the path
        requested_path = (base_dir / path_str).resolve()
        
        # Strict check: resolved path must start with base_dir
        try:
            requested_path.relative_to(base_dir)
            return requested_path
        except ValueError:
            return None
            
    except Exception:
        pass
    
    return None


def get_file_type(filename):
    """Get file type based on extension"""
    ext = Path(filename).suffix.lower()
    type_map = {
        '.txt': 'text', '.log': 'text', '.md': 'text',
        '.yml': 'yaml', '.yaml': 'yaml',
        '.json': 'json',
        '.properties': 'properties',
        '.jar': 'jar',
        '.zip': 'archive', '.tar': 'archive', '.gz': 'archive',
        '.png': 'image', '.jpg': 'image', '.jpeg': 'image', '.gif': 'image',
        '.dat': 'binary', '.mca': 'binary', '.nbt': 'binary'
    }
    return type_map.get(ext, 'file')


def is_text_file(filepath):
    """Check if file is likely a text file that can be edited"""
    text_extensions = {
        '.txt', '.log', '.md', '.yml', '.yaml', '.json', '.properties',
        '.cfg', '.conf', '.ini', '.toml', '.xml', '.sh', '.bat', '.cmd',
        '.java', '.js', '.py', '.lua', '.sk'  # Script files
    }
    return Path(filepath).suffix.lower() in text_extensions


# ============ MODS MANAGEMENT ============

def get_mods_list():
    """Get list of mods in the mods folder"""
    MODS_DIR.mkdir(parents=True, exist_ok=True)
    mods = []
    
    for item in MODS_DIR.iterdir():
        if item.is_file() and item.suffix.lower() == '.jar':
            # Check if disabled (has .disabled extension)
            is_disabled = item.name.endswith('.jar.disabled')
            actual_name = item.name.replace('.disabled', '') if is_disabled else item.name
            
            mods.append({
                'name': actual_name,
                'filename': item.name,
                'size': item.stat().st_size,
                'size_human': format_size(item.stat().st_size),
                'enabled': not is_disabled,
                'modified': datetime.fromtimestamp(item.stat().st_mtime).isoformat()
            })
        elif item.is_file() and item.suffix.lower() == '.disabled':
            # Handle .jar.disabled files
            mods.append({
                'name': item.stem,  # Remove .disabled
                'filename': item.name,
                'size': item.stat().st_size,
                'size_human': format_size(item.stat().st_size),
                'enabled': False,
                'modified': datetime.fromtimestamp(item.stat().st_mtime).isoformat()
            })
    
    return sorted(mods, key=lambda x: x['name'].lower())


def format_size(size_bytes):
    """Format bytes to human readable string"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def toggle_mod(filename, enable):
    """Enable or disable a mod"""
    MODS_DIR.mkdir(parents=True, exist_ok=True)
    
    if enable:
        # Enable: remove .disabled extension
        disabled_path = MODS_DIR / filename
        if disabled_path.exists() and filename.endswith('.disabled'):
            enabled_name = filename[:-9]  # Remove '.disabled'
            enabled_path = MODS_DIR / enabled_name
            disabled_path.rename(enabled_path)
            return True, f"Mod {enabled_name} enabled"
    else:
        # Disable: add .disabled extension
        enabled_path = MODS_DIR / filename
        if enabled_path.exists() and not filename.endswith('.disabled'):
            disabled_path = MODS_DIR / (filename + '.disabled')
            enabled_path.rename(disabled_path)
            return True, f"Mod {filename} disabled"
    
    return False, "Mod not found or already in requested state"


def delete_mod(filename):
    """Delete a mod file"""
    mod_path = MODS_DIR / filename
    if mod_path.exists() and mod_path.parent == MODS_DIR:
        mod_path.unlink()
        return True, f"Mod {filename} deleted"
    return False, "Mod not found"


def validate_mod_file(file):
    """Validate mod file with magic byte check for JAR files"""
    if not file.filename:
        return False, "No filename provided"
    
    if not file.filename.lower().endswith('.jar'):
        return False, "Only .jar files are allowed"
    
    # Check file size (max 100MB for a single mod)
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    
    if size > 300 * 1024 * 1024:
        return False, "Mod file too large (max 300MB)"
    
    if size < 100:
        return False, "File too small to be a valid mod"
    
    # Check JAR/ZIP magic bytes (PK\x03\x04)
    header = file.read(4)
    file.seek(0)
    
    if header[:2] != b'PK':
        return False, "File does not appear to be a valid JAR archive"
    
    return True, "Valid"


# ============ ROUTES ============

@app.route('/')
def index():
    """Main page - show setup wizard or dashboard"""
    if not is_setup_complete():
        return render_template('setup.html')
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    return render_template('dashboard.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page"""
    if not is_password_set():
        return redirect(url_for('setup'))
    
    if request.method == 'POST':
        ip = request.remote_addr
        allowed, msg = check_rate_limit(ip)
        
        if not allowed:
            return render_template('login.html', error=True, message=msg)
            
        password = request.form.get('password', '')
        if check_password(password):
            if ip in login_attempts:
                del login_attempts[ip]
                
            session.permanent = True
            session['logged_in'] = True
            return redirect(url_for('index'))
            
        record_failed_attempt(ip)
        return render_template('login.html', error=True, message="Invalid password")
    return render_template('login.html')


@app.route('/logout')
def logout():
    """Logout"""
    session.clear()
    return redirect(url_for('login'))


@app.route('/setup')
def setup():
    """Force show setup page"""
    return render_template('setup.html')


@app.route('/dashboard')
@login_required
def dashboard():
    """Force show dashboard"""
    return render_template('dashboard.html')


# ============ API ROUTES ============

@app.route('/api/setup/status')
def api_setup_status():
    """Check what's installed and system requirements"""
    java_info = get_java_version()
    return jsonify({
        "java_installed": java_info['installed'],
        "java_version": java_info['version'],
        "java_major": java_info['major'],
        "java_compatible": java_info['compatible'],
        "java_recommended": java_info['recommended'],
        "java_message": java_info['message'],
        "playit_installed": check_playit_installed(),
        "playit_running": check_playit_status(),
        "setup_complete": is_setup_complete(),
        "password_set": is_password_set()
    })


@app.route('/api/setup/complete', methods=['POST'])
def api_complete_setup():
    """Complete the setup wizard"""
    data = request.json
    
    # Password is REQUIRED
    password = data.get('password', '').strip()
    if not password:
        return jsonify({"success": False, "error": "Password is required"}), 400
    
    if len(password) < 6:
        return jsonify({"success": False, "error": "Password must be at least 6 characters"}), 400
    
    # Set password with secure hash
    set_setting('password_hash', hash_password(password))
    
    # Set other settings
    set_setting('setup_complete', 'true')
    set_setting('server_name', data.get('server_name', 'My Server'))
    set_setting('server_type', data.get('server_type', 'paper'))
    set_setting('server_version', data.get('version', '1.21.4'))
    _, normalized_ram = normalize_ram_setting(data.get('ram', '4G'))
    set_setting('server_ram', normalized_ram)
    set_setting('playit_address', data.get('playit_address', ''))
    set_setting('ngrok_token', data.get('ngrok_token', ''))
    
    # Forge-specific settings
    if data.get('forge_version'):
        set_setting('forge_version', data.get('forge_version'))
    
    log_action('Setup Completed')
    
    # Setup ngrok if token provided
    if data.get('ngrok_token'):
        threading.Thread(target=setup_ngrok, daemon=True).start()
    
    return jsonify({"success": True})


@app.route('/api/versions/<server_type>')
def api_get_versions(server_type):
    """Get available versions for a server type"""
    if server_type == 'vanilla':
        versions = fetch_vanilla_versions()
    elif server_type == 'paper':
        versions = fetch_paper_versions()
    elif server_type == 'forge':
        versions = fetch_forge_versions()
    else:
        return jsonify({"error": "Unknown server type"}), 400
    
    return jsonify({"versions": versions})


@app.route('/api/versions/paper/<version>/builds')
def api_get_paper_builds(version):
    """Get available builds for a Paper version"""
    builds = fetch_paper_builds(version)
    return jsonify({"builds": builds})


@app.route('/api/versions/forge/<mc_version>/builds')
def api_get_forge_builds(mc_version):
    """Get available Forge builds for a specific Minecraft version"""
    builds = get_forge_versions_for_mc(mc_version)
    return jsonify({"builds": builds, "mc_version": mc_version})


@app.route('/api/download-jar', methods=['POST'])
@login_required
def api_download_jar():
    """Download server JAR"""
    data = request.json or {}
    server_type = data.get('server_type') or get_setting('server_type', 'paper')
    version = data.get('version') or get_setting('server_version', '1.21.4')
    build = data.get('build')
    forge_version = data.get('forge_version')  # For Forge servers
    
    success, message = download_server_jar(server_type, version, build, forge_version)
    if success:
        set_setting('server_type', server_type)
        set_setting('server_version', version)
        log_action(f'Downloaded {server_type} {version}')
    return jsonify({"success": success, "message": message})


@app.route('/api/status')
@login_required
def api_status():
    """Get full status"""
    settings = get_all_settings()
    jar_exists = (SERVER_DIR / "server.jar").exists()
    
    return jsonify({
        "server": {
            "online": is_server_running(),
            "name": settings.get('server_name', 'Server'),
            "type": settings.get('server_type', 'paper'),
            "version": settings.get('server_version', '1.21.4'),
            "address": settings.get('playit_address', ''),
            "ram": settings.get('server_ram', '4G'),
            "jar_exists": jar_exists,
            "auto_restart": settings.get('auto_restart', 'false') == 'true'
        },
        "playit": {
            "installed": check_playit_installed(),
            "running": check_playit_status()
        },
        "system": get_system_stats(),
        "ngrok": {
            "url": ngrok_url,
            "status": ngrok_status,
            "error": ngrok_error
        }
    })


@app.route('/api/start', methods=['POST'])
@login_required
def api_start():
    """Start the server"""
    success, message = start_minecraft()
    return jsonify({"success": success, "message": message})


@app.route('/api/stop', methods=['POST'])
@login_required
def api_stop():
    """Stop the server"""
    success, message = stop_minecraft()
    return jsonify({"success": success, "message": message})


@app.route('/api/console', methods=['POST'])
@login_required
def api_console():
    """Send console command"""
    data = request.json
    command = data.get('command', '').strip()
    if not command:
        return jsonify({"success": False, "message": "No command provided"})
    success, message = send_console_command(command)
    return jsonify({"success": success, "message": message})


@app.route('/api/logs')
@login_required
def api_logs():
    """Get server logs"""
    return jsonify({"logs": get_logs()})


@app.route('/api/playit/start', methods=['POST'])
@login_required
def api_playit_start():
    """Start Playit tunnel"""
    success, message = start_playit()
    return jsonify({"success": success, "message": message})


@app.route('/api/playit/stop', methods=['POST'])
@login_required
def api_playit_stop():
    """Stop Playit tunnel"""
    success, message = stop_playit()
    return jsonify({"success": success, "message": message})


@app.route('/api/playit/status')
@login_required
def api_playit_status():
    """Get Playit status"""
    if check_playit_status():
        get_playit_address_from_logs()
        
    return jsonify({
        "installed": check_playit_installed(),
        "running": check_playit_status(),
        "address": get_setting('playit_address', '')
    })


@app.route('/api/playit/logs')
@login_required
def api_playit_logs():
    """Get Playit logs"""
    return jsonify({"logs": get_playit_logs()})


@app.route('/api/playit/claim-link')
@login_required
def api_playit_claim_link():
    """Get Playit claim link if available"""
    link = get_playit_claim_link()
    return jsonify({"link": link})


@app.route('/api/history')
@login_required
def api_history():
    """Get action history"""
    return jsonify({"history": get_history()})


@app.route('/api/performance')
@login_required
def api_performance():
    """Get performance history for charts"""
    return jsonify({"data": get_performance_history()})


@app.route('/api/properties', methods=['GET', 'POST'])
@login_required
def api_properties():
    """Get or update server.properties"""
    if request.method == 'GET':
        return jsonify(read_server_properties())
    else:
        data = request.json
        write_server_properties(data)
        log_action('Server properties updated')
        return jsonify({"success": True})


@app.route('/api/settings', methods=['GET', 'POST'])
@login_required
def api_settings():
    """Get or update settings"""
    if request.method == 'GET':
        settings = get_all_settings()
        # Remove sensitive data
        if 'password_hash' in settings:
            settings['password_set'] = True
            del settings['password_hash']
        return jsonify(settings)
    else:
        data = request.json
        
        if 'server_name' in data:
            set_setting('server_name', data['server_name'])
        if 'server_type' in data:
            set_setting('server_type', data['server_type'])
        if 'server_version' in data:
            set_setting('server_version', data['server_version'])
        if 'server_ram' in data:
            _, normalized_ram = normalize_ram_setting(data['server_ram'])
            set_setting('server_ram', normalized_ram)
        if 'playit_address' in data:
            set_setting('playit_address', data['playit_address'])
        if 'ngrok_token' in data and data['ngrok_token'] != '***configured***':
            set_setting('ngrok_token', data['ngrok_token'])
            threading.Thread(target=setup_ngrok, daemon=True).start()
        if 'auto_restart' in data:
            set_setting('auto_restart', 'true' if data['auto_restart'] else 'false')
        if 'auto_playit' in data:
            set_setting('auto_playit', 'true' if data['auto_playit'] else 'false')
        if 'password' in data and data['password']:
            set_setting('password_hash', hash_password(data['password']))
        # Forge-specific settings
        if 'forge_version' in data:
            set_setting('forge_version', data['forge_version'])
        
        log_action('Settings Updated')
        return jsonify({"success": True})


@app.route('/api/ngrok/status')
@login_required
def api_ngrok_status():
    """Get ngrok tunnel status"""
    return jsonify({
        "url": ngrok_url,
        "status": ngrok_status,
        "error": ngrok_error,
        "configured": bool(get_setting('ngrok_token', ''))
    })


@app.route('/api/ngrok/restart', methods=['POST'])
@login_required
def api_ngrok_restart():
    """Restart ngrok tunnel"""
    stop_ngrok()
    time.sleep(1)
    threading.Thread(target=setup_ngrok, daemon=True).start()
    return jsonify({"success": True, "message": "Ngrok restart initiated"})


@app.route('/api/reset', methods=['POST'])
@login_required
def api_reset():
    """Reset to setup wizard"""
    set_setting('setup_complete', 'false')
    log_action('Reset to Setup')
    return jsonify({"success": True})


# ============ FILE BROWSER API ============

@app.route('/api/files/list', methods=['GET'])
@login_required
def api_files_list():
    """List files and directories"""
    relative_path = request.args.get('path', '')
    
    try:
        current_path = secure_path(relative_path)
        if not current_path or not current_path.exists():
            return jsonify({"error": "Invalid path"}), 400
        
        if not current_path.is_dir():
            return jsonify({"error": "Not a directory"}), 400
        
        items = []
        for item in sorted(os.scandir(current_path), key=lambda e: (e.is_file(), e.name.lower())):
            try:
                stat_info = item.stat()
                items.append({
                    "name": item.name,
                    "path": os.path.join(relative_path, item.name).replace("\\", "/").strip('/'),
                    "is_dir": item.is_dir(),
                    "size": stat_info.st_size if item.is_file() else None,
                    "size_human": format_size(stat_info.st_size) if item.is_file() else None,
                    "type": get_file_type(item.name),
                    "editable": is_text_file(item.name) if item.is_file() else False,
                    "modified": datetime.fromtimestamp(stat_info.st_mtime).isoformat()
                })
            except (PermissionError, OSError):
                continue
            
        return jsonify({
            "path": relative_path.replace("\\", "/").strip('/'),
            "items": items
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/files/get', methods=['GET'])
@login_required
def api_files_get():
    """Get content of a file"""
    relative_path = request.args.get('path', '')
    if not relative_path:
        return jsonify({"error": "Path is required"}), 400

    file_path = secure_path(relative_path)
    if not file_path or not file_path.is_file():
        return jsonify({"error": "File not found or not a file"}), 404

    try:
        content = file_path.read_text(encoding='utf-8', errors='replace')
        return jsonify({
            "path": relative_path,
            "content": content,
            "size": file_path.stat().st_size,
            "editable": is_text_file(file_path.name)
        })
    except Exception as e:
        return jsonify({"error": f"Failed to read file: {e}"}), 500


@app.route('/api/files/save', methods=['POST'])
@login_required
def api_files_save():
    """Save content to a file"""
    data = request.json
    relative_path = data.get('path')
    content = data.get('content')

    if not relative_path:
        return jsonify({"error": "Path is required"}), 400

    file_path = secure_path(relative_path)
    if not file_path:
        return jsonify({"error": "Invalid or insecure path"}), 400
        
    if file_path.is_dir():
        return jsonify({"error": "Cannot write to a directory"}), 400

    try:
        # Create parent directory if it doesn't exist
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding='utf-8')
        log_action(f'File Edited: {relative_path}')
        return jsonify({"success": True, "message": "File saved!"})
    except Exception as e:
        return jsonify({"error": f"Failed to save file: {e}"}), 500


@app.route('/api/files/create', methods=['POST'])
@login_required
def api_files_create():
    """Create a new file or folder"""
    data = request.json
    parent_path = data.get('path', '')
    name = data.get('name', '')
    is_folder = data.get('is_folder', False)
    
    if not name:
        return jsonify({"error": "Name is required"}), 400
    
    # Sanitize name
    name = secure_filename(name)
    if not name:
        return jsonify({"error": "Invalid name"}), 400
    
    parent = secure_path(parent_path)
    if not parent or not parent.is_dir():
        return jsonify({"error": "Invalid parent directory"}), 400
    
    new_path = parent / name
    
    # Verify new path is still within allowed directory
    if not (SERVER_DIR.resolve() in new_path.resolve().parents or SERVER_DIR.resolve() == new_path.resolve().parent):
        return jsonify({"error": "Invalid path"}), 400
    
    try:
        if is_folder:
            new_path.mkdir(parents=True, exist_ok=True)
            log_action(f'Folder Created: {name}')
            return jsonify({"success": True, "message": f"Folder '{name}' created!"})
        else:
            new_path.touch()
            log_action(f'File Created: {name}')
            return jsonify({"success": True, "message": f"File '{name}' created!"})
    except Exception as e:
        return jsonify({"error": f"Failed to create: {e}"}), 500


@app.route('/api/files/delete', methods=['POST'])
@login_required
def api_files_delete():
    """Delete a file or folder"""
    data = request.json
    relative_path = data.get('path')
    
    if not relative_path:
        return jsonify({"error": "Path is required"}), 400
    
    target_path = secure_path(relative_path)
    if not target_path or not target_path.exists():
        return jsonify({"error": "File or folder not found"}), 404
    
    # Don't allow deleting the server directory itself
    if target_path.resolve() == SERVER_DIR.resolve():
        return jsonify({"error": "Cannot delete root server directory"}), 400
    
    try:
        if target_path.is_dir():
            shutil.rmtree(target_path)
            log_action(f'Folder Deleted: {relative_path}')
        else:
            target_path.unlink()
            log_action(f'File Deleted: {relative_path}')
        return jsonify({"success": True, "message": "Deleted successfully!"})
    except Exception as e:
        return jsonify({"error": f"Failed to delete: {e}"}), 500


@app.route('/api/files/rename', methods=['POST'])
@login_required
def api_files_rename():
    """Rename a file or folder"""
    data = request.json
    relative_path = data.get('path')
    new_name = data.get('new_name')
    
    if not relative_path or not new_name:
        return jsonify({"error": "Path and new name are required"}), 400
    
    new_name = secure_filename(new_name)
    if not new_name:
        return jsonify({"error": "Invalid new name"}), 400
    
    target_path = secure_path(relative_path)
    if not target_path or not target_path.exists():
        return jsonify({"error": "File or folder not found"}), 404
    
    new_path = target_path.parent / new_name
    
    try:
        target_path.rename(new_path)
        log_action(f'Renamed: {target_path.name} -> {new_name}')
        return jsonify({"success": True, "message": f"Renamed to '{new_name}'!"})
    except Exception as e:
        return jsonify({"error": f"Failed to rename: {e}"}), 500


@app.route('/api/files/upload', methods=['POST'])
@login_required
def api_files_upload():
    """Upload files"""
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400
    
    files = request.files.getlist('file')
    upload_path = request.form.get('path', '')
    
    target_dir = secure_path(upload_path)
    if not target_dir or not target_dir.is_dir():
        return jsonify({"error": "Invalid upload directory"}), 400
    
    uploaded = []
    errors = []
    
    for file in files:
        if file.filename:
            filename = secure_filename(file.filename)
            if filename:
                try:
                    file_path = target_dir / filename
                    file.save(file_path)
                    uploaded.append(filename)
                except Exception as e:
                    errors.append(f"{filename}: {str(e)}")
    
    if uploaded:
        log_action(f'Files Uploaded: {", ".join(uploaded)}')
    
    return jsonify({
        "success": len(uploaded) > 0,
        "uploaded": uploaded,
        "errors": errors,
        "message": f"Uploaded {len(uploaded)} file(s)" + (f", {len(errors)} failed" if errors else "")
    })


@app.route('/api/files/download')
@login_required
def api_files_download():
    """Download a file"""
    relative_path = request.args.get('path', '')
    
    if not relative_path:
        return jsonify({"error": "Path is required"}), 400
    
    file_path = secure_path(relative_path)
    if not file_path or not file_path.is_file():
        return jsonify({"error": "File not found"}), 404
    
    try:
        return send_file(
            file_path,
            as_attachment=True,
            download_name=file_path.name
        )
    except Exception as e:
        return jsonify({"error": f"Failed to download: {e}"}), 500


# ============ MODS API ============

@app.route('/api/mods/list')
@login_required
def api_mods_list():
    """Get list of mods"""
    return jsonify({"mods": get_mods_list()})


@app.route('/api/mods/toggle', methods=['POST'])
@login_required
def api_mods_toggle():
    """Enable or disable a mod"""
    data = request.json
    filename = data.get('filename')
    enable = data.get('enable', True)
    
    if not filename:
        return jsonify({"error": "Filename is required"}), 400
    
    success, message = toggle_mod(filename, enable)
    if success:
        log_action(f'Mod {"enabled" if enable else "disabled"}: {filename}')
    return jsonify({"success": success, "message": message})


@app.route('/api/mods/delete', methods=['POST'])
@login_required
def api_mods_delete():
    """Delete a mod"""
    data = request.json
    filename = data.get('filename')
    
    if not filename:
        return jsonify({"error": "Filename is required"}), 400
    
    success, message = delete_mod(filename)
    if success:
        log_action(f'Mod deleted: {filename}')
    return jsonify({"success": success, "message": message})


@app.route('/api/mods/upload', methods=['POST'])
@login_required
def api_mods_upload():
    """Upload a mod"""
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400
    
    file = request.files['file']
    
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400
    
    # Validate mod file
    valid, message = validate_mod_file(file)
    if not valid:
        return jsonify({"error": message}), 400
    
    MODS_DIR.mkdir(parents=True, exist_ok=True)
    filename = secure_filename(file.filename)
    
    try:
        file_path = MODS_DIR / filename
        file.save(file_path)
        log_action(f'Mod uploaded: {filename}')
        return jsonify({"success": True, "message": f"Mod '{filename}' uploaded successfully!"})
    except Exception as e:
        return jsonify({"error": f"Upload failed: {e}"}), 500


# ============ WEBSOCKET ============

@socketio.on('connect')
def handle_connect():
    """Handle WebSocket connection"""
    emit('connected', {'status': 'ok'})


def background_status_updates():
    """Send status updates via WebSocket"""
    last_perf_save = 0
    last_playit_check = 0
    playit_running = False
    while True:
        try:
            now = time.time()
            stats = get_system_stats()
            if now - last_playit_check >= PLAYIT_STATUS_CACHE_SEC:
                playit_running = check_playit_status()
                last_playit_check = now
            status = {
                "server_online": is_server_running(),
                "playit_running": playit_running,
                "system": stats,
                "ngrok": {
                    "url": ngrok_url,
                    "status": ngrok_status,
                    "error": ngrok_error
                }
            }
            socketio.emit('status_update', status)
            
            if now - last_perf_save >= PERF_SAMPLE_INTERVAL_SEC:
                save_performance_data(stats['cpu'], stats['ram_percent'])
                last_perf_save = now
        except:
            pass
        time.sleep(STATUS_EMIT_INTERVAL_SEC)


# ============ MAIN ============

if __name__ == "__main__":
    # Ensure directories exist
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    SERVER_DIR.mkdir(parents=True, exist_ok=True)
    MODS_DIR.mkdir(parents=True, exist_ok=True)
    (BASE_DIR / "logs").mkdir(parents=True, exist_ok=True)
    (BASE_DIR / "backups").mkdir(parents=True, exist_ok=True)
    
    # Initialize database
    init_db()
    
    # Setup ngrok if configured
    setup_ngrok()
    
    # Start background status updates
    status_thread = threading.Thread(target=background_status_updates, daemon=True)
    status_thread.start()
    
    print("[CursedMC] 💀 Dashboard starting on http://0.0.0.0:8080")
    print("[CursedMC] © 2026 CursedMC. All rights reserved.")
    socketio.run(app, host='0.0.0.0', port=8080, debug=False, allow_unsafe_werkzeug=True)
