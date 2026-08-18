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
    """按板块返回涨停判定幅度(留0.2%容差由调用方处理)"""
    n = (name or "").upper()
    if "ST" in n or "*" in n or "退" in n:
        return 0.048
    if code.startswith("sh688") or code.startswith("sz30"):
        return 0.198
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
def fetch_kline(sym, n=70):
    """日线K线: 主源东方财富历史K线(push2his,沙箱内稳定), 失败兜底新浪"""
    code = sym[2:] if sym[:2] in ("sh", "sz") else sym
    sec = ("1." + code) if code.startswith("6") else ("0." + code)
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    p = {"secid": sec, "fields1": "f1,f2,f3", "fields2": "f51,f52,f53,f54,f55,f56",
         "klt": "101", "fqt": "0", "beg": "0", "end": "20500101", "lmt": str(n)}
    for _ in range(3):
        try:
            r = requests.get(url, p, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
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
            time.sleep(0.4)
    return _fetch_kline_sina(sym, n)


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
def is_trade_day():
    today = datetime.now()
    if today.weekday() >= 5:
        return False
    kl = fetch_kline("sh600519", n=6)
    if not kl:
        return False
    last = datetime.strptime(kl[-1]["day"], "%Y-%m-%d").date()
    if (today.date() - last).days > 4:        # 长假/非交易日
        return False
    return True


# ---------------- 规则读写 ----------------
def load_rules():
    if os.path.exists(RULES_FILE):
        try:
            return json.load(open(RULES_FILE, encoding="utf-8"))
        except Exception:
            pass
    save_rules(DEFAULT_RULES)
    return json.loads(json.dumps(DEFAULT_RULES))


def save_rules(r):
    r["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    json.dump(r, open(RULES_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


# ---------------- 评分 ----------------
def score_stock(feat, spot, rules):
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
    wsum = sum(f[k]["weight"] for k in comps) or 1.0
    total = sum(comps[k] * f[k]["weight"] for k in comps) / wsum
    return total, comps


def signal_text(feat, spot=None, comps=None):
    """根据因子生成核心信号摘要；优先用 comps(已归一化评分)，缺失时退回 feat 经验阈值"""
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


def fetch_klines_cached(codes, target, n=70):
    """K线批量获取(按交易日缓存, 增量：仅抓取 codes 中缺失项)：
       配合两阶段筛选, 每日只抓'异动子集'(数百只)而非全市场, 规避东方财富限流导致的长时间抓取。"""
    data = {}
    if os.path.exists(KLINES_CACHE):
        try:
            cache = json.load(open(KLINES_CACHE, encoding="utf-8"))
            if cache.get("updated") == target:
                data = cache.get("data", {})
        except Exception:
            pass
    syms = [tenc_code(c) for c in codes if tenc_code(c) and c not in data]
    if not syms:
        return {c: data[c] for c in codes if c in data}
    new = {}

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
    if new:
        try:
            json.dump({"updated": target, "data": data},
                      open(KLINES_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            pass
    return {c: data[c] for c in codes if c in data}


def build_features(codes, for_close=False, n=70):
    """返回 {code: feat_dict}；开盘前/收盘后复用同一份按交易日缓存的K线数据"""
    target = target_day(for_close)
    data = fetch_klines_cached(list(codes), target, n=n)
    out = {}
    for c in codes:
        kl = data.get(c)
        if kl:
            f = compute_kline_features(kl)
            if f:
                out[c] = f
    return out
