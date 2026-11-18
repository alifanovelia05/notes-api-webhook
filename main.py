from dotenv import load_dotenv
load_dotenv()

import os
import logging
from typing import List, Literal, Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, EmailStr, field_validator
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Central Ticket Intake Hub & Webhook Dispatcher")


# ==============================================================================
# ZONA 1: PYDANTIC MODELS & VALIDATORS (Gatekeeper & Data Cleaning)
# ==============================================================================
PriorityType = Literal["low", "normal", "high"]
StatusType = Literal["open", "in_progress", "resolved"]


class TicketIn(BaseModel):
    sender_name: str
    sender_email: EmailStr
    source: str = "web_form"
    priority: PriorityType = "normal"
    message: str

    @field_validator("sender_name", "message")
    @classmethod
    def must_not_be_blank(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Field tidak boleh kosong atau hanya berisi spasi")
        return cleaned

    @field_validator("source")
    @classmethod
    def normalize_source(cls, v: str) -> str:
        cleaned = v.strip().lower()
        return cleaned if cleaned else "web_form"

    @field_validator("priority", mode="before")
    @classmethod
    def normalize_priority(cls, v):
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("sender_email", mode="before")
    @classmethod
    def normalize_email(cls, v):
        if isinstance(v, str):
            return v.strip().lower()
        return v


class TicketUpdate(BaseModel):
    priority: Optional[PriorityType] = None
    status: Optional[StatusType] = None

    @field_validator("priority", "status", mode="before")
    @classmethod
    def normalize_enum_fields(cls, v):
        if isinstance(v, str):
            return v.strip().lower()
        return v


class Ticket(BaseModel):
    id: str
    sender_name: str
    sender_email: str
    source: str
    priority: PriorityType
    status: StatusType
    message: str
    created_at: str


# ==============================================================================
# ZONA 2: ASYNC WEBHOOK DISPATCHER (Non-blocking Event Emitter)
# ==============================================================================
def get_webhook_url() -> str | None:
    url = os.getenv("WEBHOOK_URL", "").strip()
    return url if url else None


async def send_webhook(event_name: str, ticket: dict) -> None:
    url = get_webhook_url()
    if not url:
        logger.info("WEBHOOK_URL tidak diset, lewati pengiriman webhook.")
        return
    secret = os.getenv("WEBHOOK_SECRET", "").strip()
    headers = {"X-Webhook-Secret": secret} if secret else {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json={"event": event_name, "data": ticket},
                headers=headers,
            )
            resp.raise_for_status()
        logger.info("Webhook [%s] terkirim ke %s (status %s)", event_name, url, resp.status_code)
    except Exception as e:
        logger.warning("Gagal kirim webhook: %s", e)


# ==============================================================================
# ZONA 3: CRUD ENDPOINTS (/tickets)
# ==============================================================================
@app.get("/tickets", response_model=List[Ticket])
def list_tickets():
    response = supabase.table("tickets").select("*").order("id").execute()
    return [format_ticket(row) for row in response.data]


@app.post("/tickets", status_code=201, response_model=Ticket)
async def create_ticket(body: TicketIn):
    payload = {
        "sender_name": body.sender_name,
        "sender_email": str(body.sender_email),
        "source": body.source,
        "priority": body.priority,
        "status": "open",
        "message": body.message,
    }
    response = supabase.table("tickets").insert(payload).execute()

    if not response.data:
        raise HTTPException(status_code=500, detail="Gagal menyimpan tiket ke database")

    ticket = format_ticket(response.data[0])
    await send_webhook("ticket.created", ticket)
    return ticket


@app.patch("/tickets/{ticket_id}", response_model=Ticket)
async def update_ticket(ticket_id: str, body: TicketUpdate):
    if not ticket_id.isdigit():
        raise HTTPException(status_code=400, detail="Invalid ticket ID format")

    payload = body.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(status_code=400, detail="Minimal satu field (priority atau status) harus diisi")

    response = (
        supabase.table("tickets")
        .update(payload)
        .eq("id", int(ticket_id))
        .execute()
    )

    if not response.data:
        raise HTTPException(status_code=404, detail="Ticket not found")

    ticket = format_ticket(response.data[0])
    await send_webhook("ticket.updated", ticket)
    return ticket


@app.delete("/tickets/{ticket_id}")
async def delete_ticket(ticket_id: str):
    if not ticket_id.isdigit():
        raise HTTPException(status_code=400, detail="Invalid ticket ID format")

    response = (
        supabase.table("tickets")
        .delete()
        .eq("id", int(ticket_id))
        .execute()
    )

    if not response.data:
        raise HTTPException(status_code=404, detail="Ticket not found")

    deleted_ticket = format_ticket(response.data[0])
    await send_webhook("ticket.deleted", deleted_ticket)
    return {"message": "Ticket deleted", "data": deleted_ticket}


# ==============================================================================
# ZONA 4: SUPABASE CLIENT & FORMATTER (Single Source of Truth)
# ==============================================================================
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL dan SUPABASE_KEY wajib diisi di file .env")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def format_ticket(row: dict) -> dict:
    """Menyamakan format output dari Supabase agar konsisten."""
    return {
        "id": str(row["id"]),
        "sender_name": row["sender_name"],
        "sender_email": row["sender_email"],
        "source": row.get("source") or "web_form",
        "priority": row.get("priority") or "normal",
        "status": row.get("status") or "open",
        "message": row["message"],
        "created_at": str(row.get("created_at") or ""),
    }