"""Thin wrapper around the Devin REST API (v3)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"exit", "error"}
FINISHED_DETAILS = {"finished", "waiting_for_user"}


@dataclass
class SessionResult:
    session_id: str
    url: str
    status: str
    status_detail: str | None
    structured_output: dict[str, Any] | None
    pull_requests: list[dict[str, Any]] = field(default_factory=list)


class DevinAPIError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Devin API error {status_code}: {detail}")


class DevinClient:
    """Minimal client for the Devin v3 Organizations API."""

    def __init__(self, api_key: str, org_id: str, api_base: str = "https://api.devin.ai/v3"):
        self._api_key = api_key
        self._org_id = org_id
        self._base = f"{api_base}/organizations/{org_id}"
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    def _check(self, resp: requests.Response) -> dict[str, Any]:
        if resp.status_code >= 400:
            raise DevinAPIError(resp.status_code, resp.text)
        return resp.json()  # type: ignore[no-any-return]

    def create_session(
        self,
        prompt: str,
        *,
        repos: list[str] | None = None,
        title: str | None = None,
        tags: list[str] | None = None,
        structured_output_schema: dict[str, Any] | None = None,
        structured_output_required: bool = False,
        max_acu_limit: int | None = None,
    ) -> SessionResult:
        body: dict[str, Any] = {"prompt": prompt}
        if repos:
            body["repos"] = repos
        if title:
            body["title"] = title
        if tags:
            body["tags"] = tags
        if structured_output_schema:
            body["structured_output_schema"] = structured_output_schema
            body["structured_output_required"] = structured_output_required
        if max_acu_limit is not None:
            body["max_acu_limit"] = max_acu_limit

        resp = self._session.post(self._url("/sessions"), json=body)
        data = self._check(resp)
        logger.info("Created session %s: %s", data["session_id"], data["url"])
        return self._to_result(data)

    def get_session(self, session_id: str) -> SessionResult:
        resp = self._session.get(self._url(f"/sessions/devin-{session_id}"))
        return self._to_result(self._check(resp))

    def send_message(self, session_id: str, message: str) -> SessionResult:
        resp = self._session.post(
            self._url(f"/sessions/devin-{session_id}/messages"),
            json={"message": message},
        )
        return self._to_result(self._check(resp))

    def poll_until_done(
        self,
        session_id: str,
        *,
        timeout_seconds: int = 3600,
        poll_interval: int = 30,
    ) -> SessionResult:
        """Poll a session until it reaches a terminal state or times out."""
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            result = self.get_session(session_id)
            logger.info(
                "Session %s: status=%s detail=%s",
                session_id,
                result.status,
                result.status_detail,
            )
            if result.status in TERMINAL_STATUSES:
                return result
            if result.status_detail in FINISHED_DETAILS:
                return result
            time.sleep(poll_interval)
        logger.warning("Session %s timed out after %ds", session_id, timeout_seconds)
        return self.get_session(session_id)

    def poll_with_budget(
        self,
        session_id: str,
        *,
        budget_minutes: int = 30,
        wrap_up_buffer_minutes: int = 5,
        poll_interval: int = 30,
    ) -> SessionResult:
        """Poll a session with a time budget, sending a wrap-up message near the deadline."""
        start = time.time()
        budget_seconds = budget_minutes * 60
        wrap_up_at = budget_seconds - (wrap_up_buffer_minutes * 60)
        wrap_up_sent = False

        while True:
            elapsed = time.time() - start
            result = self.get_session(session_id)
            logger.info(
                "Session %s [%.0f/%.0fs]: status=%s detail=%s",
                session_id,
                elapsed,
                budget_seconds,
                result.status,
                result.status_detail,
            )

            if result.status in TERMINAL_STATUSES or result.status_detail in FINISHED_DETAILS:
                return result

            if elapsed >= wrap_up_at and not wrap_up_sent:
                logger.info("Sending wrap-up message to session %s", session_id)
                self.send_message(
                    session_id,
                    f"You have {wrap_up_buffer_minutes} minutes remaining. "
                    "Please wrap up your current task, commit any in-progress work, "
                    "and summarize what you accomplished and what remains.",
                )
                wrap_up_sent = True

            if elapsed >= budget_seconds:
                logger.warning("Session %s hit time budget of %d minutes", session_id, budget_minutes)
                return result

            time.sleep(poll_interval)

    @staticmethod
    def _to_result(data: dict[str, Any]) -> SessionResult:
        return SessionResult(
            session_id=data["session_id"],
            url=data["url"],
            status=data["status"],
            status_detail=data.get("status_detail"),
            structured_output=data.get("structured_output"),
            pull_requests=data.get("pull_requests", []),
        )
