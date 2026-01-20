const express = require('express');
const axios = require('axios');
const path = require('path');
const moment = require('moment-timezone');
const { google } = require('googleapis');
const session = require('express-session');
const bodyParser = require('body-parser');
const fs = require('fs');

const app = express();
const PORT = 5000;

// --- GLOBALS & CONFIG ---
// Storing config in memory as requested (no .env file management)
let CONFIG = {
    SDUI_USER_ID: null,
    SDUI_AUTH_TOKEN: null,
    GOOGLE_CALENDAR_ID: 'primary',
    TIMEZONE: 'Europe/Berlin',
    SYNC_YEAR: new Date().getFullYear(),
    // Hidden/Unused but kept for structure if needed later
    SDUI_EMAIL: '',
    SDUI_PASSWORD: '',
    SDUI_SCHOOL_ID: ''
};

let LOG_BUFFER = [];
let IS_RUNNING = false;
let ABORT_FLAG = false;

// Google Auth Setup
const SCOPES = ['https://www.googleapis.com/auth/calendar'];
const CREDENTIALS_PATH = path.join(__dirname, 'auth', 'credentials.json');
const TOKEN_PATH = path.join(__dirname, 'auth', 'token.json');

// --- HELPERS ---

function logMsg(message) {
    const timestamp = new Date().toLocaleTimeString('en-US', { hour12: false });
    const entry = `[${timestamp}] ${message}`;
    console.log(entry);
    LOG_BUFFER.push(entry);
    if (LOG_BUFFER.length > 500) LOG_BUFFER.shift();
}

async function getCalendarClient() {
    // Load client secrets from a local file.
    if (!fs.existsSync(CREDENTIALS_PATH)) {
        logMsg("ERROR: auth/credentials.json missing.");
        return null;
    }
    
    const content = fs.readFileSync(CREDENTIALS_PATH);
    const credentials = JSON.parse(content);
    const { client_secret, client_id, redirect_uris } = credentials.installed || credentials.web;
    const oAuth2Client = new google.auth.OAuth2(client_id, client_secret, redirect_uris[0]);

    // Check if we have previously stored a token.
    if (fs.existsSync(TOKEN_PATH)) {
        const token = fs.readFileSync(TOKEN_PATH);
        oAuth2Client.setCredentials(JSON.parse(token));
    } else {
        // NOTE: In a real headless node app, you'd trigger a CLI auth flow here.
        // For simplicity, we assume token.json exists or is handled separately.
        logMsg("ERROR: auth/token.json missing. Authenticate locally first.");
        return null;
    }
    
    return google.calendar({ version: 'v3', auth: oAuth2Client });
}

// --- CORE FUNCTIONS (Translated from Python) ---

async function getSduiData(startDate, endDate) {
    if (!CONFIG.SDUI_AUTH_TOKEN || !CONFIG.SDUI_USER_ID) {
        logMsg("Error: Missing Token or User ID. Check Configuration.");
        return null;
    }

    const begins_at = startDate.format("YYYY-MM-DD");
    const ends_at = endDate.format("YYYY-MM-DD");
    
    logMsg(`Fetching SDUI: ${begins_at} -> ${ends_at}`);
    
    // Auto-append 'Bearer ' if missing
    let token = CONFIG.SDUI_AUTH_TOKEN.trim();
    if (!token.toLowerCase().startsWith('bearer ')) {
        token = `Bearer ${token}`;
    }

    const url = `https://api.sdui.app/v1/timetables/users/${CONFIG.SDUI_USER_ID}/timetable?begins_at=${begins_at}&ends_at=${ends_at}`;
    
    try {
        const response = await axios.get(url, {
            headers: { 
                'Authorization': token,
                'User-Agent': 'Mozilla/5.0'
            }
        });
        return response.data;
    } catch (error) {
        logMsg(`Network Error: ${error.message}`);
        if(error.response && error.response.status === 401) {
            logMsg("Error: Token Expired or Invalid (401).");
        }
        return null;
    }
}

