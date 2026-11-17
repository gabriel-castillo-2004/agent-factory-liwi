"""Outbound-only Telegram Bot API long polling client (INV-02, INV-04, INV-19, ADR-003)."""

import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any

from agent_factory.ports.notifier import (
    HttpResponse,
    HttpTransport,
    InlineButton,
    SentMessage,
    TelegramTransportError,
    TelegramUpdate,
)
from agent_factory.secrets import redact_secrets


class UrllibHttpTransport:
    """Standard-library outbound HTTP JSON POST transport (no server sockets, no extra deps)."""

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        timeout_seconds: float = 35.0,
        headers: Mapping[str, str] | None = None,
        extra_secrets: Sequence[str] = (),
    ) -> HttpResponse:
        """Execute outbound HTTPS POST using urllib.request."""
        data = json.dumps(payload).encode("utf-8")
        req_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(
            url,
            data=data,
            headers=req_headers,
            method="POST",
        )
        extra: list[str] = list(extra_secrets)
        if headers:
            for v in headers.values():
                if v:
                    extra.append(v)
                    if v.startswith("Bearer "):
                        extra.append(v.removeprefix("Bearer ").strip())

        err_to_raise: TelegramTransportError | None = None
        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                body = resp.read().decode("utf-8")
                return HttpResponse(status_code=resp.status, body=body)
        except (urllib.error.HTTPError, urllib.error.URLError, http.client.HTTPException, OSError) as err:
            clean_msg = redact_secrets(
                f"HTTP transport failed for {url}: {err}",
                extra_secrets=extra,
            )
            err_to_raise = TelegramTransportError(clean_msg)

        if err_to_raise is not None:
            err_to_raise.__cause__ = None
            err_to_raise.__context__ = None
            raise err_to_raise


class TelegramHttpClient:
    """Telegram Bot API adapter using outbound getUpdates/sendMessage only (no webhooks, INV-04)."""

    def __init__(
        self,
        bot_token: str,
        transport: HttpTransport | None = None,
        api_base_url: str = "https://api.telegram.org",
    ) -> None:
        self.bot_token = bot_token
        self.transport: HttpTransport = (
            transport if transport is not None else UrllibHttpTransport()
        )
        self.api_base_url = api_base_url.rstrip("/")

    def _scrub(self, text: str) -> str:
        """Redact bot_token and other credentials from diagnostic text."""
        return redact_secrets(text, extra_secrets=(self.bot_token,))

    def _post(self, method: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Send an outbound POST to Telegram API and validate response, redacting secrets on failure."""
        url = f"{self.api_base_url}/bot{self.bot_token}/{method}"
        transport_error: TelegramTransportError | None = None
        resp: HttpResponse | None = None
        try:
            resp = self.transport.post_json(url, payload)
        except (http.client.HTTPException, OSError, RuntimeError, ValueError) as exc:
            clean_err = self._scrub(str(exc))
            transport_error = TelegramTransportError(
                f"Telegram transport failure for {method}: {clean_err}"
            )
        # Raise outside except block so __context__ / __cause__ never retain raw exception/token
        if transport_error is not None:
            raise transport_error
        assert resp is not None

        if resp.status_code != 200:
            clean_body = self._scrub(resp.body)
            raise TelegramTransportError(
                f"Telegram API HTTP {resp.status_code} for {method}: {clean_body}"
            )

        parse_error: TelegramTransportError | None = None
        data: Any = None
        try:
            data = json.loads(resp.body)
        except (json.JSONDecodeError, ValueError) as exc:
            clean_parse_err = self._scrub(str(exc))
            parse_error = TelegramTransportError(
                f"Invalid JSON response from Telegram for {method}: {clean_parse_err}"
            )
        if parse_error is not None:
            raise parse_error

        if not isinstance(data, dict) or not data.get("ok"):
            desc = (
                data.get("description", "Unknown error")
                if isinstance(data, dict)
                else "Non-dict response"
            )
            clean_desc = self._scrub(str(desc))
            raise TelegramTransportError(f"Telegram API error for {method}: {clean_desc}")

        return data

    def get_updates(
        self,
        offset: int | None = None,
        timeout: int = 30,
    ) -> list[TelegramUpdate]:
        """Call Telegram getUpdates API via outbound HttpTransport."""
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset

        data = self._post("getUpdates", payload)
        raw_updates = data.get("result", [])
        if not isinstance(raw_updates, list):
            return []

        updates: list[TelegramUpdate] = []
        for item in raw_updates:
            if not isinstance(item, dict):
                continue
            update_id = int(item.get("update_id") or 0)
            chat_id = 0
            user_id = 0
            text = ""
            callback_data: str | None = None
            callback_query_id: str | None = None
            username = ""

            if isinstance(item.get("callback_query"), dict):
                cb = item["callback_query"]
                callback_query_id = cb.get("id")
                callback_data = cb.get("data")
                sender = cb.get("from")
                if isinstance(sender, dict):
                    user_id = int(sender.get("id") or 0)
                    username = str(sender.get("username") or "")
                msg = cb.get("message")
                if isinstance(msg, dict) and isinstance(msg.get("chat"), dict):
                    chat_id = int(msg["chat"].get("id") or 0)
            elif isinstance(item.get("message"), dict):
                msg = item["message"]
                sender = msg.get("from")
                if isinstance(sender, dict):
                    user_id = int(sender.get("id") or 0)
                    username = str(sender.get("username") or "")
                chat = msg.get("chat")
                if isinstance(chat, dict):
                    chat_id = int(chat.get("id") or 0)
                text = str(msg.get("text") or "")

            updates.append(
                TelegramUpdate(
                    update_id=update_id,
                    chat_id=chat_id,
                    user_id=user_id,
                    text=text,
                    callback_data=callback_data,
                    callback_query_id=callback_query_id,
                    username=username,
                )
            )
        return updates

    def send_message(
        self,
        chat_id: int,
        text: str,
        buttons: Sequence[Sequence[InlineButton]] | None = None,
    ) -> SentMessage:
        """Call Telegram sendMessage API with optional inline keyboard."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        if buttons:
            inline_keyboard = [
                [{"text": btn.text, "callback_data": btn.callback_data} for btn in row]
                for row in buttons
            ]
            payload["reply_markup"] = {"inline_keyboard": inline_keyboard}

        data = self._post("sendMessage", payload)
        res = data.get("result", {})
        msg_id = int(res.get("message_id") or 0) if isinstance(res, dict) else 0
        resp_chat_id = chat_id
        if isinstance(res, dict) and isinstance(res.get("chat"), dict):
            resp_chat_id = int(res["chat"].get("id") or chat_id)

        return SentMessage(
            message_id=msg_id,
            chat_id=resp_chat_id,
            text=text,
            buttons=[list(r) for r in (buttons or [])],
        )

    def answer_callback_query(
        self,
        callback_query_id: str,
        text: str = "",
    ) -> None:
        """Call Telegram answerCallbackQuery API."""
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self._post("answerCallbackQuery", payload)
