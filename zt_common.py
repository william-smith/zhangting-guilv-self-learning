# -*- coding: utf-8 -*-
"""
zt_common.py —— 涨停候选筛选 共享引擎
数据源：新浪日线K线(量能/均线/涨停基因/突破) + 腾讯批量快照(流通市值/换手率/涨跌幅)
仅依赖 requests / akshare(用于一次性取全A代码列表并缓存)。
"""
import os, json, time, statistics, re, subprocess, sys
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor

WORKDIR = os.path.dirname(os.path.abspath(__file__))
CODES_CACHE = os.path.join(WORKDIR, "codes_cache.json")
RULES_FILE = os.path.join(WORKDIR, "rules.json")
HISTORY_FILE = os.path.join(WORKDIR, "history.jsonl")
KLINES_CACHE = os.path.join(WORKDIR, "klines_cache.json")

DEFAULT_RULES = {
    "version": 1,
    "updated_at": "",
    "candidate_count": 25,
    "min_volume_ratio": 1.3,          # 量能放大阈值(昨量/前5日均量)，可自学习调整
    "factors": {
        "volume_ratio": {"weight": 0.22},   # 量能放大(核心)
        "zt_gene":      {"weight": 0.18},   # 涨停基因(近20日涨停次数)
        "breakout":     {"weight": 0.15},   # 突破创高
        "ma_bull":      {"weight": 0.12},   # 均线多头排列
        "strength":     {"weight": 0.13},   # 近期强度(近10日涨幅)
        "small_cap":    {"weight": 0.10},   # 小市值偏好
        "turnover":     {"weight": 0.10},   # 换手放大
        "news_sentiment": {"weight": 0.10}, # 信息面: 新闻情绪(利好加分)
        "news_heat":      {"weight": 0.05}, # 信息面: 新闻热度(关注度)
    },
    "filters": {
        "min_close_price": 2.0,      # 剔除仙股/退市风险
        "exclude_st": False,         # 是否剔除ST
        "exclude_bj": True,          # 剔除北交所(30%涨跌停,玩法不同)
    },
}

import requests


def ensure_deps():
    try:
        import akshare  # noqa
    except Exception:
        subprocess.run([sys.executable, "-m", "pip", "install",
                        "akshare", "--break-system-packages", "-q"], check=False)


def tenc_code(code):
    """6位代码 -> 腾讯/新浪前缀代码；北交所(8/4开头)返回None"""
    if code.startswith("6"):
        return "sh" + code
    if code.startswith("0") or code.startswith("3"):
        return "sz" + code
    return None


def limit_pct(name, code):
    """统计口径阈值：涨幅达到该幅度即计入, 不要求封板(以收盘涨幅为准)。

    【口径确认 2026-10-19】"上涨10%就统计进去, 不一定封板"。
    因此全市场统一按 10% 阈值(0.098, 调用方再留 0.2pct 容差),
    创业板/科创板(涨停板20%)同样只要求涨满10%, 未封板也计入 —— 这与
    通达信"涨停"选股(严格封板口径)不是同一统计口径, 数量差异属预期, 非缺陷。

    例外: ST/退市股涨停板仅 5%, 按其自身涨停幅度 0.048 判定。

    ⚠️ 维护提示: 本函数收到的 code 为 6 位裸代码(无 sh/sz 前缀, 见 tenc_code 调用面)。
    旧版在此判断 startswith("sh688")/("sz30") 属永不触发的死分支, 已移除;
    请勿改回按板块 20% 判定, 那会改变上述已确认的统计口径。
    """
    n = (name or "").upper()
    if "ST" in n or "*" in n or "退" in n:
        return 0.048
    return 0.098


