import os
import time
import random
import threading
import requests
from datetime import datetime, timedelta
import pytz
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

app = Flask(__name__)
app.secret_key = 'supersecretkey'

# --- GLOBALS ---
LOG_BUFFER = []
ABORT_FLAG = False
IS_RUNNING = False

# Global Config Variables
SDUI_USER_ID = None
SDUI_AUTH_TOKEN = None
TIMEZONE = None
GOOGLE_CALENDAR_ID = None

def log_msg(message):
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = f"[{timestamp}] {message}"
    print(entry)
    LOG_BUFFER.append(entry)
    if len(LOG_BUFFER) > 500:
        LOG_BUFFER.pop(0)

# --- CONFIG MANAGEMENT ---
def read_env_key(key, default=None):
    try:
        if os.path.exists('.env'):
            with open('.env', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'): continue
                    if '=' in line:
                        k, v = line.split('=', 1)
                        if k.strip() == key: return v.strip().strip('"').strip("'")
    except: pass
    return os.environ.get(key, default)

def update_env_file(updates):
    """Updates multiple keys in the .env file."""
    lines = []
    if os.path.exists('.env'):
        with open('.env', 'r') as f:
            lines = f.readlines()
    
    # Process updates
    for key, value in updates.items():
        found = False
        new_lines = []
        for line in lines:
            if line.strip().startswith(f"{key}="):
                new_lines.append(f"{key}='{value}'\n")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"{key}='{value}'\n")
        lines = new_lines

    with open('.env', 'w') as f:
        f.writelines(lines)

def load_config():
    """Reloads global config variables from .env"""
    global SDUI_USER_ID, SDUI_AUTH_TOKEN, TIMEZONE, GOOGLE_CALENDAR_ID
    SDUI_USER_ID = read_env_key('SDUI_USER_ID')
    SDUI_AUTH_TOKEN = read_env_key('SDUI_AUTH_TOKEN')
    TIMEZONE = read_env_key('TIMEZONE', 'Europe/Berlin')
    GOOGLE_CALENDAR_ID = read_env_key('GOOGLE_CALENDAR_ID', 'primary')

# Initialize Config
load_config()

SCOPES = ['https://www.googleapis.com/auth/calendar']
TOKEN_FILE = 'auth/token.json'
CREDENTIALS_FILE = 'auth/credentials.json'

# --- HELPERS ---
def get_calendar_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        try: creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except: pass
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try: creds.refresh(Request())
            except: creds = None

        if not creds:
            if not os.path.exists(CREDENTIALS_FILE):
                log_msg(f"ERROR: {CREDENTIALS_FILE} missing.")
                return None
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        
        if not os.path.exists('auth'): os.makedirs('auth')
        with open(TOKEN_FILE, 'w') as token: token.write(creds.to_json())
            
    return build('calendar', 'v3', credentials=creds)

def get_sdui_data(start_date, end_date):
    if not SDUI_AUTH_TOKEN or not SDUI_USER_ID: 
        log_msg("Error: Missing .env variables.")
        return None
    
    log_msg(f"Fetching SDUI: {start_date} -> {end_date}")
    headers = {'Authorization': f'Bearer {SDUI_AUTH_TOKEN}', 'User-Agent': 'Mozilla/5.0'}
    begins_at = start_date.strftime("%Y-%m-%d")
    ends_at = end_date.strftime("%Y-%m-%d")
    url = f"https://api.sdui.app/v1/timetables/users/{SDUI_USER_ID}/timetable?begins_at={begins_at}&ends_at={ends_at}"
    
    try:
        r = requests.get(url, headers=headers)
        r.raise_for_status()
        return r.json()
    except Exception as e: 
        log_msg(f"Network Error: {e}")
        return None

