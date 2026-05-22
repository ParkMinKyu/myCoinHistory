"""Flask 단일 페이지 서버.

GET /            : index.html
GET /api/pnl     : 거래내역 fetch + 손익 계산 결과 JSON
GET /api/health  : ping
"""

from __future__ import annotations

import json
import os
import time
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
CASH_CACHE_FILE = ROOT / "data" / "cash_cache.json"

app = Flask(__name__, static_folder=None)


def _client() -> UpbitClient:
    access = os.environ.get("UPBIT_ACCESS_KEY", "").strip()
    secret = os.environ.get("UPBIT_SECRET_KEY", "").strip()
    if not access or not secret:
        raise RuntimeError("UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY env required")
    return UpbitClient(UpbitCredentials(access, secret))


def _load_orders(use_cache: bool, start_str: str | None = None) -> list[dict]:
    if use_cache and CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())

    client = _client()
    # 우선순위: 요청 인자 > 환경변수 > 업비트 오픈일
    start_str = start_str or os.environ.get("FETCH_START_DATE", "2017-10-24")
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


def _load_cash(use_cache: bool) -> dict:
    """KRW 입출금 내역 → 요약. 캐시 우선, 없으면 업비트 조회.

    반환: {total_in, total_out, net, deposits:[{date,amount}], withdraws:[...]}
    완료된 건만 집계: 입금 state ACCEPTED, 출금 state DONE.
    """
    if use_cache and CASH_CACHE_FILE.exists():
        return json.loads(CASH_CACHE_FILE.read_text())

    client = _client()
    deposits = client.get_krw_transfers("deposits")
    withdraws = client.get_krw_transfers("withdraws")

    def _ok(item: dict, kind: str) -> bool:
        st = (item.get("state") or "").upper()
        return st in ("ACCEPTED", "DONE")  # 입금=ACCEPTED, 출금=DONE

    total_in = sum(float(d.get("amount") or 0) for d in deposits if _ok(d, "in"))
    total_out = sum(float(w.get("amount") or 0) for w in withdraws if _ok(w, "out"))
    withdraw_fee = sum(float(w.get("fee") or 0) for w in withdraws if _ok(w, "out"))
    summary = {
        "total_in": total_in,
        "total_out": total_out,
        "net": total_in - total_out,
        "withdraw_fee": withdraw_fee,  # KRW 출금 수수료 합 (각주용)
        "deposits": [
            {"date": (d.get("created_at") or "")[:10], "amount": float(d.get("amount") or 0)}
            for d in deposits
            if _ok(d, "in")
        ],
        "withdraws": [
            {"date": (w.get("created_at") or "")[:10], "amount": float(w.get("amount") or 0)}
            for w in withdraws
            if _ok(w, "out")
        ],
    }
    CASH_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CASH_CACHE_FILE.write_text(json.dumps(summary, ensure_ascii=False))
    return summary


TRANSFER_CACHE_FILE = ROOT / "data" / "transfer_markets.json"


def _load_transfer_markets(use_cache: bool) -> set[str]:
    """외부 전송 있는 마켓 집합. 캐시 우선. 실패 시 빈 집합(=제외 안 함)."""
    if use_cache and TRANSFER_CACHE_FILE.exists():
        return set(json.loads(TRANSFER_CACHE_FILE.read_text()))
    try:
        markets = _client().get_transfer_markets()
    except Exception as e:
        print(f"[transfer] failed: {e}")
        return set()
    TRANSFER_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRANSFER_CACHE_FILE.write_text(json.dumps(sorted(markets), ensure_ascii=False))
    return markets


WALLET_CACHE_FILE = ROOT / "data" / "wallet_cache.json"


