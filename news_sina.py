# -*- coding: utf-8 -*-
"""
news_sina.py —— 信息面因子（新浪财经滚动新闻）

把用户给的新浪财经股市滚动新闻作为「信息面」参数，映射到个股，
用金融词典法产出：个股情绪 sentiment(0~1) / 个股热度 heat(0~1) / 强负面标记 negative。

设计定位（与已定稿方案一致）：
  1. 信息面因子**仅作加分项 / 负面过滤项**，不单独破格入选；
  2. 负面（退市 / 立案 / 处罚等）置 negative=True，由调用方剔除候选；
  3. 无新闻或接口不可达时，调用方自动退回纯技术面打分；
  4. 零额外依赖（仅 requests，与 zt_common 一致），可回测、可解释。

依赖: requests
"""
import os, json, re, time
from datetime import datetime, date

import requests

WORKDIR = os.path.dirname(os.path.abspath(__file__))
NEWS_CACHE = os.path.join(WORKDIR, "news_cache_%s.json")
SINA_URL = "https://feed.mix.sina.com.cn/api/roll/get"
PAGEID = "153"
LID = "2509"

# ---------------- 金融情绪词典（中文，与项目零 ML 架构一致）----------------
POS_WORDS = ["利好", "涨停", "大涨", "中标", "签约", "回购", "增持", "收购", "获批",
             "扭亏", "预增", "重组", "高送转", "合作", "订单", "突破", "受益",
             "政策扶持", "业绩增长", "上调", "增资", "新签订单", "大单",
             "翻倍", "爆发", "领涨", "利好消息", "提振", "超预期", "创新高"]
NEG_WORDS = ["利空", "大跌", "退市", "暂停上市", "立案", "处罚", "违规", "减持",
             "商誉减值", "亏损", "下调", "风险警示", "警示函", "问询函", "诉讼",
             "暴跌", "爆雷", "负面", "降级", "被否", "终止", "查封", "冻结", "下调评级"]
# 强负面：触发候选剔除 / 高风险过滤
HARD_NEG = ["退市", "暂停上市", "立案", "处罚", "退市风险警示", "终止上市",
            "被实施退市风险警示", "重大违法", "财务造假", "遭立案调查"]
NEGATE = ["不", "未", "无", "没有", "并非", "并未", "暂未", "尚未", "免于", "免除", "不存在"]

# 常见简称别名（新闻常用简称 -> 全称关键词），提升命中率
ALIAS = {
    "茅台": "贵州茅台", "宁王": "宁德时代", "中芯": "中芯国际", "东财": "东方财富",
    "同花顺": "同花顺", "比亚迪": "比亚迪", "平安": "中国平安", "招行": "招商银行",
    "隆基": "隆基绿能", "立讯": "立讯精密", "三一": "三一重工", "格力": "格力电器",
    "美的": "美的集团", "五粮液": "五粮液",
}


def fetch_sina_news(num_pages=4, window_start=None):
    """抓取新浪财经滚动新闻。
    num_pages: 抓取页数（每页 50 条）；window_start: 仅保留 ctime>=该秒级时间戳的条目。
    返回 [(title, intro, ctime, stock_code_or_None), ...]（ctime 秒级时间戳，倒序去重）。
    """
    items = []
    for page in range(1, num_pages + 1):
        p = {"pageid": PAGEID, "lid": LID, "num": 50, "page": page,
             "versionNumber": "1.2.4", "callback": "news"}
        try:
            r = requests.get(SINA_URL, params=p, timeout=15,
                             headers={"User-Agent": "Mozilla/5.0",
                                      "Referer": "https://news.sina.com.cn/roll/"})
            r.encoding = "utf-8"
            raw = r.text
            # 解析：优先 JSONP 包裹 try{ news( ... ) }(e){}; 否则纯 JSON
            start = raw.find("news(")
            if start >= 0:
                start += 5
                obj, _ = json.JSONDecoder().raw_decode(raw, start)
            else:
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
            data = obj.get("result", {}).get("data", [])
            if not data:
                break
            for it in data:
                ctime = int(it.get("ctime") or 0)
                if window_start and ctime < window_start:
                    continue
                title = (it.get("title") or "").strip()
                intro = (it.get("wapsummary") or it.get("intro") or "").strip()
                code = None
                hq = it.get("hqChart") or ""
                m = re.search(r'"stockCode"\s*:\s*"([0-9]{6})"', hq)
                if m and m.group(1) != "000000":
                    code = m.group(1)
                items.append((title, intro, ctime, code))
        except Exception as e:
            print("[warn] 新浪新闻抓取失败 page=%d: %s" % (page, e))
            time.sleep(1)
    # 按时间倒序去重（标题+时间）
    seen = set()
    out = []
    for it in sorted(items, key=lambda x: -x[2]):
        key = (it[0], it[2])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _analyze_sentiment(text):
    """金融词典法情绪。返回 (score 0~1, pos次数, neg次数, hard_neg布尔)。"""
    pos = sum(text.count(w) for w in POS_WORDS)
    neg = sum(text.count(w) for w in NEG_WORDS)
    # 否定削弱：否定词紧贴利空词时扣减 neg
    for nw in NEGATE:
        for kw in NEG_WORDS:
            if nw + kw in text:
                neg = max(0, neg - 1)
    hard = any(h in text for h in HARD_NEG)
    net = pos - neg
    if net >= 0:
        score = 0.5 + 0.5 * min(1.0, net / 3.0)
    else:
        score = 0.5 - 0.5 * min(1.0, -net / 3.0)
    return score, pos, neg, hard


