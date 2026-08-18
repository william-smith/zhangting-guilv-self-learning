# -*- coding: utf-8 -*-
"""
screen_open.py —— 开盘前筛选(约09:00运行)
用上一交易日收盘数据，按 rules.json 规律对全市场打分，输出当日涨停候选。
产出: candidates_YYYY-MM-DD.md(可读报告) + candidates_YYYY-MM-DD.json(供收盘复盘比对)
"""
import os, json, time
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor

import zt_common as C


def main():
    rules = C.load_rules()
    today = date.today()
    datestr = today.isoformat()

    if not C.is_trade_day():
        msg = "【跳过】%s 非交易日，不进行开盘前筛选。" % datestr
        print(msg)
        return

    print("== 开盘前筛选 %s ==" % datestr)
    codes = C.get_codes()
    flt = rules["filters"]
    pool = [(c, n) for c, n in codes
            if C.tenc_code(c) is not None
            and not (flt["exclude_bj"] and c.startswith(("8", "4")))]
    if flt.get("exclude_st"):
        pool = [(c, n) for c, n in pool
                if "ST" not in n.upper() and "*" not in n and "退" not in n]
    print("全市场候选池: %d 只" % len(pool))

    raw = [c for c, n in pool]
    spot = C.fetch_tencent(raw)
    print("腾讯快照获取: %d 只" % len(spot))
    # 快速探测K线源是否可用且快(东财等); 限流/失败时降级纯快照, 避免长时阻塞
    t0 = time.time()
    probe = [c for c in raw[:20] if C.tenc_code(c)]
    okp = sum(1 for c in probe if C.fetch_kline(C.tenc_code(c), 5))
    dt = time.time() - t0
    kline_ok = (okp >= len(probe) * 0.5) and (dt < 60)
    # 阶段1 粗筛: 量比/涨跌幅/换手 异动 -> 子集
    sub = [c for c in raw
           if (spot.get(c, {}).get("vratio", 0) or 0) >= 1.3
           or (spot.get(c, {}).get("chg_pct", 0) or 0) >= 3
           or (spot.get(c, {}).get("turnover", 0) or 0) >= 3]
    if len(sub) > 800:
        def _kk(c):
            sp = spot.get(c, {})
            return ((sp.get("vratio", 0) or 0)
                    + (sp.get("chg_pct", 0) or 0) * 0.15
                    + (sp.get("turnover", 0) or 0) * 0.15)
        sub = sorted(sub, key=lambda c: -_kk(c))[:800]
    print("粗筛异动子集: %d 只" % len(sub))
    if kline_ok:
        print("K线源可用, 抓K线算精细因子(涨停基因/均线/突破)...")
        feats = C.build_features(sub, for_close=False, n=70)
    else:
        print("K线源受限(探测 %d/%d, 耗时%.0fs), 降级纯快照因子" % (okp, len(probe), dt))
        feats = {c: C.dummy_feat(spot[c]) for c in sub if c in spot}

    results = []
    for c, n in pool:
        f = feats.get(c)
        if not f:
            continue
        if f["last"] < flt.get("min_close_price", 2.0):
            continue
        sp = spot.get(c, {})
        # 剔除停牌/无成交(换手或流通市值为0)的无效标的
        if (sp.get("turnover", 0) or 0) <= 0 or (sp.get("float_mkt", 0) or 0) <= 0:
            continue
        sc, comps = C.score_stock(f, sp, rules)
        results.append(dict(score=sc, code=c, name=n, feat=f, spot=sp, comps=comps))

    results.sort(key=lambda x: -x["score"])
    top = results[: rules["candidate_count"]]
    print("打分完成，取 Top %d" % len(top))

    # ---- 写 JSON(供复盘) ----
    jdata = {"date": datestr, "rule_version": rules.get("version"),
             "candidates": [dict(rank=i + 1, code=r["code"], name=r["name"],
                                 score=round(r["score"], 4)) for i, r in enumerate(top)]}
    jpath = os.path.join(C.WORKDIR, "candidates_%s.json" % datestr)
    json.dump(jdata, open(jpath, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # ---- 写 MD 报告 ----
    lines = []
    lines.append("# 开盘前涨停候选 %s\n" % datestr)
    src = "腾讯快照(纯快照降级·缺K线精细因子)" if not kline_ok else "新浪/东财K线 + 腾讯快照"
    lines.append("> 数据源：%s；评分规则版本 v%s；阈值 量比≥%.2f"
                 % (src, rules.get("version"), rules["min_volume_ratio"]))
    lines.append("> ⚠️ 量化辅助筛选，非投资建议，涨停不保证。\n")
    lines.append("| 排名 | 代码 | 名称 | 评分 | 量比 | 近20日涨停 | 创20日高 | 均线多头 | 近10日% | 流通市值(亿) | 换手% | 核心信号 |")
    lines.append("|------|------|------|------|------|-----------|---------|---------|--------|------------|-------|---------|")
    for i, r in enumerate(top):
        f = r["feat"]; sp = r["spot"]
        lines.append("| %d | %s | %s | %.3f | %.2f | %d | %s | %s | %.1f | %.0f | %.1f | %s |" % (
            i + 1, r["code"], r["name"], r["score"], f["vratio"], f["zt20"],
            "是" if f["newhigh20"] else "否", "是" if f["bull"] else "否",
            f["chg10"] * 100, sp.get("float_mkt", 0) or 0, sp.get("turnover", 0) or 0,
            C.signal_text(f, sp, r["comps"])))
    lines.append("\n## 因子权重\n")
    for k, v in rules["factors"].items():
        lines.append("- %s: 权重 %.2f" % (k, v["weight"]))
    mpath = os.path.join(C.WORKDIR, "candidates_%s.md" % datestr)
    open(mpath, "w", encoding="utf-8").write("\n".join(lines))

    # ---- 控制台摘要(供agent推送) ----
    print("\n=== 今日涨停候选 Top %d ===" % len(top))
    for i, r in enumerate(top[:10]):
        print("%2d. %s %s  评分%.3f  量比%.2f  涨停基因%d  %s"
              % (i + 1, r["code"], r["name"], r["score"], r["feat"]["vratio"],
                 r["feat"]["zt20"], C.signal_text(r["feat"], r["spot"], r["comps"])))
    print("\n报告: %s" % mpath)
    print("数据: %s" % jpath)


if __name__ == "__main__":
    main()
