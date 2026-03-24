"""
Trend Analyzer Demo — 주식 시장 트렌드 분석 & 자동 브리핑 봇
- 뉴스 수집 (RSS + 네이버 API) + 속보 감지
- 트럼프 Truth Social 모니터링
- LLM 기반 시장 분석 & 브리핑 생성
- Threads / Discord / Telegram 자동 게시
"""

import os
import re
import time
import math
import json
import random
import logging
import threading
import schedule
import requests
import yfinance as yf
import feedparser
from datetime import datetime, timedelta, timezone
import email.utils
import calendar
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import concurrent.futures

load_dotenv()

# ===== 로깅 설정 =====
from logging.handlers import RotatingFileHandler
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler("bot.log", encoding="utf-8", maxBytes=5*1024*1024, backupCount=3),
    ],
)
logger = logging.getLogger(__name__)

# ===== 환경 변수 =====
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN", "")
THREADS_USER_ID = os.getenv("THREADS_USER_ID", "")
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ===== 속보 감지 설정 =====
FRESHNESS_DECAY_LAMBDA = float(os.getenv("FRESHNESS_DECAY_LAMBDA", "0.005"))
BREAKING_SCORE_THRESHOLD = int(os.getenv("BREAKING_SCORE_THRESHOLD", "15"))
BREAKING_BONUS_BASE = int(os.getenv("BREAKING_BONUS_BASE", "10"))
BREAKING_FAST_PATH_MAX = int(os.getenv("BREAKING_FAST_PATH_MAX", "2"))
VELOCITY_TIME_WINDOW_MINUTES = int(os.getenv("VELOCITY_TIME_WINDOW_MINUTES", "15"))
VELOCITY_MIN_SOURCES = int(os.getenv("VELOCITY_MIN_SOURCES", "3"))
JACCARD_THRESHOLD = float(os.getenv("JACCARD_THRESHOLD", "0.3"))
DAILY_FLASH_LIMIT = int(os.getenv("DAILY_FLASH_LIMIT", "3"))
FLASH_POST_SCORE_THRESHOLD = int(os.getenv("FLASH_POST_SCORE_THRESHOLD", "20"))
FLASH_POLL_INTERVAL_SECONDS = int(os.getenv("FLASH_POLL_INTERVAL_SECONDS", "600"))
FLASH_COOLDOWN_MINUTES = int(os.getenv("FLASH_COOLDOWN_MINUTES", "15"))

# 트럼프 모니터 설정
TRUMP_POLL_INTERVAL = int(os.getenv("TRUMP_POLL_INTERVAL", "1800"))
TRUMP_DAILY_POST_LIMIT = int(os.getenv("TRUMP_DAILY_POST_LIMIT", "3"))
TRUMP_ARCHIVE_URL = "https://www.cnn.com/api/cnn/trump-social-media-posts/posts.json"
TRUMP_RANGE_BYTES = 65536

KST = timezone(timedelta(hours=9))

# ===== 휴장일 체크 =====
KOREAN_HOLIDAYS_2026 = {
    (1,1),(2,16),(2,17),(2,18),(3,1),(3,2),(5,5),(5,24),(5,25),
    (6,6),(8,15),(10,3),(10,4),(10,5),(10,6),(10,9),(12,25),
}

def is_market_day(dt=None):
    if dt is None: dt = datetime.now()
    if dt.weekday() >= 5: return False
    if (dt.month, dt.day) in KOREAN_HOLIDAYS_2026: return False
    return True


# ============================================================
# 1. 거시경제 데이터 수집
# ============================================================
def _fetch_naver_index(code: str) -> dict:
    """네이버 금융에서 국내 지수 실시간 데이터 크롤링."""
    try:
        url = f"https://finance.naver.com/sise/sise_index.naver?code={code}"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        now_val_el = soup.select_one("#now_value")
        if not now_val_el: return {}
        price = float(now_val_el.text.strip().replace(",", ""))
        change_pct = 0.0
        change_el = soup.select_one("#change_value_and_rate")
        if change_el:
            pct_match = re.search(r'([+-]?\d+\.?\d*)%', change_el.text.strip())
            if pct_match: change_pct = float(pct_match.group(1))
        quotient_el = soup.select_one("#quotient")
        if quotient_el:
            classes = quotient_el.get("class", [])
            if "dn" in classes: change_pct = -abs(change_pct)
            elif "up" in classes: change_pct = abs(change_pct)
        return {"price": round(price, 2), "change_pct": round(change_pct, 2)}
    except Exception as e:
        logger.warning(f"네이버 {code} 크롤링 실패: {e}")
        return {}


def fetch_premarket_data() -> dict:
    """장 전 전용 데이터 수집"""
    data = {}
    # 미국 전일 종가
    for name, ticker in {"나스닥":"^IXIC","S&P500":"^GSPC","다우존스":"^DJI"}.items():
        try:
            hist = yf.Ticker(ticker).history(period="5d")
            if len(hist) >= 2:
                close, prev = hist["Close"].iloc[-1], hist["Close"].iloc[-2]
                data[name] = {"price": round(close,2), "change_pct": round(((close-prev)/prev)*100,2)}
        except: pass
    # 미국 선물
    for name, ticker in {"S&P500선물":"ES=F","나스닥선물":"NQ=F"}.items():
        try:
            hist = yf.Ticker(ticker).history(period="5d")
            if len(hist) >= 2:
                close, prev = hist["Close"].iloc[-1], hist["Close"].iloc[-2]
                data[name] = {"price": round(close,2), "change_pct": round(((close-prev)/prev)*100,2)}
        except: pass
    # 한국 지수
    for name, code in {"코스피(전일)":"KOSPI","코스닥(전일)":"KOSDAQ"}.items():
        naver = _fetch_naver_index(code)
        if naver: data[name] = naver
    # 야간 지표
    for name, ticker in {"달러/원":"KRW=X","VIX":"^VIX","WTI":"CL=F","금":"GC=F","비트코인":"BTC-USD","미국10년물국채":"^TNX"}.items():
        try:
            hist = yf.Ticker(ticker).history(period="5d")
            if len(hist) >= 2:
                close, prev = hist["Close"].iloc[-1], hist["Close"].iloc[-2]
                data[name] = {"price": round(close,2), "change_pct": round(((close-prev)/prev)*100,2)}
        except: pass
    # 공포탐욕지수
    try:
        resp = requests.get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
                          headers={"User-Agent":"Mozilla/5.0"}, timeout=10)
        if resp.status_code == 200:
            fg = resp.json().get("fear_and_greed",{})
            data["공포탐욕지수"] = {"score": round(fg.get("score",0),1), "rating": fg.get("rating","N/A")}
    except: pass
    # 유럽
    for name, ticker in {"유로스톡스50":"^STOXX50E","DAX":"^GDAXI"}.items():
        try:
            hist = yf.Ticker(ticker).history(period="5d")
            if len(hist) >= 2:
                close, prev = hist["Close"].iloc[-1], hist["Close"].iloc[-2]
                data[name] = {"price": round(close,2), "change_pct": round(((close-prev)/prev)*100,2)}
        except: pass
    logger.info(f"장 전 데이터 수집 완료: {list(data.keys())}")
    return data


