# -*- coding: utf-8 -*-
"""
build_sector_cache.py —— 生成 sector_cache.json(个股 -> 所属行业/板块)
=====================================================================
为什么需要它:
  zt_market 的【板块效应】因子依赖"个股属于哪个板块"。只靠股票名称关键词(如"XX电力")
  覆盖率很低(大量个股名称不含行业字眼), 板块效应因子会严重失真。本脚本用新浪行业分类
  建立全市场 精确行业映射, 写入 sector_cache.json, zt_market 自动加载并与名称词典合并。

数据源(按优先级自动降级; 也可用 --source 手动指定):
  1) em   东方财富 clist 的 f100 字段(所属行业板块): **1~3 次请求拿全市场 5000+ 只**, 最快最全。
     zt_common 的 K 线主源就是东财, 生产环境必通(沙箱内不可达, 故本地只能实测 sina)。
  2) sina 新浪财经行业分类: 行业列表 + 逐行业成分股, 约 150 次请求, 约 50 个粗粒度行业。
     ⚠️ 新浪对高频来源会限流(HTTP 456); 脚本内置 requests->curl 兜底与退避重试。
  3) 名称关键词词典: 零网络兜底(zt_market 内置 SECTOR_KEYWORDS), 覆盖率低但永远可用。

  【为什么不用腾讯源】腾讯 proxy.finance.qq.com 能返回板块列表(board_type=hy 一级 /
  hy2 申万二级 124 个 / gn 概念板块), 但板块成分股接口未对外开放 —— 实测
  pt/getBoardStockList、hs/getBoardStockList、pt/getBoardStock、pt/getStockList、
  stock/getStockPlate、qt.gtimg.cn?q=pk* 全部 404 或 rpc invalid, 拿不到成分股就
  建不出【股票 -> 行业】映射。腾讯的 qt.gtimg.cn 行情仍服务于 zt_common 的快照获取。

用法(无需每天跑, 建议每周一次或新股批量上市后跑):
  python3.11 build_sector_cache.py                 # 自动选源(em -> sina)
  python3.11 build_sector_cache.py --source em     # 只用东财
  python3.11 build_sector_cache.py --source sina   # 只用新浪
  python3.11 build_sector_cache.py --check         # 只看缓存统计, 不联网
产物: sector_cache.json = {"600176": ["玻璃行业"], ...}
失败不影响主流程: zt_market 读不到缓存会自动退回名称关键词词典。
"""
import os, sys, json, re, time, subprocess
from concurrent.futures import ThreadPoolExecutor

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from zt_market import SECTOR_CACHE
except Exception:                                    # 独立运行兜底
    SECTOR_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sector_cache.json")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Referer": "https://finance.sina.com.cn"}
HY_LIST = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
HY_STOCKS = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             "Market_Center.getHQNodeData?page=%d&num=%d&sort=symbol&asc=1&node=%s")
TIMEOUT = 12

# 东财: clist 的 f100 = 所属行业板块, 一次可拉全市场(与 zt_common 的 K 线同源)
EM_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EM_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"   # 沪深A股(含创业/科创/北交过滤后)
EM_PAGE = 500


def _curl(url, raw=False):
    """curl 兜底: 部分环境下 requests 的 TLS 指纹会被财经站点拦截(HTTP 456), curl 正常。"""
    try:
        out = subprocess.run(["curl", "-s", "-m", str(TIMEOUT), "-A", "Mozilla/5.0",
                              "-H", "Referer: https://finance.sina.com.cn", url],
                             capture_output=True, timeout=TIMEOUT + 5)
        if out.returncode == 0 and out.stdout:
            return out.stdout if raw else out.stdout.decode("utf-8", errors="ignore")
    except Exception:
        pass
    return None


def _get_json(url):
    """GET -> python 对象(dict/list); requests 失败自动退回 curl。"""
    for _ in range(2):
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            if r.status_code == 200 and r.content:
                return json.loads(r.content.decode("utf-8", errors="ignore"))
        except Exception:
            time.sleep(0.4)
    txt = _curl(url)
    if txt:
        try:
            return json.loads(txt)
        except Exception:
            return None
    return None


def _get_json_url(url, params=None):
    """GET(url + params) -> python 对象; requests 失败自动退回 curl。"""
    full = url
    if params:
        from urllib.parse import urlencode
        full = url + "?" + urlencode(params)
    for _ in range(2):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
            if r.status_code == 200 and r.content:
                return json.loads(r.content.decode("utf-8", errors="ignore"))
        except Exception:
            time.sleep(0.4)
    txt = _curl(full)
    if txt:
        try:
            return json.loads(txt)
        except Exception:
            return None
    return None


