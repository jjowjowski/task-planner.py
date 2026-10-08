import os, json, time, requests
from datetime import datetime, timedelta, timezone
from google.oauth2 import service_account
from googleapiclient.discovery import build
from flask import Flask, request
import pytz
import threading

KEY = os.getenv("ANTHROPIC_KEY")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_ID = os.getenv("TELEGRAM_CHAT_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT")
RAILWAY_URL = os.getenv("RAILWAY_STATIC_URL", "http://localhost:5000")

TASKS_FILE = "tasks.json"
PENDING_SCHEDULE_FILE = "pending_schedule.json"

app = Flask(__name__)

def get_calendar_service():
    try:
        creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=['https://www.googleapis.com/auth/calendar']
        )
        return build('calendar', 'v3', credentials=creds)
    except Exception as e:
        print(f"Calendar service error: {e}")
        return None

def get_calendar_events_tomorrow():
    try:
        service = get_calendar_service()
        if not service:
            return []
        
        nz_tz = pytz.timezone('Pacific/Auckland')
        tomorrow = datetime.now(nz_tz) + timedelta(days=1)
        start = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
        end = tomorrow.replace(hour=23, minute=59, second=59, microsecond=0)
        
        events_result = service.events().list(
            calendarId='primary',
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])
        return events
    except Exception as e:
        print(f"Calendar fetch error: {e}")
        return []

def load_tasks():
    if os.path.exists(TASKS_FILE):
        with open(TASKS_FILE, 'r') as f:
            return json.load(f)
    return {"tasks": [], "last_updated": None}

def save_tasks(tasks):
    with open(TASKS_FILE, 'w') as f:
        json.dump(tasks, f, indent=2)

def load_pending_schedule():
    if os.path.exists(PENDING_SCHEDULE_FILE):
        with open(PENDING_SCHEDULE_FILE, 'r') as f:
            return json.load(f)
    return None

def save_pending_schedule(schedule):
    with open(PENDING_SCHEDULE_FILE, 'w') as f:
        json.dump(schedule, f, indent=2)

def delete_pending_schedule():
    if os.path.exists(PENDING_SCHEDULE_FILE):
        os.remove(PENDING_SCHEDULE_FILE)

def generate_schedule(task_list):
    calendar_events = get_calendar_events_tomorrow()
    
    events_str = ""
    if calendar_events:
        events_str = "Already scheduled:\n"
        for event in calendar_events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            summary = event['summary']
            events_str += f"- {summary} at {start}\n"
    else:
        events_str = "No events scheduled yet tomorrow"
    
    tasks_str = "\n".join(f"- {task}" for task in task_list)
    
    prompt = f"""You are a scheduling assistant. Create an optimal daily schedule for tomorrow.

Tasks to schedule:
{tasks_str}

Already on calendar:
{events_str}

Generate a realistic schedule that:
1. Respects existing calendar events
2. Groups similar tasks together
3. Includes breaks
4. Prioritizes important tasks (ECON, Thesis, applications)
5. Leaves buffer time

Return ONLY a schedule in this format, no other text:

9:00 AM - 11:00 AM: Task name
11:00 AM - 12:00 PM: Break
12:00 PM - 1:00 PM: Task name
...

Be specific with times."""
    
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-5-5", "max_tokens": 400, "messages": [{"role": "user", "content": prompt}]},
            timeout=30)
        
        if r.status_code == 200:
            response_data = r.json()
            if "content" in response_data and len(response_data["content"]) > 0:
                schedule_text = response_data["content"][0].get("text", "").strip()
                return schedule_text
        
        return "Could not generate schedule"
    except Exception as e:
        print(f"Claude error: {e}")
        return "Error generating schedule"

