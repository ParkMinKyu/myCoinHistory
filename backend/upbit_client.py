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
from urllib.parse import unquote, urlencode

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

    def _auth_headers(self, query_string: str | None = None) -> dict:
        payload = {
            "access_key": self._creds.access_key,
            "nonce": str(uuid.uuid4()),
        }
        if query_string:
            # 업비트 공식 규칙: unquote된 평문 query_string에 SHA512.
            # (urlencode 결과를 그대로 해싱하면 +,: 인코딩 차이로 query 검증 실패)
            h = hashlib.sha512()
            h.update(query_string.encode("utf-8"))
            payload["query_hash"] = h.hexdigest()
            payload["query_hash_alg"] = "SHA512"
        token = jwt.encode(payload, self._creds.secret_key, algorithm="HS512")
        return {"Authorization": f"Bearer {token}"}

    def _get(self, path: str, query: dict | None = None) -> list | dict:
        # 공식 레시피와 동일: query_string을 직접 만들어 해싱하고 URL에 그대로 붙인다.
        # requests의 params= 자동 인코딩에 맡기면 해시 대상과 실제 전송이 어긋난다.
        query_string = unquote(urlencode(query, doseq=True)) if query else None
        url = f"{BASE_URL}{path}"
        if query_string:
            url = f"{url}?{query_string}"
        headers = self._auth_headers(query_string)
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_accounts(self) -> list[dict]:
        return self._get("/v1/accounts")  # type: ignore[return-value]

    def _get_transfers(self, kind: str, currency: str | None = None) -> list[dict]:
        """입금('deposits')/출금('withdraws') 전체를 페이지로 모아 반환.

        currency 지정 시 그 통화만, 없으면 전체 통화. (한 페이지 100건)
        """
        assert kind in ("deposits", "withdraws")
        out: list[dict] = []
        page = 1
        while True:
            query: dict = {"limit": 100, "page": page, "order_by": "asc"}
            if currency:
                query["currency"] = currency
            time.sleep(RATE_SLEEP_SEC)
            data = self._get(f"/v1/{kind}", query)
            if not isinstance(data, list) or not data:
                break
            out.extend(data)
            if len(data) < 100:
                break
            page += 1
        return out

    def get_krw_transfers(self, kind: str) -> list[dict]:
        """KRW 입금/출금 전체."""
        return self._get_transfers(kind, currency="KRW")

    def get_transfer_markets(self) -> set[str]:
        """외부 입출금(코인 전송)이 한 번이라도 있는 마켓 집합. 예: {"KRW-XRP"}.

        KRW 입출금은 제외(현금이므로). 완료된 건(ACCEPTED/DONE)만 본다.
        이 코인들은 거래소 밖 처분이 섞여 평단법 손익이 무의미하다.
        """
        markets: set[str] = set()
        for kind in ("deposits", "withdraws"):
            for x in self._get_transfers(kind):
                cur = x.get("currency")
                state = (x.get("state") or "").upper()
                if cur and cur != "KRW" and state in ("ACCEPTED", "DONE"):
                    markets.add(f"KRW-{cur}")
        return markets

    def get_coin_transfers(self, kind: str) -> list[dict]:
        """코인 입금/출금 내역 (KRW 제외, 완료 건만).

        반환: [{currency, amount, created_at}, ...]
        """
        out = []
        for x in self._get_transfers(kind):
            cur = x.get("currency")
            state = (x.get("state") or "").upper()
            if not cur or cur == "KRW" or state not in ("ACCEPTED", "DONE"):
                continue
            try:
                amt = float(x.get("amount") or 0)
                fee = float(x.get("fee") or 0)
            except (TypeError, ValueError):
                continue
            if amt <= 0:
                continue
            out.append(
                {"currency": cur, "amount": amt, "fee": fee, "created_at": x.get("created_at")}
            )
        return out

    def get_ticker(self, markets: list[str]) -> list[dict]:
        if not markets:
            return []
        url = f"{BASE_URL}/v1/ticker"
        resp = requests.get(url, params={"markets": ",".join(markets)}, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_active_markets(self) -> set[str]:
        """현재 업비트에 상장된 마켓 코드 집합 (인증 불필요).

        여기 없는데 거래내역엔 있는 코인 = 상폐된 것.
        """
        resp = requests.get(f"{BASE_URL}/v1/market/all", timeout=10)
        resp.raise_for_status()
        return {m["market"] for m in resp.json()}

    def get_daily_close(self, market: str, on_iso: str) -> float | None:
        """on_iso(거래 일시)가 속한 날의 일봉 종가. 시점 시세 평가용. (인증 불필요)

        rate limit(429)/일시 오류는 재시도. 상장전·없는 마켓이면 None.
        """
        try:
            dt = datetime.fromisoformat(on_iso).astimezone(KST)
        except (ValueError, TypeError):
            return None
        to = (dt + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00+09:00")
        for attempt in range(4):
            try:
                resp = requests.get(
                    f"{BASE_URL}/v1/candles/days",
                    params={"market": market, "count": 1, "to": to},
                    timeout=15,
                )
                if resp.status_code == 429:  # rate limit — 대기 후 재시도
                    time.sleep(0.5 * (attempt + 1))
                    continue
                if not resp.ok:
                    return None  # 404 등 = 없는 마켓
                data = resp.json()
                return float(data[0]["trade_price"]) if data else None
            except requests.RequestException:
                time.sleep(0.5 * (attempt + 1))
            except (KeyError, ValueError, IndexError):
                return None
        return None

    def fetch_closed_orders_window(
        self,
        start: datetime,
        end: datetime,
        states: tuple[str, ...] = ("done", "cancel"),
    ) -> list[dict]:
        """단일 윈도우(<=7일) 조회. limit 채워지면 둘로 쪼개 재귀.

        반환 순서는 보장하지 않음. 호출자가 created_at 기준 정렬해야 함.

        states에 cancel을 반드시 포함할 것: 업비트 시장가 매수(ord_type=price)는
        지정 금액 소진 후 잔여가 취소되어 체결이 끝나도 state가 'cancel'로 남는다.
        done만 조회하면 시장가 매수가 통째로 누락되어 평단·손익이 망가진다.
        실제 체결분 판별은 executed_volume>0 으로 calc_pnl에서 거른다.
        """
        if (end - start) > timedelta(days=WINDOW_MAX_DAYS):
            raise ValueError("window must be <= 7 days")

        query: dict = {
            "states[]": list(states),
            "start_time": _to_millis(start),
            "end_time": _to_millis(end),
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
        states: tuple[str, ...] = ("done", "cancel"),
    ) -> Iterator[dict]:
        """start_date부터 end_date(default=now)까지 7일 윈도우로 슬라이드.

        states 기본값에 cancel 포함 — 시장가 매수 누락 방지
        (fetch_closed_orders_window 도크 참고).
        """
        end_date = end_date or datetime.now(tz=KST)
        cursor = start_date
        while cursor < end_date:
            win_end = min(cursor + timedelta(days=WINDOW_MAX_DAYS), end_date)
            yield from self.fetch_closed_orders_window(cursor, win_end, states)
            cursor = win_end


def _to_millis(dt: datetime) -> int:
    """업비트 orders/closed의 start_time/end_time은 밀리초 timestamp(13자리).

    ISO8601 문자열을 보내면 invalid_query_payload(401)로 거부된다.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return int(dt.timestamp() * 1000)
