import requests
from datetime import datetime, timedelta
import pytz
import os
import json
import sys
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

# --- Configuration Helper ---
def read_env_key(key, default=None):
    """Read a key from .env file or environment variables."""
    try:
        if os.path.exists('.env'):
            with open('.env', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' in line:
                        k, v = line.split('=', 1)
                        if k.strip() == key:
                            return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return os.environ.get(key, default)

# Load Configuration
SDUI_USER_ID = read_env_key('SDUI_USER_ID')
SDUI_AUTH_TOKEN = read_env_key('SDUI_AUTH_TOKEN')
TIMEZONE = read_env_key('TIMEZONE', 'Europe/Berlin')
GOOGLE_CALENDAR_ID = read_env_key('GOOGLE_CALENDAR_ID', 'primary')

# --- AUTO-FIX: Sanity Check for Calendar ID ---
# If the user accidentally pasted the JSON credentials into the ID field, revert to primary.
if GOOGLE_CALENDAR_ID and (len(GOOGLE_CALENDAR_ID) > 80 or '{' in GOOGLE_CALENDAR_ID):
    print("! Warning: GOOGLE_CALENDAR_ID in .env looks incorrect (too long or contains JSON).")
    print("! Auto-correcting to 'primary'.")
    GOOGLE_CALENDAR_ID = 'primary'

# --- Google Calendar Config ---
SCOPES = ['https://www.googleapis.com/auth/calendar']
TOKEN_FILE = 'token.json'
CREDENTIALS_FILE = 'credentials.json'

def get_google_credentials():
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except Exception:
            print("! Token file invalid, re-authenticating...")
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                print("! Refresh failed. Please re-login.")
                creds = None
        
        if not creds:
            if not os.path.exists(CREDENTIALS_FILE):
                print(f"ERROR: {CREDENTIALS_FILE} not found. Please upload it.")
                return None
            
            print("... Starting Google Authentication")
            # Use run_local_server for modern authentication
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0, open_browser=False)

        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
            
    return creds

def get_calendar_service():
    print("... Connecting to Google Calendar")
    creds = get_google_credentials()
    if creds:
        return build('calendar', 'v3', credentials=creds)
    return None

# --- SDUI Data Fetching ---
def get_sdui_data(start_date, end_date):
    if not SDUI_AUTH_TOKEN or not SDUI_USER_ID:
        print("\nERROR: SDUI_USER_ID or SDUI_AUTH_TOKEN is missing in .env")
        return None
    
    print(f"... Fetching SDUI data from {start_date} to {end_date}")
    
    headers = {
        'Authorization': f'Bearer {SDUI_AUTH_TOKEN}', 
        'User-Agent': 'Mozilla/5.0'
    }
    
    begins_at = start_date.strftime("%Y-%m-%d")
    ends_at = end_date.strftime("%Y-%m-%d")
    
    url = f"https://api.sdui.app/v1/timetables/users/{SDUI_USER_ID}/timetable?begins_at={begins_at}&ends_at={ends_at}"
    
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        if response.status_code == 401:
             print("\nERROR: Authentication Failed (401). Token expired.")
        else:
            print(f"\nERROR: Network Error: {e}")
        return None

def process_sdui_data(sdui_data):
    events = []
    if not sdui_data or 'data' not in sdui_data:
        return events
        
    lessons = sdui_data.get('data', {}).get('lessons', [])
    if not lessons:
        return events

    tz = pytz.timezone(TIMEZONE)

    oftype_map = {
        "CANCLED": "❌ Cancelled: ",
        "BOOKABLE_CHANGE": "⚠️ Room: ",
        "SUBSTITUTION": "🔄 Sub: ",
        "EXAM": "📝 Exam: ",
        "HOLIDAY": "🏖️ Holiday: "
    }

    for lesson in lessons:
        # Safe access using 'or {}'
        course = lesson.get('course') or {}
        meta = course.get('meta') or {}
        
        subject = meta.get('displayname', 'Unknown')
        if '_' in subject:
            subject = subject.split('_')[-1]

        kind = lesson.get('kind')
        oftype = lesson.get('oftype')
        prefix = oftype_map.get(oftype, "")
        
        summary = f"{prefix}{subject}"
        
        ts_start = lesson.get('begins_at')
        ts_end = lesson.get('ends_at')
        if not ts_start or not ts_end:
            continue
            
        dt_start = datetime.fromtimestamp(ts_start, tz)
        dt_end = datetime.fromtimestamp(ts_end, tz)
        
        bookables = lesson.get('bookables') or []
        teachers_list = lesson.get('teachers') or []
        
        rooms = [b['name'] for b in bookables if 'name' in b]
        teachers = [t['name'] for t in teachers_list if 'name' in t]
        
        location = ", ".join(rooms)
        description = f"Teacher: {', '.join(teachers)}\nType: {kind or oftype}"
        
        events.append({
            'summary': summary,
            'start': dt_start.isoformat(),
            'end': dt_end.isoformat(),
            'location': location,
            'description': description
        })
    return events