def _load_wallet(use_cache: bool) -> dict:
    """코인 입금/출금을 '출금/입금 시점 시세'로 평가한 요약.

    - 출금(빠져나간 가치) 평가합 / 입금(들어온 가치) 평가합
    - 코인별 명세 포함
    출금 시점 시세를 일봉 종가로 평가하므로 다수의 시세 호출 → 캐시 필수.
    """
    if use_cache and WALLET_CACHE_FILE.exists():
        return json.loads(WALLET_CACHE_FILE.read_text())

    client = _client()
    price_cache: dict[tuple, float | None] = {}

    def price_at(market: str, on_iso: str) -> float | None:
        key = (market, (on_iso or "")[:10])
        if key not in price_cache:
            price_cache[key] = client.get_daily_close(market, on_iso)
            time.sleep(0.08)  # 시세 API rate limit 여유
        return price_cache[key]

    def value(items: list[dict], include_fee: bool):
        """items의 시점시세 평가. 출금이면 include_fee=True → 수수료까지 더해
        '계좌에서 실제 빠진 양'으로 평가. fee의 KRW 환산합도 반환.

        반환: (total, fee_krw, 코인별합계 rows, 건별 events[date,currency,amount,value])
        """
        total = 0.0
        fee_krw = 0.0
        detail: dict[str, dict] = {}
        events: list[dict] = []
        for it in items:
            cur = it["currency"]
            market = f"KRW-{cur}"
            price = price_at(market, it["created_at"]) or 0
            fee = float(it.get("fee") or 0) if include_fee else 0.0
            qty = it["amount"] + fee  # 출금: amount(받은양)+fee = 계좌에서 나간 총량
            val = price * qty
            total += val
            fee_krw += price * fee
            d = detail.setdefault(cur, {"currency": cur, "amount": 0.0, "value": 0.0, "count": 0})
            d["amount"] += qty
            d["value"] += val
            d["count"] += 1
            events.append({"date": (it.get("created_at") or "")[:10], "currency": cur,
                           "amount": qty, "value": val})
        rows = sorted(detail.values(), key=lambda r: -r["value"])
        return total, fee_krw, rows, events

    try:
        deps = client.get_coin_transfers("deposits")
        wds = client.get_coin_transfers("withdraws")
    except Exception as e:
        print(f"[wallet] transfer fetch failed: {e}")
        return {}

    in_total, _, in_rows, in_events = value(deps, include_fee=False)
    out_total, out_fee_krw, out_rows, out_events = value(wds, include_fee=True)
    summary = {
        "in_value": in_total,   # 외부에서 입금받은 코인의 시점평가 (자산 유입)
        "out_value": out_total,  # 외부로 출금한 코인의 시점평가 (수수료 포함, 자산 유출)
        # 순이동 = 입금 - 출금. 나간 게 많으면 음수(계좌에서 자산이 더 빠져나감).
        "net_flow": in_total - out_total,
        "out_fee_krw": out_fee_krw,  # 코인 출금 수수료의 KRW 환산합 (각주용)
        "in_detail": in_rows,
        "out_detail": out_rows,
        "in_events": in_events,    # 건별 (날짜 필터용)
        "out_events": out_events,
    }
    WALLET_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    WALLET_CACHE_FILE.write_text(json.dumps(summary, ensure_ascii=False))
    return summary


MARKETPNL_CACHE_FILE = ROOT / "data" / "marketpnl_cache.json"


