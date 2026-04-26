from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, WebDriverException
import threading
import time
import json
import os
import hashlib
import secrets
import gc
import psutil
from datetime import datetime, timedelta
from typing import Dict, List
import signal
import sys
import logging

# ============================================================================
# CONFIGURATION
# ============================================================================

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

USERS_FILE = 'users.json'
MONITORS_DIR = 'monitors_data'
MEMORY_CLEANUP_INTERVAL = 3600  # 1 hour

# Create directories
os.makedirs(MONITORS_DIR, exist_ok=True)

# Configure logging - SILENT for Render logs
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Global variables
user_sessions = {}
monitor_threads = {}
threads_running = True

# Chromium paths for Render
CHROMIUM_PATHS = [
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser', 
    '/usr/bin/google-chrome',
    '/usr/bin/chrome'
]

# ============================================================================
# MEMORY MANAGEMENT
# ============================================================================

def get_memory_usage():
    """Get current memory usage in MB"""
    process = psutil.Process(os.getpid())
    return process.memory_info().rss / 1024 / 1024

def cleanup_memory():
    """Force garbage collection and memory cleanup"""
    gc.collect()
    if hasattr(gc, 'collect'):
        gc.collect(2)
    logger.info(f"Memory cleanup completed. Current usage: {get_memory_usage():.1f} MB")

def start_memory_cleanup_thread():
    """Start background thread for periodic memory cleanup"""
    def cleanup_loop():
        while threads_running:
            time.sleep(MEMORY_CLEANUP_INTERVAL)
            cleanup_memory()
    
    thread = threading.Thread(target=cleanup_loop, daemon=True)
    thread.start()
    logger.info("Memory cleanup thread started")

# ============================================================================
# USER MANAGEMENT
# ============================================================================

def load_users():
    """Load users from JSON file"""
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_users(users):
    """Save users to JSON file"""
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)

def hash_password(password):
    """Hash password using SHA-256"""
    return hashlib.sha256(password.encode()).hexdigest()

