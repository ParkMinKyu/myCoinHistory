# myCoinHistory

업비트 거래내역 → 손익 시각화 (개인용).

업비트 UI는 종합 수익률을 한눈에 안 보여주고 CSV도 안 줘서 직접 만듦.
**데이터는 본인 PC에서만 처리** — 외부 서버로 안 나감 (세무 서비스와 다른 점).

🧪 데모: https://ParkMinKyu.github.io/myCoinHistory/ (가상 데이터, "DEMO" 배너 표시)

## 기능

- **손익 3종** — 화면에 나란히:
  - **현금 순손익**: 통장 입출금(총출금 − 총입금) 기준. 가장 정확.
  - **순현금 손익**: 코인별 (매도 − 매수). 출금=상실로 봄.
  - **시점시세 손익**: 입금=시점매수 / 출금=시점매도로 보정. 출금한 코인 가치를
    그 시점 시세로 인정한 추정치.
- **코인 손익 랭킹** — 수익/손해 순위, 평균 매수가·매도가, 수익률(%),
  손익/수익률 정렬 토글, 더보기 10개씩, 상폐 코인 뱃지
- **요약 그룹** — 현금 / 코인 / 지갑(코인 입출금 시점평가) / 거래(승률) / 통계(평균 단가)
- **차트** — 누적 실현 손익 라인, 일자별 막대, 코인별 비중 도넛, 손익 분포, 거래 빈도 히트맵
- **매매 흐름 타임라인** + **일자별 거래내역 페이지** (`ledger.html`, 기간/코인/검색 필터)
- **외부 출금/입금 패널** — 거래내역엔 없는데 지갑에서 빠지거나 들어온 코인
- 한국식 이동평균법 평단가 + 기간/코인 클라이언트 필터

## 안 함 (의도적)

- 다중 사용자, 로그인, SaaS화
- BTC/USDT 마켓 (KRW 마켓만 — 다른 마켓은 환율 환산 필요)
- 양도세 계산 (2027.1.1 시행 후 별도)

## 모드

| 모드 | 데이터 출처 | 표시 |
|---|---|---|
| **Live (Flask)** | 업비트 API 실시간 fetch | 본인 실거래, 배너 없음 |
| **정적 (Demo)** | `frontend/result.json` | 가상 데이터, "🧪 DEMO" 배너 |

프론트는 먼저 `/api/health`로 Flask 서버 생존을 확인한다:
**서버 있으면 API(실거래) 우선, 없으면 `result.json`(데모)** 사용.
→ 클론한 사람이 자기 키로 서버를 띄우면 `result.json`이 있어도 자기 실거래가 보임.

## Live 모드 (실제 거래내역)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# .env 열어서 UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 채우기
# 업비트 → 마이페이지 → Open API 관리에서 발급
# 권한: "자산 조회" + "주문 조회"만 체크 (출금/주문 권한 X)
# IP 제한: 본인 PC 공인 IP 등록 필수

python -m backend.app          # → http://localhost:11666
```

처음 진입하면 캐시가 없으니 **"전체(2017~) 긁어오기"** 또는 **"업비트에서 새로"** 클릭.
가입일~현재까지 7일 윈도우로 슬라이드 fetch (거래 많으면 수 분).
raw 응답·입출금·시세 평가는 `data/`에 캐시됨 (gitignore).

## 데모 배포 (GitHub Pages)

데모는 **실거래를 가상화한 `result.json`을 커밋해서** 배포한다 (CI 러너엔 실거래
캐시가 없어 빌드 시 생성 불가).

```bash
# 본인 캐시(data/orders_cache.json)를 변형해 데모 데이터 생성:
python3 scripts/generate_demo_from_real.py   # 금액 가림 + 빈 달 채움 + 최근거래 + _demo 플래그
# 또는 순수 랜덤 mock:
python3 scripts/generate_mock.py

git add frontend/result.json && git commit && git push
# .github/workflows/pages.yml 이 result.json 그대로 배포
```

⚠️ 데모 `result.json`은 git에 박제된다. 금액은 가렸지만 **코인 종류·대략 시점은
노출**됨 (완전 익명 아님). 공개가 꺼려지면 `generate_mock.py`의 순수 랜덤본을 쓸 것.

## 업비트 API 함정 (디버깅하며 확정)

`backend/upbit_client.py`에 주석으로 남겨둠. 요약:

- **JWT query_hash**: `unquote(urlencode(...))` 평문에 SHA512. URL에 직접 부착
  (requests params= 자동인코딩에 맡기면 401 invalid_query_payload).
- **시간 파라미터**: `start_time`/`end_time`은 밀리초 timestamp(13자리). ISO 문자열 거부.
- **시장가 매수**(`ord_type=price`)는 체결 완료돼도 `state=cancel`로 남음 →
  `states=("done","cancel")` 둘 다 조회해야 누락 안 됨. (이거 빠지면 손익 폭증)
- **조회 가능 기간**: 약 최근 4~5년. 그 이전 주문은 안 줌.

## 손익 정확도 한계

- **외부 전송 코인**(입금/출금): 거래소 밖 처분이 섞여 평단법 손익이 왜곡됨.
  → 실현손익에서 제외하거나 시점시세로 보정. 화면에 별도 표시.
- **상폐 코인**: 시세 API에서 404 → 입금분 원가를 못 구해 손익 부정확.
  랭킹에 "상폐" 뱃지 + 경고 각주.
- **BTC/USDT 마켓 거래**: KRW 마켓만 처리하므로 손익에서 빠짐.
- 가장 신뢰할 수 있는 건 **현금 순손익(입출금 기준)**.

## 검증 포인트 (첫 실행 시)

1. fetch가 끝까지 도는가 (rate limit 에러 없는지)
2. `data/orders_cache.json`의 가장 오래된 `created_at`이 본인 첫 거래일과 맞는가
3. **현금 순손익 ≈ 코인 순현금 손익 합** 인가 (입출금과 거래가 정합하는지)
4. "현재 보유" 표가 실제 업비트 잔고와 일치하는가
