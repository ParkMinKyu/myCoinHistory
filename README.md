# myCoinHistory

업비트 거래내역 → 누적 손익 시각화 (개인용).

## 왜 만듦

업비트 UI에서 종합 수익률 한눈에 안 보임. CSV 다운로드도 안 줌. 그래서 직접.

## 1차 MVP 범위

- 업비트 `/v1/orders/closed` 7일 슬라이딩 윈도우로 가입일까지 거슬러 fetch
- KRW 마켓 한정, 한국식 이동평균법으로 평단가 계산
- 일자별 누적 실현 손익 라인 차트 1개
- 현재 보유 코인 미실현 손익 표

## 안 함 (의도적)

- 다중 사용자, 로그인, SaaS화
- BTC/USDT 마켓 (1차에서 제외)
- 거래소 간 전송 매칭, 에어드랍, 스테이킹
- 양도세 계산 (2027.1.1 시행 후 별도)

## 실행

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# .env 열어서 UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 채우기
# 업비트 → 마이페이지 → Open API 관리에서 발급
# 권한: "자산 조회" + "주문 조회"만 체크 (출금/주문 권한 X)
# IP 제한: 본인 PC IP 등록 필수

python -m backend.app
# → http://localhost:5050
```

처음 진입하면 캐시가 없으니 "업비트에서 새로 가져오기" 클릭.
가입일~현재까지 슬라이딩 윈도우로 fetch (가입 오래됐으면 수 분 소요).
가져온 raw 응답은 `data/orders_cache.json`에 저장됨 (gitignore됨).

## 검증 포인트 (첫 실행시 직접 확인할 것)

1. fetch가 실제로 끝까지 도는가 (rate limit 에러 없는지)
2. `data/orders_cache.json` 열어서 가장 오래된 `created_at`이 본인 첫 거래일과 일치하는가
3. 누적 손익 차트의 끝 값 ≈ 업비트 앱의 "누적 손익" 표시값과 비슷한가
4. "현재 보유" 표의 수량/평단가가 업비트 앱과 일치하는가

하나라도 안 맞으면 `backend/pnl.py` 또는 `upbit_client.py` 수정.

## 다음 단계 후보 (요약 우선순위 기준)

- C. 코인별 비중 도넛/트리맵
- E. 매매 흐름 타임라인
- D. 캘린더 히트맵