def fetch_macro_data() -> dict:
    """장중/장후 거시경제 지표 수집"""
    data = {}
    # 한국 지수 (네이버 우선)
    for name, code in {"코스피":"KOSPI","코스닥":"KOSDAQ"}.items():
        naver = _fetch_naver_index(code)
        if naver: data[name] = naver
    # Yahoo Finance
    tickers = {"나스닥":"^IXIC","S&P500":"^GSPC","다우존스":"^DJI","코스피":"^KS11","코스닥":"^KQ11",
               "VIX":"^VIX","달러/원":"KRW=X","WTI":"CL=F","미국10년물국채":"^TNX","비트코인":"BTC-USD","금":"GC=F"}
    for name, ticker in tickers.items():
        if name in data: continue
        try:
            hist = yf.Ticker(ticker).history(period="5d")
            if len(hist) >= 2:
                close, prev = hist["Close"].iloc[-1], hist["Close"].iloc[-2]
                data[name] = {"price": round(close,2), "change_pct": round(((close-prev)/prev)*100,2)}
        except: data[name] = {"price":"N/A","change_pct":0}
    # 공포탐욕지수
    try:
        resp = requests.get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
                          headers={"User-Agent":"Mozilla/5.0"}, timeout=10)
        if resp.status_code == 200:
            fg = resp.json().get("fear_and_greed",{})
            data["공포탐욕지수"] = {"score": round(fg.get("score",0),1), "rating": fg.get("rating","N/A")}
    except: pass
    logger.info(f"거시경제 데이터 수집 완료: {list(data.keys())}")
    return data


def format_macro_summary(data: dict) -> str:
    lines = ["[거시경제 데이터 요약]"]
    for name, info in data.items():
        if name == "공포탐욕지수":
            lines.append(f"- {name}: {info.get('score','N/A')} ({info.get('rating','N/A')})")
        else:
            price, chg = info.get("price","N/A"), info.get("change_pct",0)
            lines.append(f"- {name}: {price} ({'+' if chg>0 else ''}{chg}%)")
    return "\n".join(lines)


def format_premarket_summary(data: dict) -> str:
    sections = []
    sections.append("[미국 증시 전일 마감]")
    for n in ["나스닥","S&P500","다우존스"]:
        i = data.get(n,{})
        if i: sections.append(f"- {n}: {i['price']} ({'+' if i['change_pct']>0 else ''}{i['change_pct']}%)")
    sections.append("\n[미국 선물]")
    for n in ["S&P500선물","나스닥선물"]:
        i = data.get(n,{})
        if i: sections.append(f"- {n}: {i['price']} ({'+' if i['change_pct']>0 else ''}{i['change_pct']}%)")
    sections.append("\n[유럽 증시 전일 마감]")
    for n in ["유로스톡스50","DAX"]:
        i = data.get(n,{})
        if i: sections.append(f"- {n}: {i['price']} ({'+' if i['change_pct']>0 else ''}{i['change_pct']}%)")
    sections.append("\n[한국 증시 전일 마감]")
    for n in ["코스피(전일)","코스닥(전일)"]:
        i = data.get(n,{})
        if i: sections.append(f"- {n}: {i['price']} ({'+' if i['change_pct']>0 else ''}{i['change_pct']}%)")
    sections.append("\n[야간/글로벌 지표]")
    for n in ["달러/원","VIX","WTI","금","비트코인","미국10년물국채"]:
        i = data.get(n,{})
        if i: sections.append(f"- {n}: {i['price']} ({'+' if i['change_pct']>0 else ''}{i['change_pct']}%)")
    fg = data.get("공포탐욕지수",{})
    if fg: sections.append(f"- 공포탐욕지수: {fg.get('score','N/A')} ({fg.get('rating','N/A')})")
    return "\n".join(sections)


# ============================================================
# 2. 뉴스 수집 + 속보 감지 시스템
# ============================================================
_recent_news_cache = []
_recent_news_cache_date = None

_RSS_FEEDS = [
    ("매경증권","https://www.mk.co.kr/rss/30100041/","domestic",10),
    ("한경금융","https://rss.hankyung.com/feed/finance","domestic",10),
    ("이데일리증권","https://rss.edaily.co.kr/edaily/stock.xml","domestic",10),
    ("연합뉴스경제","https://www.yna.co.kr/rss/economy.xml","domestic",5),
    ("머니투데이","https://rss.mt.co.kr/mt_news_stock.xml","domestic",10),
    ("연합뉴스속보","https://www.yna.co.kr/rss/news.xml","domestic",3),
    ("한경글로벌","https://rss.hankyung.com/feed/globalmarket","global",10),
    ("매경국제","https://www.mk.co.kr/rss/30300018/","global",10),
    ("매경IT","https://www.mk.co.kr/rss/30200030/","theme",15),
]

