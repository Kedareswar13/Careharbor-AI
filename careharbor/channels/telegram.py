"""Optional Telegram channel (long polling). Starts only when TELEGRAM_BOT_TOKEN is set."""

from __future__ import annotations

import asyncio
import logging

import httpx

from careharbor.agent.runtime import ChatService

log = logging.getLogger("careharbor.telegram")


async def run_telegram(svc: ChatService, token: str) -> None:
    base = f"https://api.telegram.org/bot{token}"
    offset = 0
    async with httpx.AsyncClient(timeout=40) as client:
        while True:
            try:
                r = await client.get(f"{base}/getUpdates", params={"timeout": 30, "offset": offset})
                for upd in r.json().get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    text, chat_id = msg.get("text"), (msg.get("chat") or {}).get("id")
                    if not text or chat_id is None:
                        continue
                    sid = f"tg-{chat_id}"
                    if await svc.pending_confirmation(sid) and text.strip().lower() in {"/confirm", "/cancel", "/change"}:
                        res = await svc.resume(sid, text.strip().lower()[1:])
                    else:
                        res = await svc.turn(sid, text)
                    reply = res["reply"] + ("\n\nReply /confirm, /change or /cancel." if res.get("awaiting_confirmation") else "")
                    await client.post(f"{base}/sendMessage", json={"chat_id": chat_id, "text": reply})
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep polling through transient network errors
                log.warning("telegram polling error: %s", exc.__class__.__name__)
                await asyncio.sleep(5)
