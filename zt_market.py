# -*- coding: utf-8 -*-
"""
zt_market.py —— 市场维度(题材/板块/情绪)因子模块

接口契约来自 review_close.py 与 zt_common.score_stock：
  - ensure_market_rules : 向 rules 增量注入市场维度因子与 market 节，不覆盖已学习权重；
  - collect_board_context: 取当日实际涨停，结合东方财富涨停池 enrichment，
    构建市场指数 mkt_idx（情绪周期 / 热点板块 / 热点题材），并落盘 last_board.json
    供次日盘前复用；网络不可达时退化为"仅情绪(由涨停广度推导)"的最小可用版本；
  - load_board_context : 读取 T-1 的 last_board.json（次日盘前复用）；
  - leave_one_out      : 复盘学习时扣掉个股自身对板块/题材计数的贡献，避免自证循环；
  - stock_market_scores: 返回 (题材热度分, 板块效应分)，基于个股所属热点题材/板块；
  - emotion_multiplier : 情绪周期乘子（强/中/弱），由涨停广度推导；
  - score_with_market  : 向后兼容包装，直接复用 zt_common.score_stock；
  - review_market_report: 市场维度复盘文本。

与 zt_common.score_stock 的分工：score_stock 统一产出 comps（含 topic_effect /
block_effect / emotion_M），本模块只负责"提供 mkt_idx 上下文"与"由 mkt_idx 推导
个股的题材/板块/情绪得分"，不直接参与 base 因子归一化。
"""
import os
import json
import re
import copy
import time

try:
    import requests
except Exception:
    requests = None

WORKDIR = os.path.dirname(os.path.abspath(__file__))
LAST_BOARD = os.path.join(WORKDIR, "last_board.json")

_EM_HTTP = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}

_DEFAULT_MARKET = {
    "block": {"min_limit_ups": 3, "cap": 8},
    "emotion": {"strong_at": 80, "weak_at": 40,
                "strong_M": 1.1, "neutral_M": 1.0, "weak_M": 0.9},
    "topic": {"min_topic_count": 2, "cap": 6},
}


def ensure_market_rules(rules):
    """向 rules 增量注入市场维度因子与 market 节，不覆盖任何已学习权重。"""
    m = rules.setdefault("market", {})
    for sec, dv in _DEFAULT_MARKET.items():
        m.setdefault(sec, dict(dv))
    factors = rules.setdefault("factors", {})
    for k, w in (("topic_effect", 0.0516), ("block_effect", 0.0516)):
        if k not in factors:
            factors[k] = {"weight": w}
    return rules


def _load_rules_raw():
    """只读最新 rules（不触发 zt_common 的保存副作用），供阈值/情绪推导。"""
    try:
        import zt_common as C
        if os.path.exists(C.RULES_FILE):
            return json.load(open(C.RULES_FILE, encoding="utf-8")) or {}
    except Exception:
        pass
    return {"market": _DEFAULT_MARKET}


def _emotion_from_breadth(total, rules):
    """由涨停广度推导情绪周期乘子 M。"""
    em = ((rules.get("market") or {}).get("emotion") or {}) or _DEFAULT_MARKET["emotion"]
    strong_at = em.get("strong_at", 80)
    weak_at = em.get("weak_at", 40)
    if total >= strong_at:
        return float(em.get("strong_M", 1.1))
    if total <= weak_at:
        return float(em.get("weak_M", 0.9))
    return float(em.get("neutral_M", 1.0))