_MARKET_IMPACT_KEYWORDS = {
    "macro": {"weight":3,"keywords":["금리","기준금리","연준","fed","fomc","인플레이션","cpi","gdp","경기침체","환율","달러","국채"]},
    "geopolitical": {"weight":3,"keywords":["전쟁","제재","관세","무역전쟁","중동","러시아","우크라이나","대만","이란","이스라엘","opec","유가"]},
    "flow": {"weight":2,"keywords":["외국인","기관","순매수","순매도","수급","공매도","etf"]},
    "corporate": {"weight":2,"keywords":["실적","영업이익","매출","어닝","자사주","배당","합병","인수","공시"]},
    "sector": {"weight":1,"keywords":["반도체","ai","인공지능","2차전지","배터리","전기차","바이오","방산","코스피","코스닥","나스닥"]},
}

_TITLE_STOPWORDS = {"에서","으로","에게","하는","하고","있는","없는","그리고","하지만","오늘","내일","어제","대한","위한","이번"}

_last_poll_times = {}


def _should_poll_feed(source_name, interval):
    now = datetime.now(KST)
    last = _last_poll_times.get(source_name)
    if last is None: return True
    return (now - last).total_seconds() / 60 >= interval


def _extract_rss_timestamp(entry):
    for field in ("published_parsed","updated_parsed"):
        parsed = entry.get(field)
        if parsed:
            try:
                utc_dt = datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
                return (utc_dt.astimezone(KST), False)
            except: continue
    return (datetime.now(KST), True)


def _extract_naver_timestamp(pub_date_str):
    if not pub_date_str: return (datetime.now(KST), True)
    try:
        return (email.utils.parsedate_to_datetime(pub_date_str).astimezone(KST), False)
    except: return (datetime.now(KST), True)


def _calculate_age_minutes(published_at):
    if published_at.tzinfo is None: published_at = published_at.replace(tzinfo=KST)
    return max(0.0, (datetime.now(KST) - published_at).total_seconds() / 60)


def _calculate_freshness(age_minutes):
    return max(0.0, min(1.0, math.exp(-FRESHNESS_DECAY_LAMBDA * max(0, age_minutes))))


def _tokenize_title(title):
    tokens = set()
    for w in re.findall(r'[가-힣]{2,6}', title):
        if w not in _TITLE_STOPWORDS: tokens.add(w)
    for w in re.findall(r'[a-zA-Z]{2,}', title):
        tokens.add(w.lower())
    return tokens


def _jaccard_similarity(a, b):
    if not a and not b: return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _cluster_news_by_topic(news_items):
    n = len(news_items)
    if n == 0: return []
    parent = list(range(n))
    def find(x):
        while parent[x] != x: parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def union(x,y):
        rx,ry = find(x),find(y)
        if rx != ry: parent[ry] = rx
    tokens = [_tokenize_title(item.get("title","")) for item in news_items]
    for i in range(n):
        for j in range(i+1,n):
            if _jaccard_similarity(tokens[i],tokens[j]) >= JACCARD_THRESHOLD: union(i,j)
    clusters = {}
    for i in range(n):
        r = find(i)
        clusters.setdefault(r,[]).append(news_items[i])
    return list(clusters.values())


def _detect_velocity_signals(clusters):
    result = []
    for cluster in clusters:
        if len(cluster) < VELOCITY_MIN_SOURCES: continue
        sources = {item.get("source","") for item in cluster}
        if len(sources) < VELOCITY_MIN_SOURCES: continue
        ts = [item["published_at"] for item in cluster if item.get("published_at")]
        if len(ts) >= 2 and (max(ts)-min(ts)).total_seconds()/60 <= VELOCITY_TIME_WINDOW_MINUTES:
            result.append(cluster)
    return result


def _detect_breaking_news(news_items):
    for item in news_items:
        item.setdefault("is_breaking", False)
        item.setdefault("breaking_score", 0.0)
    if not news_items: return news_items
    clusters = _cluster_news_by_topic(news_items)
    velocity_clusters = _detect_velocity_signals(clusters)
    for cluster in velocity_clusters:
        for item in cluster:
            item["breaking_score"] = item.get("breaking_score",0) + 10
    for item in news_items:
        text = (item.get("title","") + " " + item.get("snippet","")).lower()
        if item.get("age_minutes", float("inf")) > 60: continue
        matched = 0
        for cat in ["macro","geopolitical"]:
            for kw in _MARKET_IMPACT_KEYWORDS[cat]["keywords"]:
                if kw in text: matched += 1
        if matched >= 2: item["breaking_score"] = item.get("breaking_score",0) + 5
    for item in news_items:
        if item.get("breaking_score",0) > 0: item["is_breaking"] = True
    breaking = [i for i in news_items if i.get("is_breaking")]
    if breaking:
        for i in breaking:
            logger.warning(f"🚨 속보 감지: [{i.get('title','')}] score={i.get('breaking_score',0):.1f}")
    return news_items


