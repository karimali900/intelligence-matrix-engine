import asyncio
import csv
import io
import logging
import os
import re
import smtplib
import time
from contextlib import asynccontextmanager
from email.mime.text import MIMEText
from datetime import datetime, timedelta
import httpx
import yt_dlp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fake_useragent import UserAgent
from fastapi import FastAPI, BackgroundTasks, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from supabase import create_client, Client
from fastapi.staticfiles import StaticFiles
from flask import send_from_directory
logger = logging.getLogger("matrix")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

load_dotenv()

# --- CONFIGURATION ---
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")

ALERT_EMAILS = ["90.karim@gmail.com"]

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
INTERVAL_SECONDS = 300
SCRAPE_LIMIT = 50
MAX_CONCURRENT = 10
ALREADY_ALERTED = set()

if not SUPABASE_URL or not SUPABASE_KEY:
    logger.warning("Supabase credentials missing — DB operations will fail")
if not OPENAI_API_KEY:
    logger.warning("OPENAI_API_KEY missing — AI Agent endpoints will fail")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

ua = UserAgent()

CATEGORY_WEIGHTS = {
    "economy": 1.5,
    "politics": 1.4,
    "society": 1.2,
    "technology": 1.3,
    "sports": 0.8,
    "general": 1.0,
}

SCRAPE_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT)

# RSS Configuration with strict 24h filter
RSS_FEEDS = [
    ("https://news.google.com/rss/search?q=%D9%85%D8%B5%D8%B1+OR+%D8%A7%D9%84%D8%B3%D8%B9%D9%88%D8%AF%D9%8A%D8%A9+when:1d&hl=ar&gl=EG&ceid=EG:ar", "General News"),
    ("https://news.google.com/rss/search?q=AlJazeera+when:1d&hl=ar&gl=EG&ceid=EG:ar", "Al Jazeera"),
    ("https://news.google.com/rss/search?q=France24+when:1d&hl=ar&gl=EG&ceid=EG:ar", "France24"),
    ("https://news.google.com/rss/search?q=CNNArabic+when:1d&hl=ar&gl=EG&ceid=EG:ar", "CNN Arabic"),
    ("https://news.google.com/rss/search?q=BBCArabic+when:1d&hl=ar&gl=EG&ceid=EG:ar", "BBC Arabic"),
    ("https://news.google.com/rss/search?q=Twitter+Egypt+Saudi+when:1d&hl=ar&gl=EG&ceid=EG:ar", "X (Twitter)"),
    ("https://news.google.com/rss/search?q=Facebook+Egypt+Saudi+when:1d&hl=ar&gl=EG&ceid=EG:ar", "Facebook")
]

TELEGRAM_CHANNELS = [
    "AlArabiya_EGY",
    "skynewsarabia",
    "Cairo24news",
]

app_start_time = time.time()


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        dead = []
        for conn in self.active_connections:
            try:
                await conn.send_json(message)
            except Exception:
                dead.append(conn)
        for conn in dead:
            self.disconnect(conn)


manager = ConnectionManager()


def send_email_alert(title: str, score: float, source: str, category: str):
    if not SMTP_PASS:
        return
    try:
        msg = MIMEText(
            f"""
🚨 INTELLIGENCE ALERT TRIGGERED

Threat Level: HIGH
Velocity Score: {score}

Headline:
{title}

Source:
{source}

Category:
{category}
"""
        )
        msg["Subject"] = f"CRITICAL MATRIX ALERT: {category.upper()} [{score}]"
        msg["From"] = SMTP_USER
        msg["To"] = ", ".join(ALERT_EMAILS)

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        logger.info("Email alert sent for score=%.1f", score)
    except (smtplib.SMTPException, OSError) as e:
        logger.error("Email error: %s", e)


