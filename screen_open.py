# -*- coding: utf-8 -*-
"""
screen_open.py —— 开盘前筛选(约09:00运行)
用上一交易日收盘数据，按 rules.json 规律对全市场打分，输出当日涨停候选。
信息面增强(v17)：盘前抓取新浪财经新闻，产出个股情绪/热度因子并入打分，
强负面(退市/立案/处罚等)直接剔除候选；无新闻时退回纯技术面。
产出: candidates_YYYY-MM-DD.md(可读报告) + candidates_YYYY-MM-DD.json(供收盘复盘比对)
"""
import os, json, time
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor

import zt_common as C
import news_sina as N
import zt_market as M


def main():
    rules = C.load_rules()
    today = date.today()
    datestr = today.isoformat()

    # ---- v25 市场维度因子(题材热度/板块效应/情绪周期) ----
    # 规则自动升级: 缺失则注入, 已有则保留自学习权重; 载入 T-1 复盘产物 last_board.json
    mkt_idx = None
    try:
        M.ensure_market_rules(rules)
        mkt_idx = M.load_board_context()
        if mkt_idx:
            print("市场上下文(T-1): 涨停 %d 只, 情绪乘子 M=%.2f"
                  % (mkt_idx.get("total", 0), M.emotion_multiplier(mkt_idx, rules)))
        else:
            print("[warn] 无 T-1 市场上下文(首次运行/尚未复盘), 市场维度因子本轮不生效。")
    except Exception as e:
        mkt_idx = None
        print("[warn] 市场维度因子初始化失败, 退回技术面+信息面: %s" % e)

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

    # ---- 信息面: 新浪财经新闻 -> 个股情绪/热度(窗口: 上一交易日15:00~今09:00) ----
    news_map = {}
    try:
        prev_day = C.target_day(for_close=False)        # 上一交易日
        ws = int(datetime.strptime(prev_day + " 15:00:00", "%Y-%m-%d %H:%M:%S").timestamp())
        news_map, news_cached = N.load_or_build(pool, datestr, window_start=ws)
        print("信息面(新浪新闻): 命中 %d 只个股 (缓存=%s)" % (len(news_map), news_cached))
    except Exception as e:
        print("[warn] 信息面新闻获取失败, 退回纯技术面: %s" % e)

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
        # v2: 探测通过不等于批量抓取成功，用实际新鲜度复核
        _st = C.KLINE_STAT
        if not _st.get("ok"):
            print("[告警] K线新鲜度不足(target=%s, 新鲜 %s/%s, 缺失 %s)，"
                  "已丢弃陈旧样本，有效因子仅 %d 只。"
                  % (_st.get("target"), _st.get("fresh"), _st.get("total"),
                     _st.get("missing"), len(feats)))
    else:
        print("K线源受限(探测 %d/%d, 耗时%.0fs), 降级纯快照因子" % (okp, len(probe), dt))
        feats = {c: C.dummy_feat(spot[c]) for c in sub if c in spot}

    results = []
    neg_filtered = 0
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
        # 信息面风控: 强负面(退市/立案/处罚等)直接剔除候选
        nv = news_map.get(c)
        if nv and nv.get("negative"):
            neg_filtered += 1
            continue
        # 打分: 技术面 base + 题材热度/板块效应加成, 再乘情绪周期乘子 M
        sc, comps = M.score_with_market(f, sp, rules, news=news_map.get(c),
                                       code=c, name=n, mkt_idx=mkt_idx)
        results.append(dict(score=sc, code=c, name=n, feat=f, spot=sp, comps=comps,
                            news=news_map.get(c)))

    results.sort(key=lambda x: -x["score"])
    top = results[: rules["candidate_count"]]
    print("打分完成，取 Top %d" % len(top))
    if not top:
        print("[严重告警] 有效候选为 0 —— 极可能数据源不可用，本次产物仅供排障参考。")

    # ---- 写 JSON(供复盘) ----
    jdata = {"date": datestr, "rule_version": rules.get("version"),
             "news_coverage": len(news_map),
             "candidates": [dict(rank=i + 1, code=r["code"], name=r["name"],
                                 score=round(r["score"], 4),
                                 news=round((r.get("news") or {}).get("sentiment", 0) or 0, 3),
                                 news_heat=round((r.get("news") or {}).get("heat", 0) or 0, 3),
                                 topic=round((r.get("comps") or {}).get("topic_effect", 0) or 0, 3),
                                 block=round((r.get("comps") or {}).get("block_effect", 0) or 0, 3),
                                 emotion_M=round((r.get("comps") or {}).get("emotion_M", 1.0) or 1.0, 3))
                            for i, r in enumerate(top)]}
    jpath = os.path.join(C.WORKDIR, "candidates_%s.json" % datestr)
    json.dump(jdata, open(jpath, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # ---- 写 MD 报告 ----
    lines = []
    lines.append("# 开盘前涨停候选 %s\n" % datestr)
    src = "腾讯快照(纯快照降级·缺K线精细因子)" if not kline_ok else "新浪/东财K线 + 腾讯快照"
    src += "；信息面: 新浪财经新闻(个股情绪/热度)"
    lines.append("> 数据源：%s；评分规则版本 v%s；阈值 量比≥%.2f"
                 % (src, rules.get("version"), rules["min_volume_ratio"]))
    if kline_ok:
        _st = C.KLINE_STAT
        if _st.get("ok"):
            lines.append("> ✅ 数据新鲜度校验通过：目标交易日 %s，新鲜样本 %d/%d (%.1f%%)。"
                         % (_st.get("target"), _st.get("fresh"), _st.get("total"),
                            100.0 * _st.get("fresh") / max(1, _st.get("total") or 1)))
        else:
            lines.append("> ❌ **数据新鲜度校验失败**：目标 %s，新鲜样本仅 %d/%d。"
                         "K线源不可达，下表基于不完整样本，慎用。"
                         % (_st.get("target"), _st.get("fresh"), _st.get("total")))
    if not top:
        lines.append("> ❌ **有效候选为 0**：数据源异常，本报告无参考价值，请检查网络与K线源。")
    if neg_filtered:
        lines.append("> 信息面风控：已剔除 **%d** 只强负面新闻股（退市/立案/处罚等）。" % neg_filtered)
    if mkt_idx:
        lines.append("> 🧭 市场维度(T-1)：涨停 **%d** 只，情绪乘子 **M=%.2f**；热点板块 %s"
                     % (mkt_idx.get("total", 0), M.emotion_multiplier(mkt_idx, rules),
                        "、".join("%s(%d)" % (b, c) for b, c in
                                  sorted(mkt_idx.get("blocks", {}).items(),
                                         key=lambda kv: -kv[1])[:5]) or "—"))
    else:
        lines.append("> 🧭 市场维度：无 T-1 上下文，题材/板块/情绪因子本轮未生效。")
    lines.append("> ⚠️ 量化辅助筛选，非投资建议，涨停不保证。\n")
    lines.append("| 排名 | 代码 | 名称 | 评分 | 量比 | 近20日涨停 | 创20日高 | 均线多头 | 近10日% | 流通市值(亿) | 换手% | 新闻情绪 | 新闻热度 | 题材 | 板块 | 核心信号 |")
    lines.append("|------|------|------|------|------|-----------|---------|---------|--------|------------|-------|---------|---------|------|------|---------|")
    for i, r in enumerate(top):
        f = r["feat"]; sp = r["spot"]
        nv = r.get("news") or {}
        lines.append("| %d | %s | %s | %.3f | %.2f | %d | %s | %s | %.1f | %.0f | %.1f | %.2f | %.2f | %.2f | %.2f | %s |" % (
            i + 1, r["code"], r["name"], r["score"], f["vratio"], f["zt20"],
            "是" if f["newhigh20"] else "否", "是" if f["bull"] else "否",
            f["chg10"] * 100, sp.get("float_mkt", 0) or 0, sp.get("turnover", 0) or 0,
            nv.get("sentiment", 0) or 0, nv.get("heat", 0) or 0,
            (r.get("comps") or {}).get("topic_effect", 0) or 0,
            (r.get("comps") or {}).get("block_effect", 0) or 0,
            C.signal_text(f, sp, r["comps"], nv or None)))
    lines.append("\n## 因子权重\n")
    for k, v in rules["factors"].items():
        lines.append("- %s: 权重 %.2f" % (k, v["weight"]))
    mpath = os.path.join(C.WORKDIR, "candidates_%s.md" % datestr)
    open(mpath, "w", encoding="utf-8").write("\n".join(lines))

    # ---- 控制台摘要(供agent推送) ----
    print("\n=== 今日涨停候选 Top %d ===" % len(top))
    for i, r in enumerate(top[:10]):
        nv = r.get("news") or {}
        news_tag = (" [利好%.2f]" % nv["sentiment"]) if (nv.get("sentiment", 0) >= 0.6 and not nv.get("negative")) else (" [风险]" if nv.get("negative") else "")
        print("%2d. %s %s  评分%.3f  量比%.2f  涨停基因%d  %s%s"
              % (i + 1, r["code"], r["name"], r["score"], r["feat"]["vratio"],
                 r["feat"]["zt20"], C.signal_text(r["feat"], r["spot"], r["comps"], nv or None), news_tag))
    print("\n报告: %s" % mpath)
    print("数据: %s" % jpath)

    # ---- ❗ 上传提醒（重要：本地写盘 ≠ 网盘已上传）----
    # 本地产物仅落在本地工作目录(WORKDIR=%s)。开盘前定时任务必须把它们推送至
    # 项目资产网盘(tdrive)根目录 QknZxBQNxaGn(覆盖)，并以网盘 dir_list 命中且字节数>0
    # 为完成判据。仅本地存在不代表已上传，收盘后复盘任务在网盘上找不到即视为缺失。
    print("\n[上传提醒] 本地产物: %s" % mpath)
    print("[上传提醒]   %s" % jpath)
    print("[上传提醒] ⚠️ 必须由 agent 通过项目网盘(tdrive)通道上传到根目录 QknZxBQNxaGn，")
    print("[上传提醒]   以网盘 dir_list 能查到且大小>0 才算完成。本地存在 ≠ 网盘已上传。")


if __name__ == "__main__":
    main()