def _score_news_item(title, snippet, dynamic_keywords, age_minutes=0, is_breaking=False,
                     breaking_score=0, source_name="", category=""):
    text = (title + " " + snippet).lower()
    impact = 0
    for cat_info in _MARKET_IMPACT_KEYWORDS.values():
        for kw in cat_info["keywords"]:
            if kw in text: impact += cat_info["weight"]; break
    for dkw in dynamic_keywords:
        for w in dkw.lower().split():
            if len(w) >= 2 and w in text: impact += 2; break
    HIGH = {"연합뉴스경제","연합뉴스속보","한경금융","한경글로벌","이데일리증권"}
    MID = {"매경증권","매경국제","매경IT","머니투데이","네이버뉴스"}
    source_trust = 3 if source_name in HIGH else 2 if source_name in MID else 1
    specificity = 0
    if re.search(r'\d+[\.\d]*\s*[%원달러조억만배]', text): specificity += 1
    if re.search(r'삼성|SK|LG|현대|카카오|네이버|테슬라|엔비디아|FOMC', text, re.IGNORECASE): specificity += 1
    sentiment = 0
    strong = ["급락","급등","폭락","폭등","쇼크","위기","사상최고","사상최저"]
    if any(k in text for k in strong): sentiment = 2
    freshness = _calculate_freshness(age_minutes)
    bonus = float(BREAKING_BONUS_BASE) if is_breaking else 0.0
    base = impact + source_trust + specificity + sentiment
    return {"impact_score":impact,"source_trust":source_trust,"specificity_score":specificity,
            "sentiment_score":sentiment,"freshness_score":freshness,"breaking_bonus":bonus,
            "final_score": base * freshness + bonus}


def _generate_dynamic_keywords(macro_data):
    keywords = []
    kospi = macro_data.get("코스피", macro_data.get("코스피(전일)",{}))
    if abs(kospi.get("change_pct",0)) >= 1.5:
        keywords.append(f"코스피 {'급락' if kospi['change_pct']<0 else '급등'}")
    fx = macro_data.get("달러/원",{})
    if abs(fx.get("change_pct",0)) >= 0.8: keywords.append("환율 원달러")
    wti = macro_data.get("WTI",{})
    if abs(wti.get("change_pct",0)) >= 2.0: keywords.append("유가")
    nasdaq = macro_data.get("나스닥",{})
    if abs(nasdaq.get("change_pct",0)) >= 1.5: keywords.append("나스닥 미국증시")
    return keywords


def _naver_news_search(query, display=5, sort="date"):
    if not NAVER_CLIENT_ID: return []
    try:
        resp = requests.get("https://openapi.naver.com/v1/search/news.json",
            headers={"X-Naver-Client-Id":NAVER_CLIENT_ID,"X-Naver-Client-Secret":NAVER_CLIENT_SECRET},
            params={"query":query,"display":display,"sort":sort}, timeout=10)
        if resp.status_code != 200: return []
        results = []
        for item in resp.json().get("items",[]):
            title = BeautifulSoup(item.get("title",""),"html.parser").get_text().strip()
            desc = BeautifulSoup(item.get("description",""),"html.parser").get_text().strip()
            link = item.get("originallink","") or item.get("link","")
            pub_at, est = _extract_naver_timestamp(item.get("pubDate",""))
            age = _calculate_age_minutes(pub_at)
            results.append({"source":"네이버뉴스","title":title,"snippet":desc[:150],"link":link,
                "category":"realtime_breaking" if age<=30 else "realtime",
                "published_at":pub_at,"age_minutes":age,"timestamp_estimated":est,
                "is_breaking":False,"breaking_score":0,"fast_path":False,"final_score":0,"score":0})
        return results
    except: return []


def _fetch_rss_news():
    all_news = []
    for src, url, cat, interval in _RSS_FEEDS:
        if not _should_poll_feed(src, interval): continue
        _last_poll_times[src] = datetime.now(KST)
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:8]:
                title = entry.get("title","").strip()
                summary = entry.get("summary","").strip()
                if summary: summary = BeautifulSoup(summary,"html.parser").get_text()[:150]
                if title and len(title) > 10:
                    pub_at, est = _extract_rss_timestamp(entry)
                    all_news.append({"source":src,"category":cat,"title":title,"snippet":summary,
                        "link":entry.get("link",""),"published_at":pub_at,
                        "age_minutes":_calculate_age_minutes(pub_at),"timestamp_estimated":est,
                        "is_breaking":False,"breaking_score":0,"fast_path":False,"final_score":0,"score":0})
        except Exception as e:
            logger.warning(f"RSS 수집 실패 ({src}): {e}")
    logger.info(f"RSS 수집: {len(all_news)}건")
    return all_news


def fetch_news(macro_data=None):
    """뉴스 수집 → 속보 감지 → 스코어링 → 상위 8건 선별"""
    global _recent_news_cache, _recent_news_cache_date
    today = datetime.now().date()
    if _recent_news_cache_date != today:
        _recent_news_cache = []
        _recent_news_cache_date = today

    all_news = _fetch_rss_news()
    dynamic_kw = _generate_dynamic_keywords(macro_data) if macro_data else []

    # 네이버 API 보강
    search_queries = ["주식 시장 속보","코스피"] + dynamic_kw[:3]
    for q in search_queries[:5]:
        all_news.extend(_naver_news_search(q, display=3))

    # 속보 감지
    _detect_breaking_news(all_news)

    # 캐시 업데이트
    _recent_news_cache.extend(all_news)
    if len(_recent_news_cache) > 200:
        _recent_news_cache = _recent_news_cache[-200:]

    # 스코어링
    for item in all_news:
        scores = _score_news_item(item.get("title",""), item.get("snippet",""), dynamic_kw,
            item.get("age_minutes",0), item.get("is_breaking",False),
            item.get("breaking_score",0), item.get("source",""), item.get("category",""))
        item.update(scores)

    # 패스트패스 + 정렬
    fast, rest = [], []
    for item in all_news:
        if item.get("breaking_score",0) >= BREAKING_SCORE_THRESHOLD: fast.append(item)
        else: rest.append(item)
    fast.sort(key=lambda x: x.get("breaking_score",0), reverse=True)
    fast = fast[:BREAKING_FAST_PATH_MAX]
    for f in fast: f["fast_path"] = True
    rest.sort(key=lambda x: x.get("final_score",0), reverse=True)
    selected = fast + rest[:8-len(fast)]

    # 텍스트 생성
    lines = ["[주요 뉴스 헤드라인]"]
    breaking_news = [i for i in all_news if i.get("is_breaking")]
    for i, item in enumerate(selected, 1):
        tag = "🚨" if item.get("fast_path") else "🔴" if item["final_score"]>=4 else "🟡" if item["final_score"]>=2 else "⚪"
        lines.append(f"{tag} {i}. [{item['source']}] {item['title']} (score:{item['final_score']:.1f})")
    news_text = "\n".join(lines)

    breaking_info = {"selected_news": selected, "breaking_news": breaking_news,
                     "has_breaking": len(breaking_news)>0}
    logger.info(f"뉴스 선별 완료: {len(selected)}건 (속보 {len(breaking_news)}건)")
    return news_text, breaking_info