def detect_country(text: str) -> str:
    t = text.lower()
    eg_keywords = [
        "مصر", "القاهرة", "egypt", "cairo", "الإسكندرية", "سيناء", "eg",
        "السيسي", "الرئيس", "مدبولي", "الحكومة المصرية", "البرلمان المصري",
        "المعارضة", "اعتقال", "الجنيه", "الدولار", "قروض", "المركزي المصري",
        "صندوق النقد", "العاصمة الإدارية", "الديون", "الأهلي", "الزمالك",
        "المنتخب المصري", "برلمان", "زيادة", "اسعار", "معاشات", "ال "
    ]
    sa_keywords = [
        "السعودية", "الرياض", "الهلال", "النفط", "saudi", "نيوم",
        "بن سلمان", "النصر", "جدة", "المملكة", "البترول", "قصف", "ال", "ولى", "الملك"
    ]
    if any(k in t for k in eg_keywords):
        return "Egypt 🇪🇬"
    if any(k in t for k in sa_keywords):
        return "Saudi Arabia 🇸🇦"
    return "Regional / International 🌐"


def detect_category(text: str) -> str:
    t = text.lower()
    mapping = {
        "economy": [
            "اقتصاد", "نفط", "دولار", "بورصة", "أسعار", "inflation",
            "market", "currency", "الذهب", "قروض", "تضخم", "استثمار",
            "economy", "finance", "stocks", "trade", "barrel", "كهرباء", "أحمال"
        ],
        "politics": [
            "حكومة", "وزير", "رئيس", "انتخابات", "سياسة", "غزة", "برلمان",
            "trump", "government", "الخليج", "هجوم", "اسرائيل", "حرب",
            "امريكا", "صراع", "المعارضة", "اعتقال", "politics", "military",
            "conflict", "biden", "strike", "diplomacy", "البحر الأحمر"
        ],
        "sports": [
            "كرة", "مباراة", "كأس", "الهلال", "النصر", "الأهلي",
            "الزمالك", "ملعب", "football", "league", "sports", "soccer",
            "championship", "tournament"
        ],
        "technology": [
            "ذكاء", "تكنولوجيا", "تطبيق", "روبوت", "تحديث", "ai",
            "software", "cyber", "tech", "technology", "artificial intelligence",
            "hacker", "apple", "google", "microsoft", "رؤية 2030"
        ],
    }
    for cat, keywords in mapping.items():
        if any(k in t for k in keywords):
            return cat
    return "society"


def detect_sentiment(text: str) -> str:
    t = text.lower()
    negative = [
        "انهيار", "حرب", "انفجار", "اغتيال", "مقتل", "أزمة", "خسائر",
        "وفاة", "عاجل", "تراجع", "هجوم", "كارثة", "اعتقال", "قطع",
        "crisis", "crash", "death", "attack", "collapse", "killed", "warning"
    ]
    positive = [
        "نمو", "نجاح", "تطور", "فوز", "ارتفاع", "إنجاز", "مكاسب",
        "اتفاق", "تحسن", "تتويج", "شراكة", "حل",
        "growth", "success", "win", "rise", "achieve", "partnership", "deal"
    ]
    if any(n in t for n in negative):
        return "Critical 🔴"
    if any(p in t for p in positive):
        return "Positive 🟢"
    return "Neutral ⚪"



# ... your other routes ...

@app.route('/.well-known/assetlinks.json')
def serve_assetlinks():
    return send_from_directory(
        os.path.join(app.root_path, 'static'),
        'assetlinks.json',
        mimetype='application/json'
    )
