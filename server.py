from dotenv import load_dotenv
from pathlib import Path

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, timedelta, date
from jose import jwt, JWTError
from fastapi.responses import JSONResponse
import uuid
import os
import hashlib
from openai import OpenAI

# ================= CONFIG =================

MONGO_URL = os.getenv("MONGO_URL")
DB_NAME = os.getenv("DB_NAME")

JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = os.getenv("ALGORITHM", "HS256")
TOKEN_EXPIRE_DAYS = 7

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

PROMPT_VERSION = "v3"

print("OPENAI_API_KEY LOADED:", OPENAI_API_KEY[:10] if OPENAI_API_KEY else "NONE")

openai_client = OpenAI(api_key=OPENAI_API_KEY)

# ================= APP =================

app = FastAPI(title="AI Language Tutor API", version="3.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================= ERROR =================

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    print("GLOBAL ERROR:", exc)
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

# ================= SECURITY =================

security = HTTPBearer()

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload["sub"]
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

# ================= DB =================

client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]

users = db.users
lesson_progress = db.lesson_progress

user_mistakes = db.user_mistakes
ai_usage = db.ai_usage

ai_cache = db.ai_cache
daily_usage = db.daily_usage

# ================= HELPERS =================

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(password: str, hashed: str) -> bool:
    return hash_password(password) == hashed


def create_token(user_id: str):
    payload = {
        "sub": user_id,
        "exp": datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


# 🔥 UPGRADED HASH (SMART CACHE)
import re

def generate_sentence_hash(sentence: str):

    # 1. lowercase
    normalized = sentence.lower()

    # 2. ukloni SVU interpunkciju (ne samo .!?)
    normalized = re.sub(r"[^\w\s]", "", normalized)

    # 3. ukloni višak razmaka
    normalized = " ".join(normalized.split())

    return hashlib.sha256(normalized.encode()).hexdigest()

    # ================= AI LIMIT =================

FREE_DAILY_LIMIT = 10
PREMIUM_DAILY_LIMIT = 200

async def check_ai_limit(user_id: str, is_premium: bool):

    today = date.today().isoformat()

    usage = await daily_usage.find_one({
        "user_id": user_id,
        "date": today
    })

    limit = PREMIUM_DAILY_LIMIT if is_premium else FREE_DAILY_LIMIT

    if usage and usage["count"] >= limit:
        raise HTTPException(status_code=403, detail="Daily AI limit reached")

    if usage:
        await daily_usage.update_one(
            {"_id": usage["_id"]},
            {"$inc": {"count": 1}}
        )
    else:
        await daily_usage.insert_one({
            "user_id": user_id,
            "date": today,
            "count": 1
        })


# ================= AI MEMORY =================

async def save_user_mistake(user_id: str, sentence: str):

    await user_mistakes.insert_one({
        "user_id": user_id,
        "sentence": sentence,
        "created_at": datetime.utcnow()
    })


# ================= GET USER MISTAKES =================

async def get_user_common_mistakes(user_id: str):

    cursor = user_mistakes.find(
        {"user_id": user_id}
    ).sort("created_at", -1).limit(20)

    mistakes = []

    async for m in cursor:
        mistakes.append(m["sentence"])

    return mistakes


# ================= ADAPTIVE AI LESSON =================

async def generate_adaptive_lesson(user_id: str):

    mistakes = await get_user_common_mistakes(user_id)

    # ako nema dovoljno grešaka → nema AI lekcije
    if len(mistakes) < 3:
        return None

    prompt = f"""
You are a professional English teacher.

A student made these mistakes:

{mistakes}

Create a short personalized lesson.

Return EXACTLY in this format:

Title:
<lesson title>

Explanation:
<short explanation>

Example:
<example sentence>

Task:
<task for the student>
"""

    completion = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "user", "content": prompt}
        ],
    )

    return completion.choices[0].message.content