def fetch_industries():
    """-> [("new_blhy", "玻璃行业", 19), ...]"""
    for _ in range(3):
        txt = None
        try:
            r = requests.get(HY_LIST, headers=UA, timeout=TIMEOUT)
            if r.status_code == 200:
                txt = r.content.decode("gbk", errors="ignore")
        except Exception:
            txt = None
        if not txt or "sinaindustry" not in txt:
            raw = _curl(HY_LIST, raw=True)
            txt = raw.decode("gbk", errors="ignore") if raw else ""
        m = re.search(r"=\s*(\{.*\})", txt, re.S) if txt else None
        if m:
            try:
                data = json.loads(m.group(1))
            except Exception:
                data = None
            if data:
                out = []
                for k, v in data.items():
                    parts = str(v).split(",")
                    if len(parts) >= 3:
                        try:
                            cnt = int(parts[2])
                        except Exception:
                            cnt = 100
                        out.append((parts[0], parts[1], cnt))
                if out:
                    return out
        time.sleep(0.8)
    return []


def fetch_members(node, total, page_size=100):
    """-> ["600176", ...]"""
    codes = []
    pages = min(30, max(1, (total + page_size - 1) // page_size))
    for p in range(1, pages + 1):
        got = _get_json(HY_STOCKS % (p, page_size, node))
        if not isinstance(got, list) or not got:
            break
        for it in got:
            c = str(it.get("code") or "").strip()
            if c:
                codes.append(c)
        if len(got) < page_size:
            break
    return codes


def fetch_em_industry():
    """东财 clist f100(所属行业): 1~3 次请求拿全市场 -> {"600176": ["玻璃玻纤"], ...}
    f100 在东财口径下即"所属行业板块", 粒度接近申万二级, 足够板块效应因子使用。
    """
    mapping = {}
    for pn in range(1, 40):
        p = {"pn": str(pn), "pz": str(EM_PAGE), "po": "1", "np": "1", "fltt": "2",
             "invt": "2", "fid": "f3", "fs": EM_FS, "fields": "f12,f14,f100"}
        d = _get_json_url(EM_URL, p)
        diff = ((d or {}).get("data") or {}).get("diff") or {}
        if not diff:
            break
        for v in diff.values():
            code = str(v.get("f12") or "").strip()
            ind = str(v.get("f100") or "").strip()
            if code and ind and ind != "-":
                mapping.setdefault(code, [])
                if ind not in mapping[code]:
                    mapping[code].append(ind)
        if len(diff) < EM_PAGE:
            break
    return mapping


def fetch_sina_industry():
    """新浪行业分类 -> {"600176": ["玻璃行业"], ...}"""
    inds = fetch_industries()
    if not inds:
        return {}
    mapping = {}

    def work(item):
        node, name, cnt = item
        return name, fetch_members(node, cnt)

    with ThreadPoolExecutor(max_workers=6) as ex:
        for name, mem in ex.map(work, inds):
            for c in mem:
                mapping.setdefault(c, [])
                if name not in mapping[c]:
                    mapping[c].append(name)
    return mapping


def build(source="auto"):
    """source: auto | em | sina"""
    order = {"auto": ["em", "sina"], "em": ["em"], "sina": ["sina"]}[source]
    mapping, used = {}, None
    for src in order:
        print("[尝试数据源] %s ..." % src)
        try:
            m = fetch_em_industry() if src == "em" else fetch_sina_industry()
        except Exception as e:
            print("  异常: %s" % repr(e)[:120])
            m = {}
        if m and len(m) >= 500:                 # 少于 500 只视为失败, 换下一个源
            mapping, used = m, src
            break
        print("  未取到有效数据(得到 %d 只)" % len(m))
    if not mapping:
        print("[失败] 所有数据源均不可用, 未修改现有缓存。系统会自动退回名称关键词词典。")
        return None
    json.dump(mapping, open(SECTOR_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    print("数据源=%s 已写入 %s: %d 只个股, 平均 %.2f 个行业标签"
          % (used, SECTOR_CACHE, len(mapping),
             sum(len(v) for v in mapping.values()) / len(mapping)))
    return mapping


def check():
    if not os.path.exists(SECTOR_CACHE):
        print("sector_cache.json 不存在 —— 板块映射将只使用名称关键词词典。")
        return
    d = json.load(open(SECTOR_CACHE, encoding="utf-8"))
    print("sector_cache.json: %d 只个股" % len(d))
    for c in ["600519", "000001", "600176", "000725", "601899"]:
        print("  样例 %s -> %s" % (c, d.get(c, "-")))


if __name__ == "__main__":
    src = "auto"
    if "--source" in sys.argv:
        try:
            src = sys.argv[sys.argv.index("--source") + 1]
        except Exception:
            src = "auto"
    if src not in ("auto", "em", "sina"):
        print("未知 --source=%s (可选 auto|em|sina)" % src)
        src = "auto"
    if "--check" in sys.argv and "--source" not in sys.argv:
        check()
    else:
        build(src)
        check()