async def fetch_rss(client: httpx.AsyncClient, url: str, source_tag: str) -> list[dict]:
    async with SCRAPE_SEMAPHORE:
        for attempt in range(2):
            try:
                res = await client.get(url, timeout=30)
                res.raise_for_status()
                soup = BeautifulSoup(res.content, features="xml")
                items = []
                for item in soup.find_all("item")[:SCRAPE_LIMIT]:
                    clean = re.sub(r" - .*$", "", item.title.text).strip()
                    if len(clean) > 8:
                        items.append({
                            "title": clean,
                            "source": source_tag,
                            "metrics": 4500,
                            "url": item.link.text if item.link else "",
                        })
                logger.info("Fetched %d items from %s", len(items), source_tag)
                return items
            except httpx.HTTPStatusError as e:
                logger.warning("HTTP %d for %s (attempt %d)", e.response.status_code, source_tag, attempt + 1)
            except (httpx.RequestError, asyncio.TimeoutError) as e:
                logger.warning("Request failed for %s [%s]: %s (attempt %d)", source_tag, type(e).__name__, e, attempt + 1)
                if attempt == 0:
                    await asyncio.sleep(2)
            except Exception as e:
                logger.error("Unexpected error fetching %s [%s]: %s", source_tag, type(e).__name__, e)
                break
    return []


async def collect_telegram(client: httpx.AsyncClient) -> list[dict]:
    items = []
    for ch in TELEGRAM_CHANNELS:
        async with SCRAPE_SEMAPHORE:
            try:
                res = await client.get(f"https://t.me/s/{ch}", timeout=30)
                res.raise_for_status()
                soup = BeautifulSoup(res.text, "html.parser")
                for msg in soup.find_all("div", class_="tgme_widget_message_text")[:8]:
                    text = msg.get_text(strip=True)
                    if len(text) > 15:
                        items.append({
                            "title": text[:120] + ("..." if len(text) > 120 else ""),
                            "source": "Telegram",
                            "metrics": 2800,
                            "url": f"https://t.me/s/{ch}",
                        })
                logger.info("Fetched Telegram posts from %s", ch)
            except httpx.HTTPStatusError as e:
                logger.warning("HTTP %d for Telegram/%s", e.response.status_code, ch)
            except (httpx.RequestError, asyncio.TimeoutError) as e:
                logger.warning("Telegram request failed for %s [%s]: %s", ch, type(e).__name__, e)
            except Exception as e:
                logger.error("Unexpected Telegram error for %s [%s]: %s", ch, type(e).__name__, e)
    return items