# 🔥 STREAK IZ LESSON-A (GLAVNI FIX)
async def get_streak_summary(user_id: str):
    cursor = lesson_progress.find({"user_id": user_id})

    days = []
    async for record in cursor:
        try:
            days.append(date.fromisoformat(record["date"]))
        except:
            pass

    if not days:
        return 0, 0

    days = sorted(set(days), reverse=True)

    current_streak = 1
    longest_streak = 1

    for i in range(1, len(days)):
        if (days[i - 1] - days[i]).days == 1:
            current_streak += 1
            longest_streak = max(longest_streak, current_streak)
        else:
            break

    return current_streak, longest_streak

# ================= MODELS =================

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    name: str

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class ChatRequest(BaseModel):
    message: str

class LessonAnswerRequest(BaseModel):
    answer: str

# ================= AUTH =================

@app.post("/api/auth/register")
async def register(data: RegisterRequest):
    if await users.find_one({"email": data.email}):
        raise HTTPException(status_code=400, detail="Email exists")

    user_id = str(uuid.uuid4())

    await users.insert_one({
        "_id": user_id,
        "email": data.email,
        "name": data.name,
        "password": hash_password(data.password),
        "total_xp": 0,
        "is_premium": False,
    })

    return {
        "session_token": create_token(user_id),
        "user": {"id": user_id, "email": data.email, "name": data.name},
    }

