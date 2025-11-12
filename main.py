
import requests
from datetime import datetime, timedelta
import pytz
import csv
import os
import json
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request
import json
import tkinter as tk
from tkinter import messagebox, ttk
from tkcalendar import DateEntry

# Terminal color helpers
def color(text, code):
    return f"\033[{code}m{text}\033[0m"

def green(text):
    return color(text, '32')

def red(text):
    return color(text, '31')

def yellow(text):
    return color(text, '33')

def cyan(text):
    return color(text, '36')

# --- Конфігурація (замість .env) ---
def read_env_key(key, default=None):
    """Read a single key from a local .env file (simple parser). If not found, fall back to environment variables."""
    try:
        with open('.env', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    k, v = line.split('=', 1)
                    if k.strip() == key:
                        return v.strip()
    except Exception:
        pass
    return os.environ.get(key, default)

# Load personal/config values from .env or environment (keeps secrets out of the code)
SDUI_USER_ID = read_env_key('SDUI_USER_ID') or "557035"
SDUI_AUTH_TOKEN = read_env_key('SDUI_AUTH_TOKEN') or ""
TIMEZONE = read_env_key('TIMEZONE') or 'Europe/Berlin'

# --- Google Calendar config ---
def get_calendar_id():
    """Return calendar id from .env or environment variable GOOGLE_CALENDAR_ID."""
    return read_env_key('GOOGLE_CALENDAR_ID')

SCOPES = ['https://www.googleapis.com/auth/calendar']
TOKEN_FILE = 'token.json'
CREDENTIALS_FILE = 'credentials.json'

def get_google_credentials():
    creds = None
    # Try token from .env first (TOKEN_JSON). If present, load credentials from that JSON blob.
    token_json = read_env_key('TOKEN_JSON')
    if token_json:
        try:
            creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
        except Exception:
            creds = None
    elif os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # Try credentials config from .env (CREDENTIALS_JSON) then fall back to file
            cred_json = read_env_key('CREDENTIALS_JSON')
            if cred_json:
                cred_data = json.loads(cred_json)
            else:
                with open(CREDENTIALS_FILE, encoding='utf-8') as cred_file:
                    cred_data = json.load(cred_file)
            from google_auth_oauthlib.flow import InstalledAppFlow
            # use client config (dict) to avoid requiring a file on disk
            flow = InstalledAppFlow.from_client_config(cred_data, SCOPES)
            creds = flow.run_local_server(port=0)
        # If user stores token in .env (TOKEN_JSON), do not overwrite files. Otherwise persist token to TOKEN_FILE.
        if not read_env_key('TOKEN_JSON'):
            with open(TOKEN_FILE, 'w') as token:
                token.write(creds.to_json())
    return creds


def clear_events_gui(start_date, end_date):
    calendar_id = get_calendar_id()
    if not calendar_id:
        messagebox.showerror("Error", 'Не знайдено GOOGLE_CALENDAR_ID у .env')
        return
    creds = get_google_credentials()
    service = build('calendar', 'v3', credentials=creds)
    # Make end_date inclusive
    end_date = end_date.replace(hour=23, minute=59, second=59)
    tz = pytz.timezone(TIMEZONE)
    time_min = tz.localize(start_date).isoformat()
    time_max = tz.localize(end_date).isoformat()
    try:
        deleted_count = 0
        page_token = None
        all_events = []
        while True:
            events_result = service.events().list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy='startTime',
                pageToken=page_token
            ).execute()
            all_events.extend(events_result.get('items', []))
            page_token = events_result.get('nextPageToken')
            if not page_token:
                break
        if not all_events:
            messagebox.showinfo("Info", "Подій у вказаному діапазоні немає.")
            return
        confirm = messagebox.askyesno("Confirm", f"Знайдено {len(all_events)} подій у діапазоні. Ви впевнені, що хочете видалити всі ці події?")
        if not confirm:
            return
        for event in all_events:
            start = event.get('start', {})
            start_dt_str = start.get('dateTime') or start.get('date')
            if not start_dt_str:
                continue
            try:
                if 'dateTime' in start:
                    event_dt = datetime.fromisoformat(start_dt_str)
                else:
                    event_dt = datetime.strptime(start_dt_str, '%Y-%m-%d')
                event_dt = event_dt.astimezone(tz) if event_dt.tzinfo else tz.localize(event_dt)
            except Exception as e:
                continue
            # Inclusive range check
            if tz.localize(start_date) <= event_dt <= tz.localize(end_date):
                try:
                    service.events().delete(
                        calendarId=calendar_id,
                        eventId=event['id']
                    ).execute()
                    deleted_count += 1
                except HttpError as error:
                    pass
        messagebox.showinfo("Success", f"Видалення завершено. Видалено {deleted_count} подій.")
    except HttpError as error:
        messagebox.showerror("Error", f"Помилка при отриманні подій: {error}")