# ============================================================
# 3. LLM 호출 (OpenAI / Anthropic / Gemini)
# ============================================================
def _call_llm(system: str, user_message: str, max_tokens=1000, temperature=0.7) -> str:
    """LLM_PROVIDER에 따라 적절한 API 호출"""
    if LLM_PROVIDER == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=LLM_API_KEY)
        resp = client.chat.completions.create(
            model="gpt-4o", messages=[
                {"role":"system","content":system},
                {"role":"user","content":user_message}],
            max_tokens=max_tokens, temperature=temperature)
        return resp.choices[0].message.content.strip()

    elif LLM_PROVIDER == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=LLM_API_KEY)
        resp = client.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=max_tokens,
            system=system, messages=[{"role":"user","content":user_message}])
        return resp.content[0].text.strip()

    elif LLM_PROVIDER == "gemini":
        import google.generativeai as genai
        genai.configure(api_key=LLM_API_KEY)
        model = genai.GenerativeModel(model_name="gemini-2.0-flash", system_instruction=system)
        resp = model.generate_content(user_message)
        return resp.text.strip()

    else:
        raise ValueError(f"지원하지 않는 LLM_PROVIDER: {LLM_PROVIDER} (openai/anthropic/gemini 중 선택)")


# ===== 프롬프트 =====
MARKET_ANALYSIS_PROMPT = """당신은 20년 경력의 글로벌 매크로 전략가입니다.
제공된 실시간 데이터를 종합 분석하여 시장 정세를 판단하세요.

[분석 항목]
1. 시장 국면 판단: 강세/약세/혼조/전환기
2. 핵심 리스크 요인 2가지
3. 기회 요인 2가지
4. 뉴스 헤드라인에서 읽히는 시장 내러티브
5. 향후 1~2일 단기 전망

[규칙] 제공된 데이터 수치만 근거로 사용. 500자 내외."""

MORNING_SYSTEM_PROMPT = """당신은 한국 주식시장 전문 브리핑 작성자입니다.
오늘 날짜: {date}

[포맷 — 번호 리스트 10항목]
{date} - 어제 밤부터 아침사이에 일어난 일

1. 미국 증시 핵심 (25자 이내)
2. 야간 지표 핵심 (25자 이내)
3~8. 주요 뉴스 6건 (각 25자 이내)
9. 오늘 장 전망 한줄
10. 팔로우&하트로 매일 속보와 브리핑 확인

[규칙]
- 각 항목 25자 이내
- 제공된 데이터의 실제 수치만 사용
- 해시태그 금지
- 총 500자 이내"""

CLOSING_SYSTEM_PROMPT = """당신은 한국 주식시장 전문 브리핑 작성자입니다.
오늘 날짜: {date}

[포맷 — 번호 리스트 10항목]
{date} - 오늘 주식 시장 일어난 일
[한줄요약 25자 이내]

1. 코스피 지수 + 등락률 (25자 이내)
2. 코스닥 + 환율 또는 수급 (25자 이내)
3~8. 주요 뉴스 6건 (각 25자 이내)
9. 내일 전망 한줄
10. 팔로우&하트로 매일 속보와 브리핑 확인

[규칙]
- 각 항목 25자 이내
- 제공된 데이터의 실제 수치만 사용
- 해시태그 금지
- 총 500자 이내

본문 끝에 ---THEME--- 구분자 후 테마 섹션 작성:
🔥 오늘의 주도 테마
- 테마명: 관련 종목 2~3개 (각 한줄)
(테마 섹션도 500자 이내)"""

TRUMP_IMPACT_PROMPT = """너는 한국 주식시장 전문가야.
트럼프의 Truth Social 발언을 보고 한국 주식시장/글로벌 경제에 미치는 영향을 판단해.

[HIGH 기준] 관세/무역, 금리/연준, 전쟁/군사, 에너지/원유, 중국/한국 언급
[LOW 기준] 생일 축하, 정치 공격, 선거 캠페인, 개인 자랑, 미국 내정

[출력 형식]
IMPACT: HIGH 또는 LOW

(HIGH일 경우에만)
---POST---
(장중한마디: 반말, 2~4줄, 100~200자, 해시태그 금지)"""


def get_date_string():
    weekdays = ["월","화","수","목","금","토","일"]
    now = datetime.now()
    return f"{now.month:02d}월{now.day:02d}일 {weekdays[now.weekday()]}요일"


def generate_briefing(briefing_type, macro_text, macro_data=None):
    """2단계 LLM: 정세 판단 → 브리핑 생성"""
    date_str = get_date_string()
    system_prompt = MORNING_SYSTEM_PROMPT if briefing_type == "morning" else CLOSING_SYSTEM_PROMPT

    # 1차: 시장 정세 판단
    analysis = _call_llm(MARKET_ANALYSIS_PROMPT,
        f"아래는 실시간 수집된 시장 데이터입니다.\n\n{macro_text}", max_tokens=800, temperature=0.4)
    logger.info(f"시장 정세 판단 완료:\n{analysis}")

    # 2차: 브리핑 생성
    combined = f"[AI 시장 정세 판단]\n{analysis}\n\n[원본 데이터]\n{macro_text}"
    briefing = _call_llm(
        system_prompt.replace("{date}", date_str),
        f"위 정세 판단을 핵심 관점으로, 원본 데이터의 실제 수치를 인용하여 브리핑 작성. 500자 이내.\n\n{combined}")

    # 500자 초과 시 축약
    if len(briefing) > 500:
        logger.warning(f"브리핑 {len(briefing)}자 → 축약 재요청")
        briefing = _call_llm(system_prompt.replace("{date}", date_str),
            f"아래 브리핑이 {len(briefing)}자입니다. 500자 이내로 줄여주세요.\n\n{briefing}")
    return briefing