def _sync_collect_youtube() -> list[dict]:
    trends = []
    queries = [
        "عاجل أزمة الاقتصاد والأسعار مصر",
        "تخفيف الأحمال وقطع الكهرباء",
        "قرارات الحكومة المصرية اليوم",
        "تريند السعودية ورؤية 2030",
        "تطورات غزة والبحر الأحمر",
        "أسواق المال والطاقة الشرق الأوسط"
    ]

    ydl_opts = {"quiet": True, "extract_flat": False, "skip_download": True}
    per_query = max(3, SCRAPE_LIMIT // len(queries))
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

    for q in queries:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                res = ydl.extract_info(f"ytsearchdate{per_query}:{q}", download=False)
                for entry in res.get("entries", []):
                    upload_date = entry.get("upload_date")
                    if upload_date and upload_date >= yesterday_str:
                        if entry.get("title"):
                            trends.append({
                                "title": entry["title"],
                                "source": "YouTube",
                                "metrics": entry.get("view_count") or 1200,
                                "url": entry.get("webpage_url") or f"https://www.youtube.com/watch?v={entry.get('id')}"
                            })
        except Exception as e:
            logger.error("YouTube scrape error for query '%s': %s", q, e)

    return trends


async def collect_youtube() -> list[dict]:
    return await asyncio.to_thread(_sync_collect_youtube)


async def collect_google_trends(client: httpx.AsyncClient) -> list[dict]:
    trends = []
    if not SERPAPI_KEY:
        logger.warning("SERPAPI_KEY missing — skipping Google Trends collection.")
        return trends
    try:
        url = "https://serpapi.com/search.json"
        params = {
            "engine": "google_trends_trending_now",
            "geo": "EG",
            "api_key": SERPAPI_KEY
        }
        res = await client.get(url, params=params, timeout=30)
        res.raise_for_status()
        data = res.json()
        trending_searches = data.get("trending_searches", [])[:SCRAPE_LIMIT]
        for item in trending_searches:
            query_text = item.get("query", "")
            raw_volume = item.get("search_volume", "10000")
            if isinstance(raw_volume, str):
                clean_volume = int(raw_volume.replace('K+', '000').replace(',', '').replace('M+', '000000').replace('+', ''))
            else:
                clean_volume = raw_volume

            if len(query_text) > 2:
                trends.append({
                    "title": query_text,
                    "source": "Google Trends",
                    "metrics": clean_volume,
                    "url": f"https://trends.google.com/trends/explore?q={query_text}&geo=EG"
                })
        logger.info("Fetched Google Trends successfully")
    except Exception as e:
        logger.warning("Google Trends error [%s]: %s", type(e).__name__, e)
    return trends


async def ingestion_cycle():
    logger.info("Starting ingestion cycle")

    async with httpx.AsyncClient(
        headers={"User-Agent": ua.random},
        follow_redirects=True,
    ) as client:
        rss_tasks = [fetch_rss(client, url, tag) for url, tag in RSS_FEEDS]
        telegram_task = collect_telegram(client)
        google_task = collect_google_trends(client)
        youtube_task = collect_youtube()

        results = await asyncio.gather(*rss_tasks, telegram_task, google_task, youtube_task)
        pool = []
        for r in results:
            if isinstance(r, list):
                pool.extend(r)

    logger.info("Collected %d raw items across all vectors", len(pool))

    for item in pool:
        category = detect_category(item["title"])
        geo = detect_country(item["title"])
        sentiment = detect_sentiment(item["title"])
        weight = CATEGORY_WEIGHTS.get(category, 1.0)
        score = min(round((item["metrics"] * 0.0005) * weight, 2), 100.0)

        now = datetime.utcnow().isoformat()
        data = {
            "title": item["title"],
            "source": item["source"],
            "geographic_vector": geo,
            "category": category,
            "velocity_score": score,
            "url": item["url"],
            "sentiment": sentiment,
            "collected_at": now,
        }

        if score >= 85.0 and item["title"] not in ALREADY_ALERTED:
            send_email_alert(item["title"], score, item["source"], category)
            ALREADY_ALERTED.add(item["title"])

            if len(ALREADY_ALERTED) > 1000:
                ALREADY_ALERTED.clear()

        if supabase:
            try:
                supabase.table("trends").upsert(data, on_conflict="title,source").execute()
            except Exception as e:
                logger.error("DB upsert error: %s", e)

    await manager.broadcast({"type": "refresh_data"})
    logger.info("Ingestion cycle complete — %d upserted", len(pool))


async def continuous_worker():
    while True:
        await ingestion_cycle()
        await asyncio.sleep(INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.warning("Supabase not configured — DB endpoints will fail")
    if not SMTP_PASS:
        logger.warning("SMTP not configured — email alerts disabled")
    task = asyncio.create_task(continuous_worker())
    yield
    task.cancel()


app = FastAPI(title="Intelligence Matrix Engine", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")



# --- NEW: AI AGENT INTERFACE ENDPOINT ---
@app.get("/api/agent")
async def intelligence_agent(request: Request, q: str):
    """
    AI Agent Endpoint: Takes user requests, extracts recent DB context from Supabase,
    and returns an analytical intelligence brief.
    Usage: http://127.0.0.1:8001/api/agent?q=اكتب تحليل عن أزمة الكهرباء والليرة
    """
    if not OPENAI_API_KEY:
        return {"status": "error", "message": "OpenAI API Key is not configured in .env"}
    if not q:
        return {"status": "error", "message": "Please supply a query parameter 'q'"}

    # 1. Fetch latest real-time context from Supabase to ground the AI
    context_brief = "No live database records found."
    if supabase:
        try:
            db_resp = supabase.table("trends").select("title,source,category,sentiment").order("id", desc=True).limit(25).execute()
            if db_resp.data:
                context_brief = "\n".join([
                    f"- [{row['category']}] ({row['sentiment']}) {row['title']} (المصدر: {row['source']})"
                    for row in db_resp.data
                ])
        except Exception as e:
            logger.error("AI Agent failed to pull database grounding context: %s", e)

    # 2. Construct systemic intelligence prompt
    system_prompt = (
        "You are an elite, multi-source Intelligence Analyst Agent operating an Automated Matrix Platform.\n"
        "Your mission is to evaluate specific thematic trends, socioeconomic vulnerabilities, and political undercurrents "
        "in the Middle East, primarily focusing on Egypt and Saudi Arabia.\n\n"
        "Here is the absolute latest context collected by your ingestion vectors over the past 24-48 hours:\n"
        f"{context_brief}\n\n"
        "Synthesize this factual data with your deep domain knowledge. Provide explicit, analytical, objective, "
        "and clear structural insights. Respond directly in Arabic."
    )

    # 3. Request completion via httpx
    try:
        async with httpx.AsyncClient() as client:
            openai_url = "https://api.openai.com/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": q}
                ],
                "temperature": 0.4
            }
            res = await client.post(openai_url, json=payload, headers=headers, timeout=30)
            res.raise_for_status()
            ai_data = res.json()
            analysis_output = ai_data["choices"][0]["message"]["content"]

            return {
                "status": "success",
                "query": q,
                "agent_brief": analysis_output
            }
    except Exception as e:
        logger.error("AI Agent generation error: %s", e)
        return {"status": "error", "message": f"AI Generation failed: {str(e)}"}