def _match_codes(text, code_by_name, alias_map, code_set):
    """返回文本命中的股票代码集合。"""
    matched = set()
    # 1) 全称命中
    for nm, c in code_by_name.items():
        if len(nm) >= 2 and nm in text:
            matched.add(c)
    # 2) 别名命中
    for al, c in alias_map.items():
        if al in text:
            matched.add(c)
    # 3) 6 位代码命中（仅当确为池内代码，降低噪声）
    for m in re.findall(r"(?<!\d)(\d{6})(?!\d)", text):
        if m in code_set:
            matched.add(m)
    return matched


def build_news_factor(pool, window_start=None):
    """pool: [(code, name), ...]
    返回 {code: {"sentiment":0~1, "heat":0~1, "pos":int, "neg":int,
                 "negative":bool, "samples":[title,...]}}。
    window_start 为秒级时间戳（仅统计该时间之后的新闻）。
    """
    code_by_name = {}
    for c, n in pool:
        n2 = (n or "").strip()
        if n2:
            code_by_name.setdefault(n2, c)
    alias_map = {}
    for alias, full in ALIAS.items():
        for c, n in pool:
            if full and full in (n or ""):
                alias_map[alias] = c
                break
    code_set = {c for c, n in pool}

    news = fetch_sina_news(num_pages=4, window_start=window_start)
    agg = {}
    for title, intro, ctime, code in news:
        text = title + "。" + intro
        matched = _match_codes(text, code_by_name, alias_map, code_set)
        # stockCode 仅在池内才采信：新浪偶发返回 561890 等非 A 股/无效代码，
        # 池外代码一律丢弃，保证 news_map 只含候选池标的
        if code and code in code_set:
            matched.add(code)
        if not matched:
            continue
        s, pos, neg, hard = _analyze_sentiment(text)
        for c in matched:
            a = agg.setdefault(c, {"pos": 0, "neg": 0, "hard": False,
                                   "samples": [], "score_sum": 0.0})
            a["pos"] += pos
            a["neg"] += neg
            a["hard"] = a["hard"] or hard
            a["score_sum"] += s
            a["samples"].append(title)

    out = {}
    for c, a in agg.items():
        mentions = len(a["samples"])
        heat = min(1.0, mentions / 5.0)
        sentiment = a["score_sum"] / mentions if mentions else 0.5
        if a["hard"]:                      # 强负面：情绪压到 0，交由调用方过滤
            sentiment = 0.0
        out[c] = {"sentiment": sentiment, "heat": heat, "pos": a["pos"],
                  "neg": a["neg"], "negative": a["hard"],
                  "samples": a["samples"][:5]}
    return out


def load_or_build(pool, cache_date, window_start=None):
    """带缓存的新闻因子：优先读 news_cache_<date>.json，否则构建并落盘。
    返回 (factor_dict, from_cache_bool)。
    """
    path = NEWS_CACHE % cache_date
    if cache_date and os.path.exists(path):
        try:
            d = json.load(open(path, encoding="utf-8"))
            if d.get("date") == cache_date and "factor" in d:
                return d["factor"], True
        except Exception:
            pass
    factor = build_news_factor(pool, window_start=window_start)
    try:
        json.dump({"date": cache_date,
                   "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "count": len(factor), "factor": factor},
                  open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass
    return factor, False


if __name__ == "__main__":
    import sys
    test_pool = [("600519", "贵州茅台"), ("300750", "宁德时代"),
                 ("000001", "平安银行"), ("002594", "比亚迪")]
    ws = int((datetime.now().timestamp() - 3 * 86400))
    f, cached = load_or_build(test_pool, date.today().isoformat(), window_start=ws)
    print("news factor count:", len(f), "(cached=%s)" % cached)
    for c, v in list(f.items())[:8]:
        print("  %s  情绪=%.2f 热度=%.2f 负面=%s 样本=%s"
              % (c, v["sentiment"], v["heat"], v["negative"], v["samples"][:1]))