@app.post("/api/auth/login")
async def login(data: LoginRequest):
    user = await users.find_one({"email": data.email})
    if not user or not verify_password(data.password, user["password"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return {
        "session_token": create_token(user["_id"]),
        "user": {"id": user["_id"], "email": user["email"], "name": user["name"]},
    }

# ================= CHAT =================

@app.post("/api/chat/send")
async def chat_send(data: ChatRequest, user_id: str = Depends(get_current_user)):

    try:

        user = await users.find_one({"_id": user_id})
        is_premium = user.get("is_premium", False)

        # 🧠 1. SMART BLOCK (anti abuse)
        if len(data.message) > 300:
            raise HTTPException(status_code=400, detail="Message too long")
        if not data.message.strip():
            raise HTTPException(status_code=400, detail="Empty message")    

        # 🧠 2. HASH
        clean_message = data.message.strip()
        sentence_hash = generate_sentence_hash(clean_message)

        # 🧠 3. CACHE CHECK (PRVO!)
        cached = await ai_cache.find_one({
            "sentence_hash": sentence_hash,
            "prompt_version": PROMPT_VERSION
        })

        if cached:
            print("CACHE HIT")

            await ai_cache.update_one(
                {"_id": cached["_id"]},
                {"$inc": {"usage_count": 1}}
            )

            # 🔥 NE TROŠI LIMIT
            return {"response": cached["ai_response"]}

        # 🧠 4. TEK SAD LIMIT
        await check_ai_limit(user_id, is_premium)

        # 🧠 5. AI CALL
        completion = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.3,
            max_tokens=300,
            messages=[
                {
                    "role": "system",
                    "content": """
You are an English tutor.

Tasks:
- Detect language
- Translate to correct English
- Fix grammar AND spelling mistakes
- Give a VERY short explanation (1–2 sentences max)

Rules:
- If input is English → explanation ONLY in English
- If not → also include explanation in user language
- Never use <wrong> tags outside Original sentence
- You MUST wrap ALL grammar AND spelling mistakes using <wrong>...</wrong>
- Never skip marking a mistake

Format:

Detected language:
(language name)

Original sentence:
(original text with ALL mistakes wrapped in <wrong> tags)

English version:
(corrected sentence)

Explanation (English):
(short explanation)

(If not English)
Explanation (User language):
(translated explanation)
"""
                },
                {
                    "role": "user",
                    "content": clean_message
                },
            ],
        )

        ai_response = completion.choices[0].message.content

        # 🧠 6. SAVE CACHE
        await ai_cache.insert_one({
            "sentence_hash": sentence_hash,
            "prompt_version": PROMPT_VERSION,
            "user_input": clean_message,
            "ai_response": ai_response,
            "created_at": datetime.utcnow(),
            "usage_count": 1
        })

        return {"response": ai_response}

    except Exception as e:
        print("CHAT ERROR:", e)
        raise HTTPException(status_code=500, detail="AI failed")


# ================= LESSON AI CHECK =================

@app.post("/api/lesson/ai-check")
async def lesson_ai_check(
    data: LessonAnswerRequest,
    user_id: str = Depends(get_current_user)
):

    try:
        user = await users.find_one({"_id": user_id})
        is_premium = user.get("is_premium", False)

        await check_ai_limit(user_id, is_premium)

        sentence_hash = generate_sentence_hash(data.answer)

        cached = await ai_cache.find_one({
            "sentence_hash": sentence_hash,
            "prompt_version": PROMPT_VERSION
        })

        # ✅ CACHE HIT
        if cached:

            await ai_cache.update_one(
                {"_id": cached["_id"]},
                {"$inc": {"usage_count": 1}}
            )

            print("CACHE HIT LESSON")

            return {"feedback": cached["ai_response"]}

        # ✅ SAVE MISTAKE
        if is_premium:
            await save_user_mistake(user_id, data.answer)

        # ✅ AI CALL (NE DIRAMO PROMPT)
        completion = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=500,
            messages=[
                {
                    "role": "system",
                    "content": """
You are a professional English teacher.

You MUST respond using the EXACT structure below.

Each section MUST start on a new line.
Each section MUST contain text.
Never merge sections.
Never skip sections.

STRUCTURE:

Detected language:
<language name>

Original sentence:
<repeat the user's sentence>

English version:
<correct English sentence>

Explanation (English):
<clear grammar explanation, MAX 2 sentences>

Explanation (User language):
<explanation in user language OR simple English, MAX 2 sentences>

Task feedback:
<does the answer match the lesson task>

Encouragement:
<short motivational sentence>

Sentence suggestions:
• <sentence 1>
• <sentence 2>
• <sentence 3>


IMPORTANT RULES:

WRONG TAGS

<wrong> tags are allowed ONLY inside the "Original sentence" section.

Example:

Original sentence:
I <wrong>goed</wrong> to school yesterday.

The "English version" section MUST NEVER contain <wrong> tags.
It must always show the clean corrected sentence.


LANGUAGE RULES

If the detected language is English:
Write BOTH explanations in simple English.

Never use another language.

If the detected language is NOT English:
Translate the second explanation into that language.


SENTENCE SUGGESTIONS

Provide EXACTLY 3 sentences.

Each suggestion MUST:
- be correct English
- be on its own line
- start with "•"

Example:

Sentence suggestions:
• I went to school early yesterday.
• I walked to school yesterday.
• I went to school with my friend yesterday.

Never include suggestions inside Encouragement or Explanation.

Never add extra commentary.
"""
                },
                {
                    "role": "user",
                    "content": data.answer
                }
            ],
        )

        ai_response = completion.choices[0].message.content

        # ✅ CACHE SAVE
        await ai_cache.insert_one({
            "sentence_hash": sentence_hash,
            "user_input": data.answer,
            "ai_response": ai_response,
            "created_at": datetime.utcnow(),
            "usage_count": 1,
            "prompt_version": PROMPT_VERSION
        })

        return {"feedback": ai_response}

    except Exception as e:
        print("LESSON AI ERROR:", e)
        raise HTTPException(status_code=500, detail="AI failed")


        # ================= TODAY LESSON =================

@app.get("/api/lesson/today")
async def get_today_lesson(user_id: str = Depends(get_current_user)):

    today = date.today().isoformat()

    existing = await lesson_progress.find_one({
        "user_id": user_id,
        "date": today
    })

    # pokušaj da generiše AI lekciju
    adaptive = await generate_adaptive_lesson(user_id)

    if adaptive:

        # parsiranje AI odgovora
        title = ""
        explanation = ""
        example = ""
        task = ""

        lines = adaptive.split("\n")
        current = None

        for line in lines:

            line = line.strip()

            if line.startswith("Title"):
                current = "title"
                continue

            if line.startswith("Explanation"):
                current = "explanation"
                continue

            if line.startswith("Example"):
                current = "example"
                continue

            if line.startswith("Task"):
                current = "task"
                continue

            if current == "title":
                title += line + " "

            elif current == "explanation":
                explanation += line + " "

            elif current == "example":
                example += line + " "

            elif current == "task":
                task += line + " "

        lesson = {
            "title": title.strip() or "AI Lesson",
            "explanation": explanation.strip(),
            "example": example.strip(),
            "task": task.strip(),
            "completed_today": existing is not None
        }

        return lesson

    # fallback standard lesson
    lesson = {
        "title": "Present Simple",
        "explanation": "We use Present Simple for habits.",
        "example": "I work every day.",
        "task": "Write 2 sentences.",
        "completed_today": existing is not None
    }

    return lesson

