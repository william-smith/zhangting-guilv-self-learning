任务①：开盘前候选筛选与发布（GitHub 连接器 + jsDelivr 版）

── 通用前置（开头先执行）──
REPO=/workspace/zt_repo
GIT=git
REMOTE_PATH=william-smith/zhangting-guilv-self-learning
OWNER=william-smith
REPO_NAME=zhangting-guilv-self-learning
BRANCH=master

# PAT 取用：①本任务内置明文（首选）→ ②沙箱安全文件兜底（git 首选推送通道的认证凭据）
# 注：本文件为公开仓库文档，PAT 以占位符呈现；明文仅存于调度器任务 #12649265 配置中。
GH_PAT="<见调度器任务配置>"
[ -z "$GH_PAT" ] && [ -r /root/.config/zt/gh_pat ] && GH_PAT="$(cat /root/.config/zt/gh_pat 2>/dev/null)"
PAT="${GH_PAT:-}"

unset HTTPS_PROXY HTTP_PROXY ALL_PROXY https_proxy http_proxy all_proxy
export GIT_TERMINAL_PROMPT=0
REMOTE="https://${PAT}@github.com/${REMOTE_PATH}.git"
# SSL 后端自适应：openssl 不可用（本沙箱实测仅 gnutls）时自动降级为 git 默认后端
SSLARG="-c http.sslBackend=openssl"
"$GIT" $SSLARG ls-remote "$REMOTE" >/dev/null 2>&1 || SSLARG=""

注：仓库为公开扁平结构，screen_open.py 与 candidates_*.md/json 均在根目录；jsDelivr 只读，仅作拉取/读取备选；
推送唯一真源为 GitHub master，沙箱本地仅临时产物。
推送通道优先级：① PAT/git push（实测可用，首选）→ ② GitHub 连接器 push_files（当前对本仓库只读、
写返回 403，仅作兜底；待连接器写入权限开通后可改回首选）→ ③ Contents API（PAT 认证）。

── 执行步骤 ──

0) 通达信盘前预检（记录用，失败不阻断主流程）：
   调用通达信连接器 tdx_quotes（code="000001", setcode="1"，上证指数），读取返回 JSON 的 HQInfo：
   HQDate(YYYYMMDD)/HQTime 为行情时间戳，另记录 Close/Now、涨跌幅、上涨家数、下跌家数。
   输出一行预检结论，如：「通达信预检：行情 20261016 15:30，上证 3716.28 (+0.24%)，涨1398/跌902」。
   仅记录不裁决：HQDate==今日→行情已就绪；HQDate<今日→行情未滚动至今日（盘前或非交易日均可能），
   是否交易日以脚本内置权威交易日历 is_trade_day 为最终依据。本任务为后台自动化，无需呈现 MCP App 卡片。
   若 tdx_quotes 调用失败：记录「通达信预检不可用」后继续执行，不影响主流程。

1) 准备仓库（git 直连 GitHub，jsDelivr 备选）：
   if [ ! -d "$REPO/.git" ]; then
     "$GIT" $SSLARG clone "$REMOTE" "$REPO" || { echo "[备选] git 直连失败，改用 jsDelivr 拉取"; mkdir -p "$REPO"; cd "$REPO"; for f in screen_open.py zt_common.py zt_market.py news_sina.py rules.json history.jsonl sector_cache.json last_board.json; do curl -fsSL "https://cdn.jsdelivr.net/gh/${REMOTE_PATH}@master/$f" -o "$f" || echo "[warn] 拉取 $f 失败"; done; }
   else
     "$GIT" -C "$REPO" $SSLARG pull "$REMOTE" HEAD:master || echo "[warn] git pull 失败，继续用本地副本"
   fi

2) 运行筛选：cd "$REPO" && python3 screen_open.py
   - 输出含「【跳过】非交易日」→ 无产物属预期行为，记录后正常结束，不算失败。
   - 否则确认生成 $REPO/candidates_$(date +%F).md 与 .json 且 size>0；否则告警并结束。
   - 数据源应急（出现「有效候选为 0」「K线新鲜度校验失败」「腾讯快照获取: 0 只」等告警时必做）：
     用通达信连接器交叉验证——tdx_screener(message="涨停") 核对上一交易日涨停池、
     tdx_quotes 抽查 Top 候选代码行情是否真实存在；在最终汇报中说明数据源状态与影响；严禁编造行情数字。

3) 推送（PAT/git 首选；连接器兜底；沙箱本地非真源）：
   先确定待推送文件：cd "$REPO" && git status --porcelain（无 .git 时跳过此检查）。
   除 candidates_$(date +%F).md/.json 外，若 rules.json 等已跟踪文件被修改（版本迁移）一并推送；
   未跟踪的缓存文件（codes_cache.json / klines_cache.json / trade_calendar.json / 新闻缓存等）不推送。
   ① 首选 PAT/git：cd "$REPO" && "$GIT" $SSLARG add -A &&
      "$GIT" $SSLARG -c user.email=bot@workbuddy.local -c user.name=WorkBuddy commit -m "候选 $(date +%F)：开盘前筛选产物" &&
      "$GIT" $SSLARG push "$REMOTE" HEAD:master
   ② 兜底1（PAT 不可用或 git 推送失败）：调用 GitHub 连接器 mcp__github__push_files，owner="william-smith"、
      repo="zhangting-guilv-self-learning"、branch="master"，files 为上述文件的
      [{path, content}] 数组，content 取本地文件全文（原样文本，无需 base64，连接器自动编码），
      message="候选 $(date +%F)：开盘前筛选产物"。
      注：该连接器当前对本仓库仅只读权限（写返回 403），此路径待其写入权限开通后才可能成功。
   ③ 兜底2（$REPO 无 .git 走了 jsDelivr 兜底、或前两路均失败）：用 python 调 GitHub Contents API
      直传根目录（PUT /repos/william-smith/zhangting-guilv-self-learning/contents/<文件名>，
      body 含 message、branch=master、base64 内容；文件已存在时先 GET 取 sha 并带上；用 PAT 认证）。
   任一路径成功即通过；全部失败必须输出「任务①产物推送失败」+原因，禁止静默结束。

4) 校验与下游地址：
   - mcp__github__get_file_contents(owner="william-smith", repo="zhangting-guilv-self-learning",
     path="candidates_$(date +%F).md", ref="master") 确认存在且 size>0（.json 同理）；
     或本地 "$GIT" -C "$REPO" ls-files | grep "$(date +%F)"。
   - 团队成员经 jsDelivr 读取（CDN 缓存约 10 分钟，强一致可用 commit hash）：
     https://cdn.jsdelivr.net/gh/william-smith/zhangting-guilv-self-learning@master/candidates_$(date +%F).md
     https://cdn.jsdelivr.net/gh/william-smith/zhangting-guilv-self-learning@master/candidates_$(date +%F).json
   - 最终汇报必含：通达信预检结论、脚本运行摘要（候选数/数据源/新鲜度）、Top10 候选概览、
     推送通道（连接器/git/Contents API）与校验结果。

【注意】脚本内置非交易日守卫（is_trade_day，基于权威交易日历）：法定节假日触发后脚本自动跳过且不产出，属预期行为，不算任务失败。