def load_user_monitors(username):
    """Load monitors for specific user"""
    user_file = os.path.join(MONITORS_DIR, f"{username}.json")
    if os.path.exists(user_file):
        try:
            with open(user_file, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_user_monitors(username, monitors):
    """Save monitors for specific user"""
    user_file = os.path.join(MONITORS_DIR, f"{username}.json")
    with open(user_file, 'w') as f:
        json.dump(monitors, f, indent=2)

def get_uptime_string(created_at_str):
    """Convert created_at timestamp to 'X days X hours X minutes' format"""
    try:
        created_at = datetime.fromisoformat(created_at_str)
        now = datetime.now()
        diff = now - created_at
        
        days = diff.days
        hours = diff.seconds // 3600
        minutes = (diff.seconds % 3600) // 60
        
        if days > 0:
            return f"{days}d {hours}h {minutes}m"
        elif hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m"
    except:
        return "0m"

# ============================================================================
# SELENIUM BROWSER FUNCTION - WITH SLEEP DETECTION
# ============================================================================

def get_chromium_path():
    """Find chromium/chrome binary path"""
    for path in CHROMIUM_PATHS:
        if os.path.exists(path):
            return path
    return None

def open_url_in_browser(url: str, timeout_seconds: int = 30) -> dict:
    """
    Open URL and check if Streamlit is sleeping or awake
    """
    result = {
        'success': False,
        'title': '',
        'error': '',
        'status': 'unknown',
        'timestamp': datetime.now().isoformat()
    }
    
    options = Options()
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1024,768')
    options.add_argument('--disable-images')
    options.add_argument('--disable-extensions')
    options.add_argument('--disable-plugins')
    
    # Set timeout
    options.add_argument(f'--page-load-timeout={timeout_seconds * 1000}')
    
    chromium_path = get_chromium_path()
    if chromium_path:
        options.binary_location = chromium_path
    
    driver = None
    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(timeout_seconds)
        
        driver.get(url)
        time.sleep(5)  # Wait for page to render
        
        # Check page source for sleep message
        page_source = driver.page_source.lower()
        
        if "gone to sleep" in page_source or "has gone to sleep" in page_source:
            result['status'] = 'sleeping'
            result['success'] = True
            result['title'] = "😴 SLEEPING - App was asleep"
        else:
            result['status'] = 'awake'
            result['success'] = True
            result['title'] = "🟢 AWAKE - App is running"
        
    except TimeoutException:
        result['error'] = f"Timeout after {timeout_seconds}s"
        result['status'] = 'timeout'
        result['success'] = False
    except Exception as e:
        result['error'] = f"Error: {str(e)[:80]}"
        result['status'] = 'error'
        result['success'] = False
    finally:
        if driver:
            driver.quit()
            driver = None
    
    return result

# ============================================================================
# MONITOR THREAD
# ============================================================================

def schedule_monitor_task(username: str, monitor_id: str, name: str, url: str, 
                          interval_minutes: int, timeout_seconds: int = 30):
    """
    Background task for each monitor
    """
    logger.info(f"Monitor '{name}' started for user {username}")
    
    while threads_running:
        try:
            # Check if monitor still exists
            user_monitors = load_user_monitors(username)
            if monitor_id not in user_monitors:
                break
            
            monitor = user_monitors[monitor_id]
            if not monitor.get('enabled', True):
                time.sleep(60)
                continue
            
            # Update status to checking
            monitor['status'] = 'checking'
            monitor['last_check'] = datetime.now().isoformat()
            save_user_monitors(username, user_monitors)
            
            # Open URL in browser
            result = open_url_in_browser(url, timeout_seconds)
            
            # Update monitor status
            if result['success']:
                if result['status'] == 'awake':
                    monitor['status'] = 'online'
                elif result['status'] == 'sleeping':
                    monitor['status'] = 'online'  # After wake up, it becomes online
                else:
                    monitor['status'] = 'online'
                
                monitor['last_success'] = result['timestamp']
                monitor['last_title'] = result['title']
                monitor['error'] = ''
            else:
                monitor['status'] = 'offline'
                monitor['last_error'] = result['error']
                monitor['error'] = result['error']
                monitor['failures'] = monitor.get('failures', 0) + 1
            
            monitor['last_check'] = result['timestamp']
            save_user_monitors(username, user_monitors)
            
            # Wait for next interval
            interval_seconds = interval_minutes * 60
            for _ in range(interval_seconds):
                if not threads_running or monitor_id not in load_user_monitors(username):
                    break
                time.sleep(1)
                
        except Exception as e:
            logger.error(f"Error in monitor '{name}': {e}")
            time.sleep(60)

def start_user_monitors(username):
    """Start all monitor threads for a user"""
    monitors = load_user_monitors(username)
    
    if username not in monitor_threads:
        monitor_threads[username] = {}
    
    for monitor_id, monitor in monitors.items():
        if monitor.get('enabled', True) and monitor_id not in monitor_threads[username]:
            thread = threading.Thread(
                target=schedule_monitor_task,
                args=(username, monitor_id, monitor['name'], monitor['url'], 
                      monitor['interval_minutes'], monitor.get('timeout', 30)),
                daemon=True
            )
            thread.start()
            monitor_threads[username][monitor_id] = thread
            logger.info(f"Started monitor thread for user {username}: {monitor['name']}")

# ============================================================================
# FLASK ROUTES
# ============================================================================

@app.route('/')
def index():
    """Login page or dashboard"""
    if 'username' not in session:
        return render_template('login.html')
    return render_template('dashboard.html', username=session['username'])

@app.route('/login', methods=['POST'])
def login():
    """User login"""
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    
    users = load_users()
    
    if username in users and users[username]['password'] == hash_password(password):
        session['username'] = username
        session.permanent = True
        
        start_user_monitors(username)
        
        return jsonify({'success': True})
    
    # Create new user if doesn't exist
    if username not in users and len(username) >= 3 and len(password) >= 3:
        users[username] = {
            'password': hash_password(password),
            'created_at': datetime.now().isoformat()
        }
        save_users(users)
        session['username'] = username
        
        save_user_monitors(username, {})
        
        return jsonify({'success': True, 'new': True})
    
    return jsonify({'success': False, 'error': 'Invalid credentials'})

@app.route('/logout')
def logout():
    """User logout"""
    session.pop('username', None)
    return redirect(url_for('index'))

@app.route('/api/monitors', methods=['GET'])
def api_get_monitors():
    """Get all monitors for logged-in user"""
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    monitors = load_user_monitors(session['username'])
    
    # Add uptime to each monitor
    for monitor_id, monitor in monitors.items():
        monitor['uptime_display'] = get_uptime_string(monitor.get('created_at', datetime.now().isoformat()))
    
    return jsonify(monitors)

@app.route('/api/monitor/add', methods=['POST'])
def api_add_monitor():
    """Add a new monitor for logged-in user"""
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    data = request.json
    username = session['username']
    monitors = load_user_monitors(username)
    
    import uuid
    monitor_id = str(uuid.uuid4())[:8]
    
    current_time = datetime.now().isoformat()
    
    monitors[monitor_id] = {
        'id': monitor_id,
        'name': data.get('name', 'Untitled'),
        'url': data.get('url', ''),
        'interval_minutes': int(data.get('interval_minutes', 60)),
        'timeout': int(data.get('timeout', 30)),
        'enabled': True,
        'status': 'pending',
        'created_at': current_time,
        'last_check': '',
        'last_success': '',
        'last_title': '',
        'error': '',
        'failures': 0,
        'uptime': 0
    }
    
    save_user_monitors(username, monitors)
    
    # Start monitor thread
    monitor = monitors[monitor_id]
    thread = threading.Thread(
        target=schedule_monitor_task,
        args=(username, monitor_id, monitor['name'], monitor['url'], 
              monitor['interval_minutes'], monitor['timeout']),
        daemon=True
    )
    thread.start()
    
    if username not in monitor_threads:
        monitor_threads[username] = {}
    monitor_threads[username][monitor_id] = thread
    
    return jsonify({'success': True, 'monitor_id': monitor_id})

@app.route('/api/monitor/delete/<monitor_id>', methods=['DELETE'])
def api_delete_monitor(monitor_id):
    """Delete a monitor"""
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    username = session['username']
    monitors = load_user_monitors(username)
    
    if monitor_id in monitors:
        del monitors[monitor_id]
        save_user_monitors(username, monitors)
        
        if username in monitor_threads and monitor_id in monitor_threads[username]:
            del monitor_threads[username][monitor_id]
        
        return jsonify({'success': True})
    
    return jsonify({'success': False, 'error': 'Monitor not found'}), 404

@app.route('/api/monitor/toggle/<monitor_id>', methods=['POST'])
def api_toggle_monitor(monitor_id):
    """Enable/disable a monitor"""
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    username = session['username']
    monitors = load_user_monitors(username)
    
    if monitor_id in monitors:
        monitors[monitor_id]['enabled'] = not monitors[monitor_id].get('enabled', True)
        save_user_monitors(username, monitors)
        return jsonify({'success': True, 'enabled': monitors[monitor_id]['enabled']})
    
    return jsonify({'success': False, 'error': 'Monitor not found'}), 404

@app.route('/api/monitor/check/<monitor_id>', methods=['POST'])
def api_check_now(monitor_id):
    """Manually check a monitor immediately - FIXED"""
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    username = session['username']
    monitors = load_user_monitors(username)
    
    if monitor_id not in monitors:
        return jsonify({'success': False, 'error': 'Monitor not found'}), 404
    
    monitor = monitors[monitor_id]
    
    # Update status to checking immediately
    monitor['status'] = 'checking'
    monitor['last_check'] = datetime.now().isoformat()
    save_user_monitors(username, monitors)
    
    def manual_check():
        try:
            result = open_url_in_browser(monitor['url'], monitor.get('timeout', 30))
            
            # Reload monitors to get latest
            current_monitors = load_user_monitors(username)
            if monitor_id not in current_monitors:
                return
            
            current_monitor = current_monitors[monitor_id]
            
            if result['success']:
                if result['status'] == 'awake':
                    current_monitor['status'] = 'online'
                elif result['status'] == 'sleeping':
                    current_monitor['status'] = 'online'
                else:
                    current_monitor['status'] = 'online'
                
                current_monitor['last_success'] = result['timestamp']
                current_monitor['last_title'] = result['title']
                current_monitor['error'] = ''
            else:
                current_monitor['status'] = 'offline'
                current_monitor['error'] = result['error']
                current_monitor['last_error'] = result['error']
                current_monitor['failures'] = current_monitor.get('failures', 0) + 1
            
            current_monitor['last_check'] = result['timestamp']
            save_user_monitors(username, current_monitors)
            
        except Exception as e:
            current_monitors = load_user_monitors(username)
            if monitor_id in current_monitors:
                current_monitors[monitor_id]['status'] = 'offline'
                current_monitors[monitor_id]['error'] = str(e)[:80]
                save_user_monitors(username, current_monitors)
    
    thread = threading.Thread(target=manual_check, daemon=True)
    thread.start()
    
    return jsonify({'success': True, 'message': 'Check started'})

@app.route('/api/stats', methods=['GET'])
def api_stats():
    """Get dashboard statistics"""
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    monitors = load_user_monitors(session['username'])
    
    total = len(monitors)
    online = sum(1 for m in monitors.values() if m.get('status') == 'online')
    offline = sum(1 for m in monitors.values() if m.get('status') == 'offline')
    checking = sum(1 for m in monitors.values() if m.get('status') == 'checking')
    
    return jsonify({
        'total': total,
        'online': online,
        'offline': offline,
        'checking': checking,
        'memory_usage': f"{get_memory_usage():.1f}"
    })

@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.now().isoformat(),
        'memory_mb': get_memory_usage()
    })

def shutdown_signal_handler(signum, frame):
    """Handle shutdown signals"""
    global threads_running
    logger.info("Received shutdown signal, stopping...")
    threads_running = False
    cleanup_memory()
    sys.exit(0)

# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    signal.signal(signal.SIGINT, shutdown_signal_handler)
    signal.signal(signal.SIGTERM, shutdown_signal_handler)
    
    start_memory_cleanup_thread()
    
    port = int(os.environ.get('PORT', 5000))
    
    logger.info("=" * 50)
    logger.info(f"🚀 Monitor Dashboard Started")
    logger.info(f"💾 Initial Memory: {get_memory_usage():.1f} MB")
    logger.info(f"🌐 Port: {port}")
    logger.info("=" * 50)
    
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