def create_calendar_events(schedule_text):
    try:
        service = get_calendar_service()
        if not service:
            return False
        
        nz_tz = pytz.timezone('Pacific/Auckland')
        tomorrow = datetime.now(nz_tz) + timedelta(days=1)
        
        lines = schedule_text.strip().split('\n')
        
        for line in lines:
            if ':' not in line or '-' not in line:
                continue
            
            try:
                parts = line.split(':')
                if len(parts) < 2:
                    continue
                
                time_and_task = ':'.join(parts[1:]).strip()
                
                if ' - ' not in time_and_task:
                    continue
                
                time_range, task_name = time_and_task.split(' - ', 1)
                start_time_str = time_range.strip()
                end_time_str = parts[0].split('-')[-1].strip() if '-' in parts[0] else ""
                
                try:
                    start_time = datetime.strptime(f"{tomorrow.strftime('%Y-%m-%d')} {start_time_str}", "%Y-%m-%d %I:%M %p")
                    end_time = datetime.strptime(f"{tomorrow.strftime('%Y-%m-%d')} {time_and_task.split(':')[-1]}", "%Y-%m-%d %I:%M %p")
                except:
                    continue
                
                start_time = nz_tz.localize(start_time)
                end_time = nz_tz.localize(end_time)
                
                event = {
                    'summary': task_name.strip(),
                    'start': {'dateTime': start_time.isoformat()},
                    'end': {'dateTime': end_time.isoformat()}
                }
                
                service.events().insert(calendarId='primary', body=event).execute()
                print(f"Created event: {task_name}")
            except Exception as e:
                print(f"Error parsing line '{line}': {e}")
                continue
        
        return True
    except Exception as e:
        print(f"Calendar creation error: {e}")
        return False

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        r = requests.post(url, json={"chat_id": TG_ID, "text": msg}, timeout=10)
        if r.status_code == 200:
            print("Telegram sent")
        else:
            print(f"Telegram error: {r.status_code}")
    except Exception as e:
        print(f"Telegram error: {e}")

def is_7pm_nzdt():
    nz_tz = pytz.timezone('Pacific/Auckland')
    nz_now = datetime.now(nz_tz)
    return nz_now.hour == 19 and nz_now.minute < 1

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json
        if 'message' in data:
            message = data['message']
            text = message.get('text', '').lower()
            
            pending = load_pending_schedule()
            
            if pending is None:
                tasks = [task.strip() for task in text.split(',')]
                tasks = [t for t in tasks if t and len(t) > 2]
                
                if tasks:
                    print(f"Generating schedule for: {tasks}")
                    schedule = generate_schedule(tasks)
                    
                    msg = f"""📅 Proposed Schedule for Tomorrow:

{schedule}

Reply: YES to confirm and create events"""
                    
                    send_telegram(msg)
                    save_pending_schedule({"schedule": schedule, "tasks": tasks})
            else:
                if text in ["yes", "confirm", "ok"]:
                    print("Creating calendar events")
                    create_calendar_events(pending["schedule"])
                    
                    tasks_obj = load_tasks()
                    tasks_obj["tasks"] = pending["tasks"]
                    tasks_obj["last_updated"] = datetime.now().isoformat()
                    save_tasks(tasks_obj)
                    
                    send_telegram("✅ Calendar events created!")
                    delete_pending_schedule()
                elif "done:" in text:
                    task_to_remove = text.replace("done:", "").strip()
                    tasks_obj = load_tasks()
                    original_count = len(tasks_obj["tasks"])
                    tasks_obj["tasks"] = [t for t in tasks_obj["tasks"] if task_to_remove not in t.lower()]
                    
                    if len(tasks_obj["tasks"]) < original_count:
                        save_tasks(tasks_obj)
                        send_telegram(f"✅ Marked done: {task_to_remove}")
                    else:
                        send_telegram(f"❌ Task not found: {task_to_remove}")
        
        return {"ok": True}
    except Exception as e:
        print(f"Webhook error: {e}")
        return {"ok": False}

def ask_for_tasks():
    while True:
        nz_tz = pytz.timezone('Pacific/Auckland')
        nz_now = datetime.now(nz_tz)
        
        if is_7pm_nzdt():
            print("7 PM - Asking for tasks")
            send_telegram("📋 What tasks do you need to do tomorrow? (Send as comma-separated list)")
            time.sleep(3600)
        
        time.sleep(60)

if __name__ == '__main__':
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/setWebhook", json={"url": f"{RAILWAY_URL}/webhook"})
    print("Webhook registered")
    
    threading.Thread(target=ask_for_tasks, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)
