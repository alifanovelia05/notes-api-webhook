from dotenv import load_dotenv
load_dotenv()

import os
import logging
from typing import List

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Notes API + Webhook Integration")


# ==============================================================================
# ZONA 4: GUDANG PENYIMPANAN (Storage - Supabase PostgreSQL)
# ==============================================================================
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL dan SUPABASE_KEY wajib diisi di file .env")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def format_note(row: dict) -> dict:
    """Menyamakan format output dari Supabase agar id konsisten berupa string."""
    return {
        "id": str(row["id"]),
        "title": row["title"],
        "content": row.get("content") or "",
    }

class NoteIn(BaseModel):
    title: str
    content: str = ""


class Note(NoteIn):
    id: str

def get_webhook_url() -> str | None:
    url = os.getenv("WEBHOOK_URL", "").strip()
    return url if url else None


async def send_webhook(event_name: str, note: dict) -> None:
    url = get_webhook_url()
    if not url:
        logger.info("WEBHOOK_URL tidak diset, lewati pengiriman webhook.")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json={"event": event_name, "data": note})
            resp.raise_for_status()
        logger.info("Webhook [%s] terkirim ke %s (status %s)", event_name, url, resp.status_code)
    except Exception as e:
        logger.warning("Gagal kirim webhook: %s", e)

@app.get("/notes", response_model=List[Note])
def list_notes():
    response = supabase.table("notes").select("*").order("id").execute()
    return [format_note(row) for row in response.data]


@app.post("/notes", status_code=201, response_model=Note)
async def create_note(body: NoteIn):
    if not body.title or not body.title.strip():
        raise HTTPException(status_code=400, detail="title is required")

    payload = {
        "title": body.title.strip(),
        "content": body.content or "",
    }
    response = supabase.table("notes").insert(payload).execute()

    if not response.data:
        raise HTTPException(status_code=500, detail="Gagal menyimpan note ke database")

    note = format_note(response.data[0])
    await send_webhook("note.created", note)
    return note


@app.put("/notes/{note_id}", response_model=Note)
async def update_note(note_id: str, body: NoteIn):
    if not body.title or not body.title.strip():
        raise HTTPException(status_code=400, detail="title is required")

    if not note_id.isdigit():
        raise HTTPException(status_code=400, detail="Invalid note ID format")

    payload = {
        "title": body.title.strip(),
        "content": body.content or "",
    }
    response = (
        supabase.table("notes")
        .update(payload)
        .eq("id", int(note_id))
        .execute()
    )

    if not response.data:
        raise HTTPException(status_code=404, detail="Note not found")

    note = format_note(response.data[0])
    await send_webhook("note.updated", note)
    return note


@app.delete("/notes/{note_id}")
async def delete_note(note_id: str):
    if not note_id.isdigit():
        raise HTTPException(status_code=400, detail="Invalid note ID format")

    response = (
        supabase.table("notes")
        .delete()
        .eq("id", int(note_id))
        .execute()
    )

    if not response.data:
        raise HTTPException(status_code=404, detail="Note not found")

    deleted_note = format_note(response.data[0])
    await send_webhook("note.deleted", deleted_note)
    return {"message": "Note deleted", "data": deleted_note}