def _fetch_em_zt_pool(datestr):
    """抓取东方财富涨停池，返回 [{code, name, industry, concepts:[...]}]。
    失败/不可达返回 []（由调用方 graceful fallback）。"""
    if requests is None:
        return []
    ymd = datestr.replace("-", "")
    urls = [
        "https://push2ex.eastmoney.com/getAllZTPool?ut=7eea3edcaed734bea9cbfc24409ed989"
        "&date=%s&Pageindex=0&Pagesize=1000" % ymd,
        "https://push2ex.eastmoney.com/getZTBB?ut=7eea3edcaed734bea9cbfc24409ed989"
        "&date=%s&pn=1&pz=1000" % ymd,
    ]
    for url in urls:
        try:
            r = requests.get(url, headers=_EM_HTTP, timeout=10)
            d = r.json()
            items = ((d.get("data") or {}).get("pool")
                     or d.get("data") or [])
            if not items:
                continue
            out = []
            for it in items:
                code = str(it.get("c") or it.get("code") or "")
                if not code:
                    continue
                name = it.get("n") or it.get("name") or ""
                ind = (it.get("hy") or "").strip()
                zs = it.get("zs") or it.get("zt") or ""
                cons = [x.strip() for x in re.split(r"[,，]", zs) if x.strip()] if zs else []
                out.append({"code": code, "name": name,
                            "industry": ind, "concepts": cons})
            if out:
                return out
        except Exception:
            continue
    return []


