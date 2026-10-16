# 开盘前候选产物上传 / 收盘复盘 发布 Runbook（GitHub + jsDelivr 版）

适用：每日 `zhangting-guilv-self-learning` 系统筛出 `candidates_YYYY-MM-DD.{md,json}`（开盘前）、
生成 `review_YYYY-MM-DD.md` 并更新 `rules.json`/`history.jsonl`（收盘后）后，将产物提交到
**GitHub 仓库 `william-smith/zhangting-guilv-self-learning`**，供团队成员经 **jsDelivr CDN** 读取。

> 变更（2026-10-16）：原「项目资产网盘（tdrive）上传」整体迁移至 **GitHub + jsDelivr**。
> 不再使用 netdrive(tdrive) 连接器的 `dir_list` / `file_upload` / `file_upload_complete`。
> - **写入**：一律走 `git`（PAT 克隆/拉取 → 提交 → 推送）。
> - **读取**：一律走 jsDelivr：`https://cdn.jsdelivr.net/gh/william-smith/zhangting-guilv-self-learning@master/<path>`。
> - jsDelivr 是**只读 CDN**，不能用于上传；上传只能 git push。

---

## 仓库与工作目录

- 仓库：`https://github.com/william-smith/zhangting-guilv-self-learning`
- 本地工作副本（首次 clone，之后 pull）：`REPO=<本地仓库目录>`（各 `.py` 脚本即位于此，`WORKDIR=脚本自身所在目录`）。
- 注意：仓库内 `gitignore` 文件**不带前导点**，故不会被 git 当作忽略规则；
  `git add -A` 会纳入全部产物（含 `candidates_*.json/md`、`review_*.md`、`rules.json`、`history.jsonl`）。
  如需让该文件生效（忽略这些产物），请明确指示后再改名 `.gitignore` 并处理已跟踪文件。

---

## ★ Step 0 前置（git / 网络探针，必做）

```bash
# 清空代理变量（否则直连 github:443 会被代理卡死）
unset HTTPS_PROXY HTTP_PROXY ALL_PROXY https_proxy http_proxy all_proxy
export GIT_TERMINAL_PROMPT=0   # 无头环境禁用交互凭据
GIT="<git 路径>"   # WorkBuddy 自带：C:/Users/86136/.workbuddy/binaries/PortableGit/versions/1.3.0/cmd/git.exe
PAT="<github_pat_...>"   # 具备该仓库写权限的 PAT
REPO="<本地仓库目录>"
```
校验 `git --version` 可用；网络不通时**禁止继续**，输出明确告警（含「任务产物推送失败」字样），不得静默结束。

---

## Step 1 拉取最新仓库（保证取数状态最新）

```bash
if [ ! -d "$REPO/.git" ]; then
  "$GIT" -c http.sslBackend=openssl clone \
    "https://${PAT}@github.com/william-smith/zhangting-guilv-self-learning.git" "$REPO"
else
  "$GIT" -C "$REPO" -c http.sslBackend=openssl pull \
    "https://${PAT}@github.com/william-smith/zhangting-guilv-self-learning.git" HEAD:master
fi
```
`pull` 确保 `rules.json` / `history.jsonl` / `sector_cache.json` / `last_board.json` 等取数状态为最新。

## Step 2 确认本地产物已生成

在 `$REPO` 内运行脚本（内置非交易日跳过逻辑）：

- 开盘前：`python3 screen_open.py` → 生成 `candidates_$(date +%F).md` 与 `.json`。
- 收盘后：`python3 review_close.py` → 更新 `rules.json`、追加 `history.jsonl`、生成 `review_$(date +%F).md`。

产物必须存在且 `size > 0`；否则本次结束并告警。

## Step 3 提交并推送（取代原网盘上传）

```bash
cd "$REPO"
"$GIT" -c http.sslBackend=openssl add -A
"$GIT" -c http.sslBackend=openssl -c user.email="bot@workbuddy.local" -c user.name="WorkBuddy" \
  commit -m "候选/复盘 $(date +%F)：自动发布到 GitHub"
"$GIT" -c http.sslBackend=openssl push \
  "https://${PAT}@github.com/william-smith/zhangting-guilv-self-learning.git" HEAD:master
```
校验退出码 = 0；任一步失败 → 明确输出「任务产物推送失败」+ 原因，禁止静默结束。

## Step 4 推送校验 + 下游读取地址

- 校验：`"$GIT" -C "$REPO" ls-files | grep "$(date +%F)"` 确认新文件已跟踪；或用
  `gh api repos/william-smith/zhangting-guilv-self-learning/contents/candidates_$(date +%F).md` 确认存在。
- 下游读取（jsDelivr，约 10 分钟 CDN 缓存；要强一致可用 commit hash 或手动 purge）：
  - `https://cdn.jsdelivr.net/gh/william-smith/zhangting-guilv-self-learning@master/candidates_$(date +%F).md`
  - `https://cdn.jsdelivr.net/gh/william-smith/zhangting-guilv-self-learning@master/candidates_$(date +%F).json`
  - `https://cdn.jsdelivr.net/gh/william-smith/zhangting-guilv-self-learning@master/review_$(date +%F).md`

---

## 旧产物清理

- 本地：可保留近期 7 天对照，`find "$REPO" -maxdepth 1 -name 'candidates_*.{md,json}' -mtime +7 -delete`。
- 仓库：git 保留全部提交历史，无需手动删云端文件（如需裁剪历史另议）。

## 踩坑记录

- 2026-09-18：netdrive 未连接导致云上传失败 → 本次已整体迁移到 GitHub，不再依赖 netdrive。
- 推送须 **清空代理变量 + `GIT_TERMINAL_PROMPT=0` + `http.sslBackend=openssl`**，否则卡死/挂起。
- jsDelivr 为只读 CDN，**写入一律走 git push**；不要试图用 jsDelivr 上传。
- 仓库内 `gitignore` 不带点、不生效；若改名 `.gitignore`，`candidates_*.{json,md}`、`review_*.md`、
  `rules.json`、`history.jsonl` 将被忽略，与「全部材料入库」冲突，需谨慎。
