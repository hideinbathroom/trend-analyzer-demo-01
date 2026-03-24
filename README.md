# 📊 Trend Analyzer Demo

실시간 주식 시장 데이터를 수집하고, LLM으로 분석하여 Threads / Discord / Telegram에 자동 브리핑을 게시하는 봇입니다.

> 뉴스 수집 → 속보 감지 → LLM 시장 분석 → 멀티 채널 자동 게시

---

## 🎯 주요 기능

### 1. 뉴스 수집 & 속보 감지 시스템
- RSS 피드 9개 소스 + 네이버 뉴스 API로 실시간 뉴스 수집
- 9차원 다차원 스코어링으로 뉴스 중요도 자동 평가
- 자카드 유사도 기반 주제 클러스터링 + Velocity Signal로 속보 자동 감지
- 지수 감쇠 함수(`exp(-λ × age)`)로 신선도 반영

### 2. 거시경제 데이터 수집
- Yahoo Finance: 미국 3대 지수, 선물, VIX, 환율, 원자재, 국채, 비트코인
- 네이버 금융: 코스피/코스닥 실시간 크롤링 (yfinance 폴백)
- CNN: 공포탐욕지수 (API → 크롤링 → VIX 기반 추정 3단계 폴백)
- 유럽 지수: 유로스톡스50, DAX

### 3. LLM 기반 2단계 분석 & 브리핑 생성
```
데이터 수집 → [1차 LLM] 시장 정세 판단 → [2차 LLM] 브리핑 생성 → 게시
```
- 1차: 매크로 전략가 관점에서 시장 국면/리스크/기회 분석
- 2차: 분석 결과 + 원본 데이터로 500자 이내 브리핑 생성

### 4. 트럼프 Truth Social 모니터
- CNN 아카이브에서 트럼프 최신 글 수집 (Range 헤더로 64KB만 요청)
- LLM이 한국 주식시장 영향도 판단 (HIGH/LOW)
- HIGH 판정 시 자동으로 시장 영향 분석 포스트 생성 & 게시

### 5. 속보 자동 게시
- 10분 간격 폴링으로 고영향 속보 감지 (score ≥ 20)
- 자카드 유사도 기반 중복 방지
- 일일 3건 한도 + 15분 쿨다운

### 6. 멀티 채널 동시 게시
- **Threads**: Meta Graph API (본문 + 테마 댓글 분리)
- **Discord**: Embed 카드 형식 (뉴스 헤드라인 + 브리핑)
- **Telegram**: Bot API 메시지 전송

---

## ⏰ 하루 운영 타임라인

```
06:50          📋 한마디 타임라인 생성 (5슬롯 랜덤 시간 배정)
07:00~08:59    ⚡ 프리마켓 한마디
08:00          📊 모닝 브리핑 (장 전 데이터 기반)
09:00~15:30    ⚡ 장중 한마디 2회
16:50          📊 장 마감 리포트
17:21~19:00    ⚡ 장후 한마디
22:00~23:59    ⚡ 야간 한마디
상시           🚨 속보 자동 게시 (10분 폴링, 일일 3건)
상시           🇺🇸 트럼프 모니터 (30분 폴링, 일일 3건)
```

---

## 🔧 핵심 메커니즘

### 뉴스 스코어링 (9차원)

| 차원 | 설명 | 범위 |
|------|------|------|
| 영향도 (impact) | 키워드 사전 매칭 (매크로/지정학/수급/기업/섹터) | 0~15+ |
| 출처 신뢰도 | 연합뉴스/한경=3, 매경/머니투데이=2, 기타=1 | 1~3 |
| 구체성 | 수치/종목명/금액 포함 여부 | 0~3 |
| 시장 방향성 | 급락/폭등 등 강한 톤 키워드 | 0~3 |
| 신선도 | `exp(-0.005 × age_minutes)` — 0분=1.0, 1시간=0.74, 24시간≈0 | 0~1 |
| 속보 보너스 | Velocity Signal 감지 시 +10 | 0/10 |
| **최종** | `(영향도+신뢰도+구체성+방향성) × 신선도 + 속보보너스` | |

### 속보 감지 파이프라인

```
뉴스 수집 → 자카드 유사도 클러스터링 (≥0.3)
  → Velocity Signal: 동일 주제 3건+ / 다른 소스 / 15분 이내
    → 고영향 키워드: macro+geopolitical 2개+ & 60분 이내
      → breaking_score 계산 → 패스트패스 (≥15) 최상위 배치
```

### 2단계 LLM 브리핑 생성

```
[1차 호출] 매크로 전략가 프롬프트
  입력: 거시경제 데이터 + 뉴스 헤드라인
  출력: 시장 국면/리스크/기회/전망 (500자)

[2차 호출] 브리핑 작성자 프롬프트
  입력: 1차 분석 + 원본 데이터
  출력: 번호 리스트 10항목 브리핑 (500자)
  → 초과 시 축약 재요청
```

---

## 🤖 LLM 사용 방식

### 지원 모델

| Provider | 모델 | 용도 |
|----------|------|------|
| **Anthropic** | Claude Sonnet 4 | 시장 분석, 브리핑 생성, 속보 요약, 트럼프 영향도 판단 |
| **OpenAI** | GPT-4o | 동일 |
| **Google** | Gemini 2.0 Flash | 동일 |