# --- CLI Helpers ---
def get_date_input(prompt):
    while True:
        date_str = input(prompt + " (YYYY-MM-DD): ").strip()
        try:
            return datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            print("Invalid format. Please use YYYY-MM-DD")

def print_progress(current, total):
    percent = float(current) / total
    bar_length = 20
    arrow = '-' * int(round(percent * bar_length) - 1) + '>'
    spaces = ' ' * (bar_length - len(arrow))
    sys.stdout.write(f"\rProgress: [{arrow + spaces}] {int(percent * 100)}%")
    sys.stdout.flush()

# --- Logic Core ---
def perform_sync(start, end):
    """Reusable logic to fetch and upload events."""
    try:
        data = get_sdui_data(start, end)
        if not data:
            return

        events = process_sdui_data(data)
        if not events:
            print("\nNo events found.")
            return

        print(f"\nFound {len(events)} events.")
        service = get_calendar_service()
        if not service:
            return

        print(f"Uploading to calendar: {GOOGLE_CALENDAR_ID}")
        count = 0
        for i, event in enumerate(events):
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
                # Catch 404s cleanly
                if '404' in str(e) and 'Not Found' in str(e):
                    print(f"\nCRITICAL ERROR: Calendar ID '{GOOGLE_CALENDAR_ID}' not found.")
                    print("Please check your .env file or credentials.")
                    return
                print(f"\nError on item {i}: {e}")
            
            print_progress(i + 1, len(events))
        
        print(f"\n\nDone! Imported {count} events.")
    except Exception as e:
        print(f"\nCRITICAL ERROR DURING SYNC: {e}")
        import traceback
        traceback.print_exc()

# --- Main Actions ---
def run_sync_week():
    print("\n--- SYNC SPECIFIC WEEK (2026) ---")
    try:
        week_input = input("Enter Week Number (1-53): ").strip()
        if not week_input.isdigit():
             print("Invalid input. Please enter a number.")
             return
             
        week_num = int(week_input)
        year = 2026
        
        if week_num < 1 or week_num > 53:
            print("Week number must be between 1 and 53.")
            return

        start_dt = datetime.fromisocalendar(year, week_num, 1)
        end_dt = datetime.fromisocalendar(year, week_num, 7)
        
        start = start_dt.date()
        end = end_dt.date()
        
        print(f"Syncing Week {week_num}, {year} ({start} to {end})...")
        perform_sync(start, end)
        
    except Exception as e:
        print(f"Error calculating dates: {e}")

def run_import_custom():
    print("\n--- CUSTOM RANGE SYNC ---")
    start = get_date_input("Start Date")
    end = get_date_input("End Date")
    
    if end < start:
        print("Error: End date is before start date.")
        return
        
    perform_sync(start, end)

def run_sync_today():
    tz = pytz.timezone(TIMEZONE)
    today = datetime.now(tz).date()
    print(f"\n--- SYNCING TODAY ({today}) ---")
    perform_sync(today, today)

def run_clear():
    print("\n--- DELETE EVENTS ---")
    start = get_date_input("Start Date")
    end = get_date_input("End Date")
    
    confirm = input(f"Are you sure you want to DELETE ALL events from {start} to {end}? (y/n): ")
    if confirm.lower() != 'y':
        return

    service = get_calendar_service()
    if not service:
        return

    tz = pytz.timezone(TIMEZONE)
    start_dt = tz.localize(datetime.combine(start, datetime.min.time()))
    end_dt = tz.localize(datetime.combine(end, datetime.max.time()))
    
    page_token = None
    deleted = 0
    
    print("Deleting...")
    while True:
        events_result = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=start_dt.isoformat(),
            timeMax=end_dt.isoformat(),
            singleEvents=True,
            pageToken=page_token
        ).execute()
        
        events = events_result.get('items', [])
        for event in events:
            try:
                service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event['id']).execute()
                deleted += 1
                sys.stdout.write(f"\rDeleted: {deleted}")
                sys.stdout.flush()
            except:
                pass
        
        page_token = events_result.get('nextPageToken')
        if not page_token:
            break
            
    print(f"\nDone. Deleted {deleted} events.")

def main_menu():
    print("\n=== SDUI to Google Calendar (CLI) ===")
    while True:
        print("\n1. Sync Today")
        print("2. Sync Week (2026)")
        print("3. Sync Custom Range")
        print("4. Clear Events")
        print("5. Exit")
        choice = input("Select option (1-5): ").strip()
        
        if choice == '1':
            run_sync_today()
        elif choice == '2':
            run_sync_week()
        elif choice == '3':
            run_import_custom()
        elif choice == '4':
            run_clear()
        elif choice == '5':
            print("Bye!")
            break
        else:
            print("Invalid option.")

if __name__ == "__main__":
    main_menu()