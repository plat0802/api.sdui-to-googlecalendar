import os
from datetime import datetime, timedelta
import pytz
import json
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request

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

# Завантаження змінних оточення
load_dotenv()
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
TIMEZONE = os.getenv('TIMEZONE', 'Europe/Kyiv')
SCOPES = ['https://www.googleapis.com/auth/calendar']
TOKEN_FILE = 'token.json'
CREDENTIALS_FILE = 'credentials.json'

def get_google_calendar_service():
    creds = None
    # Try token from .env first (TOKEN_JSON). If present, load credentials from that JSON blob.
    token_json = os.getenv('TOKEN_JSON')
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
            cred_json = os.getenv('CREDENTIALS_JSON')
            if cred_json:
                cred_data = json.loads(cred_json)
                flow = InstalledAppFlow.from_client_config(cred_data, SCOPES)
            else:
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        # persist token to file unless user stores it in .env
        if not os.getenv('TOKEN_JSON'):
            with open(TOKEN_FILE, 'w') as token:
                token.write(creds.to_json())
            
    service = build('calendar', 'v3', credentials=creds)
    return service

def parse_date_input(date_str):
    # Accept ddmmyy and convert to ddmmyyyy (current century)
    try:
        if len(date_str) == 6 and date_str.isdigit():
            day = int(date_str[:2])
            month = int(date_str[2:4])
            year = int(date_str[4:])
            if year < 100:
                year += 2000
            return datetime(year, month, day)
        else:
            raise ValueError
    except ValueError:
        print(red("Невірний формат дати! Використовуйте ддммрр (наприклад, 240925 для 24.09.2025)"))
        return None

def clear_events():
    service = get_google_calendar_service()
    print("\n" + "="*40)
    print(cyan("SDUI Calendar Clear Tool"))
    print("="*40)
    while True:
        start_date_str = input(cyan("Введіть дату початку (ддммрр): ")).strip()
        end_date_str = input(cyan("Введіть дату кінця (ддммрр): ")).strip()
        start_date = parse_date_input(start_date_str)
        end_date = parse_date_input(end_date_str)
        if start_date and end_date:
            break
        print(red("Спробуйте ще раз."))

    # Кінцева дата включно
    end_date += timedelta(days=1)
    tz = pytz.timezone(TIMEZONE)
    time_min = tz.localize(start_date).isoformat()
    time_max = tz.localize(end_date).isoformat()

    try:
        print(yellow(f"Пошук подій з {start_date.strftime('%d.%m.%Y')} по {(end_date-timedelta(days=1)).strftime('%d.%m.%Y')}..."))
        events_result = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])
        if not events:
            print(yellow("Подій у вказаному діапазоні немає."))
            return
        print(yellow(f"Знайдено {len(events)} подій у діапазоні."))
        confirm = input(yellow("Ви впевнені, що хочете видалити всі ці події? (так/ні): ")).strip().lower()
        if confirm != 'так':
            print(yellow("Видалення скасовано."))
            return
        for event in events:
            try:
                service.events().delete(
                    calendarId=GOOGLE_CALENDAR_ID,
                    eventId=event['id']
                ).execute()
                print(green(f"Видалено: {event.get('summary', 'без назви')} ({event.get('start').get('dateTime', event.get('start').get('date'))})"))
            except HttpError as error:
                print(red(f"Помилка видалення події {event.get('summary', 'без назви')}: {error}"))
        print(green("Видалення завершено."))
    except HttpError as error:
        print(red(f"Помилка при отриманні подій: {error}"))

if __name__ == "__main__":
    clear_events()