def insert_events_to_gcal_gui(events, date_str, progress_callback=None):
    calendar_id = get_calendar_id()
    if not calendar_id:
        messagebox.showerror("Error", 'Не знайдено GOOGLE_CALENDAR_ID у .env')
        return
    creds = get_google_credentials()
    service = build('calendar', 'v3', credentials=creds)
    count = 0
    for row in events:
        event = {
            'summary': row['Назва'],
            'description': row['Опис'],
            'location': row['Аудиторія'],
            'start': {
                'dateTime': datetime.strptime(row['Початок'], '%Y-%m-%d %H:%M').astimezone(pytz.timezone(TIMEZONE)).isoformat(),
                'timeZone': TIMEZONE
            },
            'end': {
                'dateTime': datetime.strptime(row['Кінець'], '%Y-%m-%d %H:%M').astimezone(pytz.timezone(TIMEZONE)).isoformat(),
                'timeZone': TIMEZONE
            },
        }
        try:
            service.events().insert(calendarId=calendar_id, body=event).execute()
            count += 1
            if progress_callback:
                progress_callback(count)
        except Exception as e:
            pass
    messagebox.showinfo("Success", f"Додано {count} подій у Google Calendar ({calendar_id})")
ALLOWED_NAMES = []

# --- Отримання даних SDUI ---
def get_sdui_data_with_token(url, auth_token):
    headers = {'Authorization': auth_token, 'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Помилка підключення до SDUI: {e}")
        return None

# --- Перетворення SDUI у CSV-рядки ---
def transform_sdui_to_csv_rows(sdui_data):
    rows = []
    lessons = sdui_data.get('data', {}).get('lessons', [])

    # Mapping for event types
    oftype_map = {
        "": "",
        "CANCLED": "Entfall: ",
        "BOOKABLE_CHANGE": "Raum Geändert: ",
        "ADDITIONAL": "Info: ",
        "SUBSTITUTION": "Vertretung: ",
        "EVENT": "EVENT: ",
        "HOLIDAY": "Ferien: "
    }

    for sdui_event in lessons:
        if not sdui_event:
            continue

        # Defensive extraction
        course = sdui_event.get('course') or {}
        meta = course.get('meta') or {}
        subject_name_full = meta.get('displayname', 'Невідоме заняття').strip()

        # Improved subject name extraction (split only on first underscore)
        parts = subject_name_full.split('_', 1)
        original_name = parts[0] if len(parts) == 1 else parts[0] + '_' + parts[1].split('_')[0]
        if original_name in ALLOWED_NAMES:
            continue

        # Use the part after the first underscore, or the whole name if none
        subject_name = parts[1] if len(parts) > 1 else parts[0]


        # Event type prefix and status in summary
        oftype = sdui_event.get('oftype', '')
        prefix = oftype_map.get(oftype, f"{oftype}: ")
        # Always include subject name and event type in summary
        summary_parts = []
        if prefix.strip():
            summary_parts.append(prefix.strip())
        if subject_name.strip():
            summary_parts.append(subject_name.strip())
        else:
            summary_parts.append('Без назви')
        summary = ' '.join(summary_parts)

        begins_at = sdui_event.get('begins_at')
        ends_at = sdui_event.get('ends_at')
        if not begins_at or not ends_at:
            print(f"Warning: Missing time for event {subject_name_full}")
            continue

        local_timezone = pytz.timezone(TIMEZONE)
        start_datetime = local_timezone.localize(datetime.fromtimestamp(begins_at))
        end_datetime = local_timezone.localize(datetime.fromtimestamp(ends_at))

        # Multiple bookables/rooms
        bookables = sdui_event.get('bookables', [])
        room_info = ', '.join([b.get('name', 'Невідомо') for b in bookables]) if bookables else 'Невідомо'

        # Multiple teachers
        teachers = sdui_event.get('teachers', [])
        teacher_info = ', '.join([t.get('name', 'Невідомо') for t in teachers]) if teachers else 'Невідомо'

        # Add notes if present
        notes = sdui_event.get('notes', '')
        description = f"Вчитель: {teacher_info} | Аудиторія: {room_info}"
        if notes:
            description += f" | Примітка: {notes}"

        rows.append({
            'Назва': summary,
            'Вчитель': teacher_info,
            'Аудиторія': room_info,
            'Початок': start_datetime.strftime('%Y-%m-%d %H:%M'),
            'Кінець': end_datetime.strftime('%Y-%m-%d %H:%M'),
            'Опис': description
        })

    return rows

# --- GUI Functions ---
def add_sdui_events_gui(start_date, end_date, progress_var, progress_label):
    current_date = start_date
    all_rows = []
    total_days = (end_date - start_date).days + 1
    progress_var.set(0)
    progress_label.config(text="Fetching SDUI data...")
    root.update_idletasks()

    for i, current_date in enumerate(range((end_date - start_date).days + 1)):
        date = start_date + timedelta(days=i)
        date_str = date.strftime("%Y-%m-%d")
        url = f"https://api.sdui.app/v1/timetables/users/{SDUI_USER_ID}/timetable?begins_at={date_str}&ends_at={date_str}"
        sdui_data = get_sdui_data_with_token(url, SDUI_AUTH_TOKEN)
        if sdui_data:
            rows = transform_sdui_to_csv_rows(sdui_data)
            if rows:
                all_rows.extend(rows)
        progress_var.set((i + 1) / total_days * 50)  # First 50% for fetching
        progress_label.config(text=f"Fetching data for {date_str}...")
        root.update_idletasks()

    if not all_rows:
        messagebox.showinfo("Info", "Не знайдено подій після фільтрації.")
        return

    progress_label.config(text="Adding events to Google Calendar...")
    root.update_idletasks()

    def progress_callback(count):
        progress_var.set(50 + (count / len(all_rows)) * 50)  # Next 50% for adding
        progress_label.config(text=f"Added {count}/{len(all_rows)} events...")
        root.update_idletasks()

    insert_events_to_gcal_gui(all_rows, f"{start_date.strftime('%Y-%m-%d')} - {end_date.strftime('%Y-%m-%d')}", progress_callback)

def create_gui():
    global root
    root = tk.Tk()
    root.title("SDUI Calendar Tool")
    root.geometry("400x300")

    ttk.Label(root, text="SDUI Calendar Tool", font=("Arial", 16)).pack(pady=10)

    def on_add_events():
        dialog = tk.Toplevel(root)
        dialog.title("Add SDUI Events")
        dialog.geometry("300x250")

        ttk.Label(dialog, text="Start Date:").pack(pady=5)
        start_date_entry = DateEntry(dialog, width=12, background='darkblue', foreground='white', borderwidth=2)
        start_date_entry.pack(pady=5)

        ttk.Label(dialog, text="End Date:").pack(pady=5)
        end_date_entry = DateEntry(dialog, width=12, background='darkblue', foreground='white', borderwidth=2)
        end_date_entry.pack(pady=5)

        progress_var = tk.DoubleVar()
        progress_bar = ttk.Progressbar(dialog, variable=progress_var, maximum=100)
        progress_bar.pack(pady=10, fill=tk.X, padx=20)

        progress_label = ttk.Label(dialog, text="")
        progress_label.pack(pady=5)

        def start_add():
            start_date = start_date_entry.get_date()
            end_date = end_date_entry.get_date()
            if end_date < start_date:
                messagebox.showerror("Error", "End date must be after start date")
                return
            add_sdui_events_gui(start_date, end_date, progress_var, progress_label)

        ttk.Button(dialog, text="Add Events", command=start_add).pack(pady=10)

    def on_clear_events():
        dialog = tk.Toplevel(root)
        dialog.title("Clear Events")
        dialog.geometry("300x200")

        ttk.Label(dialog, text="Start Date:").pack(pady=5)
        start_date_entry = DateEntry(dialog, width=12, background='darkblue', foreground='white', borderwidth=2)
        start_date_entry.pack(pady=5)

        ttk.Label(dialog, text="End Date:").pack(pady=5)
        end_date_entry = DateEntry(dialog, width=12, background='darkblue', foreground='white', borderwidth=2)
        end_date_entry.pack(pady=5)

        def start_clear():
            start_date = start_date_entry.get_date()
            end_date = end_date_entry.get_date()
            if end_date < start_date:
                messagebox.showerror("Error", "End date must be after start date")
                return
            clear_events_gui(start_date, end_date)

        ttk.Button(dialog, text="Clear Events", command=start_clear).pack(pady=10)

    ttk.Button(root, text="Add SDUI Events to Google Calendar", command=on_add_events).pack(pady=10)
    ttk.Button(root, text="Clear Events in Google Calendar", command=on_clear_events).pack(pady=10)

    root.mainloop()

# --- Основна функція ---
def main():
    create_gui()

if __name__ == '__main__':
    main()