### LLM 호출 지점

| 기능 | 호출 횟수/일 | 용도 |
|------|-------------|------|
| 모닝 브리핑 | 2~3회 | 정세 판단 + 브리핑 생성 + (축약) |
| 장 마감 리포트 | 2~3회 | 정세 판단 + 브리핑 생성 + (축약) |
| 장중 한마디 | 5회 | 한마디 생성 |
| 속보 자동 게시 | 0~3회 | 속보 요약 |
| 트럼프 모니터 | 10~20회 | 영향도 판단 (대부분 LOW) |
| **합계** | **~20~35회/일** | |

### 프롬프트 설계 원칙
- 역할 부여: "20년 경력 매크로 전략가", "주식시장 전문 브리핑 작성자"
- 데이터 기반: "제공된 데이터의 실제 수치만 사용"
- 포맷 강제: 번호 리스트 10항목, 각 25자 이내
- 길이 제한: 500자 이내 (초과 시 축약 재요청)

---

## 💰 예상 비용

### Anthropic Claude Sonnet 4 기준
- Input: $3 / 1M tokens
- Output: $15 / 1M tokens

| 항목 | 일일 호출 | 평균 토큰 (in/out) | 일일 비용 |
|------|----------|-------------------|----------|
| 브리핑 (모닝+마감) | 4~6회 | ~2K / ~500 | $0.03~0.06 |
| 장중 한마디 | 5회 | ~1.5K / ~200 | $0.02~0.04 |
| 속보 | 0~3회 | ~1K / ~300 | $0~0.02 |
| 트럼프 모니터 | 10~20회 | ~1K / ~200 | $0.03~0.08 |
| **합계** | **~20~35회** | | **$0.08~0.20** |

### 월간 비용 (개장일 22일 기준)

| 시나리오 | 월 비용 |
|----------|---------|
| 최소 (속보 없음) | ~$2 |
| 일반 | ~$3~5 |
| 최대 (속보+트럼프 활발) | ~$5~7 |

### OpenAI GPT-4o 기준
- Input: $2.50 / 1M tokens, Output: $10 / 1M tokens
- 월 비용: 약 $2~5 (Claude와 유사)

### Google Gemini 2.0 Flash 기준
- Input: $0.10 / 1M tokens, Output: $0.40 / 1M tokens
- 월 비용: 약 $0.1~0.5 (가장 저렴)

> 네이버 검색 API: 하루 25,000건 무료  
> Discord 웹훅, Threads API, Telegram Bot API, RSS 피드: 무료

---

## 🚀 설치 및 실행

### 1. 클론
```bash
git clone https://github.com/hideinbathroom/trend-analyzer-demo-01.git
cd trend-analyzer-demo-01
```

### 2. 의존성 설치
```bash
pip install -r requirements.txt
```

### 3. 환경변수 설정
```bash
cp .env.example .env
# .env 파일을 열어서 값을 채워주세요
```

### 4. 실행
```bash
python main.py
```

---

## ⚙️ 환경변수 (.env)

```env
# LLM 설정 (openai / anthropic / gemini 중 택1)
LLM_PROVIDER=anthropic
LLM_API_KEY=your_api_key_here

# Threads API (Meta Developer 포털에서 발급)
THREADS_ACCESS_TOKEN=your_token
THREADS_USER_ID=your_user_id

# Discord 웹훅
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# Telegram 봇 (@BotFather에서 생성)
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# 네이버 검색 API (https://developers.naver.com)
NAVER_CLIENT_ID=your_id
NAVER_CLIENT_SECRET=your_secret
```

---

## 📁 프로젝트 구조

```
├── main.py              # 메인 봇 (데이터 수집 + 속보 감지 + LLM 분석 + 멀티채널 게시 + 스케줄러)
├── requirements.txt     # Python 의존성
├── .env.example         # 환경변수 템플릿
├── .gitignore
├── README.md
└── output/              # 브리핑 저장 (자동 생성, git 제외)
```

---

## 📡 데이터 소스

| 소스 | 데이터 | 비용 |
|------|--------|------|
| Yahoo Finance (yfinance) | 미국/한국/유럽 지수, 환율, 원자재, 국채, 암호화폐 | 무료 |
| 네이버 금융 크롤링 | 코스피/코스닥 실시간 | 무료 |
| RSS 피드 9개 | 매경, 한경, 이데일리, 연합뉴스, 머니투데이 등 | 무료 |
| 네이버 뉴스 API | 동적 키워드 기반 뉴스 검색 | 무료 (25,000건/일) |
| CNN Fear & Greed | 공포탐욕지수 | 무료 |
| CNN Trump Archive | 트럼프 Truth Social 포스트 | 무료 |

---

## 🔧 기술 스택

| 구분 | 기술 |
|------|------|
| LLM | Claude Sonnet 4 / GPT-4o / Gemini 2.0 Flash (택1) |
| 데이터 수집 | yfinance, 네이버 금융 크롤링, RSS (feedparser), 네이버 뉴스 API |
| 속보 감지 | 자카드 유사도 클러스터링 + Velocity Signal + 지수 감쇠 신선도 |
| SNS 게시 | Threads API (Meta Graph API), Discord Webhook, Telegram Bot API |
| 스케줄링 | schedule + threading.Timer |
| 언어 | Python 3.10+ |

---

## 📝 라이선스

MIT License
