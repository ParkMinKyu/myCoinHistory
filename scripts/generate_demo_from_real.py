"""실거래 캐시(data/orders_cache.json)를 변형해 데모용 result.json 생성.

목적: 본인 실거래 '패턴'은 살리되 실제 금액은 가려서 공개 데모로 쓰기.
- 거래 금액에 코인별 랜덤 배율 곱함 (액수 가림, 패턴 유지)
- 거래 없는 달에 가상 거래 채워넣기
- 최근(2026-03~05) 가상 거래 추가
- _demo 플래그 → 프론트에서 "DEMO" 배너 표시

⚠️ 그래도 실거래 코인 종류/대략적 시점은 드러남. 완전 익명 아님.
실행: python scripts/generate_demo_from_real.py
"""

from __future__ import annotations

import json
import random
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.pnl import calc_pnl, unrealized_pnl  # noqa: E402

KST = timezone(timedelta(hours=9))
CACHE = ROOT / "data" / "orders_cache.json"
OUT = ROOT / "frontend" / "result.json"

random.seed(7)


def _kept_orders() -> list[dict]:
    """캐시에서 KRW 마켓 체결분만 추려 간소화."""
    raw = json.loads(CACHE.read_text())
    out = []
    for o in raw:
        m = o.get("market", "")
        try:
            ev = float(o.get("executed_volume") or 0)
            ef = float(o.get("executed_funds") or 0)
        except (TypeError, ValueError):
            continue
        if not m.startswith("KRW-") or ev <= 0 or ef <= 0:
            continue
        out.append({
            "uuid": o.get("uuid") or str(uuid.uuid4()),
            "side": o.get("side"),
            "market": m,
            "executed_volume": ev,
            "executed_funds": ef,
            "paid_fee": float(o.get("paid_fee") or 0),
            "created_at": o.get("created_at"),
        })
    return out


def _scramble_amounts(orders: list[dict]) -> None:
    """코인별 랜덤 배율로 금액 가림 (수량×단가 일관성 유지)."""
    mult = {}
    for o in orders:
        m = o["market"]
        if m not in mult:
            mult[m] = random.uniform(0.6, 1.5)
        k = mult[m]
        o["executed_funds"] *= k
        o["executed_volume"] *= k  # 수량도 같이 키워 단가 유지
        o["paid_fee"] *= k


def _coin_price_hint(orders: list[dict]) -> dict[str, float]:
    """코인별 마지막 체결 단가(데모 가상거래·현재가용)."""
    last = {}
    for o in sorted(orders, key=lambda x: x["created_at"]):
        last[o["market"]] = o["executed_funds"] / o["executed_volume"]
    return last


def _fake_order(market: str, side: str, price: float, ts: datetime, krw: float) -> dict:
    vol = krw / price
    return {
        "uuid": str(uuid.uuid4()),
        "side": side,
        "market": market,
        "executed_volume": vol,
        "executed_funds": krw,
        "paid_fee": krw * 0.0005,
        "created_at": ts.replace(hour=random.randint(9, 22), minute=random.randint(0, 59)).isoformat(),
    }


def _fill_empty_months(orders: list[dict], prices: dict[str, float]) -> list[dict]:
    """거래 0건인 달에 가상 거래 몇 건씩 추가 (그 시점까지 등장한 코인으로)."""
    months = {o["created_at"][:7] for o in orders}
    markets = list(prices.keys())
    first = min(o["created_at"] for o in orders)[:7]
    extra = []
    y0, m0 = map(int, first.split("-"))
    end = datetime.now(tz=KST)
    cy, cm = y0, m0
    while (cy, cm) <= (end.year, end.month):
        key = f"{cy}-{cm:02d}"
        if key not in months:
            for _ in range(random.randint(2, 5)):
                mk = random.choice(markets)
                day = random.randint(1, 26)
                ts = datetime(cy, cm, day, tzinfo=KST)
                krw = random.uniform(100_000, 2_000_000)
                # 매수→같은 양 매도 쌍으로 (잔고 안 꼬이게)
                p = prices[mk] * random.uniform(0.8, 1.3)
                extra.append(_fake_order(mk, "bid", p, ts, krw))
                extra.append(_fake_order(mk, "ask", p * random.uniform(0.85, 1.25),
                                         ts + timedelta(days=random.randint(1, 20)), krw))
        cm += 1
        if cm > 12:
            cm = 1; cy += 1
    return extra


