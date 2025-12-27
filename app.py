import os
import requests
from datetime import datetime, timedelta
import pytz
from flask import Flask, render_template, request, redirect, url_for, flash, session
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

app = Flask(__name__)
app.secret_key = 'supersecretkey'  # Required for flash messages

# --- Configuration ---
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

SDUI_USER_ID = read_env_key('SDUI_USER_ID')
SDUI_AUTH_TOKEN = read_env_key('SDUI_AUTH_TOKEN')
TIMEZONE = read_env_key('TIMEZONE', 'Europe/Berlin')
GOOGLE_CALENDAR_ID = read_env_key('GOOGLE_CALENDAR_ID', 'primary')

SCOPES = ['https://www.googleapis.com/auth/calendar']

# --- UPDATED PATHS ---
TOKEN_FILE = 'auth/token.json'
CREDENTIALS_FILE = 'auth/credentials.json'

# --- Logic Helpers ---
def get_calendar_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except: pass
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except: creds = None

        if not creds:
            # Note: This requires a browser on the server machine the first time
            if not os.path.exists(CREDENTIALS_FILE):
                print(f"Error: {CREDENTIALS_FILE} not found. Please put it in the 'auth' folder.")
                return None
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        
        # Ensure auth folder exists before writing
        if not os.path.exists('auth'):
            os.makedirs('auth')
            
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
            
    return build('calendar', 'v3', credentials=creds)

def get_sdui_data(start_date, end_date):
    if not SDUI_AUTH_TOKEN or not SDUI_USER_ID: return None
    
    headers = {'Authorization': f'Bearer {SDUI_AUTH_TOKEN}', 'User-Agent': 'Mozilla/5.0'}
    begins_at = start_date.strftime("%Y-%m-%d")
    ends_at = end_date.strftime("%Y-%m-%d")
    url = f"https://api.sdui.app/v1/timetables/users/{SDUI_USER_ID}/timetable?begins_at={begins_at}&ends_at={ends_at}"
    
    try:
        r = requests.get(url, headers=headers)
        r.raise_for_status()
        return r.json()
    except: return None

def process_sdui_data(sdui_data):
    events = []
    if not sdui_data or 'data' not in sdui_data: return events
    lessons = sdui_data.get('data', {}).get('lessons', [])
    tz = pytz.timezone(TIMEZONE)
    
    oftype_map = {
        "CANCLED": "❌ Cancelled: ", 
        "BOOKABLE_CHANGE": "⚠️ Room: ", 
        "SUBSTITUTION": "🔄 Sub: ", 
        "EXAM": "📝 Exam: "
    }

    for lesson in lessons:
        kind = lesson.get('kind')
        oftype = lesson.get('oftype')
        
        # --- Handle Holidays and Events (No Course Object) ---
        if kind in ['HOLIDAY', 'EVENT']:
            meta = lesson.get('meta') or {}
            subject = meta.get('displayname') or lesson.get('comment') or "Event"
            
            if kind == 'HOLIDAY':
                summary = f"🏖️ {subject}"
            else:
                summary = f"📅 {subject}"
            
            description = f"Type: {kind}\nComment: {lesson.get('comment', '')}"
            location = ""
            
        # --- Handle Standard Lessons ---
        else:
            course = lesson.get('course') or {}
            course_meta = course.get('meta') or {}
            subject = course_meta.get('displayname', 'Unknown')
            if '_' in subject: subject = subject.split('_')[-1]

            prefix = oftype_map.get(oftype, "")
            summary = f"{prefix}{subject}"
            
            bookables = lesson.get('bookables') or []
            teachers_list = lesson.get('teachers') or []
            
            rooms = [b['name'] for b in bookables if 'name' in b]
            teachers = [t['name'] for t in teachers_list if 'name' in t]
            
            location = ", ".join(rooms)
            description = f"Teacher: {', '.join(teachers)}\nType: {kind or oftype}"

        ts_start, ts_end = lesson.get('begins_at'), lesson.get('ends_at')
        if not ts_start or not ts_end: continue
        
        events.append({
            'summary': summary,
            'start': datetime.fromtimestamp(ts_start, tz).isoformat(),
            'end': datetime.fromtimestamp(ts_end, tz).isoformat(),
            'location': location,
            'description': description
        })
    return events