# ---------------- 代码列表(缓存) ----------------
def get_codes(force=False):
    if not force and os.path.exists(CODES_CACHE):
        try:
            data = json.load(open(CODES_CACHE, encoding="utf-8"))
            if (date.today() - date.fromisoformat(data["date"])).days < 7:
                return data["items"]
        except Exception:
            pass
    ensure_deps()
    import akshare as ak
    items = []
    # 优先新浪全市场快照(沙箱内稳定)；失败再试 akshare 内置列表(含深交所,可能SSL不稳)
    try:
        df = ak.stock_zh_a_spot()
        items = [(str(r["代码"]), str(r["名称"])) for _, r in df.iterrows()]
    except Exception as e:
        print("[warn] stock_zh_a_spot 失败, 回退:", e)
        try:
            df = ak.stock_info_a_code_name()
            items = [(str(r["code"]), str(r["name"])) for _, r in df.iterrows()]
        except Exception as e2:
            print("[error] 代码列表获取失败:", e2)
    if items:
        # 归一化：去掉 sh/sz/bj 前缀，统一为6位纯数字(北交所剥离后由 tenc_code 剔除)
        norm = []
        for c, n in items:
            c = str(c).strip().lower()
            if c.startswith(("sh", "sz", "bj")):
                c = c[2:]
            norm.append((c, n))
        json.dump({"date": date.today().isoformat(), "items": norm},
                  open(CODES_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
        return norm
    return items


# ---------------- 日线K线(主源:东方财富历史K线 / 兜底:新浪) ----------------
def _fetch_kline_em(sym, n=70):
    """东方财富历史K线(push2his)"""
    code = sym[2:] if sym[:2] in ("sh", "sz") else sym
    sec = ("1." + code) if code.startswith("6") else ("0." + code)
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    p = {"secid": sec, "fields1": "f1,f2,f3", "fields2": "f51,f52,f53,f54,f55,f56",
         "klt": "101", "fqt": "0", "beg": "0", "end": "20500101", "lmt": str(n)}
    for _ in range(2):
        try:
            r = requests.get(url, p, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
            d = r.json()
            kl = d.get("data", {}).get("klines", [])
            if kl:
                out = []
                for row in kl[-n:]:
                    x = row.split(",")
                    if len(x) < 6:
                        continue
                    out.append({"day": x[0], "open": float(x[1]), "close": float(x[2]),
                                "high": float(x[3]), "low": float(x[4]), "volume": float(x[5])})
                if out:
                    return out
        except Exception:
            time.sleep(0.3)
    return None


def _fetch_kline_tx_kline(sym, n=70):
    """腾讯日K(ifzq.gtimg.cn/appstock/app/kline/kline)，沙箱内稳定可用。
    返回行格式: [day, open, close, high, low, volume(手)]，volume 单位为手。"""
    url = "https://ifzq.gtimg.cn/appstock/app/kline/kline"
    p = {"param": "%s,day,,,%d" % (sym, n)}
    for _ in range(2):
        try:
            r = requests.get(url, p, timeout=8,
                             headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
            d = r.json()
            node = (d.get("data") or {}).get(sym) or {}
            rows = node.get("day") or node.get("qfqday")
            if not rows:
                return None
            out = []
            for row in rows[-n:]:
                if len(row) < 6:
                    continue
                out.append({"day": row[0], "open": float(row[1]), "close": float(row[2]),
                            "high": float(row[3]), "low": float(row[4]), "volume": float(row[5])})
            if out:
                return out
        except Exception:
            time.sleep(0.3)
    return None


def _fetch_kline_sina_cn(sym, n=70):
    """新浪日K(quotes.sina.cn openapi)。原始 volume 单位为股，统一折算为手。"""
    url = "https://quotes.sina.cn/cn/api/openapi.php/CN_MarketDataService.getKLineData"
    p = {"symbol": sym, "scale": 240, "ma": "no", "datalen": n}
    for _ in range(2):
        try:
            r = requests.get(url, p, timeout=8,
                             headers={"User-Agent": "Mozilla/5.0",
                                      "Referer": "https://finance.sina.com.cn"})
            d = r.json()
            rows = (d.get("result") or {}).get("data") or []
            out = []
            for x in rows[-n:]:
                out.append({"day": x["day"], "open": float(x["open"]),
                            "close": float(x["close"]), "high": float(x["high"]),
                            "low": float(x["low"]), "volume": float(x["volume"]) / 100.0})
            if out:
                return out
        except Exception:
            time.sleep(0.3)
    return None


def _fetch_kline_sohu(sym, n=70):
    """搜狐历史K线兜底。返回降序行:
    [day, open, close, 涨跌额, 涨跌幅, low, high, volume(手), 成交额(万), 换手]"""
    code = sym[2:] if sym[:2] in ("sh", "sz") else sym
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=int(n * 1.8) + 40)).isoformat()
    url = "https://q.stock.sohu.com/hisHq?code=cn_%s&start=%s&end=%s" % (code, start, end)
    for _ in range(2):
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            d = r.json()
            if not isinstance(d, list) or not d or not d[0].get("hq"):
                return None
            rows = d[0]["hq"]
            out = []
            for row in reversed(rows[-n:]):
                out.append({"day": row[0], "open": float(row[1]), "close": float(row[2]),
                            "high": float(row[6]), "low": float(row[5]),
                            "volume": float(row[7])})
            if out:
                return out
        except Exception:
            time.sleep(0.3)
    return None


def _fetch_kline_tencent(sym, n=70):
    """腾讯K线兜底(沙箱/内网场景 eastmoney/sina 不可达时启用)，返回与 compute_kline_features 兼容的列表。"""
    code = sym[2:] if sym[:2] in ("sh", "sz") else sym
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    p = {"param": "%s,day,,,%d,qfq" % (sym, n)}
    for _ in range(3):
        try:
            r = requests.get(url, p, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            d = r.json()
            node = d.get("data", {}).get(sym, {}).get("qfqday") or \
                   d.get("data", {}).get(code, {}).get("qfqday")
            if not node:
                return None
            out = []
            for row in node[-n:]:
                if len(row) < 6:
                    continue
                out.append({"day": row[0], "open": float(row[1]), "close": float(row[2]),
                            "high": float(row[3]), "low": float(row[4]), "volume": float(row[5])})
            if out:
                return out
        except Exception:
            time.sleep(0.4)
    return None


def _fetch_kline_sina(sym, n=70):
    url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    p = {"symbol": sym, "scale": 240, "ma": "no", "datalen": n}
    for _ in range(2):
        try:
            r = requests.get(url, p, headers={"Referer": "https://finance.sina.com.cn"}, timeout=8)
            d = json.loads(r.text)
            if isinstance(d, list) and d:
                return d
        except Exception:
            time.sleep(0.4)
    return None


# ---------------- K线多源容灾调度 ----------------
# 沙箱/内网环境下东财、web.ifzq、money.finance 可能被拦，故加入可用备源；
# 一旦某源首次成功即记为首选源，后续调用直接命中，避免逐只遍历全部源。
_KL_PREF = {"src": None}
KLINE_SOURCES = [
    ("em", _fetch_kline_em),
    ("tx_kline", _fetch_kline_tx_kline),
    ("sina_cn", _fetch_kline_sina_cn),
    ("sohu", _fetch_kline_sohu),
    ("tx_fq", _fetch_kline_tencent),
    ("sina", _fetch_kline_sina),
]


def fetch_kline(sym, n=70):
    """日线K线统一入口: 东财 → 腾讯kline → 新浪openapi → 搜狐 → 腾讯fqkline → 新浪老接口"""
    order = list(KLINE_SOURCES)
    pref = _KL_PREF["src"]
    if pref:
        order.sort(key=lambda kv: 0 if kv[0] == pref else 1)
    for name, fn in order:
        try:
            kl = fn(sym, n)
        except Exception:
            kl = None
        if kl:
            _KL_PREF["src"] = name
            return kl
    return None


def compute_kline_features(kl):
    closes = [float(x["close"]) for x in kl]
    highs = [float(x["high"]) for x in kl]
    lows = [float(x["low"]) for x in kl]
    vols = [float(x["volume"]) for x in kl]
    n = len(closes)
    if n < 30:
        return None
    vma5 = statistics.mean(vols[-6:-1]) or 1
    vratio = vols[-1] / vma5                       # 量能放大: 昨量/前5日均量
    ma5 = statistics.mean(closes[-5:])
    ma10 = statistics.mean(closes[-10:])
    ma20 = statistics.mean(closes[-20:])
    ma60 = statistics.mean(closes[-60:]) if n >= 60 else statistics.mean(closes)
    zt20 = 0
    zt5 = 0
    for i in range(1, min(20, n - 1)):
        chg = (closes[-i] - closes[-i - 1]) / closes[-i - 1]
        if chg >= 0.095:
            zt20 += 1
            if i <= 5:
                zt5 += 1
    hh20 = max(highs[-20:])
    newhigh20 = closes[-1] >= hh20 * 0.995         # 创20日新高(突破)
    bull = (ma5 > ma10 > ma20 > ma60)              # 均线多头
    chg1 = (closes[-1] - closes[-2]) / closes[-2]
    chg5 = (closes[-1] - closes[-5]) / closes[-5]
    chg10 = (closes[-1] - closes[-10]) / closes[-10]
    above20 = closes[-1] >= ma20
    above60 = closes[-1] >= ma60
    hh60 = max(highs[-60:]) if n >= 60 else max(highs)
    drawdown = (hh60 - closes[-1]) / hh60           # 距60日高点回撤
    return dict(vratio=vratio, ma5=ma5, ma10=ma10, ma20=ma20, ma60=ma60,
                zt20=zt20, zt5=zt5, newhigh20=newhigh20, bull=bull,
                chg1=chg1, chg5=chg5, chg10=chg10, above20=above20, above60=above60,
                drawdown=drawdown, last=closes[-1], last_day=kl[-1]["day"])


# ---------------- 腾讯批量快照(市值/换手/涨跌幅) ----------------
def fetch_tencent(codes):
    syms = [tenc_code(c) for c in codes if tenc_code(c)]
    out = {}
    chunks = [syms[i:i + 80] for i in range(0, len(syms), 80)]
    for ch in chunks:
        q = ",".join(ch)
        try:
            r = requests.get("https://qt.gtimg.cn/q=" + q, timeout=15)
            r.encoding = "gbk"
            for line in r.text.strip().split(";"):
                m = re.search(r'v_(\w+)="([^"]*)"', line)
                if not m:
                    continue
                parts = m.group(2).split("~")
                if len(parts) < 50:
                    continue
                try:
                    out[m.group(1)[2:]] = dict(
                        name=parts[1],
                        price=float(parts[3] or 0),
                        chg_pct=float(parts[32] or 0),
                        turnover=float(parts[38] or 0),
                        float_mkt=float(parts[45] or 0),   # 流通市值(亿)
                        vratio=float(parts[43] or 0),      # 量比
                    )
                except Exception:
                    continue
        except Exception:
            time.sleep(0.5)
    return out


# ---------------- 交易日判断 ----------------
# 主判定基于权威交易日历(akshare 历史交易日, 已含周末与法定节假日)，本地缓存 + 按需刷新；
# 日历不可达/超范围时降级为多只蓝筹最近K线日期探测(兜底, 不静默全停)。
# 彻底消除旧版"单只参考股 + 4天缺口猜测"带来的两类误判:
#   1) 长假后首日(gap>4)被误跳过;  2) 周中单日假(gap<=4)被误判为交易日。

TRADE_CALENDAR_FILE = os.path.join(WORKDIR, "trade_calendar.json")
_CALENDAR_CACHE = None          # 进程内缓存: set("2026-09-25", ...)
_CALENDAR_CACHE_MAX = None      # 日历覆盖的最大日期(判断是否需要刷新)
_CALENDAR_REFRESH_MIN = 1       # 缓存最短刷新间隔(天)


def tz_now_shanghai():
    """返回上海时区当前 datetime（中国不实行夏令时，UTC+8 恒定）。"""
    return datetime.utcnow() + timedelta(hours=8)


def _prev_trade_day(d):
    """d 之前最近的(跳过周末的)交易日，仅用于兜底判定。"""
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def get_trade_calendar(force=False):
    """返回 A 股交易日集合(set of 'YYYY-MM-DD')，本地缓存 + 按需刷新。
    数据源: akshare.tool_trade_date_hist_sina()（权威, 含节假日）；
    拉取失败返回 None，交由 is_trade_day 走兜底探测。
    """
    global _CALENDAR_CACHE, _CALENDAR_CACHE_MAX
    today = tz_now_shanghai().date()
    # 1) 进程内未加载但有本地缓存文件 -> 载入
    if _CALENDAR_CACHE is None and os.path.exists(TRADE_CALENDAR_FILE):
        try:
            data = json.load(open(TRADE_CALENDAR_FILE, encoding="utf-8"))
            _CALENDAR_CACHE = set(data.get("dates", []))
            _CALENDAR_CACHE_MAX = datetime.strptime(
                data.get("max", "1970-01-01"), "%Y-%m-%d").date()
        except Exception:
            _CALENDAR_CACHE, _CALENDAR_CACHE_MAX = None, None
    # 2) 今天超出日历覆盖 -> 需要刷新
    need_refresh = (_CALENDAR_CACHE is None) or (today > _CALENDAR_CACHE_MAX)
    # 3) 缓存较旧(超过最短间隔)也刷新；否则直接复用缓存(日常运行不联网)
    if not need_refresh and os.path.exists(TRADE_CALENDAR_FILE):
        mtime = datetime.fromtimestamp(os.path.getmtime(TRADE_CALENDAR_FILE)).date()
        if (today - mtime).days < _CALENDAR_REFRESH_MIN:
            return _CALENDAR_CACHE
    # 4) 刷新: 调用 akshare 拉全量历史交易日
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        dates = set(x.strftime("%Y-%m-%d") for x in df["trade_date"].tolist())
        _CALENDAR_CACHE = dates
        _CALENDAR_CACHE_MAX = datetime.strptime(max(dates), "%Y-%m-%d").date()
        json.dump({"max": _CALENDAR_CACHE_MAX.isoformat(), "dates": sorted(dates)},
                  open(TRADE_CALENDAR_FILE, "w", encoding="utf-8"), ensure_ascii=False)
        print("  交易日历已刷新, 共 %d 天 (至 %s)" % (len(dates), _CALENDAR_CACHE_MAX))
    except Exception as e:
        print("  [warn] 交易日历刷新失败: %s" % e)
    return _CALENDAR_CACHE


def is_trade_day(d=None):
    """判断 d(默认上海今天)是否为 A 股交易日。
    主依据: 权威交易日历；日历不可达/超范围时降级为多只蓝筹K线日期探测。
    """
    today = tz_now_shanghai().date()
    d = d or today
    if d.weekday() >= 5:                 # 周末永不交易
        return False
    cal = get_trade_calendar()
    if cal is not None and d <= _CALENDAR_CACHE_MAX:
        # 日历已覆盖该日: 在->交易日, 不在->休市(精确识别周中假/长假后首日)
        return d.isoformat() in cal
    # ---- 兜底: 多只蓝筹最近K线日期(避免单只停牌误杀, 避免静默全停) ----
    ref_days = []
    for sym in ("sh600519", "sh601398", "sz000001", "sh600036"):
        try:
            kl = fetch_kline(sym, n=6)
            if kl:
                ref_days.append(datetime.strptime(kl[-1]["day"], "%Y-%m-%d").date())
        except Exception:
            continue
    if ref_days:
        last = max(ref_days)             # 多只中最新的有效交易日
        if last == d or last == _prev_trade_day(d):
            return True
    return False


# ---------------- 规则读写 ----------------
# v17 新增信息面(news)因子默认权重; load_rules 会自动补全并跳版
NEWS_DEFAULTS = {"news_sentiment": {"weight": 0.10}, "news_heat": {"weight": 0.05}}


def load_rules():
    r = None
    if os.path.exists(RULES_FILE):
        try:
            r = json.load(open(RULES_FILE, encoding="utf-8"))
        except Exception:
            r = None
    if not r:
        save_rules(DEFAULT_RULES)
        return json.loads(json.dumps(DEFAULT_RULES))
    # v17 迁移: 若规则缺少信息面(news)因子, 则补全并升到 v17
    migrated = False
    factors = r.setdefault("factors", {})
    for k, v in NEWS_DEFAULTS.items():
        if k not in factors:
            factors[k] = dict(v)
            migrated = True
    if migrated:
        r["version"] = max(int(r.get("version", 1)), 17)
        save_rules(r)
    return r


def save_rules(r):
    r["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    json.dump(r, open(RULES_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


# ---------------- 评分 ----------------
def score_stock(feat, spot, rules, news=None, mkt_idx=None, code=None, name=None):
    """news: 来自 news_sina.build_news_factor 的个股因子字典(可空)。
    有新闻数据时并入 news_sentiment/news_heat 子分；否则退回纯技术面。
    mkt_idx/code/name: 市场维度因子(题材热度/板块效应/情绪周期)上下文。
        传入则额外产出 topic_effect / block_effect / emotion_M 三个 comps 并计入总分;
        不传则退化为纯技术面(三者记 0 / 1.0)，完全向后兼容。
        注: 三个市场 comps 由本函数统一产出，自学习阶段(review_close.learn)读这里的
        comps 即可同步差分调整相应权重，无需再依赖外部包装模块。
    """
    f = rules["factors"]
    thr = rules["min_volume_ratio"]
    # 量能放大(核心)
    s_vol = max(0.0, min(1.0, (feat["vratio"] - thr) / (2.5 - thr))) if feat["vratio"] > thr else 0.0
    # 涨停基因
    s_zt = min(feat["zt20"] / 3.0, 1.0)
    # 突破创高
    s_brk = 1.0 if feat["newhigh20"] else max(0.0, 1.0 - min(feat["drawdown"] / 0.10, 1.0))
    # 均线多头
    s_ma = 1.0 if feat["bull"] else (0.6 if (feat["above20"] and feat["above60"]) else (0.3 if feat["above20"] else 0.0))
    # 近期强度
    s_str = max(0.0, min(1.0, (feat["chg10"] + 0.1) / 0.30))
    # 小市值
    fm = spot.get("float_mkt", 0) or 0
    s_cap = (1.0 - min(fm / 800.0, 1.0)) if fm > 0 else 0.5
    # 换手放大
    s_turn = max(0.0, min(1.0, (spot.get("turnover", 0) or 0) / 8.0))

    comps = {"volume_ratio": s_vol, "zt_gene": s_zt, "breakout": s_brk,
             "ma_bull": s_ma, "strength": s_str, "small_cap": s_cap, "turnover": s_turn}

    def _weighted(keys):
        w = sum(f.get(k, {}).get("weight", 0.0) for k in keys) or 1.0
        return sum(comps[k] * f.get(k, {}).get("weight", 0.0) for k in keys) / w

    # 纯技术面总分(无新闻时的基准分)
    total = _weighted(list(comps))
    # 信息面因子: 仅作加分项 —— 利好提分; 中性/弱负面不低于纯技术面;
    # 强负面(negative=True)不靠分数, 由 screen_open 流程层直接剔除。
    if news:
        comps["news_sentiment"] = float(news.get("sentiment", 0.0) or 0.0)
        comps["news_heat"] = float(news.get("heat", 0.0) or 0.0)
        total = max(total, _weighted(list(comps)))

    # ---- 市场维度因子(题材热度 / 板块效应 / 情绪周期): 加法项, 不参与 base 归一化 ----
    # 权重独立于 base, 自学习时单独差分限幅(见 review_close.learn), 不会被 base 归一化稀释。
    w_topic = _market_weight(rules, "topic_effect")
    w_block = _market_weight(rules, "block_effect")
    topic = block = 0.0
    M = 1.0
    if mkt_idx is not None:
        try:
            import zt_market as MKT
            topic, block = MKT.stock_market_scores(code or "", name or "", mkt_idx, rules)
            M = MKT.emotion_multiplier(mkt_idx, rules)
        except Exception:
            topic = block = 0.0
            M = 1.0
    comps["topic_effect"] = topic
    comps["block_effect"] = block
    comps["emotion_M"] = M
    if (w_topic > 0 or w_block > 0) and (topic > 0 or block > 0 or M != 1.0):
        total = (total + w_topic * topic + w_block * block) * M
    return total, comps


def _market_weight(rules, key):
    """读取市场维度因子(题材/板块)权重; 兼容 top-level 与 factors 内两种落点。"""
    v = rules.get(key)
    if isinstance(v, dict):
        return float(v.get("weight", 0.0) or 0.0)
    fv = (rules.get("factors") or {}).get(key)   # 兼容早期把市场因子放进 factors 的版本
    if isinstance(fv, dict):
        return float(fv.get("weight", 0.0) or 0.0)
    return 0.0


def signal_text(feat, spot=None, comps=None, news=None):
    """根据因子生成核心信号摘要；优先用 comps(已归一化评分)，缺失时退回 feat 经验阈值。
    news: 个股信息面因子(可空)，用于追加利好/风险信号。"""
    vratio = feat.get("vratio", 0)
    zt20 = feat.get("zt20", 0)
    chg10 = feat.get("chg10", 0)
    fm = (spot or {}).get("float_mkt", 0) or 0
    turn = (spot or {}).get("turnover", 0) or 0
    tags = []
    if vratio >= 1.8:
        tags.append("量能放大(量比%.1f)" % vratio)
    if zt20 > 0:
        tags.append("近20日%d次涨停" % zt20)
    if feat.get("newhigh20"):
        tags.append("创20日新高")
    if feat.get("bull"):
        tags.append("均线多头")
    if chg10 >= 0.10:
        tags.append("近10日+%.1f%%" % (chg10 * 100))
    if 0 < fm <= 150:
        tags.append("小盘")
    if turn >= 5:
        tags.append("换手活跃")
    # 信息面信号
    if news:
        if news.get("negative"):
            tags.append("风险新闻(%s)" % (news.get("samples", [""])[0][:12] or "—"))
        elif news.get("sentiment", 0) >= 0.7:
            tags.append("利好催化(情绪%.2f)" % news.get("sentiment", 0))
    return "·".join(tags) if tags else "—"


def dummy_feat(sp):
    """K线源不可用时, 用腾讯快照因子构造伪特征(量比/涨跌幅/市值), 供 score_stock 复用打分"""
    vr = (sp.get("vratio", 0) or 0)
    chg = (sp.get("chg_pct", 0) or 0)
    return dict(vratio=vr, ma5=0, ma10=0, ma20=0, ma60=0,
                zt20=0, zt5=0, newhigh20=False, bull=False,
                chg1=0, chg5=0, chg10=chg / 100.0,
                above20=False, above60=False, drawdown=0.0,
                last=sp.get("price", 0) or 0, last_day="")


# ---------------- 本地K线缓存层(按交易日缓存, 大幅降低全市场抓取耗时) ----------------
def _ref_latest_day():
    """参考股(茅台)最新K线交易日，用于判断数据新鲜度"""
    kl = fetch_kline("sh600519", n=6)
    if kl:
        try:
            return datetime.strptime(kl[-1]["day"], "%Y-%m-%d").date()
        except Exception:
            pass
    return date.today()


def target_day(for_close=False):
    """返回应使用的最新交易日：
       - 收盘复盘: 含今日(盘后日线已完整)
       - 开盘前:   上一交易日(避开今日盘中不完整日线)
    """
    latest = _ref_latest_day()
    if for_close:
        return latest.isoformat()
    d = latest
    if d >= date.today():
        # 最新已含今日或更晚(盘中), 回退到上一交易日
        d = date.today() - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    return d.isoformat()


# 最近一次K线抓取的统计（供调用方判断数据新鲜度，杜绝"静默用旧数据打分"）
KLINE_STAT = {"target": None, "total": 0, "fresh": 0, "stale": 0,
              "missing": 0, "cache_updated": None, "ok": False}


def fetch_klines_cached(codes, target, n=70):
    """K线批量获取(按交易日缓存)。

    v2 改进（针对「K线源不可达时静默复用陈旧缓存」的隐患）：
      1) 命中判断由「缓存整体标记 == target」改为「逐标的末根K线日期 == target」，
         target 推进时只增量补齐陈旧/缺失项，不再每日全量重抓数百只；
      2) 抓完统计新鲜度与覆盖率，不达标时打印醒目告警；
      3) 仅当新鲜度达标才回写缓存标记，防止旧数据被标记成新交易日；
      4) 统计写入全局 KLINE_STAT，供调用方在报告中展示。
    """
    global KLINE_STAT
    KLINE_STAT.update({"target": target, "total": 0, "fresh": 0, "stale": 0,
                       "missing": 0, "cache_updated": None, "ok": False})

    def _last_day(kl):
        if isinstance(kl, list) and kl and isinstance(kl[-1], dict):
            return kl[-1].get("day")
        return None

    cache_updated = None
    data = {}
    if os.path.exists(KLINES_CACHE):
        try:
            cache = json.load(open(KLINES_CACHE, encoding="utf-8"))
            cache_updated = cache.get("updated")
            data = cache.get("data", {}) or {}
        except Exception:
            data = {}
    KLINE_STAT["cache_updated"] = cache_updated

    # 增量：仅重抓"缺失"或"末根日期 != target"的标的
    seen, syms = set(), []
    for c in codes:
        sym = tenc_code(c)
        if not sym or sym in seen:
            continue
        seen.add(sym)
        if _last_day(data.get(c)) != target:
            syms.append(sym)

    new = {}
    if syms:
        print("K线增量抓取: %d/%d 只需更新 (缓存标记 %s -> 目标 %s)"
              % (len(syms), len(seen), cache_updated, target))

        def work(sym):
            return sym[2:], fetch_kline(sym, n=n)

        with ThreadPoolExecutor(max_workers=8) as ex:
            for code, kl in ex.map(work, syms):
                if kl:
                    new[code] = kl
        # 多轮补抓(降并发+轮间冷却, 应对东方财富限流)
        for wkr in (4, 2):
            miss = [s for s in syms if s[2:] not in new]
            if not miss:
                break
            time.sleep(2)
            with ThreadPoolExecutor(max_workers=wkr) as ex:
                for code, kl in ex.map(work, miss):
                    if kl:
                        new[code] = kl
        miss = [s for s in syms if s[2:] not in new]
        if miss:
            with ThreadPoolExecutor(max_workers=1) as ex:
                for code, kl in ex.map(work, miss):
                    if kl:
                        new[code] = kl
        data.update(new)

    # ---- 新鲜度 / 覆盖率统计 ----
    fresh = stale = 0
    for c in codes:
        kl = data.get(c)
        if not kl:
            continue
        if _last_day(kl) == target:
            fresh += 1
        else:
            stale += 1
    total = fresh + stale
    missing = len([c for c in codes if tenc_code(c) and c not in data])
    ratio = (fresh / total) if total else 0.0
    ok = total > 0 and ratio >= 0.9
    KLINE_STAT.update({"total": total, "fresh": fresh, "stale": stale,
                       "missing": missing, "ok": ok})

    if not ok:
        print("=" * 62)
        print("[严重告警] K线数据不新鲜，本次打分结果不可靠！")
        print("  目标交易日 target = %s" % target)
        print("  缓存标记 updated  = %s" % cache_updated)
        print("  新鲜 %d / 陈旧 %d / 缺失 %d (新鲜率 %.1f%%)"
              % (fresh, stale, missing, ratio * 100))
        print("  原因：K线源(东财→腾讯→新浪)不可达或部分限流。")
        print("  已阻止回写缓存标记，避免旧数据被标记为新交易日。")
        print("=" * 62)
    else:
        print("K线新鲜度校验通过: 新鲜 %d/%d (%.1f%%), 缺失 %d"
              % (fresh, total, ratio * 100, missing))

    # 仅当新鲜度达标才回写，防止陈旧数据污染 updated 标记
    if new and ok:
        try:
            json.dump({"updated": target, "data": data},
                      open(KLINES_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            pass
    return {c: data[c] for c in codes if c in data}


def build_features(codes, for_close=False, n=70):
    """返回 {code: feat_dict}；开盘前/收盘后复用同一份按交易日缓存的K线数据。

    v2: 只采用末根K线日期 == 目标交易日的样本，杜绝陈旧K线参与打分。
    """
    target = target_day(for_close)
    data = fetch_klines_cached(list(codes), target, n=n)
    out = {}
    for c in codes:
        kl = data.get(c)
        if not kl:
            continue
        if kl[-1].get("day") != target:      # 陈旧样本直接丢弃
            continue
        f = compute_kline_features(kl)
        if f:
            out[c] = f
    return out