# ============================================================
# 4. 게시 시스템 (Threads + Discord + Telegram)
# ============================================================
def _publish_to_threads(text, reply_to_id=None, topic_tag=None):
    """Threads API 단일 게시. 성공 시 post_id, 실패 시 None"""
    if not THREADS_ACCESS_TOKEN or not THREADS_USER_ID:
        logger.warning("Threads 토큰 미설정, 게시 스킵")
        return None
    create_url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads"
    payload = {"media_type":"TEXT","text":text,"access_token":THREADS_ACCESS_TOKEN}
    if reply_to_id: payload["reply_to_id"] = reply_to_id
    if topic_tag and not reply_to_id: payload["topic_tag"] = topic_tag
    try:
        resp = requests.post(create_url, data=payload)
        creation_id = resp.json().get("id")
        if not creation_id:
            logger.error(f"Threads 컨테이너 생성 실패: {resp.json()}")
            return None
        pub_resp = requests.post(
            f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads_publish",
            data={"creation_id":creation_id,"access_token":THREADS_ACCESS_TOKEN})
        post_id = pub_resp.json().get("id")
        if post_id:
            logger.info(f"✅ Threads 게시 성공 (ID: {post_id})")
        return post_id
    except Exception as e:
        logger.error(f"Threads 게시 실패: {e}")
        return None


def post_to_threads(text):
    """Threads 게시 (본문 + 테마 댓글 분리)"""
    if "---THEME---" in text:
        parts = text.split("---THEME---", 1)
        main_text, theme_text = parts[0].strip(), parts[1].strip()
    else:
        main_text, theme_text = text.strip(), None

    # 소수점 → 정수 (스팸 필터 방지)
    main_text = re.sub(r'(\d+)\.(\d+)', lambda m: str(round(float(m.group(0)))), main_text)
    if theme_text:
        theme_text = re.sub(r'(\d+)\.(\d+)', lambda m: str(round(float(m.group(0)))), theme_text)

    if len(main_text) > 500: main_text = main_text[:500]
    parent_id = _publish_to_threads(main_text, topic_tag="주식")
    if not parent_id: return False

    if theme_text:
        if len(theme_text) > 500: theme_text = theme_text[:500]
        delay = random.randint(30, 60)
        logger.info(f"테마 댓글 대기 {delay}초")
        time.sleep(delay)
        _publish_to_threads(theme_text, reply_to_id=parent_id)
    return True


