"""Outbound-only Telegram Bot API long polling client (INV-02, INV-04, INV-19, ADR-003)."""

import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any

from agent_factory.config import (
    normalize_telegram_notification_level,
    parse_telegram_notification_level,
)
from agent_factory.notifications import (
    format_decision_notification,
    format_finished_notification,
    format_needs_attention_notification,
    format_progress_notification,
)
from agent_factory.ports.notifier import (
    HttpResponse,
    HttpTransport,
    InlineButton,
    NotificationCategory,
    SentMessage,
    TelegramTransportError,
    TelegramUpdate,
    normalize_notification_category,
    resolve_notification_delivery,
)
from agent_factory.secrets import redact_secrets


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Disable urllib implicit redirects so callers handle 301/302/307/308 without method/auth loss."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        _ = (req, fp, code, msg, headers, newurl)
        return None


_DEFAULT_URLOPEN = urllib.request.urlopen
_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _extract_response_headers(
    raw_headers: Any,
    extra_secrets: Sequence[str],
) -> dict[str, str]:
    if raw_headers is None or not hasattr(raw_headers, "items"):
        return {}
    clean: dict[str, str] = {}
    for k, v in raw_headers.items():
        if v is not None:
            clean[str(k)] = redact_secrets(str(v), extra_secrets=extra_secrets)
    return clean


class UrllibHttpTransport:
    """Standard-library outbound HTTP JSON transport (no server sockets, no extra deps)."""

    supports_explicit_redirects: bool = True

    def request_json(
        self,
        method: str,
        url: str,
        payload: Mapping[str, Any] | None = None,
        timeout_seconds: float = 35.0,
        headers: Mapping[str, str] | None = None,
        extra_secrets: Sequence[str] = (),
        *,
        raise_on_http_error: bool = False,
        follow_redirects: bool = True,
    ) -> HttpResponse:
        """Execute outbound HTTPS JSON request using urllib.request."""
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req_headers = {
            "Accept": "application/json",
        }
        if data is not None:
            req_headers["Content-Type"] = "application/json"
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(
            url,
            data=data,
            headers=req_headers,
            method=method.upper(),
        )
        extra: list[str] = list(extra_secrets)
        if headers:
            for v in headers.values():
                if v:
                    extra.append(v)
                    if v.startswith("Bearer "):
                        extra.append(v.removeprefix("Bearer ").strip())

        err_to_raise: TelegramTransportError | None = None
        use_no_redirect = (not follow_redirects) and (urllib.request.urlopen is _DEFAULT_URLOPEN)
        try:
            open_cm = (
                _NO_REDIRECT_OPENER.open(req, timeout=timeout_seconds)
                if use_no_redirect
                else urllib.request.urlopen(req, timeout=timeout_seconds)
            )
            with open_cm as resp:
                body = resp.read().decode("utf-8")
                resp_headers = _extract_response_headers(getattr(resp, "headers", None), extra)
                geturl_fn = getattr(resp, "geturl", None)
                final_url = str(geturl_fn()) if callable(geturl_fn) else url
                return HttpResponse(
                    status_code=resp.status,
                    body=body,
                    headers=resp_headers,
                    final_url=final_url,
                )
        except urllib.error.HTTPError as err:
            err_headers = _extract_response_headers(getattr(err, "headers", None), extra)
            geturl_fn = getattr(err, "geturl", None)
            err_final_url = str(geturl_fn()) if callable(geturl_fn) else url
            if not raise_on_http_error:
                try:
                    raw_body = (
                        err.read().decode("utf-8", errors="replace")
                        if err.fp is not None
                        else str(err)
                    )
                except OSError:
                    raw_body = str(err)
                finally:
                    err.close()
                return HttpResponse(
                    status_code=int(err.code),
                    body=redact_secrets(raw_body, extra_secrets=extra),
                    headers=err_headers,
                    final_url=err_final_url,
                )
            clean_msg = redact_secrets(
                f"HTTP transport failed for {url}: {err}",
                extra_secrets=extra,
            )
            err.close()
            err_to_raise = TelegramTransportError(clean_msg)
        except (urllib.error.URLError, http.client.HTTPException, OSError) as err:
            clean_msg = redact_secrets(
                f"HTTP transport failed for {url}: {err}",
                extra_secrets=extra,
            )
            err_to_raise = TelegramTransportError(clean_msg)

        if err_to_raise is not None:
            err_to_raise.__cause__ = None
            err_to_raise.__context__ = None
            raise err_to_raise
        raise AssertionError("unreachable")

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        timeout_seconds: float = 35.0,
        headers: Mapping[str, str] | None = None,
        extra_secrets: Sequence[str] = (),
    ) -> HttpResponse:
        """Execute outbound HTTPS POST using urllib.request."""
        return self.request_json(
            "POST",
            url,
            payload=payload,
            timeout_seconds=timeout_seconds,
            headers=headers,
            extra_secrets=extra_secrets,
            raise_on_http_error=True,
        )


