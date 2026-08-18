# -*- coding: utf-8 -*-
"""
review_close.py —— 收盘后复盘 + 规律自学习(约15:40运行)
1. 取当日实际涨停股(按板块幅度判定)
2. 与早盘候选比对 -> 命中率(precision/recall)
3. 按"命中组 vs 漏选/落选组"因子分布差异，自调整 rules.json 权重与量比阈值(带平滑与边界)
4. 输出 review_YYYY-MM-DD.md 并追加 history.jsonl
"""
import os, json, statistics
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor

import zt_common as C


def main():
    rules = C.load_rules()
    today = date.today()
    datestr = today.isoformat()

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

    actual_zt = []
    for c, sp in spot.items():
        n = name_map.get(c, "")
        lp = C.limit_pct(n, c)
        if (sp.get("chg_pct", 0) or 0) >= lp * 100 - 0.2:
            actual_zt.append(c)
    print("全市场实际涨停 %d 只" % len(actual_zt))

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
        _, comps = C.score_stock(f, sp, rules)
        return dict(comps=comps, vratio=f["vratio"])

    hit_c = [comps_of(c) for c in hits if comps_of(c)]
    non_c = [comps_of(c) for c in (miss_top + leak) if comps_of(c)]

    # 当日已复盘则跳过学习，避免重复写入/重复跳版
    done_today = False
    if os.path.exists(C.HISTORY_FILE):
        for line in open(C.HISTORY_FILE, encoding="utf-8"):
            try:
                if json.loads(line).get("date") == datestr:
                    done_today = True
                    break
            except Exception:
                continue
    if done_today:
        print("今日(%s)已复盘，跳过学习，仅重生成报告。" % datestr)
        new_rules = rules
    else:
        new_rules = learn(rules, hit_c, non_c)
        C.save_rules(new_rules)

    # ---- 写 history ----
    if not done_today:
        rec = dict(date=datestr, rule_version=rules.get("version"),
                   cand_count=len(cand_codes), zt_count=len(actual_zt),
                   hits=len(hits), precision=round(precision, 4), recall=round(recall, 4),
                   hit_codes=hits, leak_count=len(leak))
        with open(C.HISTORY_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ---- 写 MD ----
    lines = []
    lines.append("# 收盘复盘 + 规律自学习 %s\n" % datestr)
    lines.append("- 早盘候选：**%d** 只 ｜ 全市场实际涨停：**%d** 只" % (len(cand_codes), len(actual_zt)))
    lines.append("- 命中：**%d** 只 ｜ 精确率 **%.1f%%** ｜ 召回率 **%.1f%%**" % (len(hits), precision * 100, recall * 100))
    lines.append("- 规则版本：v%s → v%s\n" % (rules.get("version"), new_rules.get("version")))
    if hits:
        lines.append("## 命中个股\n")
        for c in hits:
            n = name_map.get(c, "")
            f = feats.get(c) or {}
            lines.append("- %s %s  量比%.2f  近20日涨停%d  %s" % (
                c, n, f.get("vratio", 0), f.get("zt20", 0),
                C.signal_text(f, spot.get(c, {}))))
    else:
        lines.append("## 命中个股\n- 无（今日候选未出现涨停）\n")
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
    print("规则已更新 -> v%s，报告: %s" % (new_rules.get("version"), rpath))


def learn(rules, hit_c, non_c):
    """依据命中组/落选组因子分布，平滑调整权重与量比阈值(带边界保护，防单日过拟合)"""
    nr = json.loads(json.dumps(rules))
    nr["version"] = rules.get("version", 1) + 1
    factors = nr["factors"]

    if hit_c:
        for k in factors:
            hv = [x["comps"][k] for x in hit_c if k in x["comps"]]
            nv = [x["comps"][k] for x in non_c if k in x["comps"]]
            if not hv:
                continue
            hm = statistics.mean(hv)
            nm = statistics.mean(nv) if nv else 0.0
            eps = 1e-3
            if hm > nm + eps:
                factors[k]["weight"] = min(0.45, factors[k]["weight"] * 1.15)
            elif hm < nm - eps:
                factors[k]["weight"] = max(0.03, factors[k]["weight"] * 0.90)
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

    # 权重归一化
    wsum = sum(factors[k]["weight"] for k in factors) or 1.0
    for k in factors:
        factors[k]["weight"] = round(factors[k]["weight"] / wsum, 4)
    return nr


if __name__ == "__main__":
    main()
