# 涨停规律自学习系统（zhangting-guilv-self-learning）

基于历史量价规律的 A 股涨停预测与自学习工具。流程：

1. **开盘前筛选**（`screen_open.py`）：用上一交易日收盘数据，全市场扫描约 5500 只股票，按规则打分，输出 Top 候选。
2. **收盘后复盘**（`review_close.py`）：取当日实际涨停，与早盘候选比对算命中率，按因子分布差异**自动调整权重与阈值**（自学习），更新规则。
3. **次日复用**：开盘前用更新后的规则版本重新筛选，形成「预测 → 复盘 → 改进 → 复用」闭环。

## 数据源

- 行情/历史 K 线：**东方财富 `push2his` 历史 K 线（主源）**，新浪 `CN_MarketData.getKLineData` 兜底；按交易日本地缓存（`klines_cache.json`），每日仅抓取「异动子集」规避限流。
- 实时快照（流通市值/换手/涨跌幅/**量比**）：腾讯 `qt.gtimg.cn` 批量快照。
- 全市场代码列表：`akshare` 的 `stock_zh_a_spot`（失败回退 `stock_info_a_code_name`），本地缓存 7 天（`codes_cache.json`）。

> 注：原方案用新浪作 K 线主源；现东财 `push2his` 在本环境更稳定，故改为主源、新浪兜底。

## 核心因子

量比（昨量 / 前 5 日均量）、涨停基因（近 20 日涨停次数）、突破创高、均线多头、近期强度、小市值、换手活跃。按板块区分涨停幅度：主板 10% / 双创 20% / ST 5%。

## 文件说明

| 文件 | 作用 |
|------|------|
| `zt_common.py` | 共享引擎：代码缓存、K 线缓存层、取数（东财主源/新浪兜底）、因子计算、评分、交易日判断、规则读写 |
| `screen_open.py` | 开盘前筛选（每天 09:00 触发）：K 线源探测 + 两阶段异动粗筛 → 精细打分输出 Top 候选 |
| `review_close.py` | 收盘后复盘 + 自学习（每天 15:40 触发）：比对命中率并按因子分布自动调整权重/阈值 |
| `rules.json` | 规则文件（评分权重、量比阈值等），首次运行自动生成默认版，可直接手改 |
| `history.jsonl` | 每日复盘命中率记录（自动追加） |
| `candidates_YYYY-MM-DD.md/.json` | 每日候选输出 |
| `review_YYYY-MM-DD.md` | 每日复盘报告 |
| `klines_cache.json` / `codes_cache.json` | 本地缓存（按交易日/7天），降低重复抓取耗时与限流风险 |

## 依赖

```bash
pip install akshare requests
```

## 使用

```bash
python3 screen_open.py     # 开盘前：输出当日涨停候选
python3 review_close.py    # 收盘后：复盘并更新 rules.json
```

脚本内置交易日判断，非交易日自动跳过。

## 部署为定时任务（CodeBuddy 自动化）

两个 daily 任务（脚本内自带跳过非交易日逻辑）：

- 每天 09:00 运行 `screen_open.py`
- 每天 15:40 运行 `review_close.py`

可用 `automation-task-manager` 技能的 `scheduler-api.sh create` 创建。

## 合规声明

本系统为**量化辅助筛选工具，非投资建议**。涨停预测不保证命中，存在市场系统性风险，请独立判断、自负盈亏。
