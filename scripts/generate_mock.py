"""가짜 거래내역 생성 → frontend/result.json 출력.

UI 검증 전용. 실제 시세/패턴 시뮬레이션 X.
seed 고정이라 매번 같은 결과 나옴.
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
OUT = ROOT / "frontend" / "result.json"

MARKETS_INIT_PRICE = [
    ("KRW-BTC", 50_000_000),
    ("KRW-ETH", 3_000_000),
    ("KRW-XRP", 800),
    ("KRW-SOL", 200_000),
    ("KRW-DOGE", 200),
    ("KRW-ADA", 700),
]


def main() -> None:
    random.seed(42)
    prices = {m: p for m, p in MARKETS_INIT_PRICE}
    holdings = {m: 0.0 for m, _ in MARKETS_INIT_PRICE}
    orders: list[dict] = []

    cursor = datetime(2018, 1, 1, tzinfo=KST)
    end = datetime(2026, 5, 6, tzinfo=KST)

    while cursor < end:
        for m in prices:
            prices[m] *= random.uniform(0.97, 1.035)
            if prices[m] < 1:
                prices[m] = 1.0

        if random.random() < 0.18:
            for _ in range(random.randint(1, 3)):
                m = random.choice([mm for mm, _ in MARKETS_INIT_PRICE])
                p = prices[m]
                if holdings[m] > 0 and random.random() < 0.45:
                    vol = holdings[m] * random.uniform(0.2, 1.0)
                    funds = vol * p
                    side = "ask"
                    holdings[m] -= vol
                else:
                    funds = random.uniform(100_000, 3_000_000)
                    vol = funds / p
                    side = "bid"
                    holdings[m] += vol

                ts = cursor.replace(
                    hour=random.randint(9, 22),
                    minute=random.randint(0, 59),
                )
                orders.append(
                    {
                        "uuid": str(uuid.uuid4()),
                        "side": side,
                        "ord_type": "limit",
                        "state": "done",
                        "market": m,
                        "executed_volume": f"{vol:.8f}",
                        "executed_funds": f"{funds:.4f}",
                        "paid_fee": f"{funds * 0.0005:.4f}",
                        "created_at": ts.isoformat(),
                    }
                )
        cursor += timedelta(days=1)

    result = calc_pnl(orders)
    current = {m: prices[m] for m, _ in MARKETS_INIT_PRICE}
    unreal = unrealized_pnl(result.positions, current)

    per_market_list = []
    for m, ms in result.per_market.items():
        pos = result.positions.get(m)
        per_market_list.append(
            {
                "market": m,
                "realized": ms.realized,
                "unrealized": unreal.get(m, 0.0),
                "trades": ms.trades,
                "buy_funds": ms.buy_funds,
                "sell_funds": ms.sell_funds,
                "current_volume": pos.volume if pos else 0.0,
                "current_value": (pos.volume * current.get(m, 0.0)) if pos else 0.0,
                "avg_price": pos.avg_price if pos else 0.0,
                "current_price": current.get(m),
            }
        )

    # 타임라인용: 최근 N건만 (전체는 무거움)
    recent = sorted(orders, key=lambda o: o["created_at"], reverse=True)[:80]
    recent_orders = [
        {
            "uuid": o["uuid"],
            "side": o["side"],
            "market": o["market"],
            "executed_volume": float(o["executed_volume"]),
            "executed_funds": float(o["executed_funds"]),
            "paid_fee": float(o["paid_fee"]),
            "created_at": o["created_at"],
        }
        for o in recent
    ]

    # 클라이언트 사이드 필터용 전체 orders (시간순)
    all_orders = [
        {
            "uuid": o["uuid"],
            "side": o["side"],
            "market": o["market"],
            "executed_volume": float(o["executed_volume"]),
            "executed_funds": float(o["executed_funds"]),
            "paid_fee": float(o["paid_fee"]),
            "created_at": o["created_at"],
        }
        for o in sorted(orders, key=lambda o: o["created_at"])
    ]

    # --- 데모용 파생 필드 (Live 모드의 cash/wallet/market_pnl/delisted 흉내) ---
    total_buy = sum(ms.buy_funds for ms in result.per_market.values())
    total_sell = sum(ms.sell_funds for ms in result.per_market.values())
    trade_fee = sum(float(o["paid_fee"]) for o in orders)

    # 현금: 입금=구매액의 1.1배 가정, 출금=판매액. 데모용 그럴듯한 값.
    mock_in = total_buy * 1.1
    mock_out = total_sell
    cash = {
        "total_in": mock_in,
        "total_out": mock_out,
        "net": mock_in - mock_out,
        "withdraw_fee": 30000.0,
        "deposits": [],
        "withdraws": [],
    }

    # 코인별 시점시세 손익: mock은 전송이 없으니 평단법 실현손익 = 시점시세 손익.
    # 평균 매수/매도가와 수익률도 per_market에서 산출.
    mp_detail = {}
    mp_per = {}
    buy_qty = defaultdict(float); buy_krw = defaultdict(float)
    sell_qty = defaultdict(float); sell_krw = defaultdict(float)
    for o in orders:
        m = o["market"]; ev = float(o["executed_volume"]); ef = float(o["executed_funds"])
        if o["side"] == "bid":
            buy_qty[m] += ev; buy_krw[m] += ef
        else:
            sell_qty[m] += ev; sell_krw[m] += ef
    for m, ms in result.per_market.items():
        mp_per[m] = ms.realized
        mp_detail[m] = {
            "pnl": ms.realized,
            "avg_buy_price": (buy_krw[m] / buy_qty[m]) if buy_qty[m] > 0 else 0.0,
            "avg_sell_price": (sell_krw[m] / sell_qty[m]) if sell_qty[m] > 0 else 0.0,
            "buy_krw": ms.buy_funds,
            "return_pct": (ms.realized / ms.buy_funds * 100) if ms.buy_funds > 0 else None,
        }
    market_pnl = {"total": sum(mp_per.values()), "per_market": mp_per, "detail": mp_detail}

    # 데모용: DOGE를 상폐 코인으로 표시 (뱃지/각주 시연용)
    delisted = ["KRW-DOGE"] if "KRW-DOGE" in result.per_market else []

    payload = {
        "trades_count": result.trades_count,
        "realized_total": result.realized_total,
        "realized_clean_total": result.realized_total,  # mock은 전송 없음 → 동일
        "transfer_markets": [],
        "unrealized_total": sum(unreal.values()),
        "cash": cash,
        "wallet": None,           # mock은 코인 전송 없음
        "real_balances": {m: p.volume for m, p in result.positions.items() if p.volume > 0},
        "market_pnl": market_pnl,
        "delisted": delisted,
        "trade_fee": trade_fee,
        "trade_fee_clean": trade_fee,
        "daily": [
            {"date": d.date, "realized": d.realized, "cumulative": d.cumulative_realized}
            for d in result.daily
        ],
        "daily_trades": [
            {"date": d, "count": c} for d, c in sorted(result.daily_trades.items())
        ],
        "per_market": per_market_list,
        "positions": [
            {
                "market": m,
                "volume": p.volume,
                "avg_price": p.avg_price,
                "current_price": current.get(m),
                "unrealized": unreal.get(m, 0.0),
            }
            for m, p in result.positions.items()
            if p.volume > 0
        ],
        "recent_orders": recent_orders,
        "all_orders": all_orders,
        "current_prices": current,
        "skipped_count": len(result.skipped),
        "_mock": True,
        "_generated_at": datetime.now(tz=KST).isoformat(),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False))
    print(
        f"wrote {OUT}: orders={len(orders)} days={len(result.daily)} "
        f"positions={len(payload['positions'])} markets={len(per_market_list)}"
    )


if __name__ == "__main__":
    main()