def collect_board_context(datestr, actual_zt):
    """采集当日涨停板上下文 -> board dict，board['index'] 即 mkt_idx。"""
    rules = _load_rules_raw()
    total = len(actual_zt) if actual_zt else 0

    mkt = {
        "date": datestr,
        "total": total,
        "emotion_M": _emotion_from_breadth(total, rules),
        "boards": {},
        "topics": {},
        "stocks": {},
    }

    # 尝试东方财富涨停池 enrichment（行业/题材）
    try:
        pool = _fetch_em_zt_pool(datestr) or []
    except Exception:
        pool = []

    boards, topics, stocks = {}, {}, {}
    seen = set()
    for it in pool:
        c = it["code"]
        seen.add(c)
        ind = it.get("industry") or ""
        cons = it.get("concepts") or []
        if ind:
            boards[ind] = boards.get(ind, 0) + 1
        for t in cons:
            topics[t] = topics.get(t, 0) + 1
        stocks[c] = {"industry": ind, "concepts": cons}
    # actual_zt 中不在池内的，仍登记 code（行业/题材未知，不计入热点）
    for c in (actual_zt or []):
        if c not in seen:
            stocks.setdefault(c, {"industry": "", "concepts": []})

    mkt["boards"] = boards
    mkt["topics"] = topics
    mkt["stocks"] = stocks

    # 热点阈值（来自 rules.market）
    mrule = (rules.get("market") or {}) or _DEFAULT_MARKET
    min_block = (mrule.get("block") or {}).get("min_limit_ups", 3)
    min_topic = (mrule.get("topic") or {}).get("min_topic_count", 2)
    cap_block = (mrule.get("block") or {}).get("cap", 8)
    cap_topic = (mrule.get("topic") or {}).get("cap", 6)
    mkt["hot_boards"] = sorted([k for k, v in boards.items() if v >= min_block],
                               key=lambda k: -boards[k])[:cap_block]
    mkt["hot_topics"] = sorted([k for k, v in topics.items() if v >= min_topic],
                               key=lambda k: -topics[k])[:cap_topic]

    board = {"index": mkt}
    try:
        json.dump(mkt, open(LAST_BOARD, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    except Exception:
        pass
    return board


def load_board_context(datestr=None):
    """从 last_board.json 加载 T-1 涨停板上下文；不可用返回 None。"""
    if not os.path.exists(LAST_BOARD):
        return None
    try:
        return json.load(open(LAST_BOARD, encoding="utf-8"))
    except Exception:
        return None


def leave_one_out(mkt_idx, code=None, name=None):
    """复盘学习时扣掉个股自身对板块/题材计数的贡献，避免自证循环。"""
    if not mkt_idx or not code:
        return mkt_idx
    m = copy.deepcopy(mkt_idx)
    s = (m.get("stocks") or {}).get(code)
    if s:
        ind = s.get("industry")
        if ind and ind in m.get("boards", {}):
            m["boards"][ind] = max(0, m["boards"][ind] - 1)
        for t in s.get("concepts", []):
            if t in m.get("topics", {}):
                m["topics"][t] = max(0, m["topics"][t] - 1)
    return m


def _norm(v, cap=8):
    return max(0.0, min(1.0, float(v) / float(cap)))


def stock_market_scores(code, name, mkt_idx, rules):
    """返回 (题材热度分, 板块效应分)；无市场上下文时 (0.0, 0.0)。"""
    if not mkt_idx:
        return (0.0, 0.0)
    mrule = (rules.get("market") or {}) if isinstance(rules, dict) else {}
    cap_block = (mrule.get("block") or {}).get("cap", 8)
    cap_topic = (mrule.get("topic") or {}).get("cap", 6)
    s = (mkt_idx.get("stocks") or {}).get(code or "")
    if not s:
        return (0.0, 0.0)
    ind = s.get("industry") or ""
    cons = s.get("concepts") or []
    hot_boards = set(mkt_idx.get("hot_boards") or [])
    hot_topics = set(mkt_idx.get("hot_topics") or [])

    # 板块效应：行业涨停集中度 + 是否热点板块
    block_score = 0.0
    if ind:
        cnt = mkt_idx.get("boards", {}).get(ind, 0)
        block_score = _norm(cnt, cap_block)
        if ind in hot_boards:
            block_score = max(block_score, 0.8)

    # 题材热度：命中热点题材数量（命中>=3个记满分）
    hit = sum(1 for t in cons if t in hot_topics)
    topic_score = _norm(hit, 3)
    if cons and hot_topics:
        topic_score = max(topic_score,
                          _norm(len([t for t in cons if t in hot_topics]), cap_topic))
    return (topic_score, block_score)


def emotion_multiplier(mkt_idx, rules):
    """情绪周期乘子；无市场上下文时返回 1.0（中性）。"""
    if not mkt_idx:
        return 1.0
    return float(mkt_idx.get("emotion_M", 1.0)) or 1.0


def score_with_market(f, sp, rules, news=None, code=None, name=None, mkt_idx=None):
    """向后兼容包装：直接复用 zt_common.score_stock（comps 由其统一产出）。
    news: 个股信息面因子字典(可空)，需透传给 score_stock 参与 news_sentiment/news_heat 子分。
    """
    import zt_common as C
    return C.score_stock(f, sp, rules, news=news, code=code, name=name, mkt_idx=mkt_idx)


def review_market_report(mkt_idx, cand_codes, rules):
    """市场维度复盘报告。"""
    if not mkt_idx:
        return "(市场维度上下文不可用：板块/题材/情绪数据未采集)"
    lines = []
    lines.append("涨停总数: %d  情绪乘子 M=%.2f" % (
        mkt_idx.get("total", 0), mkt_idx.get("emotion_M", 1.0)))
    hb = mkt_idx.get("hot_boards") or []
    ht = mkt_idx.get("hot_topics") or []
    if hb:
        lines.append("热点板块(前%d): %s" % (len(hb), "、".join(hb)))
    else:
        lines.append("热点板块: 无（当日无板块达到涨停集中度阈值）")
    if ht:
        lines.append("热点题材(前%d): %s" % (len(ht), "、".join(ht)))
    else:
        lines.append("热点题材: 无")
    if cand_codes and (hb or ht):
        cov = []
        hb_set, ht_set = set(hb), set(ht)
        for c in cand_codes:
            s = (mkt_idx.get("stocks") or {}).get(c)
            if not s:
                continue
            if (s.get("industry") in hb_set
                    or any(t in ht_set for t in s.get("concepts", []))):
                cov.append(c)
        if cov:
            lines.append("候选中踩中热点: %s" % "、".join(cov))
    return "\n".join(lines)