@app.get("/sw.js")
async def service_worker():
    return FileResponse("static/sw.js", media_type="application/javascript")

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"request": request},
    )


@app.get("/api/trends")
async def get_live_trends():
    if not supabase:
        return {"status": "error", "message": "Database not configured"}
    try:
        cutoff = (datetime.utcnow() - timedelta(hours=48)).isoformat()
        response = supabase.table("trends") \
            .select("*") \
            .gte("collected_at", cutoff) \
            .order("velocity_score", desc=True) \
            .limit(500) \
            .execute()
        return {"status": "success", "data": response.data}
    except Exception as e:
        logger.error("Trends fetch error: %s", e)
        return {"status": "error", "message": str(e)}


@app.get("/api/export/csv")
async def export_csv():
    if not supabase:
        return {"status": "error", "message": "Database not configured"}
    try:
        response = supabase.table("trends").select("*").order("velocity_score", desc=True).execute()
        data = response.data
    except Exception as e:
        logger.error("CSV export fetch error: %s", e)
        return {"status": "error", "message": str(e)}

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Title", "Source", "Geographic Vector", "Category", "Sentiment", "Velocity Score", "URL", "Collected At"])
    for row in data:
        writer.writerow([
            row.get("title", ""),
            row.get("source", ""),
            row.get("geographic_vector", ""),
            row.get("category", ""),
            row.get("sentiment", "Neutral ⚪"),
            row.get("velocity_score", 0),
            row.get("url", ""),
            row.get("collected_at", ""),
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=trends_export_{int(time.time())}.csv"},
    )


@app.get("/api/health")
async def health_check():
    db_ok = False
    trend_count = 0
    if supabase:
        try:
            resp = supabase.table("trends").select("*", count="exact").limit(0).execute()
            trend_count = resp.count if hasattr(resp, "count") else 0
            db_ok = True
        except Exception:
            db_ok = False
    return {
        "status": "healthy",
        "uptime_seconds": int(time.time() - app_start_time),
        "database_connected": db_ok,
        "trend_count": trend_count,
    }


@app.post("/api/cycle/trigger")
async def force_cycle(background_tasks: BackgroundTasks):
    background_tasks.add_task(ingestion_cycle)
    return {"status": "success", "message": "Cycle started."}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    import webbrowser
    import threading
    import time
    def open_browser():
        time.sleep(2)
        webbrowser.open("http://127.0.0.1:8001")

    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