def send_to_discord(text, embed_title="📰 브리핑"):
    """Discord 웹훅으로 전송"""
    if not DISCORD_WEBHOOK_URL: return
    try:
        payload = {"embeds":[{"title":embed_title,"description":text[:4096],"color":0xE67E22,
                   "footer":{"text":datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")}}]}
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        logger.info("✅ Discord 전송 성공")
    except Exception as e:
        logger.warning(f"Discord 전송 실패: {e}")


def send_news_to_discord(selected_news, context=""):
    """뉴스 헤드라인을 Discord Embed로 전송"""
    if not DISCORD_WEBHOOK_URL or not selected_news: return
    lines = []
    for i, item in enumerate(selected_news, 1):
        tag = "🚨" if item.get("fast_path") else "🔴" if item.get("final_score",0)>=4 else "🟡" if item.get("final_score",0)>=2 else "⚪"
        title = item.get("title","")[:60]
        link = item.get("link","")
        line = f"{tag} **{i}.** [{title}]({link})" if link else f"{tag} **{i}.** {title}"
        line += f"  `{item.get('source','')}` `{item.get('final_score',0):.1f}점`"
        lines.append(line)
    text = "\n".join(lines)
    footer = f"{datetime.now(KST).strftime('%Y-%m-%d %H:%M')} | {context} | 총 {len(selected_news)}건"
    try:
        payload = {"embeds":[{"title":"📰 뉴스 헤드라인","description":text[:4096],"color":0xE67E22,
                   "footer":{"text":footer}}]}
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        logger.info(f"✅ Discord 뉴스 전송 ({context})")
    except Exception as e:
        logger.warning(f"Discord 뉴스 전송 실패: {e}")


def send_to_telegram(text, parse_mode="HTML"):
    """Telegram 봇으로 메시지 전송"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        # HTML 태그 이스케이프 (Telegram은 일부 HTML만 지원)
        clean_text = text.replace("<","&lt;").replace(">","&gt;") if parse_mode == "HTML" else text
        payload = {"chat_id":TELEGRAM_CHAT_ID,"text":clean_text[:4096],"parse_mode":parse_mode}
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info("✅ Telegram 전송 성공")
        else:
            logger.warning(f"Telegram 전송 실패: {resp.text}")
    except Exception as e:
        logger.warning(f"Telegram 전송 실패: {e}")


def broadcast(text, embed_title="📰 브리핑"):
    """모든 채널에 동시 게시 (Threads + Discord + Telegram)"""
    post_to_threads(text)
    # Discord/Telegram에는 ---THEME--- 구분자 제거 후 전송
    clean = text.replace("---THEME---", "\n\n")
    send_to_discord(clean, embed_title)
    send_to_telegram(clean, parse_mode="HTML")


# ============================================================
# 5. 트럼프 Truth Social 모니터
# ============================================================
class TrumpMonitor:
    """트럼프 Truth Social 모니터링 → LLM 영향도 판단 → 자동 포스팅"""
    def __init__(self):
        self._last_seen_id = None
        self._running = False
        self._thread = None
        self._daily_count = 0
        self._daily_count_date = None
        self._posted_ids = set()

    def start(self):
        if self._running: return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info(f"🇺🇸 트럼프 모니터 시작 (폴링: {TRUMP_POLL_INTERVAL}초)")

    def stop(self):
        self._running = False

    def _loop(self):
        # 초기화: 최신 ID만 기록
        try:
            posts = self._fetch()
            if posts: self._last_seen_id = posts[0]["id"]
        except: pass

        while self._running:
            try:
                today = datetime.now().date()
                if self._daily_count_date != today:
                    self._daily_count = 0
                    self._daily_count_date = today
                if self._daily_count >= TRUMP_DAILY_POST_LIMIT:
                    time.sleep(TRUMP_POLL_INTERVAL); continue

                new_posts = self._check_new()
                for post in new_posts:
                    if self._daily_count >= TRUMP_DAILY_POST_LIMIT: break
                    self._process(post)
            except Exception as e:
                logger.error(f"트럼프 모니터 에러: {e}", exc_info=True)
            time.sleep(TRUMP_POLL_INTERVAL)

    def _fetch(self):
        try:
            resp = requests.get(TRUMP_ARCHIVE_URL, headers={"Range":f"bytes=0-{TRUMP_RANGE_BYTES}"}, timeout=20)
            raw = resp.text.strip()
            last = raw.rfind("},")
            if last > 0: raw = raw[:last+1] + "]"
            if not raw.startswith("["): raw = "[" + raw
            return json.loads(raw)
        except: return []

    def _check_new(self):
        posts = self._fetch()
        new = []
        for p in posts:
            if p["id"] == self._last_seen_id: break
            content = BeautifulSoup(p.get("content",""),"html.parser").get_text().strip()
            if len(content) >= 20: new.append(p)
        if new and posts: self._last_seen_id = posts[0]["id"]
        return new

    def _process(self, post):
        pid = post["id"]
        if pid in self._posted_ids: return
        content = BeautifulSoup(post.get("content",""),"html.parser").get_text().strip()
        url = post.get("url","")
        logger.info(f"🇺🇸 트럼프 분석: '{content[:60]}...'")
        try:
            result = _call_llm(TRUMP_IMPACT_PROMPT,
                f"[트럼프 Truth Social]\n시각: {post.get('created_at','')}\n내용: {content}\n\n영향도 판단 후 HIGH면 한마디 작성.",
                max_tokens=400, temperature=0.8)
            if "IMPACT: HIGH" in result and "---POST---" in result:
                post_text = result.split("---POST---",1)[1].strip()
                if len(post_text) < 30: return
                if url:
                    merged = f"{post_text}\n\n📎 트럼프 원문: {url}"
                    if len(merged) <= 500: post_text = merged
                if len(post_text) > 500: post_text = post_text[:500]
                logger.info(f"🇺🇸 HIGH → 게시:\n{post_text}")
                broadcast(post_text, "🇺🇸 트럼프 시장 영향")
                self._posted_ids.add(pid)
                self._daily_count += 1
            else:
                logger.info(f"🇺🇸 LOW → 스킵")
        except Exception as e:
            logger.error(f"트럼프 처리 실패: {e}")


# ============================================================
# 6. 속보 자동 게시 모니터
# ============================================================
class BreakingNewsMonitor:
    """속보 모니터링 → score >= threshold → 자동 게시"""
    def __init__(self):
        self.daily_count = 0
        self.daily_count_date = None
        self._running = False
        self._thread = None
        self._posted_titles = set()

    def start(self):
        if self._running: return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info(f"🚨 속보 모니터 시작 (폴링: {FLASH_POLL_INTERVAL_SECONDS}초, 임계값: {FLASH_POST_SCORE_THRESHOLD})")

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            try:
                today = datetime.now().date()
                if self.daily_count_date != today:
                    self.daily_count = 0
                    self.daily_count_date = today
                if self.daily_count >= DAILY_FLASH_LIMIT or not is_market_day():
                    time.sleep(FLASH_POLL_INTERVAL_SECONDS); continue

                macro_data = fetch_macro_data()
                _, breaking_info = fetch_news(macro_data)
                for item in breaking_info.get("breaking_news",[]):
                    if self.daily_count >= DAILY_FLASH_LIMIT: break
                    if item.get("breaking_score",0) < FLASH_POST_SCORE_THRESHOLD: continue
                    if item.get("age_minutes",0) > 30: continue
                    title = item.get("title","")
                    # 간단한 중복 체크
                    tokens = _tokenize_title(title)
                    dup = any(_jaccard_similarity(tokens, _tokenize_title(t)) > 0.5 for t in self._posted_titles)
                    if dup: continue

                    # LLM으로 속보 포스트 생성
                    post_text = _call_llm(
                        "너는 주식시장 속보 전문가야. 아래 속보를 2~3줄로 요약하고 시장 영향을 분석해. 반말, 500자 이내, 해시태그 금지.",
                        f"속보: {title}\n요약: {item.get('snippet','')}\n출처: {item.get('source','')}")
                    if len(post_text) > 500: post_text = post_text[:500]

                    link = item.get("link","")
                    if link:
                        merged = f"{post_text}\n\n📎 {link}"
                        if len(merged) <= 500: post_text = merged

                    broadcast(post_text, "🚨 속보")
                    send_news_to_discord([item], "🚨 속보")
                    self._posted_titles.add(title)
                    self.daily_count += 1
                    logger.info(f"🚨 속보 게시 완료 ({self.daily_count}/{DAILY_FLASH_LIMIT})")
            except Exception as e:
                logger.error(f"속보 모니터 에러: {e}", exc_info=True)
            time.sleep(FLASH_POLL_INTERVAL_SECONDS)


# ============================================================
# 7. 메인 작업 함수
# ============================================================
def save_briefing(briefing, briefing_type):
    os.makedirs("output", exist_ok=True)
    filename = f"output/{datetime.now().strftime('%Y%m%d')}_{briefing_type}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(briefing)
    logger.info(f"📄 저장: {filename}")
    return filename


def _extract_top_headlines(news_text, count=6):
    lines = [l.strip() for l in news_text.split("\n") if l.strip() and not l.startswith("[")]
    return lines[:count]


def run_morning_briefing():
    """오전 8시 장 전 브리핑"""
    if not is_market_day():
        logger.info("📅 휴장일 — 모닝 브리핑 스킵"); return
    logger.info("=" * 50)
    logger.info("🌅 모닝 브리핑 시작")
    try:
        data = fetch_premarket_data()
        text = format_premarket_summary(data)
        news_text, breaking_info = fetch_news(data)
        headlines = _extract_top_headlines(news_text, 6)
        if headlines:
            text += f"\n\n[주요 뉴스]\n" + "\n".join(f"  · {h}" for h in headlines)
        briefing = generate_briefing("morning", text, data)
        logger.info(f"브리핑:\n{briefing}")
        save_briefing(briefing, "morning")
        broadcast(briefing, "🌅 모닝 브리핑")
        send_news_to_discord(breaking_info.get("selected_news",[]), "모닝 브리핑")
    except Exception as e:
        logger.error(f"모닝 브리핑 실패: {e}", exc_info=True)


def run_closing_briefing():
    """오후 4시 50분 장 마감 리포트"""
    if not is_market_day():
        logger.info("📅 휴장일 — 장 마감 스킵"); return
    logger.info("=" * 50)
    logger.info("🌇 장 마감 리포트 시작")
    try:
        data = fetch_macro_data()
        text = format_macro_summary(data)
        news_text, breaking_info = fetch_news(data)
        headlines = _extract_top_headlines(news_text, 6)
        if headlines:
            text += f"\n\n[주요 뉴스]\n" + "\n".join(f"  · {h}" for h in headlines)
        briefing = generate_briefing("closing", text, data)
        logger.info(f"브리핑:\n{briefing}")
        save_briefing(briefing, "closing")
        broadcast(briefing, "🌇 장 마감 리포트")
        send_news_to_discord(breaking_info.get("selected_news",[]), "장 마감")
    except Exception as e:
        logger.error(f"장 마감 실패: {e}", exc_info=True)


def run_intraday_update():
    """장중 한마디"""
    logger.info("⚡ 장중 한마디 시작")
    try:
        data = fetch_macro_data()
        text = format_macro_summary(data)
        news_text, breaking_info = fetch_news(data)
        headlines = _extract_top_headlines(news_text, 3)
        prompt_data = text
        if headlines:
            prompt_data += "\n\n[최신 뉴스]\n" + "\n".join(f"  · {h}" for h in headlines)

        post = _call_llm(
            "너는 20대 후반 직장인 투자자야. 시장 데이터를 보고 짧은 한마디를 써. "
            "반말, 1~3줄, 100자 이내, 해시태그 금지, 존댓말 금지, 단정적으로.",
            f"아래 데이터를 보고 한마디 써:\n\n{prompt_data}",
            max_tokens=200, temperature=0.9)
        if len(post) > 500: post = post[:500]
        broadcast(post, "⚡ 장중 한마디")
        send_news_to_discord(breaking_info.get("selected_news",[]), "한마디")
    except Exception as e:
        logger.error(f"한마디 실패: {e}", exc_info=True)


# ============================================================
# 8. 스케줄러
# ============================================================
_update_timers = []

def generate_intraday_timeline():
    today = datetime.now().date()
    slots = [
        [("07:00","07:29"),("08:31","08:59")],
        [("09:00","12:00")],
        [("13:00","15:30")],
        [("17:21","19:00")],
        [("22:00","23:59")],
    ]
    timeline = []
    for opts in slots:
        s, e = random.choice(opts)
        s_dt = datetime.combine(today, datetime.strptime(s,"%H:%M").time())
        e_dt = datetime.combine(today, datetime.strptime(e,"%H:%M").time())
        diff = int((e_dt - s_dt).total_seconds() / 60)
        if diff > 0:
            timeline.append(s_dt + timedelta(minutes=random.randint(0, diff)))
    return timeline


def schedule_daily_updates():
    global _update_timers
    for t in _update_timers: t.cancel()
    _update_timers.clear()
    timeline = generate_intraday_timeline()
    now = datetime.now()
    logger.info(f"📋 한마디 타임라인: {', '.join(t.strftime('%H:%M') for t in timeline)}")
    for t in timeline:
        delay = (t - now).total_seconds()
        if delay > 0:
            timer = threading.Timer(delay, run_intraday_update)
            timer.daemon = True
            timer.start()
            _update_timers.append(timer)


def main():
    logger.info("🤖 Trend Analyzer Demo 시작!")
    logger.info(f"   LLM: {LLM_PROVIDER}")
    logger.info(f"   Threads: {'✅' if THREADS_ACCESS_TOKEN else '❌'}")
    logger.info(f"   Discord: {'✅' if DISCORD_WEBHOOK_URL else '❌'}")
    logger.info(f"   Telegram: {'✅' if TELEGRAM_BOT_TOKEN else '❌'}")

    # 스케줄 등록
    schedule.every().day.at("06:50").do(schedule_daily_updates)
    schedule.every().day.at("08:00").do(run_morning_briefing)
    schedule.every().day.at("16:50").do(run_closing_briefing)

    # 즉시 실행
    schedule_daily_updates()

    # 속보 모니터 시작
    flash_monitor = BreakingNewsMonitor()
    flash_monitor.start()

    # 트럼프 모니터 시작
    trump_monitor = TrumpMonitor()
    trump_monitor.start()

    logger.info("⏳ 스케줄 대기 중... (Ctrl+C로 종료)")
    while True:
        try:
            schedule.run_pending()
            time.sleep(30)
        except KeyboardInterrupt:
            logger.info("🛑 봇 종료")
            break
        except Exception as e:
            logger.error(f"메인 루프 에러: {e}", exc_info=True)
            time.sleep(30)


if __name__ == "__main__":
    main()