def process_sdui_data(sdui_data):
    events = []
    if not sdui_data or 'data' not in sdui_data: return events
    lessons = sdui_data.get('data', {}).get('lessons', [])
    tz = pytz.timezone(TIMEZONE)
    
    oftype_map = {"CANCLED": "❌ Cancelled: ", "BOOKABLE_CHANGE": "⚠️ Room: ", "SUBSTITUTION": "🔄 Sub: ", "EXAM": "📝 Exam: "}
    COLOR_EXAM = '11' # Red
    COLOR_HOLIDAY = '10' # Green
    COLOR_CHANGE = '6' # Orange
    COLOR_EVENT = '3' # Purple
    COLOR_DEFAULT = '9' # Blue

    for lesson in lessons:
        kind = lesson.get('kind')
        oftype = lesson.get('oftype')
        color_id = COLOR_DEFAULT

        if kind in ['HOLIDAY', 'EVENT']:
            meta = lesson.get('meta') or {}
            subject = meta.get('displayname') or lesson.get('comment') or "Event"
            summary = f"🏖️ {subject}" if kind == 'HOLIDAY' else f"📅 {subject}"
            color_id = COLOR_HOLIDAY if kind == 'HOLIDAY' else COLOR_EVENT
            description = f"Type: {kind}\nComment: {lesson.get('comment', '')}"
            location = ""
        else:
            course = lesson.get('course') or {}
            subject = (course.get('meta') or {}).get('displayname', 'Unknown').split('_')[-1]
            summary = f"{oftype_map.get(oftype, '')}{subject}"
            
            if oftype == 'EXAM': color_id = COLOR_EXAM
            elif oftype in ['SUBSTITUTION', 'BOOKABLE_CHANGE']: color_id = COLOR_CHANGE
            elif oftype == 'CANCLED': color_id = '8'
            
            rooms = [b['name'] for b in (lesson.get('bookables') or []) if 'name' in b]
            teachers = [t['name'] for t in (lesson.get('teachers') or []) if 'name' in t]
            location = ", ".join(rooms)
            description = f"Teacher: {', '.join(teachers)}\nType: {kind or oftype}"

        ts_start, ts_end = lesson.get('begins_at'), lesson.get('ends_at')
        if not ts_start or not ts_end: continue
        
        events.append({
            'summary': summary,
            'start': datetime.fromtimestamp(ts_start, tz).isoformat(),
            'end': datetime.fromtimestamp(ts_end, tz).isoformat(),
            'location': location,
            'description': description,
            'colorId': color_id
        })
    return events

# --- WORKERS ---
def worker_sync(start, end):
    global IS_RUNNING, ABORT_FLAG
    IS_RUNNING = True
    ABORT_FLAG = False
    
    log_msg("--- STARTING SYNC (Background) ---")
    data = get_sdui_data(start, end)
    
    if not data:
        log_msg("Failed to get data.")
        IS_RUNNING = False
        return

    events = process_sdui_data(data)
    if not events:
        log_msg("No events found.")
        IS_RUNNING = False
        return

    service = get_calendar_service()
    if not service:
        log_msg("Auth failed.")
        IS_RUNNING = False
        return

    count = 0
    total = len(events)
    log_msg(f"Queue: {total} events.")

    for i, event in enumerate(events):
        if ABORT_FLAG:
            log_msg("!!! STOPPED BY USER !!!")
            break

        body = {
            'summary': event['summary'],
            'location': event['location'],
            'description': event['description'],
            'start': {'dateTime': event['start'], 'timeZone': TIMEZONE},
            'end': {'dateTime': event['end'], 'timeZone': TIMEZONE},
            'colorId': event['colorId']
        }

        # RETRY LOOP FOR RATE LIMITS
        max_retries = 8
        for attempt in range(max_retries):
            if ABORT_FLAG: break
            try:
                service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=body).execute()
                count += 1
                log_msg(f"[{i+1}/{total}] Uploaded: {event['summary']}")
                break 
            except HttpError as e:
                if e.resp.status == 403 and 'usageLimits' in str(e):
                    wait = (2 ** attempt) + random.random()
                    log_msg(f"Rate Limit Hit! Pausing {wait:.1f}s...")
                    time.sleep(wait)
                else:
                    log_msg(f"Error on item {i}: {e}")
                    break 

    log_msg(f"--- FINISHED. Imported {count} events. ---")
    IS_RUNNING = False

def worker_clear(start, end):
    global IS_RUNNING, ABORT_FLAG
    IS_RUNNING = True
    ABORT_FLAG = False
    
    log_msg("--- STARTING DELETE (Background) ---")
    service = get_calendar_service()
    tz = pytz.timezone(TIMEZONE)
    start_dt = tz.localize(datetime.combine(start, datetime.min.time()))
    end_dt = tz.localize(datetime.combine(end, datetime.max.time()))
    
    total_deleted = 0
    
    for pass_num in range(1, 6):
        if ABORT_FLAG: break
        log_msg(f"Pass {pass_num}: Scanning for events...")
        
        try:
            events_result = service.events().list(
                calendarId=GOOGLE_CALENDAR_ID, timeMin=start_dt.isoformat(),
                timeMax=end_dt.isoformat(), singleEvents=True, maxResults=250
            ).execute()
        except: break
        
        events = events_result.get('items', [])
        if not events:
            log_msg("Clean.")
            break
            
        log_msg(f"Found {len(events)} events to delete.")
        batch_del = 0
        
        for event in events:
            if ABORT_FLAG: break
            try:
                service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event['id']).execute()
                batch_del += 1
                total_deleted += 1
                if total_deleted % 10 == 0: log_msg(f"Deleted {total_deleted}...")
            except HttpError as e:
                if e.resp.status == 403:
                    log_msg("Rate Limit (Delete). Waiting 2s...")
                    time.sleep(2)
                elif '404' not in str(e) and '410' not in str(e):
                    log_msg(f"Del Error: {e}")

        if batch_del == 0: break
    
    if ABORT_FLAG: log_msg("!!! STOPPED BY USER !!!")
    else: log_msg(f"--- FINISHED. Deleted {total_deleted} events. ---")
    IS_RUNNING = False

