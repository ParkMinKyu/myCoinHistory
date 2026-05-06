"""업비트 Open API v1 클라이언트.

핵심 설계:
- pyupbit 의존 X (2024-10-02 신 엔드포인트 미지원)
- /v1/orders/closed 의 7일 윈도우 제한을 슬라이딩 윈도우로 우회
- 한 윈도우에서 limit(100) 채워지면 윈도우를 절반으로 좁혀 재귀
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator
from urllib.parse import urlencode

import jwt
import requests

BASE_URL = "https://api.upbit.com"
KST = timezone(timedelta(hours=9))
WINDOW_MAX_DAYS = 7
PAGE_LIMIT = 100
RATE_SLEEP_SEC = 0.15  # 업비트 주문 조회 ~8 req/s 안전 마진


@dataclass(frozen=True)
class UpbitCredentials:
    access_key: str
    secret_key: str


class UpbitClient:
    def __init__(self, creds: UpbitCredentials):
        self._creds = creds

    def _auth_headers(self, query: dict | None = None) -> dict:
        payload = {
            "access_key": self._creds.access_key,
            "nonce": str(uuid.uuid4()),
        }
        if query:
            qs = urlencode(query, doseq=True)
            h = hashlib.sha512()
            h.update(qs.encode())
            payload["query_hash"] = h.hexdigest()
            payload["query_hash_alg"] = "SHA512"
        token = jwt.encode(payload, self._creds.secret_key)
        return {"Authorization": f"Bearer {token}"}

    def _get(self, path: str, query: dict | None = None) -> list | dict:
        url = f"{BASE_URL}{path}"
        headers = self._auth_headers(query)
        resp = requests.get(url, params=query, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_accounts(self) -> list[dict]:
        return self._get("/v1/accounts")  # type: ignore[return-value]

    def get_ticker(self, markets: list[str]) -> list[dict]:
        if not markets:
            return []
        url = f"{BASE_URL}/v1/ticker"
        resp = requests.get(url, params={"markets": ",".join(markets)}, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def fetch_closed_orders_window(
        self,
        start: datetime,
        end: datetime,
        states: tuple[str, ...] = ("done",),
    ) -> list[dict]:
        """단일 윈도우(<=7일) 조회. limit 채워지면 둘로 쪼개 재귀.

        반환 순서는 보장하지 않음. 호출자가 created_at 기준 정렬해야 함.
        """
        if (end - start) > timedelta(days=WINDOW_MAX_DAYS):
            raise ValueError("window must be <= 7 days")

        query: dict = {
            "states[]": list(states),
            "start_time": _iso_with_offset(start),
            "end_time": _iso_with_offset(end),
            "limit": PAGE_LIMIT,
            "order_by": "asc",
        }
        time.sleep(RATE_SLEEP_SEC)
        data = self._get("/v1/orders/closed", query)
        if not isinstance(data, list):
            return []

        if len(data) < PAGE_LIMIT:
            return data

        # 100개 꽉 찼다 = 윈도우 안에 더 있을 수 있음. 절반으로 쪼개기.
        if (end - start) <= timedelta(minutes=2):
            # 더 이상 못 쪼갬: 짧은 구간에 100건 초과 — 그대로 사용
            return data
        mid = start + (end - start) / 2
        left = self.fetch_closed_orders_window(start, mid, states)
        right = self.fetch_closed_orders_window(mid, end, states)
        # 경계 중복 제거
        seen = set()
        merged = []
        for o in left + right:
            u = o.get("uuid")
            if u and u not in seen:
                seen.add(u)
                merged.append(o)
        return merged

    def iter_closed_orders(
        self,
        start_date: datetime,
        end_date: datetime | None = None,
        states: tuple[str, ...] = ("done",),
    ) -> Iterator[dict]:
        """start_date부터 end_date(default=now)까지 7일 윈도우로 슬라이드."""
        end_date = end_date or datetime.now(tz=KST)
        cursor = start_date
        while cursor < end_date:
            win_end = min(cursor + timedelta(days=WINDOW_MAX_DAYS), end_date)
            yield from self.fetch_closed_orders_window(cursor, win_end, states)
            cursor = win_end


def _iso_with_offset(dt: datetime) -> str:
    """업비트는 ISO8601 with offset (e.g. 2024-01-01T00:00:00+09:00)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.strftime("%Y-%m-%dT%H:%M:%S%z")