def _load_market_pnl(orders: list[dict], use_cache: bool) -> dict:
    """코인별 '시점시세' 손익: 입금=출금시점시세로 매수, 출금=시점시세로 매도 간주.

    평단법의 입출금 누락 왜곡을 보정한 손익. 출금시점=처분 가정의 추정치.
    반환: {total, per_market:{market: pnl}}. 시세 호출 많아 캐시.
    """
    if use_cache and MARKETPNL_CACHE_FILE.exists():
        return json.loads(MARKETPNL_CACHE_FILE.read_text())

    client = _client()
    price_cache: dict[tuple, float | None] = {}

    def price(market: str, iso: str) -> float:
        key = (market, (iso or "")[:10])
        if key not in price_cache:
            price_cache[key] = client.get_daily_close(market, iso)
            time.sleep(0.06)
        return price_cache[key] or 0.0

    # 코인별 이벤트: 거래 + 입출금 통합
    from collections import defaultdict

    ev: dict[str, list] = defaultdict(list)
    for o in orders:
        m = str(o.get("market", ""))
        if not m.startswith("KRW-") or float(o.get("executed_volume") or 0) <= 0:
            continue
        ev[m].append(
            (o.get("created_at"), o.get("side"),
             float(o["executed_volume"]), float(o.get("executed_funds") or 0),
             float(o.get("paid_fee") or 0))
        )
    try:
        for d in client.get_coin_transfers("deposits"):
            ev[f"KRW-{d['currency']}"].append((d["created_at"], "in", d["amount"], None, 0.0))
        for w in client.get_coin_transfers("withdraws"):
            ev[f"KRW-{w['currency']}"].append(
                (w["created_at"], "out", w["amount"], None, float(w.get("fee") or 0))
            )
    except Exception as e:
        print(f"[marketpnl] transfer fetch failed: {e}")
        return {}

    per_market: dict[str, float] = {}
    detail: dict[str, dict] = {}  # 코인별 평균 매수/매도 단가 (KRW 거래만)
    for m, evs in ev.items():
        evs.sort(key=lambda e: e[0] or "")
        vol = cost = realized = 0.0
        buy_vol = buy_krw = sell_vol = sell_krw = 0.0  # 평균 단가용 (실제 KRW 거래만)
        trades = 0  # KRW 매수/매도 체결 건수 (입출금 제외)
        for ts, side, qty, funds, fee in evs:
            if side == "bid":
                vol += qty
                cost += funds + fee
                buy_vol += qty
                buy_krw += funds
                trades += 1
            elif side == "in":  # 입금 = 시점시세 매수 (단가 통계엔 제외)
                vol += qty
                cost += price(m, ts) * qty
            else:  # ask/out = 매도
                avg = cost / vol if vol > 1e-9 else 0.0
                if side == "ask":
                    realized += (funds - fee) - avg * qty
                    out_qty = qty
                    sell_vol += qty
                    sell_krw += funds
                    trades += 1
                else:  # 출금 = 시점시세 처분 (수수료분도 원가에서 차감)
                    realized += price(m, ts) * qty - avg * qty
                    out_qty = qty + fee
                vol -= out_qty
                cost -= avg * out_qty
                if vol <= 1e-9:
                    vol = cost = 0.0
        per_market[m] = realized
        detail[m] = {
            "pnl": realized,
            "trades": trades,  # KRW 매수/매도 체결 건수
            "avg_buy_price": (buy_krw / buy_vol) if buy_vol > 0 else 0.0,
            "avg_sell_price": (sell_krw / sell_vol) if sell_vol > 0 else 0.0,
            "buy_krw": buy_krw,  # 투자 원가 (현금 매수액) — 수익률 계산용
            # 수익률 = 손익 / 투자원가. 매수 없으면(입금만) None.
            "return_pct": (realized / buy_krw * 100) if buy_krw > 0 else None,
        }

    summary = {"total": sum(per_market.values()), "per_market": per_market, "detail": detail}
    MARKETPNL_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    MARKETPNL_CACHE_FILE.write_text(json.dumps(summary, ensure_ascii=False))
    return summary


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/pnl")
def api_pnl():
    from flask import request

    use_cache = request.args.get("cache", "1") == "1"
    # start=YYYY-MM-DD 로 fetch 시작 시점 지정 (전체 긁어오기 버튼용).
    # 형식이 이상하면 무시하고 기본값 사용.
    start_str = request.args.get("start") or None
    if start_str:
        try:
            datetime.fromisoformat(start_str)
        except ValueError:
            start_str = None
    try:
        orders = _load_orders(use_cache=use_cache, start_str=start_str)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # 현금 입출금 요약 (실패해도 손익은 계속 — 권한/네트워크 이슈 대비)
    cash: dict | None = None
    try:
        cash = _load_cash(use_cache=use_cache)
    except Exception as e:
        print(f"[cash] failed: {e}")

    # 실제 업비트 잔고 (currency -> 보유수량). 실시간이라 캐시 안 함, 실패해도 진행.
    real_balances: dict[str, float] = {}
    try:
        for a in _client().get_accounts():
            cur = a.get("currency")
            if not cur or cur == "KRW":
                continue
            bal = float(a.get("balance") or 0) + float(a.get("locked") or 0)
            if bal > 0:
                real_balances[f"KRW-{cur}"] = bal
    except Exception as e:
        print(f"[accounts] failed: {e}")

    # 외부 전송(코인 입출금) 있는 마켓 — 평단법 손익이 무의미하므로 clean 손익에서 제외.
    transfer_markets = _load_transfer_markets(use_cache=use_cache)

    # 지갑(코인 입출금)을 시점 시세로 평가
    wallet: dict | None = None
    try:
        wallet = _load_wallet(use_cache=use_cache) or None
    except Exception as e:
        print(f"[wallet] failed: {e}")

    # 코인별 시점시세 손익 (입금=매수/출금=매도 보정)
    market_pnl: dict | None = None
    try:
        market_pnl = _load_market_pnl(orders, use_cache=use_cache) or None
    except Exception as e:
        print(f"[marketpnl] failed: {e}")

    result = calc_pnl(orders, transfer_markets=transfer_markets)

    # 상폐 코인: 거래내역엔 있는데 현재 마켓에 없는 것 (시세 조회 불가 → 손익 부정확)
    delisted: list[str] = []
    try:
        active = _client().get_active_markets()
        traded = {str(o.get("market", "")) for o in orders if str(o.get("market", "")).startswith("KRW-")}
        delisted = sorted(m for m in traded if m and m not in active)
    except Exception as e:
        print(f"[delisted] failed: {e}")

    # 거래 수수료 합 (KRW 마켓 체결분) — 각주용.
    # 실현손익(전송제외)에 맞춰 비전송 코인 수수료를 따로 집계.
    trade_fee = 0.0
    trade_fee_clean = 0.0
    for o in orders:
        m = str(o.get("market", ""))
        if not m.startswith("KRW-") or float(o.get("executed_volume") or 0) <= 0:
            continue
        fee = float(o.get("paid_fee") or 0)
        trade_fee += fee
        if m not in transfer_markets:
            trade_fee_clean += fee

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

    # 프론트는 all_orders를 받아 클라이언트에서 다시 시뮬레이션한다
    # (기간/코인 필터·timeline·차트가 mock 모드와 동일하게 동작하려면 필요).
    # KRW 마켓 + 체결분만, 시간 정순.
    all_orders = []
    for o in sorted(orders, key=lambda x: x.get("created_at", "")):
        if not str(o.get("market", "")).startswith("KRW-"):
            continue
        try:
            ev = float(o.get("executed_volume") or 0)
            ef = float(o.get("executed_funds") or 0)
        except (TypeError, ValueError):
            continue
        if ev <= 0 or ef <= 0:
            continue
        all_orders.append(
            {
                "uuid": o.get("uuid"),
                "side": o.get("side"),
                "market": o.get("market"),
                "executed_volume": ev,
                "executed_funds": ef,
                "paid_fee": float(o.get("paid_fee") or 0),
                "created_at": o.get("created_at"),
            }
        )

    return jsonify(
        {
            "trades_count": result.trades_count,
            "realized_total": result.realized_total,
            "realized_clean_total": result.realized_clean_total,
            "transfer_markets": result.transfer_markets,
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
            "all_orders": all_orders,
            "current_prices": current,
            "cash": cash,
            "real_balances": real_balances,
            "wallet": wallet,
            "market_pnl": market_pnl,
            "delisted": delisted,
            "trade_fee": trade_fee,
            "trade_fee_clean": trade_fee_clean,
            "skipped_count": len(result.skipped),
        }
    )


@app.get("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.get("/<path:filename>")
def static_file(filename: str):
    # frontend/ 내 정적 파일 서빙 (ledger.html 등). /api/* 는 위 라우트가 먼저 매칭됨.
    target = (FRONTEND_DIR / filename).resolve()
    if FRONTEND_DIR.resolve() not in target.parents or not target.is_file():
        return jsonify({"error": "not found"}), 404
    return send_from_directory(FRONTEND_DIR, filename)


if __name__ == "__main__":
    app.run(debug=True, port=11666)