# --- ROUTES ---
@app.route('/')
def index():
    if 'year' not in session:
        # Load year from ENV if available, else default to current
        env_year = read_env_key('SYNC_YEAR')
        session['year'] = int(env_year) if env_year else datetime.now().year

    config = {
        'sdui_id': SDUI_USER_ID or '',
        'sdui_token': SDUI_AUTH_TOKEN or '',
        'cal_id': GOOGLE_CALENDAR_ID or '',
        'year': session['year']
    }
    return render_template('index.html', year=session['year'], config=config)

@app.route('/update_settings', methods=['POST'])
def update_settings():
    try:
        # Update Session
        year_input = request.form.get('year')
        if year_input and year_input.isdigit():
            session['year'] = int(year_input)
            
        new_updates = {
            'SDUI_USER_ID': request.form.get('sdui_id').strip(),
            'SDUI_AUTH_TOKEN': request.form.get('sdui_token').strip(),
            'GOOGLE_CALENDAR_ID': request.form.get('cal_id').strip() or 'primary',
            'SYNC_YEAR': year_input
        }
        update_env_file(new_updates)
        load_config() # Refresh runtime globals
        flash("Settings Saved & Reloaded", "success")
    except Exception as e:
        flash(f"Error saving settings: {e}", "danger")
    return redirect(url_for('index'))

@app.route('/logs')
def stream_logs():
    return jsonify({'logs': LOG_BUFFER, 'running': IS_RUNNING})

@app.route('/stop', methods=['POST'])
def stop_process():
    global ABORT_FLAG
    if IS_RUNNING:
        ABORT_FLAG = True
        log_msg(">>> STOP SIGNAL RECEIVED <<<")
        return jsonify({'status': 'stopping'})
    return jsonify({'status': 'not_running'})

@app.route('/clear_logs', methods=['POST'])
def clear_logs_route():
    LOG_BUFFER.clear()
    return jsonify({"status": "cleared"})

@app.route('/set_year', methods=['POST'])
def set_year():
    try:
        session['year'] = int(request.form['year'])
        flash(f"Year set to {session['year']}", "success")
    except: flash("Invalid Year", "danger")
    return redirect(url_for('index'))

@app.route('/sync/today')
def sync_today():
    if IS_RUNNING:
        flash("Process already running! Stop it first.", "danger")
        return redirect(url_for('index'))
    tz = pytz.timezone(TIMEZONE)
    today = datetime.now(tz).date()
    threading.Thread(target=worker_sync, args=(today, today)).start()
    flash("Started Sync (Check Terminal)", "info")
    return redirect(url_for('index'))

@app.route('/sync/week', methods=['POST'])
def sync_week():
    if IS_RUNNING:
        flash("Process already running!", "danger")
        return redirect(url_for('index'))
    try:
        year = int(session.get('year', datetime.now().year))
        start_w = int(request.form['start_week'])
        end_w = int(request.form.get('end_week') or start_w)
        if start_w > end_w: raise ValueError("Invalid Range")
        
        start = datetime.fromisocalendar(year, start_w, 1).date()
        end = datetime.fromisocalendar(year, end_w, 7).date()
        
        threading.Thread(target=worker_sync, args=(start, end)).start()
        flash(f"Started Sync: Week {start_w}-{end_w}", "info")
    except: flash("Input Error", "danger")
    return redirect(url_for('index'))

@app.route('/sync/custom', methods=['POST'])
def sync_custom():
    if IS_RUNNING:
        flash("Process already running!", "danger")
        return redirect(url_for('index'))
    try:
        start = datetime.strptime(request.form['start'], "%Y-%m-%d").date()
        end = datetime.strptime(request.form['end'], "%Y-%m-%d").date()
        threading.Thread(target=worker_sync, args=(start, end)).start()
        flash("Started Custom Sync", "info")
    except: flash("Input Error", "danger")
    return redirect(url_for('index'))

@app.route('/clear/weeks', methods=['POST'])
def clear_weeks():
    if IS_RUNNING:
        flash("Process already running!", "danger")
        return redirect(url_for('index'))
    try:
        year = int(session.get('year', datetime.now().year))
        start_w = int(request.form['start_week'])
        end_w = int(request.form.get('end_week') or start_w)
        
        start = datetime.fromisocalendar(year, start_w, 1).date()
        end = datetime.fromisocalendar(year, end_w, 7).date()
        
        threading.Thread(target=worker_clear, args=(start, end)).start()
        flash("Started Deletion Process", "info")
    except: flash("Input Error", "danger")
    return redirect(url_for('index'))

if __name__ == '__main__':
    log_msg("Server Ready. Background workers enabled.")
    app.run(debug=True, port=5000, threaded=True)