def perform_sync(start, end):
    data = get_sdui_data(start, end)
    if not data: return 0, "Failed to fetch SDUI data (Check .env)."
    
    events = process_sdui_data(data)
    if not events: return 0, "No events found in this range."
    
    service = get_calendar_service()
    if not service: return 0, "Google Calendar Auth Failed."

    count = 0
    for event in events:
        body = {
            'summary': event['summary'],
            'location': event['location'],
            'description': event['description'],
            'start': {'dateTime': event['start'], 'timeZone': TIMEZONE},
            'end': {'dateTime': event['end'], 'timeZone': TIMEZONE},
        }
        try:
            service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=body).execute()
            count += 1
        except Exception as e:
            print(f"Sync Error: {e}")
            
    return count, f"Successfully imported {count} events."

def perform_clear(start, end):
    service = get_calendar_service()
    if not service: return 0, "Google Auth Failed."
    
    tz = pytz.timezone(TIMEZONE)
    start_dt = tz.localize(datetime.combine(start, datetime.min.time()))
    end_dt = tz.localize(datetime.combine(end, datetime.max.time()))
    
    page_token = None
    deleted = 0
    while True:
        try:
            events_result = service.events().list(
                calendarId=GOOGLE_CALENDAR_ID, timeMin=start_dt.isoformat(),
                timeMax=end_dt.isoformat(), singleEvents=True, pageToken=page_token
            ).execute()
        except: break
        
        events = events_result.get('items', [])
        for event in events:
            try:
                service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event['id']).execute()
                deleted += 1
            except: pass
            
        page_token = events_result.get('nextPageToken')
        if not page_token: break
        
    return deleted, f"Deleted {deleted} events."

# --- Routes ---
@app.route('/')
def index():
    if 'year' not in session:
        session['year'] = datetime.now().year
    return render_template('index.html', year=session['year'])

@app.route('/set_year', methods=['POST'])
def set_year():
    try:
        session['year'] = int(request.form['year'])
        flash(f"Year set to {session['year']}", "success")
    except:
        flash("Invalid Year", "danger")
    return redirect(url_for('index'))

@app.route('/sync/today')
def sync_today():
    tz = pytz.timezone(TIMEZONE)
    today = datetime.now(tz).date()
    count, msg = perform_sync(today, today)
    flash(msg, "success" if count > 0 else "warning")
    return redirect(url_for('index'))

@app.route('/sync/week', methods=['POST'])
def sync_week():
    try:
        year = int(session.get('year', datetime.now().year))
        week = int(request.form['week'])
        start = datetime.fromisocalendar(year, week, 1).date()
        end = datetime.fromisocalendar(year, week, 7).date()
        count, msg = perform_sync(start, end)
        flash(f"Week {week}: {msg}", "success" if count > 0 else "warning")
    except Exception as e:
        flash(f"Error: {str(e)}", "danger")
    return redirect(url_for('index'))

@app.route('/sync/custom', methods=['POST'])
def sync_custom():
    try:
        start = datetime.strptime(request.form['start'], "%Y-%m-%d").date()
        end = datetime.strptime(request.form['end'], "%Y-%m-%d").date()
        count, msg = perform_sync(start, end)
        flash(msg, "success" if count > 0 else "warning")
    except Exception as e:
        flash(f"Error: {str(e)}", "danger")
    return redirect(url_for('index'))

@app.route('/clear/weeks', methods=['POST'])
def clear_weeks():
    try:
        year = int(session.get('year', datetime.now().year))
        start_w = int(request.form['start_week'])
        end_w_input = request.form.get('end_week')
        end_w = int(end_w_input) if end_w_input else start_w
        
        if start_w > end_w: raise ValueError("Start week cannot be after end week")

        start = datetime.fromisocalendar(year, start_w, 1).date()
        end = datetime.fromisocalendar(year, end_w, 7).date()
        
        count, msg = perform_clear(start, end)
        flash(msg, "info")
    except Exception as e:
        flash(f"Error: {str(e)}", "danger")
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True, port=5000)