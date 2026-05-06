"""Flask 단일 페이지 서버.

GET /            : index.html
GET /api/pnl     : 거래내역 fetch + 손익 계산 결과 JSON
GET /api/health  : ping
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, send_from_directory

from .pnl import calc_pnl, unrealized_pnl
from .upbit_client import KST, UpbitClient, UpbitCredentials

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"
CACHE_FILE = ROOT / "data" / "orders_cache.json"

app = Flask(__name__, static_folder=None)


def _client() -> UpbitClient:
    access = os.environ.get("UPBIT_ACCESS_KEY", "").strip()
    secret = os.environ.get("UPBIT_SECRET_KEY", "").strip()
    if not access or not secret:
        raise RuntimeError("UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY env required")
    return UpbitClient(UpbitCredentials(access, secret))


def _load_orders(use_cache: bool) -> list[dict]:
    if use_cache and CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())

    client = _client()
    start_str = os.environ.get("FETCH_START_DATE", "2017-10-24")
    start = datetime.fromisoformat(start_str).replace(tzinfo=KST)
    end = datetime.now(tz=KST)
    print(f"[fetch] {start.date()} ~ {end.date()} (slide 7d windows)")

    orders: list[dict] = []
    for o in client.iter_closed_orders(start, end):
        orders.append(o)
        if len(orders) % 200 == 0:
            print(f"  fetched {len(orders)} orders...")
    print(f"[fetch] total {len(orders)} orders")

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(orders, ensure_ascii=False))
    return orders


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/pnl")
def api_pnl():
    from flask import request

    use_cache = request.args.get("cache", "1") == "1"
    try:
        orders = _load_orders(use_cache=use_cache)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    result = calc_pnl(orders)

    # 현재가 조회 → 미실현손익
    current: dict[str, float] = {}
    held = [m for m, p in result.positions.items() if p.volume > 0]
    if held:
        try:
            tickers = _client().get_ticker(held)
            for t in tickers:
                current[t["market"]] = float(t["trade_price"])
        except Exception as e:
            print(f"[ticker] failed: {e}")

    unreal = unrealized_pnl(result.positions, current)

    return jsonify(
        {
            "trades_count": result.trades_count,
            "realized_total": result.realized_total,
            "unrealized_total": sum(unreal.values()),
            "daily": [
                {"date": d.date, "realized": d.realized, "cumulative": d.cumulative_realized}
                for d in result.daily
            ],
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
            "skipped_count": len(result.skipped),
        }
    )


@app.get("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


if __name__ == "__main__":
    app.run(debug=True, port=11666)
