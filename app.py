import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
DB_PATH = Path(os.getenv("DB_PATH", BASE_DIR / "buyouts.sqlite3"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
ADMIN_KEY = os.getenv("ADMIN_KEY", "change-me")
RESERVATION_MINUTES = int(os.getenv("RESERVATION_MINUTES", "50"))
DAILY_LIMIT_PER_PRODUCT = int(os.getenv("DAILY_LIMIT_PER_PRODUCT", "5"))
MAX_INIT_DATA_AGE_SECONDS = int(os.getenv("MAX_INIT_DATA_AGE_SECONDS", "86400"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(8 * 1024 * 1024)))
DEV_MODE = os.getenv("DEV_MODE", "0") == "1"
OPERATOR_USERNAME = os.getenv("OPERATOR_USERNAME", "")
APP_TIMEZONE_NAME = os.getenv("APP_TIMEZONE", "Europe/Moscow")
try:
    APP_TZ = ZoneInfo(APP_TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    APP_TZ = timezone(timedelta(hours=3))
    APP_TIMEZONE_NAME = "UTC+03:00"

PRODUCT_SEED = [
    {
        "sku": f"ART-{i:03d}",
        "title": f"Артикул {i}",
        "description": "Замените название, фото и ссылку в админке.",
        "image_url": "",
        "marketplace_url": "",
        "daily_limit": DAILY_LIMIT_PER_PRODUCT,
        "sort_order": i,
    }
    for i in range(1, 9)
]

DEFAULT_INSTRUCTION = f"""1. Нажмите «Открыть карточку» и перейдите к выбранному товару.
2. Проверьте, что открыли именно тот артикул, который забронирован в мини-приложении.
3. Выполните действие в течение {RESERVATION_MINUTES} минут. Если время закончится, бронь автоматически освободится.
4. После выполнения вернитесь в мини-приложение и нажмите кнопку «Товар выкуплен».
5. Если выполнить не получается, нажмите «Отменить бронь», чтобы место стало доступно другому человеку."""


class ReserveRequest(BaseModel):
    product_id: int = Field(gt=0)


class CompleteRequest(BaseModel):
    reservation_id: int = Field(gt=0)


class ProductUpdate(BaseModel):
    sku: str
    title: str
    description: str = ""
    instruction: str = Field(default="", max_length=10000)
    image_url: str = ""
    marketplace_url: str = ""
    daily_limit: int = Field(default=5, ge=0, le=10000)
    is_active: bool = True


class SettingsUpdate(BaseModel):
    instruction: str = Field(default="", max_length=10000)
    operator_username: str = Field(default="", max_length=100)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_now() -> datetime:
    return datetime.now(APP_TZ)


def today_key() -> str:
    """Ключ дня для лимитов. После 00:00 в APP_TIMEZONE начинается новый день и места снова доступны."""
    return local_now().strftime("%Y-%m-%d")


def daily_reset_meta() -> Dict[str, Any]:
    return {
        "day_key": today_key(),
        "timezone": APP_TIMEZONE_NAME,
        "daily_reset_text": "Места автоматически обновляются каждый день после 00:00",
    }


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def clean_username(value: str) -> str:
    return (value or "").strip().lstrip("@")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def execute_schema() -> None:
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                instruction TEXT NOT NULL DEFAULT '',
                image_url TEXT NOT NULL DEFAULT '',
                marketplace_url TEXT NOT NULL DEFAULT '',
                daily_limit INTEGER NOT NULL DEFAULT 5,
                is_active INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 100,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL REFERENCES products(id),
                telegram_id INTEGER NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK(status IN ('reserved', 'completed', 'expired', 'cancelled')),
                day_key TEXT NOT NULL,
                reserved_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                completed_at TEXT,
                cancelled_at TEXT,
                user_agent TEXT NOT NULL DEFAULT '',
                UNIQUE(product_id, telegram_id, day_key, status) ON CONFLICT IGNORE
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bot_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                chat_id INTEGER NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_reservations_status ON reservations(status);
            CREATE INDEX IF NOT EXISTS idx_reservations_day ON reservations(day_key);
            CREATE INDEX IF NOT EXISTS idx_reservations_product_day ON reservations(product_id, day_key);
            CREATE INDEX IF NOT EXISTS idx_bot_messages_created_at ON bot_messages(created_at);
            """
        )
        product_columns = {row["name"] for row in db.execute("PRAGMA table_info(products)").fetchall()}
        if "instruction" not in product_columns:
            db.execute("ALTER TABLE products ADD COLUMN instruction TEXT NOT NULL DEFAULT ''")
        now = iso(utc_now())
        for product in PRODUCT_SEED:
            existing = db.execute("SELECT id FROM products WHERE sku=?", (product["sku"],)).fetchone()
            if not existing:
                db.execute(
                    """
                    INSERT INTO products
                    (sku, title, description, instruction, image_url, marketplace_url, daily_limit, is_active, sort_order, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        product["sku"],
                        product["title"],
                        product["description"],
                        product.get("instruction", ""),
                        product["image_url"],
                        product["marketplace_url"],
                        product["daily_limit"],
                        product["sort_order"],
                        now,
                        now,
                    ),
                )
        seed_settings = {
            "instruction": DEFAULT_INSTRUCTION,
            "operator_username": clean_username(OPERATOR_USERNAME),
        }
        for key, value in seed_settings.items():
            row = db.execute("SELECT key FROM settings WHERE key=?", (key,)).fetchone()
            if not row:
                db.execute(
                    "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, now),
                )


def expire_old_reservations(db: sqlite3.Connection) -> None:
    now = iso(utc_now())
    db.execute(
        """
        UPDATE reservations
        SET status='expired'
        WHERE status='reserved' AND expires_at <= ?
        """,
        (now,),
    )


def get_setting(db: sqlite3.Connection, key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    return row["value"] or ""


def set_setting(db: sqlite3.Connection, key: str, value: str) -> None:
    now = iso(utc_now())
    db.execute(
        """
        INSERT INTO settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (key, value, now),
    )


def get_public_settings(db: sqlite3.Connection) -> Dict[str, str]:
    return {
        "instruction": get_setting(db, "instruction", DEFAULT_INSTRUCTION),
        "operator_username": clean_username(get_setting(db, "operator_username", OPERATOR_USERNAME)),
    }


def validate_admin_key(admin_key: str = Query(default="")) -> None:
    if not ADMIN_KEY or ADMIN_KEY == "change-me":
        raise HTTPException(status_code=500, detail="ADMIN_KEY не настроен на сервере")
    if not secrets.compare_digest(admin_key, ADMIN_KEY):
        raise HTTPException(status_code=403, detail="Неверный admin_key")


def parse_telegram_init_data(init_data: str) -> Dict[str, Any]:
    if DEV_MODE and not init_data:
        return {
            "user": {
                "id": 100000001,
                "username": "dev_user",
                "first_name": "Dev",
                "last_name": "User",
            },
            "auth_date": int(time.time()),
        }
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN не настроен")
    if not init_data:
        raise HTTPException(status_code=401, detail="Нет initData Telegram")

    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Нет hash в initData")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail="initData Telegram не прошел проверку")

    auth_date = int(parsed.get("auth_date", "0") or "0")
    if auth_date and time.time() - auth_date > MAX_INIT_DATA_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="initData Telegram устарел")

    if "user" in parsed:
        parsed["user"] = json.loads(parsed["user"])
    return parsed


def get_current_user(x_telegram_init_data: str = Header(default="")) -> Dict[str, Any]:
    data = parse_telegram_init_data(x_telegram_init_data)
    user = data.get("user") or {}
    if not user.get("id"):
        raise HTTPException(status_code=401, detail="Не удалось определить пользователя Telegram")
    return user


def product_to_dict(row: sqlite3.Row, reserved_count: int, completed_count: int) -> Dict[str, Any]:
    active_count = reserved_count + completed_count
    available = max(0, int(row["daily_limit"]) - active_count)
    return {
        "id": row["id"],
        "sku": row["sku"],
        "title": row["title"],
        "description": row["description"],
        "instruction": row["instruction"] if "instruction" in row.keys() else "",
        "image_url": row["image_url"],
        "marketplace_url": row["marketplace_url"],
        "daily_limit": row["daily_limit"],
        "reserved_count": reserved_count,
        "completed_count": completed_count,
        "available": available,
        "is_active": bool(row["is_active"]),
    }


def get_product_counts(db: sqlite3.Connection, product_id: int, day: str) -> Dict[str, int]:
    row = db.execute(
        """
        SELECT
            SUM(CASE WHEN status='reserved' THEN 1 ELSE 0 END) AS reserved_count,
            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed_count
        FROM reservations
        WHERE product_id=? AND day_key=?
        """,
        (product_id, day),
    ).fetchone()
    return {
        "reserved_count": int(row["reserved_count"] or 0),
        "completed_count": int(row["completed_count"] or 0),
    }


def reservation_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "product_id": row["product_id"],
        "sku": row["sku"],
        "title": row["title"],
        "marketplace_url": row["marketplace_url"],
        "instruction": row["instruction"] if "instruction" in row.keys() else "",
        "telegram_id": row["telegram_id"],
        "username": row["username"],
        "first_name": row["first_name"],
        "last_name": row["last_name"],
        "status": row["status"],
        "day_key": row["day_key"],
        "reserved_at": row["reserved_at"],
        "expires_at": row["expires_at"],
        "completed_at": row["completed_at"],
        "cancelled_at": row["cancelled_at"],
    }


def build_confirmation(row: sqlite3.Row, operator_username: str) -> Dict[str, Any]:
    message = (
        f"Здравствуйте! Товар выкуплен. Заявка #{row['id']}. "
        f"Артикул: {row['sku']} - {row['title']}."
    )
    return {
        "title": "Отлично, спасибо!",
        "text": "Выполнение зафиксировано. Теперь продублируйте оператору, что товар выкуплен.",
        "operator_username": operator_username,
        "copy_message": message,
    }


async def set_telegram_menu_button() -> None:
    if not BOT_TOKEN or not WEBAPP_URL:
        return
    payload = {
        "menu_button": {
            "type": "web_app",
            "text": "Открыть выкупы",
            "web_app": {"url": WEBAPP_URL},
        }
    }
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/setChatMenuButton", json=payload)


@asynccontextmanager
async def lifespan(app: FastAPI):
    execute_schema()
    try:
        await set_telegram_menu_button()
    except Exception:
        pass
    yield


app = FastAPI(title="Marketplace Buyout Reservation Mini App", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin")
def admin_page(admin_key: str = Query(default="")) -> FileResponse:
    validate_admin_key(admin_key)
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "time": iso(utc_now()), "local_time": local_now().isoformat(), **daily_reset_meta()}


@app.get("/api/products")
def list_products() -> Dict[str, Any]:
    with get_db() as db:
        expire_old_reservations(db)
        day = today_key()
        rows = db.execute(
            """
            SELECT * FROM products
            WHERE is_active=1
            ORDER BY sort_order ASC, id ASC
            """
        ).fetchall()
        items = []
        for row in rows:
            counts = get_product_counts(db, row["id"], day)
            items.append(product_to_dict(row, counts["reserved_count"], counts["completed_count"]))
        return {"day_key": day, "items": items, "reservation_minutes": RESERVATION_MINUTES, "timezone": APP_TIMEZONE_NAME, "daily_reset_text": "Места обновляются после 00:00"}


@app.get("/api/my-reservation")
def my_reservation(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    with get_db() as db:
        expire_old_reservations(db)
        settings = get_public_settings(db)
        row = db.execute(
            """
            SELECT r.*, p.sku, p.title, p.marketplace_url, p.instruction
            FROM reservations r
            JOIN products p ON p.id = r.product_id
            WHERE r.telegram_id=? AND r.day_key=? AND r.status='reserved'
            ORDER BY r.id DESC
            LIMIT 1
            """,
            (int(user["id"]), today_key()),
        ).fetchone()
        if row:
            settings["instruction"] = row["instruction"] or settings["instruction"]
        return {"reservation": reservation_to_dict(row) if row else None, **settings}


@app.post("/api/reservations")
def create_reservation(
    payload: ReserveRequest,
    request: Request,
    user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    with get_db() as db:
        expire_old_reservations(db)
        settings = get_public_settings(db)
        day = today_key()
        existing = db.execute(
            """
            SELECT r.*, p.sku, p.title, p.marketplace_url, p.instruction
            FROM reservations r
            JOIN products p ON p.id = r.product_id
            WHERE r.telegram_id=? AND r.day_key=? AND r.status='reserved'
            ORDER BY r.id DESC
            LIMIT 1
            """,
            (int(user["id"]), day),
        ).fetchone()
        if existing:
            settings["instruction"] = existing["instruction"] or settings["instruction"]
            return {"reservation": reservation_to_dict(existing), **settings}

        product = db.execute(
            "SELECT * FROM products WHERE id=? AND is_active=1",
            (payload.product_id,),
        ).fetchone()
        if not product:
            raise HTTPException(status_code=404, detail="Артикул не найден или выключен")

        counts = get_product_counts(db, product["id"], day)
        if counts["reserved_count"] + counts["completed_count"] >= int(product["daily_limit"]):
            raise HTTPException(status_code=409, detail="Сегодня по этому артикулу мест больше нет")

        now = utc_now()
        expires_at = now + timedelta(minutes=RESERVATION_MINUTES)
        db.execute(
            """
            INSERT INTO reservations
            (product_id, telegram_id, username, first_name, last_name, status, day_key,
             reserved_at, expires_at, user_agent)
            VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?)
            """,
            (
                product["id"],
                int(user["id"]),
                user.get("username", "") or "",
                user.get("first_name", "") or "",
                user.get("last_name", "") or "",
                day,
                iso(now),
                iso(expires_at),
                request.headers.get("user-agent", ""),
            ),
        )
        reservation_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        row = db.execute(
            """
            SELECT r.*, p.sku, p.title, p.marketplace_url, p.instruction
            FROM reservations r
            JOIN products p ON p.id = r.product_id
            WHERE r.id=?
            """,
            (reservation_id,),
        ).fetchone()
        settings["instruction"] = row["instruction"] or settings["instruction"]
        return {"reservation": reservation_to_dict(row), **settings}


@app.post("/api/reservations/complete")
def complete_reservation(
    payload: CompleteRequest,
    user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    with get_db() as db:
        expire_old_reservations(db)
        row = db.execute(
            """
            SELECT r.*, p.sku, p.title, p.marketplace_url
            FROM reservations r
            JOIN products p ON p.id = r.product_id
            WHERE r.id=? AND r.telegram_id=?
            """,
            (payload.reservation_id, int(user["id"])),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Бронь не найдена")
        if row["status"] != "reserved":
            raise HTTPException(status_code=409, detail=f"Эту бронь уже нельзя завершить: статус {row['status']}")
        now = iso(utc_now())
        db.execute(
            "UPDATE reservations SET status='completed', completed_at=? WHERE id=?",
            (now, payload.reservation_id),
        )
        updated = db.execute(
            """
            SELECT r.*, p.sku, p.title, p.marketplace_url
            FROM reservations r
            JOIN products p ON p.id = r.product_id
            WHERE r.id=?
            """,
            (payload.reservation_id,),
        ).fetchone()
        operator_username = clean_username(get_setting(db, "operator_username", OPERATOR_USERNAME))
        return {
            "reservation": reservation_to_dict(updated),
            "confirmation": build_confirmation(updated, operator_username),
        }


@app.post("/api/reservations/cancel")
def cancel_reservation(
    payload: CompleteRequest,
    user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM reservations WHERE id=? AND telegram_id=?",
            (payload.reservation_id, int(user["id"])),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Бронь не найдена")
        if row["status"] != "reserved":
            raise HTTPException(status_code=409, detail="Эту бронь уже нельзя отменить")
        db.execute(
            "UPDATE reservations SET status='cancelled', cancelled_at=? WHERE id=?",
            (iso(utc_now()), payload.reservation_id),
        )
        return {"ok": True}


@app.get("/api/admin/settings")
def admin_get_settings(_: None = Depends(validate_admin_key)) -> Dict[str, Any]:
    with get_db() as db:
        return get_public_settings(db)


@app.put("/api/admin/settings")
def admin_update_settings(payload: SettingsUpdate, _: None = Depends(validate_admin_key)) -> Dict[str, Any]:
    with get_db() as db:
        set_setting(db, "instruction", payload.instruction.strip() or DEFAULT_INSTRUCTION)
        set_setting(db, "operator_username", clean_username(payload.operator_username))
        return get_public_settings(db)


@app.get("/api/admin/reservations")
def admin_reservations(_: None = Depends(validate_admin_key)) -> Dict[str, Any]:
    with get_db() as db:
        expire_old_reservations(db)
        rows = db.execute(
            """
            SELECT r.*, p.sku, p.title, p.marketplace_url
            FROM reservations r
            JOIN products p ON p.id = r.product_id
            WHERE r.day_key=?
            ORDER BY r.id DESC
            """,
            (today_key(),),
        ).fetchall()
        return {"day_key": today_key(), "timezone": APP_TIMEZONE_NAME, "daily_reset_text": "Места обновляются после 00:00", "items": [reservation_to_dict(row) for row in rows]}


@app.get("/api/admin/products")
def admin_products(_: None = Depends(validate_admin_key)) -> Dict[str, Any]:
    with get_db() as db:
        expire_old_reservations(db)
        rows = db.execute("SELECT * FROM products ORDER BY sort_order ASC, id ASC").fetchall()
        day = today_key()
        items = []
        for row in rows:
            counts = get_product_counts(db, row["id"], day)
            items.append(product_to_dict(row, counts["reserved_count"], counts["completed_count"]))
        return {"items": items, **daily_reset_meta()}


@app.put("/api/admin/products/{product_id}")
def update_product(product_id: int, payload: ProductUpdate, _: None = Depends(validate_admin_key)) -> Dict[str, Any]:
    with get_db() as db:
        existing = db.execute("SELECT id FROM products WHERE id=?", (product_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Артикул не найден")
        db.execute(
            """
            UPDATE products
            SET sku=?, title=?, description=?, instruction=?, image_url=?, marketplace_url=?, daily_limit=?, is_active=?, updated_at=?
            WHERE id=?
            """,
            (
                payload.sku,
                payload.title,
                payload.description,
                payload.instruction,
                payload.image_url,
                payload.marketplace_url,
                payload.daily_limit,
                1 if payload.is_active else 0,
                iso(utc_now()),
                product_id,
            ),
        )
        row = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        counts = get_product_counts(db, product_id, today_key())
        return {"product": product_to_dict(row, counts["reserved_count"], counts["completed_count"])}


def detect_image_extension(upload: UploadFile) -> str:
    suffix = Path(upload.filename or "").suffix.lower()
    content_type = (upload.content_type or "").lower()
    allowed_by_suffix = {".jpg", ".jpeg", ".png", ".webp"}
    if suffix in allowed_by_suffix:
        return suffix
    if content_type == "image/jpeg":
        return ".jpg"
    if content_type == "image/png":
        return ".png"
    if content_type == "image/webp":
        return ".webp"
    raise HTTPException(status_code=400, detail="Можно загрузить только JPG, PNG или WEBP")


@app.post("/api/admin/products/{product_id}/image")
async def upload_product_image(
    product_id: int,
    file: UploadFile = File(...),
    _: None = Depends(validate_admin_key),
) -> Dict[str, Any]:
    ext = detect_image_extension(file)
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Файл пустой")
    if len(content) > MAX_UPLOAD_BYTES:
        max_mb = round(MAX_UPLOAD_BYTES / 1024 / 1024, 1)
        raise HTTPException(status_code=413, detail=f"Фото слишком большое. Максимум {max_mb} МБ")

    with get_db() as db:
        existing = db.execute("SELECT id FROM products WHERE id=?", (product_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Артикул не найден")

        filename = f"product_{product_id}_{int(time.time())}_{secrets.token_hex(4)}{ext}"
        path = UPLOADS_DIR / filename
        path.write_bytes(content)
        image_url = f"/uploads/{filename}"
        db.execute(
            "UPDATE products SET image_url=?, updated_at=? WHERE id=?",
            (image_url, iso(utc_now()), product_id),
        )
        row = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        counts = get_product_counts(db, product_id, today_key())
        return {"image_url": image_url, "product": product_to_dict(row, counts["reserved_count"], counts["completed_count"])}


@app.get("/api/admin/messages")
def admin_messages(_: None = Depends(validate_admin_key)) -> Dict[str, Any]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT * FROM bot_messages
            ORDER BY id DESC
            LIMIT 100
            """
        ).fetchall()
        return {"items": [dict(row) for row in rows]}


@app.post("/api/admin/reset-day")
def reset_day(_: None = Depends(validate_admin_key)) -> Dict[str, Any]:
    with get_db() as db:
        db.execute("DELETE FROM reservations WHERE day_key=?", (today_key(),))
        return {"ok": True, **daily_reset_meta()}


@app.post("/api/admin/expire-now")
def expire_now(_: None = Depends(validate_admin_key)) -> Dict[str, Any]:
    with get_db() as db:
        db.execute("UPDATE reservations SET status='expired' WHERE status='reserved'")
        return {"ok": True}


@app.get("/open")
def open_app() -> RedirectResponse:
    return RedirectResponse(url="/")


WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")


async def telegram_api(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN не настроен")
    async with httpx.AsyncClient(timeout=12) as client:
        response = await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload)
    data = response.json()
    if not data.get("ok"):
        raise HTTPException(status_code=500, detail=f"Telegram API error: {data}")
    return data


async def send_open_app_message(chat_id: int) -> None:
    if not WEBAPP_URL:
        return
    text = (
        "Открой мини-приложение и выбери свободный артикул. "
        f"Бронь держится {RESERVATION_MINUTES} минут. После выполнения нажми «Товар выкуплен»."
    )
    await telegram_api(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "Открыть доступные артикулы", "web_app": {"url": WEBAPP_URL}}
                ]]
            },
        },
    )