@app.post("/api/lesson/complete")
async def complete_lesson(user_id: str = Depends(get_current_user)):
    today = date.today().isoformat()

    existing = await lesson_progress.find_one({
        "user_id": user_id,
        "date": today,
    })

    if existing:
        return {"completed": True, "xp_earned": 0}

    user = await users.find_one({"_id": user_id})
    is_premium = user.get("is_premium", False)

    await lesson_progress.insert_one({
        "user_id": user_id,
        "date": today,
        "completed_at": datetime.utcnow(),
    })

    xp = 20 if is_premium else 0

    if xp > 0:
        await users.update_one(
            {"_id": user_id},
            {"$inc": {"total_xp": xp}}
        )

    return {"completed": True, "xp_earned": xp}

# ================= GAMIFICATION =================

@app.get("/api/gamification/summary")
async def gamification_summary(user_id: str = Depends(get_current_user)):
    user = await users.find_one({"_id": user_id})

    current_streak, longest_streak = await get_streak_summary(user_id)

    return {
        "total_xp": user.get("total_xp", 0),
        "current_streak": current_streak,
        "longest_streak": longest_streak,
    }

# ================= AI PROGRESS ANALYTICS =================

@app.get("/api/progress/analytics")
async def get_progress_analytics(user_id: str = Depends(get_current_user)):

    user = await users.find_one({"_id": user_id})

    total_xp = user.get("total_xp", 0)

    # broj recenica koje je korisnik napisao
    sentences_practiced = await user_mistakes.count_documents({
        "user_id": user_id
    })

    # pronadji najcescu gresku
    pipeline = [
        {"$match": {"user_id": user_id}},
        {"$group": {
            "_id": "$sentence",
            "count": {"$sum": 1}
        }},
        {"$sort": {"count": -1}},
        {"$limit": 1}
    ]

    result = await user_mistakes.aggregate(pipeline).to_list(1)

    most_common = result[0]["_id"] if result else "No data yet"

    # jednostavna procena nivoa
    if total_xp < 100:
        level = "Beginner"
    elif total_xp < 300:
        level = "Intermediate"
    else:
        level = "Advanced"

    return {
        "grammar_level": level,
        "sentences_practiced": sentences_practiced,
        "most_common_mistake": most_common,
        "total_xp": total_xp
    }
    
# ================= SUBSCRIPTION =================

@app.get("/api/subscription/status")
async def subscription_status(user_id: str = Depends(get_current_user)):
    user = await users.find_one({"_id": user_id})
    return {"is_premium": user.get("is_premium", False)}

@app.post("/api/subscription/upgrade")
async def upgrade_subscription(user_id: str = Depends(get_current_user)):
    await users.update_one(
        {"_id": user_id},
        {"$set": {"is_premium": True}},
    )
    return {"status": "ok"}
# ================= ACHIEVEMENTS =================

@app.get("/api/achievements")
async def get_achievements(user_id: str = Depends(get_current_user)):
    user = await users.find_one({"_id": user_id})

    total_xp = user.get("total_xp", 0)

    # streak logika (ista kao gamification)
    today = date.today()
    streak = 0

    for i in range(30):
        d = (today - timedelta(days=i)).isoformat()
        exists = await lesson_progress.find_one({
            "user_id": user_id,
            "date": d
        })
        if exists:
            streak += 1
        else:
            break

    achievements = [
        {
            "id": "first_lesson",
            "title": "First Step",
            "unlocked": total_xp > 0 or streak > 0,
        },
        {
            "id": "streak_3",
            "title": "3 Day Streak",
            "unlocked": streak >= 3,
        },
        {
            "id": "xp_100",
            "title": "Earn 100 XP",
            "unlocked": total_xp >= 100,
        },
    ]

    return {
        "achievements": achievements
    }    