"""한국식 이동평균법 평단가 + 일자별 누적 손익 계산."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable


@dataclass
class Position:
    volume: float = 0.0  # 보유 수량
    cost: float = 0.0  # 보유 원가 합계 (KRW, 수수료 포함)

    @property
    def avg_price(self) -> float:
        return self.cost / self.volume if self.volume > 0 else 0.0


@dataclass
class DailyPnL:
    date: str  # YYYY-MM-DD
    realized: float = 0.0  # 그 날 실현손익
    cumulative_realized: float = 0.0


@dataclass
class CalcResult:
    daily: list[DailyPnL]
    positions: dict[str, Position]  # market -> Position (현재 보유)
    realized_total: float
    trades_count: int
    skipped: list[dict] = field(default_factory=list)


def calc_pnl(orders: Iterable[dict]) -> CalcResult:
    """업비트 closed orders 리스트(state=done) → 일자별 누적 실현손익.

    - KRW 마켓만 처리 (BTC/USDT 마켓은 별도 환산 필요해 1차 MVP 제외)
    - executed_funds = 체결 KRW 합계 (수수료 제외)
    - paid_fee = 수수료
    - 매수 시 원가 += executed_funds + paid_fee
    - 매도 시 실현손익 = (executed_funds - paid_fee) - avg * executed_volume
    """
    sorted_orders = sorted(orders, key=lambda o: o.get("created_at", ""))
    positions: dict[str, Position] = defaultdict(Position)
    daily_realized: dict[str, float] = defaultdict(float)
    skipped: list[dict] = []
    realized_total = 0.0
    trades = 0

    for o in sorted_orders:
        market: str = o.get("market", "")
        if not market.startswith("KRW-"):
            skipped.append({"uuid": o.get("uuid"), "market": market, "reason": "non-KRW market"})
            continue

        side = o.get("side")
        try:
            executed_volume = float(o.get("executed_volume") or 0)
            executed_funds = float(o.get("executed_funds") or 0)
            paid_fee = float(o.get("paid_fee") or 0)
        except (TypeError, ValueError):
            skipped.append({"uuid": o.get("uuid"), "reason": "bad numeric"})
            continue

        if executed_volume <= 0 or executed_funds <= 0:
            continue  # 미체결/취소 잔여

        date_str = (o.get("created_at") or "")[:10]
        pos = positions[market]
        trades += 1

        if side == "bid":  # 매수
            pos.volume += executed_volume
            pos.cost += executed_funds + paid_fee
        elif side == "ask":  # 매도
            avg = pos.avg_price
            cost_out = avg * executed_volume
            net_in = executed_funds - paid_fee
            realized = net_in - cost_out
            daily_realized[date_str] += realized
            realized_total += realized
            pos.volume -= executed_volume
            pos.cost -= cost_out
            if pos.volume <= 1e-12:
                pos.volume = 0.0
                pos.cost = 0.0
        else:
            skipped.append({"uuid": o.get("uuid"), "reason": f"unknown side {side}"})

    daily_sorted = sorted(daily_realized.items())
    cum = 0.0
    daily_list: list[DailyPnL] = []
    for d, r in daily_sorted:
        cum += r
        daily_list.append(DailyPnL(date=d, realized=r, cumulative_realized=cum))

    return CalcResult(
        daily=daily_list,
        positions=dict(positions),
        realized_total=realized_total,
        trades_count=trades,
        skipped=skipped,
    )


def unrealized_pnl(positions: dict[str, Position], current_prices: dict[str, float]) -> dict[str, float]:
    """market -> 미실현손익 (현재가 - 평단) * 보유수량."""
    out: dict[str, float] = {}
    for market, pos in positions.items():
        if pos.volume <= 0:
            continue
        cur = current_prices.get(market)
        if cur is None:
            continue
        out[market] = (cur - pos.avg_price) * pos.volume
    return out


def fmt_iso_to_date(s: str) -> str:
    if not s:
        return ""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return s[:10]