function processSduiData(sduiData) {
    const events = [];
    if (!sduiData || !sduiData.data) return events;
    
    const lessons = sduiData.data.lessons || [];
    
    const OFTYPE_MAP = {
        "CANCLED": "❌ Cancelled: ", 
        "BOOKABLE_CHANGE": "⚠️ Room: ", 
        "SUBSTITUTION": "🔄 Sub: ", 
        "EXAM": "📝 Exam: "
    };
    
    const COLORS = {
        EXAM: '11',
        HOLIDAY: '10',
        CHANGE: '6',
        EVENT: '3',
        DEFAULT: '9',
        CANCELLED: '8'
    };

    lessons.forEach(lesson => {
        const kind = lesson.kind;
        const oftype = lesson.oftype;
        let colorId = COLORS.DEFAULT;
        let summary = "";
        let location = "";
        let description = "";

        // Logic Translation
        if (['HOLIDAY', 'EVENT'].includes(kind)) {
            const meta = lesson.meta || {};
            const subj = meta.displayname || lesson.comment || "Event";
            summary = (kind === 'HOLIDAY') ? `🏖️ ${subj}` : `📅 ${subj}`;
            colorId = (kind === 'HOLIDAY') ? COLORS.HOLIDAY : COLORS.EVENT;
            description = `Type: ${kind}\nComment: ${lesson.comment || ''}`;
        } else {
            const course = lesson.course || {};
            const meta = course.meta || {};
            // Python: split('_')[-1]
            let subject = (meta.displayname || 'Unknown');
            if(subject.includes('_')) subject = subject.split('_').pop();

            const prefix = OFTYPE_MAP[oftype] || "";
            summary = `${prefix}${subject}`;

            if (oftype === 'EXAM') colorId = COLORS.EXAM;
            else if (['SUBSTITUTION', 'BOOKABLE_CHANGE'].includes(oftype)) colorId = COLORS.CHANGE;
            else if (oftype === 'CANCLED') colorId = COLORS.CANCELLED;

            const rooms = (lesson.bookables || []).map(b => b.name).filter(n => n);
            const teachers = (lesson.teachers || []).map(t => t.name).filter(t => t);
            
            location = rooms.join(", ");
            description = `Teacher: ${teachers.join(", ")}\nType: ${kind || oftype}`;
        }

        const ts_start = lesson.begins_at;
        const ts_end = lesson.ends_at;

        if (ts_start && ts_end) {
            events.push({
                summary,
                location,
                description,
                colorId,
                start: { dateTime: moment.unix(ts_start).tz(CONFIG.TIMEZONE).format(), timeZone: CONFIG.TIMEZONE },
                end: { dateTime: moment.unix(ts_end).tz(CONFIG.TIMEZONE).format(), timeZone: CONFIG.TIMEZONE }
            });
        }
    });

    return events;
}

// --- WORKERS (Async) ---

async function runSync(startDate, endDate) {
    if (IS_RUNNING) return;
    IS_RUNNING = true;
    ABORT_FLAG = false;
    logMsg("--- STARTING SYNC (JS Background) ---");

    const data = await getSduiData(startDate, endDate);
    if (!data) { IS_RUNNING = false; return; }

    const events = processSduiData(data);
    if (events.length === 0) { 
        logMsg("No events found."); 
        IS_RUNNING = false; 
        return; 
    }

    const calendar = await getCalendarClient();
    if (!calendar) { IS_RUNNING = false; return; }

    logMsg(`Queue: ${events.length} events.`);

    let count = 0;
    for (const [i, event] of events.entries()) {
        if (ABORT_FLAG) { logMsg("!!! STOPPED BY USER !!!"); break; }

        const body = {
            summary: event.summary,
            location: event.location,
            description: event.description,
            start: event.start,
            end: event.end,
            colorId: event.colorId
        };

        // Retry logic loop
        let uploaded = false;
        for (let attempt = 0; attempt < 8; attempt++) {
            if (ABORT_FLAG) break;
            try {
                await calendar.events.insert({
                    calendarId: CONFIG.GOOGLE_CALENDAR_ID,
                    requestBody: body
                });
                logMsg(`[${i+1}/${events.length}] Uploaded: ${event.summary}`);
                uploaded = true;
                count++;
                // Small delay to be nice to API
                await new Promise(r => setTimeout(r, 200)); 
                break;
            } catch (e) {
                if (e.code === 403 && (e.message.includes('usage') || e.message.includes('rate'))) {
                    const waitTime = Math.pow(2, attempt) * 1000;
                    logMsg(`Rate Limit! Pausing ${waitTime/1000}s...`);
                    await new Promise(r => setTimeout(r, waitTime));
                } else {
                    logMsg(`Error on item ${i}: ${e.message}`);
                    break;
                }
            }
        }
    }

    logMsg(`--- FINISHED. Imported ${count} events. ---`);
    IS_RUNNING = false;
}