def save_bot_message(db: sqlite3.Connection, message: Dict[str, Any]) -> None:
    chat = message.get("chat") or {}
    from_user = message.get("from") or {}
    text = message.get("text") or message.get("caption") or ""
    if not text:
        return
    db.execute(
        """
        INSERT INTO bot_messages
        (telegram_id, username, first_name, last_name, chat_id, text, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(from_user.get("id") or chat.get("id") or 0),
            from_user.get("username", "") or "",
            from_user.get("first_name", "") or "",
            from_user.get("last_name", "") or "",
            int(chat.get("id") or 0),
            text,
            iso(utc_now()),
        ),
    )


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> Dict[str, Any]:
    if WEBHOOK_SECRET:
        received = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not secrets.compare_digest(received, WEBHOOK_SECRET):
            raise HTTPException(status_code=403, detail="Bad Telegram secret token")
    update = await request.json()
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip().lower()
    if message:
        try:
            with get_db() as db:
                save_bot_message(db, message)
        except Exception:
            pass
    if chat_id and (text.startswith("/start") or text in {"выкупы", "бронь", "купить", "старт"} or text):
        try:
            await send_open_app_message(int(chat_id))
        except Exception:
            pass
    return {"ok": True}


@app.post("/api/admin/setup-telegram")
async def setup_telegram(_: None = Depends(validate_admin_key)) -> Dict[str, Any]:
    if not WEBAPP_URL:
        raise HTTPException(status_code=500, detail="WEBAPP_URL не настроен")
    webhook_payload: Dict[str, Any] = {"url": f"{WEBAPP_URL.rstrip('/')}/telegram/webhook"}
    if WEBHOOK_SECRET:
        webhook_payload["secret_token"] = WEBHOOK_SECRET
    webhook = await telegram_api("setWebhook", webhook_payload)
    menu = await telegram_api(
        "setChatMenuButton",
        {
            "menu_button": {
                "type": "web_app",
                "text": "Открыть выкупы",
                "web_app": {"url": WEBAPP_URL},
            }
        },
    )
    return {"ok": True, "webhook": webhook, "menu": menu}
