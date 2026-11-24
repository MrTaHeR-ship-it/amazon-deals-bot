"""Minimal Telegram Bot API client (only what the bot needs)."""

from __future__ import annotations

import json

import requests


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str, session: requests.Session | None = None):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.session = session or requests.Session()

    def call(self, method: str, **params):
        payload = {k: v for k, v in params.items() if v is not None}
        for key in ("reply_markup", "link_preview_options"):
            if isinstance(payload.get(key), dict):
                payload[key] = json.dumps(payload[key])
        resp = self.session.post(self.base + method, data=payload, timeout=30)
        try:
            data = resp.json()
        except ValueError as exc:
            raise TelegramError(f"{method}: HTTP {resp.status_code}") from exc
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description')}")
        return data["result"]

    def get_updates(self, offset: int | None):
        return self.call("getUpdates", offset=offset, timeout=0,
                         allowed_updates=json.dumps(["message"]))

    def set_commands(self, commands: list[tuple[str, str]]):
        return self.call("setMyCommands", commands=json.dumps(
            [{"command": c, "description": d} for c, d in commands]))

    def send_text(self, chat_id, text: str, button: tuple[str, str] | None = None,
                  preview: bool = True, html: bool = True):
        return self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML" if html else None,
            link_preview_options={"is_disabled": not preview},
            reply_markup=_button(button),
        )

    def send_photo(self, chat_id, photo: str, caption: str,
                   button: tuple[str, str] | None = None):
        return self.call(
            "sendPhoto",
            chat_id=chat_id,
            photo=photo,
            caption=caption,
            parse_mode="HTML",
            reply_markup=_button(button),
        )


def _button(button):
    """One button (label, url) or a list of them, shown side by side."""
    if not button:
        return None
    buttons = [button] if isinstance(button, tuple) else list(button)
    return {"inline_keyboard": [[{"text": label, "url": url} for label, url in buttons]]}


class DryTelegram:
    """Prints instead of sending. Used by --dry-run and by the tests."""

    def __init__(self, updates: list[dict] | None = None):
        self.updates = list(updates or [])
        self.sent: list[dict] = []

    def get_updates(self, offset):
        return [u for u in self.updates if offset is None or u["update_id"] >= offset]

    def set_commands(self, commands):
        return True

    def send_text(self, chat_id, text, button=None, preview=True, html=True):
        self.sent.append({"chat": chat_id, "text": text, "button": _first(button),
                          "buttons": button, "preview": preview})
        print(f"\n--- to {chat_id} ---\n{text}\n[button: {button}]")
        return {"message_id": len(self.sent)}

    def send_photo(self, chat_id, photo, caption, button=None):
        self.sent.append({"chat": chat_id, "photo": photo, "text": caption,
                          "button": _first(button), "buttons": button})
        print(f"\n--- photo to {chat_id}: {photo} ---\n{caption}\n[button: {button}]")
        return {"message_id": len(self.sent)}


def _first(button):
    if not button or isinstance(button, tuple):
        return button
    return button[0]