async function runClear(startDate, endDate) {
    if (IS_RUNNING) return;
    IS_RUNNING = true;
    ABORT_FLAG = false;
    logMsg("--- STARTING DELETE (JS Background) ---");

    const calendar = await getCalendarClient();
    if (!calendar) { IS_RUNNING = false; return; }

    const startISO = startDate.startOf('day').format();
    const endISO = endDate.endOf('day').format();
    
    let totalDeleted = 0;

    // Multi-pass deletion
    for (let pass = 1; pass <= 5; pass++) {
        if (ABORT_FLAG) break;
        logMsg(`Pass ${pass}: Scanning...`);

        try {
            const res = await calendar.events.list({
                calendarId: CONFIG.GOOGLE_CALENDAR_ID,
                timeMin: startISO,
                timeMax: endISO,
                singleEvents: true,
                maxResults: 250
            });
            
            const events = res.data.items || [];
            if (events.length === 0) { logMsg("Clean."); break; }
            
            logMsg(`Found ${events.length} events to delete.`);
            
            for (const event of events) {
                if (ABORT_FLAG) break;
                try {
                    await calendar.events.delete({
                        calendarId: CONFIG.GOOGLE_CALENDAR_ID,
                        eventId: event.id
                    });
                    totalDeleted++;
                    if (totalDeleted % 10 === 0) logMsg(`Deleted ${totalDeleted}...`);
                    await new Promise(r => setTimeout(r, 150)); // Throttling
                } catch (e) {
                    if (e.code === 403) {
                        logMsg("Rate Limit (Delete). Waiting 2s...");
                        await new Promise(r => setTimeout(r, 2000));
                    } else if (e.code !== 404 && e.code !== 410) {
                        logMsg(`Del Error: ${e.message}`);
                    }
                }
            }
        } catch (e) {
            logMsg(`List Error: ${e.message}`);
            break;
        }
    }

    if (ABORT_FLAG) logMsg("!!! STOPPED BY USER !!!");
    else logMsg(`--- FINISHED. Deleted ${totalDeleted} events. ---`);
    IS_RUNNING = false;
}

// --- EXPRESS SERVER ---

app.use(bodyParser.urlencoded({ extended: true }));
app.use(session({ secret: 'js-secret-key', resave: false, saveUninitialized: true }));
app.use(express.static('public')); // Serve static files like css/js if separated

// Setup View Engine (Using simple HTML replacement for simplicity)
app.get('/', (req, res) => {
    // We send the file, but we could also inject config if we used a template engine like EJS.
    // Since we are using raw HTML, the client will fetch logs/status dynamically.
    res.sendFile(path.join(__dirname, 'views', 'index.html'));
});

// Used to pre-populate the modal in frontend (Client fetches this)
app.get('/api/config', (req, res) => {
    res.json(CONFIG);
});

app.post('/update_settings', (req, res) => {
    const d = req.body;
    
    if (d.sdui_token) CONFIG.SDUI_AUTH_TOKEN = d.sdui_token.trim();
    if (d.sdui_id) CONFIG.SDUI_USER_ID = d.sdui_id.trim();
    if (d.cal_id) CONFIG.GOOGLE_CALENDAR_ID = d.cal_id.trim();
    if (d.year) CONFIG.SYNC_YEAR = parseInt(d.year);

    logMsg("Settings Updated via Web UI.");
    res.redirect('/');
});

app.get('/logs', (req, res) => {
    res.json({ logs: LOG_BUFFER, running: IS_RUNNING });
});

app.post('/stop', (req, res) => {
    if (IS_RUNNING) {
        ABORT_FLAG = true;
        logMsg(">>> STOP SIGNAL RECEIVED <<<");
        res.json({ status: 'stopping' });
    } else {
        res.json({ status: 'not_running' });
    }
});

app.post('/clear_logs', (req, res) => {
    LOG_BUFFER = [];
    res.json({ status: 'cleared' });
});

app.get('/sync/today', (req, res) => {
    if (IS_RUNNING) return res.redirect('/');
    const today = moment().tz(CONFIG.TIMEZONE);
    // Start async, don't await
    runSync(today, today);
    res.redirect('/');
});

app.post('/sync/week', (req, res) => {
    if (IS_RUNNING) return res.redirect('/');
    const year = CONFIG.SYNC_YEAR;
    const startW = parseInt(req.body.start_week);
    const endW = parseInt(req.body.end_week) || startW;
    
    // Calculate dates from ISO weeks
    const start = moment().year(year).isoWeek(startW).startOf('isoWeek');
    const end = moment().year(year).isoWeek(endW).endOf('isoWeek');

    runSync(start, end);
    res.redirect('/');
});

app.post('/sync/custom', (req, res) => {
    if (IS_RUNNING) return res.redirect('/');
    const start = moment(req.body.start);
    const end = moment(req.body.end);
    runSync(start, end);
    res.redirect('/');
});

app.post('/clear/weeks', (req, res) => {
    if (IS_RUNNING) return res.redirect('/');
    const year = CONFIG.SYNC_YEAR;
    const startW = parseInt(req.body.start_week);
    const endW = parseInt(req.body.end_week) || startW;
    
    const start = moment().year(year).isoWeek(startW).startOf('isoWeek');
    const end = moment().year(year).isoWeek(endW).endOf('isoWeek');

    runClear(start, end);
    res.redirect('/');
});

app.listen(PORT, () => {
    console.log(`Server running at http://localhost:${PORT}`);
});