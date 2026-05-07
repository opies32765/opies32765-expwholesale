"""cookie_jar.py — vAuto session state for direct BFF API calls.

Stores cookies + entity-scoped headers in a JSON file. Production should
refresh via Playwright login (or by lifting from a captured warmer
template) on a daily cron + on 401 detection.

Schema of the session JSON file:
    {
        "captured_at": "2026-05-07T08:35:34",
        "cookies": [{"name": "...", "value": "...", "domain": "...", ...}, ...],
        "headers": {
            "platformuserid": "...",
            "appraisalentityid": "...",
            "currententityid": "...",
            "accept": "application/json",
            "content-type": "application/json",
            "referer": "https://provision.vauto.app.coxautoinc.com/",
            "user-agent": "Mozilla/5.0 ...",
            ...
        }
    }
"""
from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Any


class CookieJar:
    """Loads + serves vAuto session state. Future: refresh() runs login."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: dict | None = None

    def load(self) -> dict:
        if not self.path.exists():
            raise FileNotFoundError(f'No session file at {self.path}')
        with open(self.path, encoding='utf-8') as fp:
            self._data = json.load(fp)
        return self._data

    def save(self, data: dict) -> None:
        with open(self.path, 'w', encoding='utf-8') as fp:
            json.dump(data, fp, indent=2, ensure_ascii=False)
        self._data = data

    @property
    def data(self) -> dict:
        if self._data is None:
            self.load()
        return self._data  # type: ignore[return-value]

    def get_cookies(self) -> dict[str, str]:
        return {c['name']: c['value'] for c in self.data.get('cookies', [])}

    def get_headers(self) -> dict[str, str]:
        return dict(self.data.get('headers', {}))

    def captured_at(self) -> str | None:
        return self.data.get('captured_at')

    def age_seconds(self) -> float:
        ts = self.captured_at()
        if not ts:
            return float('inf')
        try:
            captured_dt = time.strptime(ts, '%Y-%m-%dT%H:%M:%S')
            return time.time() - time.mktime(captured_dt)
        except ValueError:
            return float('inf')

    def get_session_appraisal_id(self) -> str | None:
        """Returns a known-valid appraisalId from when the session was
        captured. priceGuides requires a real appraisalId (any one from the
        user's account works — verified via cross-bid replay), so we keep
        one around to satisfy that endpoint."""
        return self.data.get('session_appraisal_id')

    @classmethod
    def from_warmer_template(cls, template_path: str | Path,
                             dest_path: str | Path) -> 'CookieJar':
        """One-shot: convert a warmer's request-template JSON into a
        cookie-jar session file. Use this once after running prewarmer.py
        against any bid — the resulting session is reusable across all bids
        until cookies expire (~12-24h)."""
        with open(template_path, encoding='utf-8') as fp:
            tpl = json.load(fp)
        # Extract appraisalId from the captured payload — needed for
        # priceGuides (rbook tolerates 'unused' but priceGuides 500s on it).
        session_appraisal_id = None
        try:
            payload_str = (tpl.get('request') or {}).get('post_data')
            if payload_str:
                payload = json.loads(payload_str)
                session_appraisal_id = payload.get('appraisalId')
        except Exception:
            pass
        session = {
            'captured_at': tpl.get('captured_at') or
                           time.strftime('%Y-%m-%dT%H:%M:%S'),
            'cookies': tpl.get('cookies', []),
            'headers': tpl.get('request', {}).get('headers', {}),
            'session_appraisal_id': session_appraisal_id,
        }
        jar = cls(dest_path)
        jar.save(session)
        return jar

    def refresh(self) -> None:
        """Refresh cookies by running a fresh Playwright login.

        Not implemented in v1 — for now refresh manually:
            1. Run prewarmer.py against any bid
            2. Run CookieJar.from_warmer_template(<template>, <session_path>)
        Future: this method will spawn a headless Chrome login automatically
        (~30s) using stored Cox credentials.
        """
        raise NotImplementedError(
            'Refresh not implemented yet — re-run prewarmer.py and use '
            'from_warmer_template(). Auto-refresh planned for v2.'
        )
