# -*- coding: utf-8 -*-
"""
review_close.py —— 收盘后复盘 + 规律自学习(约15:40运行)
1. 取当日实际涨停股(按板块幅度判定)
2. 与早盘候选比对 -> 命中率(precision/recall)
3. 按"命中组 vs 漏选/落选组"因子分布差异，自调整 rules.json 权重与量比阈值(带平滑与边界)
4. 输出 review_YYYY-MM-DD.md 并追加 history.jsonl
信息面增强(v17)：复盘加载全天新浪新闻，标注命中个股利好/负面信号，记录信息面覆盖与命中。
"""
import os, json, statistics
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor

import zt_common as C
import news_sina as N
import zt_market as M


def main():
    rules = C.load_rules()
    today = date.today()
    datestr = today.isoformat()
    try:
        M.ensure_market_rules(rules)
    except Exception as e:
        print("[warn] 市场维度规则初始化失败: %s" % e)

    if not C.is_trade_day():
        print("【跳过】%s 非交易日，不进行复盘。" % datestr)
        return

    jpath = os.path.join(C.WORKDIR, "candidates_%s.json" % datestr)
    if not os.path.exists(jpath):
        print("【跳过】未找到今日候选文件 %s，无法复盘。" % jpath)
        return
    cand = json.load(open(jpath, encoding="utf-8"))
    cand_codes = [c["code"] for c in cand["candidates"]]
    print("== 收盘复盘 %s ==" % datestr)
    print("早盘候选 %d 只" % len(cand_codes))

    # 全市场快照 -> 实际涨停
    codes = C.get_codes()
    flt = rules["filters"]
    pool = [(c, n) for c, n in codes
            if C.tenc_code(c) is not None
            and not (flt["exclude_bj"] and c.startswith(("8", "4")))]
    raw = [c for c, n in pool]
    spot = C.fetch_tencent(raw)
    name_map = {c: n for c, n in pool}

    # ---- 信息面: 全天新浪新闻(今00:00 ~ 现在) ----
    news_map = {}
    try:
        ws = int(datetime(today.year, today.month, today.day).timestamp())
        news_map, _ = N.load_or_build(pool, datestr, window_start=ws)
        print("信息面(新浪新闻): 命中 %d 只个股" % len(news_map))
    except Exception as e:
        print("[warn] 信息面新闻获取失败: %s" % e)

    actual_zt = []
    for c, sp in spot.items():
        n = name_map.get(c, "")
        lp = C.limit_pct(n, c)
        if (sp.get("chg_pct", 0) or 0) >= lp * 100 - 0.2:
            actual_zt.append(c)
    print("全市场涨幅≥10%%(不要求封板) %d 只" % len(actual_zt))

    # ---- 市场维度(v25): 抓当日涨停板(题材/板块/情绪) -> 写 last_board.json 供次日盘前使用 ----
    mkt_idx = None
    try:
        board = M.collect_board_context(datestr, actual_zt)
        mkt_idx = board.get("index")
        print("市场上下文: 涨停 %d 只, 已写入 last_board.json(供次日盘前)"
              % ((mkt_idx or {}).get("total", 0)))
    except Exception as e:
        print("[warn] 市场上下文采集失败, 次日盘前将无题材/板块上下文: %s" % e)

    cand_set = set(cand_codes)
    zt_set = set(actual_zt)
    hits = sorted(cand_set & zt_set, key=lambda x: cand_codes.index(x))
    miss_top = [c for c in cand_codes if c not in zt_set]          # 候选但未涨停
    leak = [c for c in actual_zt if c not in cand_set]             # 涨停但漏选
    precision = len(hits) / len(cand_codes) if cand_codes else 0
    recall = len(hits) / len(actual_zt) if actual_zt else 0
    print("命中 %d 只  精确率 %.1f%%  召回率 %.1f%%" % (len(hits), precision * 100, recall * 100))

    # ---- 取因子(命中组 / 落选组)用于学习 ----
    need = list(set(hits) | set(miss_top) | set(leak))
    feats = C.build_features(need, for_close=True, n=70)

    def comps_of(c):
        f = feats.get(c)
        if not f:
            return None
        sp = spot.get(c, {})
        nm = name_map.get(c, "")
        # leave-one-out: 扣掉该股自身对板块/题材计数的贡献, 保证学习对比公平
        idx = M.leave_one_out(mkt_idx, code=c, name=nm) if mkt_idx else None
        _, comps = M.score_with_market(f, sp, rules, code=c, name=nm, mkt_idx=idx)
        return dict(comps=comps, vratio=f["vratio"])

    hit_c = [comps_of(c) for c in hits if comps_of(c)]
    non_c = [comps_of(c) for c in (miss_top + leak) if comps_of(c)]

    # 当日已复盘则跳过学习，避免重复写入/重复跳版
    # done_today 时规则已是"学习后"版本, 报告需展示当日真实演进(v前→v后),
    # 故从 history 当日记录取学习前的 rule_version 作为起始版本。
    done_today = False
    base_version = None
    if os.path.exists(C.HISTORY_FILE):
        for line in open(C.HISTORY_FILE, encoding="utf-8"):
            try:
                rec = json.loads(line)
                if rec.get("date") == datestr:
                    done_today = True
                    base_version = rec.get("rule_version")
                    break
            except Exception:
                continue
    if done_today:
        print("今日(%s)已复盘，跳过学习，仅重生成报告。" % datestr)
        new_rules = rules
    else:
        new_rules = learn(rules, hit_c, non_c)
        C.save_rules(new_rules)

    # 信息面命中统计: 命中股中带正面催化(情绪>=0.6 且非强负面)的数量
    news_hit_codes = [c for c in hits
                      if (news_map.get(c, {}).get("sentiment", 0) >= 0.6
                          and not news_map.get(c, {}).get("negative"))]

    # ---- 写 history ----
    if not done_today:
        rec = dict(date=datestr, rule_version=rules.get("version"),
                   cand_count=len(cand_codes), zt_count=len(actual_zt),
                   hits=len(hits), precision=round(precision, 4), recall=round(recall, 4),
                   hit_codes=hits, leak_count=len(leak),
                   news_count=len(news_map), news_hits=len(news_hit_codes))
        with open(C.HISTORY_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ---- 写 MD ----
    lines = []
    lines.append("# 收盘复盘 + 规律自学习 %s\n" % datestr)
    lines.append("- 早盘候选：**%d** 只 ｜ 全市场涨幅≥10%%(不要求封板)：**%d** 只" % (len(cand_codes), len(actual_zt)))
    lines.append("- 命中：**%d** 只 ｜ 精确率 **%.1f%%** ｜ 召回率 **%.1f%%**" % (len(hits), precision * 100, recall * 100))
    lines.append("- 规则版本：v%s → v%s" % (
        base_version if done_today else rules.get("version"),
        new_rules.get("version")))
    lines.append("- 信息面覆盖：新浪新闻命中 **%d** 只个股；命中股中带利好催化 **%d** 只" % (len(news_map), len(news_hit_codes)))
    if hits:
        lines.append("## 命中个股\n")
        for c in hits:
            n = name_map.get(c, "")
            f = feats.get(c) or {}
            nv = news_map.get(c, {})
            if nv.get("negative"):
                news_tag = "  ⚠️风险新闻"
            elif nv.get("sentiment", 0) >= 0.6:
                news_tag = "  📰利好(情绪%.2f)" % nv["sentiment"]
            else:
                news_tag = ""
            lines.append("- %s %s  量比%.2f  近20日涨停%d  %s%s" % (
                c, n, f.get("vratio", 0), f.get("zt20", 0),
                C.signal_text(f, spot.get(c, {}), news=nv or None), news_tag))
    else:
        lines.append("## 命中个股\n- 无（今日候选未出现涨停）\n")
    lines.append("\n## 市场维度(题材/板块/情绪)\n")
    lines.append("```\n%s\n```" % M.review_market_report(mkt_idx, cand_codes, rules))
    if mkt_idx:
        hit_with_block = [c for c in hits
                          if (M.stock_market_scores(c, name_map.get(c, ""),
                                                    M.leave_one_out(mkt_idx, c, name_map.get(c, "")),
                                                    rules)[1] or 0) > 0]
        lines.append("- 命中股中踩中热点板块(板块内涨停≥%d)的有 **%d/%d** 只"
                     % (rules.get("market", {}).get("block", {}).get("min_limit_ups", 3),
                        len(hit_with_block), len(hits)))
    lines.append("\n## 规律更新(权重/阈值变化)\n")
    for k in rules["factors"]:
        old = rules["factors"][k]["weight"]
        new = new_rules["factors"][k]["weight"]
        arrow = "↑" if new > old else ("↓" if new < old else "=")
        lines.append("- %s: %.2f → %.2f %s" % (k, old, new, arrow))
    lines.append("- 量比阈值: %.2f → %.2f" % (rules["min_volume_ratio"], new_rules["min_volume_ratio"]))
    rpath = os.path.join(C.WORKDIR, "review_%s.md" % datestr)
    open(rpath, "w", encoding="utf-8").write("\n".join(lines))

    print("\n=== 复盘要点 ===")
    print("命中: %s" % (", ".join("%s%s" % (c, name_map.get(c, "")) for c in hits) or "无"))
    print("漏选涨停: %d 只(已用于学习)" % len(leak))
    print("信息面: 新闻覆盖 %d 只, 命中股利好催化 %d 只" % (len(news_map), len(news_hit_codes)))
    if mkt_idx:
        print("市场维度: 涨停 %d 只, 情绪 M=%.2f, 已写入 last_board.json"
              % (mkt_idx.get("total", 0), M.emotion_multiplier(mkt_idx, rules)))
    print("规则已更新 -> v%s，报告: %s" % (new_rules.get("version"), rpath))


# 市场维度因子(题材热度/板块效应): 作为加法项参与打分, 自学习时单独差分限幅,
# 绝不并入 base 因子的归一化分母(否则会被错误压低、且失去信号差分)。
MARKET_FACTOR_KEYS = ("topic_effect", "block_effect")


def learn(rules, hit_c, non_c):
    """依据命中组/落选组因子分布，平滑调整权重与量比阈值(带边界保护，防单日过拟合)。
    基础因子(base)与市场维度因子(题材/板块)分开处理:
      - base 因子: 组内归一化(权重之和=1), 对缩放不敏感(score_stock 内部已按权重归一);
      - 市场因子: 加法项权重, 独立差分调整 + 限幅[0.03,0.20], 不并入 base 归一化分母
        —— 否则会被整体归一化错误压低, 且无法体现"该因子是否真有预测力"的信号差分。
    所有因子(含市场)的 comps 均由 zt_common.score_stock 统一产出, 学习直接读这里差分。
    """
    nr = json.loads(json.dumps(rules))
    nr["version"] = rules.get("version", 1) + 1
    factors = nr["factors"]
    market_keys = [k for k in MARKET_FACTOR_KEYS if k in factors]

    def _diff_adjust(keys, lo=0.03, hi=0.45):
        """命中组因子均分 > 落选组 -> 上调权重; 反之下调。差分即"该因子是否有预测力"。"""
        for k in keys:
            hv = [x["comps"][k] for x in hit_c if k in x["comps"]]
            nv = [x["comps"][k] for x in non_c if k in x["comps"]]
            if not hv:
                continue
            hm = statistics.mean(hv)
            nm = statistics.mean(nv) if nv else 0.0
            eps = 1e-3
            if hm > nm + eps:
                factors[k]["weight"] = min(hi, factors[k]["weight"] * 1.15)
            elif hm < nm - eps:
                factors[k]["weight"] = max(lo, factors[k]["weight"] * 0.90)

    if hit_c:
        # 基础因子: 差分调整
        _diff_adjust([k for k in factors if k not in market_keys])
        # 市场维度因子(题材/板块): 差分调整(加成项, 限幅更窄)
        _diff_adjust(market_keys, lo=0.03, hi=0.20)
        # 量比阈值向命中组中位数靠拢(仅当命中组有明显量能特征)
        vr = [x["vratio"] for x in hit_c if x["vratio"]]
        if vr:
            med = statistics.median(vr)
            target = max(1.0, min(3.0, med * 0.85))
            nr["min_volume_ratio"] = round(max(1.0, min(3.0,
                0.7 * rules["min_volume_ratio"] + 0.3 * target)), 2)
    else:
        # 无命中：略微放宽量比阈值以提升次日召回
        nr["min_volume_ratio"] = round(max(1.0, rules["min_volume_ratio"] * 0.95), 2)

    # ---- 归一化: 仅对 base 因子(不含市场因子), 保持 base 行为稳定 ----
    base_keys = [k for k in factors if k not in market_keys]
    wsum = sum(factors[k]["weight"] for k in base_keys) or 1.0
    for k in base_keys:
        factors[k]["weight"] = round(factors[k]["weight"] / wsum, 4)
    # ---- 市场因子限幅(防单日过拟合放大或缩到 0) ----
    for k in market_keys:
        factors[k]["weight"] = round(max(0.03, min(0.20, factors[k]["weight"])), 4)
    return nr


if __name__ == "__main__":
    main()