def _recent_trades(prices: dict[str, float]) -> list[dict]:
    """2026-03~05 최근 가상 거래."""
    markets = list(prices.keys())
    extra = []
    for mo in (3, 4, 5):
        for _ in range(random.randint(6, 12)):
            mk = random.choice(markets)
            day = random.randint(1, 27)
            ts = datetime(2026, mo, day, tzinfo=KST)
            krw = random.uniform(200_000, 3_000_000)
            p = prices[mk] * random.uniform(0.7, 1.6)
            side = random.choice(["bid", "ask"])
            extra.append(_fake_order(mk, side, p, ts, krw))
    return extra


def main() -> None:
    if not CACHE.exists():
        print("data/orders_cache.json 없음 — Live 모드로 먼저 fetch 필요")
        sys.exit(1)

    orders = _kept_orders()
    _scramble_amounts(orders)
    prices = _coin_price_hint(orders)
    orders += _fill_empty_months(orders, prices)
    orders += _recent_trades(prices)
    orders.sort(key=lambda o: o["created_at"])

    # 현재가: 마지막 단가 ± 변동
    current = {m: p * random.uniform(0.8, 1.3) for m, p in prices.items()}

    result = calc_pnl(orders)
    unreal = unrealized_pnl(result.positions, current)

    # per_market
    buy_q = defaultdict(float); buy_k = defaultdict(float)
    sell_q = defaultdict(float); sell_k = defaultdict(float)
    for o in orders:
        m = o["market"]
        if o["side"] == "bid":
            buy_q[m] += o["executed_volume"]; buy_k[m] += o["executed_funds"]
        else:
            sell_q[m] += o["executed_volume"]; sell_k[m] += o["executed_funds"]

    per_market_list, mp_per, mp_detail = [], {}, {}
    for m, ms in result.per_market.items():
        pos = result.positions.get(m)
        per_market_list.append({
            "market": m, "realized": ms.realized, "unrealized": unreal.get(m, 0.0),
            "trades": ms.trades, "buy_funds": ms.buy_funds, "sell_funds": ms.sell_funds,
            "current_volume": pos.volume if pos else 0.0,
            "current_value": (pos.volume * current.get(m, 0.0)) if pos else 0.0,
            "avg_price": pos.avg_price if pos else 0.0,
            "current_price": current.get(m),
        })
        mp_per[m] = ms.realized
        mp_detail[m] = {
            "pnl": ms.realized,
            "avg_buy_price": (buy_k[m] / buy_q[m]) if buy_q[m] > 0 else 0.0,
            "avg_sell_price": (sell_k[m] / sell_q[m]) if sell_q[m] > 0 else 0.0,
            "buy_krw": ms.buy_funds,
            "return_pct": (ms.realized / ms.buy_funds * 100) if ms.buy_funds > 0 else None,
        }

    all_orders = [
        {"uuid": o["uuid"], "side": o["side"], "market": o["market"],
         "executed_volume": o["executed_volume"], "executed_funds": o["executed_funds"],
         "paid_fee": o["paid_fee"], "created_at": o["created_at"]}
        for o in orders
    ]
    total_sell = sum(ms.sell_funds for ms in result.per_market.values())
    mock_out = total_sell
    mock_in = mock_out - result.realized_total
    trade_fee = sum(o["paid_fee"] for o in orders)

    payload = {
        "trades_count": result.trades_count,
        "realized_total": result.realized_total,
        "realized_clean_total": result.realized_total,
        "transfer_markets": [],
        "unrealized_total": sum(unreal.values()),
        "cash": {"total_in": mock_in, "total_out": mock_out, "net": mock_out - mock_in,
                 "withdraw_fee": 40000.0, "deposits": [], "withdraws": []},
        "wallet": None,
        "real_balances": {m: p.volume for m, p in result.positions.items() if p.volume > 0},
        "market_pnl": {"total": sum(mp_per.values()), "per_market": mp_per, "detail": mp_detail},
        "delisted": [],
        "trade_fee": trade_fee,
        "trade_fee_clean": trade_fee,
        "daily": [{"date": d.date, "realized": d.realized, "cumulative": d.cumulative_realized}
                  for d in result.daily],
        "daily_trades": [{"date": d, "count": c} for d, c in sorted(result.daily_trades.items())],
        "per_market": per_market_list,
        "positions": [{"market": m, "volume": p.volume, "avg_price": p.avg_price,
                       "current_price": current.get(m), "unrealized": unreal.get(m, 0.0)}
                      for m, p in result.positions.items() if p.volume > 0],
        "all_orders": all_orders,
        "current_prices": current,
        "skipped_count": len(result.skipped),
        "_mock": True,
        "_demo": True,
        "_generated_at": datetime.now(tz=KST).isoformat(),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False))
    print(f"wrote {OUT}: orders={len(orders)} markets={len(per_market_list)} "
          f"realized={round(result.realized_total):,}")


if __name__ == "__main__":
    main()