class TelegramHttpClient:
    """Telegram Bot API adapter using outbound getUpdates/sendMessage only (no webhooks, INV-04)."""

    def __init__(
        self,
        bot_token: str,
        transport: HttpTransport | None = None,
        api_base_url: str = "https://api.telegram.org",
        notification_level: str | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.transport: HttpTransport = (
            transport if transport is not None else UrllibHttpTransport()
        )
        self.api_base_url = api_base_url.rstrip("/")
        self.notification_level: str | None = (
            normalize_telegram_notification_level(notification_level)
            if notification_level is not None
            else None
        )

    def effective_notification_level(self, override: str | None = None) -> str:
        """Resolve active notification level from explicit override, instance config, or env."""
        if override is not None:
            return normalize_telegram_notification_level(override)
        if self.notification_level is not None:
            return self.notification_level
        return parse_telegram_notification_level()

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
        parse_mode: str | None = None,
        disable_notification: bool = False,
        *,
        category: NotificationCategory | str | None = None,
        notification_level: str | None = None,
    ) -> SentMessage:
        """Call Telegram sendMessage API with optional inline keyboard, disable_notification, and level policy."""
        norm_category: NotificationCategory | None = None
        effective_disable = bool(disable_notification)
        if category is not None:
            norm_category = normalize_notification_category(category)
            should_send, policy_disable = resolve_notification_delivery(
                self.effective_notification_level(notification_level),
                norm_category,
            )
            if not should_send:
                return SentMessage(
                    message_id=0,
                    chat_id=chat_id,
                    text=text,
                    buttons=[list(r) for r in (buttons or [])],
                    parse_mode=parse_mode,
                    disable_notification=True,
                    skipped=True,
                    category=norm_category,
                )
            effective_disable = bool(disable_notification or policy_disable)

        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_notification": effective_disable,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
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
            parse_mode=parse_mode,
            disable_notification=effective_disable,
            skipped=False,
            category=norm_category,
        )

    def send_notification(
        self,
        chat_id: int,
        text: str,
        *,
        category: NotificationCategory | str = NotificationCategory.PROGRESS,
        buttons: Sequence[Sequence[InlineButton]] | None = None,
        parse_mode: str | None = None,
        notification_level: str | None = None,
    ) -> SentMessage | None:
        """Send an automatic categorized notification applying level policy (returns None when suppressed)."""
        msg = self.send_message(
            chat_id=chat_id,
            text=text,
            buttons=buttons,
            parse_mode=parse_mode,
            category=category,
            notification_level=notification_level,
        )
        return None if msg.skipped else msg

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


__all__ = [
    "NotificationCategory",
    "TelegramHttpClient",
    "UrllibHttpTransport",
    "format_decision_notification",
    "format_finished_notification",
    "format_needs_attention_notification",
    "format_progress_notification",
    "resolve_notification_delivery",
]
