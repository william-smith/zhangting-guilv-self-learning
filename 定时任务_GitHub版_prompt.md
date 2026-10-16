# 候选 / 复盘 两个定时任务 —— GitHub + jsDelivr 版 Prompt

> 用途：将原有「候选」「复盘」定时任务中**所有项目网盘（tdrive / `QknZxBQNxaGn`）相关操作**
> 整体替换为 **GitHub 仓库 `william-smith/zhangting-guilv-self-learning` + jsDelivr**。
> 下文两段即为改写后的任务 prompt，直接覆盖原任务的网盘步骤即可。
>
> ⚠️ 安全提示：下方 `<PAT>` 为具备该仓库写权限的 Personal Access Token。若调度器支持
> 密钥/环境变量，请改为引用变量（如 `${GH_PAT}`）而非明文；明文仅供本机/可信环境使用。

通用约定（两段任务共用）：
```
REPO=<本地仓库工作副本目录>            # 各 .py 脚本所在目录
GIT=<git 路径>                        # 如 C:/Users/86136/.workbuddy/binaries/PortableGit/versions/1.3.0/cmd/git.exe
PAT=${GH_PAT}   # 请改为调度器的密钥/环境变量引用，切勿明文写入
REMOTE="https://${PAT}@github.com/william-smith/zhangting-guilv-self-learning.git"
# 推送前必须：清空代理变量 + GIT_TERMINAL_PROMPT=0 + http.sslBackend=openssl
unset HTTPS_PROXY HTTP_PROXY ALL_PROXY https_proxy http_proxy all_proxy
export GIT_TERMINAL_PROMPT=0
```
- **写入走 git push**（jsDelivr 只读，不能上传）。
- **读取走 jsDelivr**：`https://cdn.jsdelivr.net/gh/william-smith/zhangting-guilv-self-learning@master/<path>`
  （约 10 分钟 CDN 缓存；要强一致可用 commit hash 或手动 purge）。

---

## 任务①：开盘前候选筛选与发布（GitHub 版）

**触发**：每日 08:35（脚本内置非交易日跳过逻辑，无需额外判断）。

**Prompt 正文（覆盖原网盘上传步骤）**：
```
1) 准备仓库：若 $REPO/.git 不存在则
     $GIT -c http.sslBackend=openssl clone $REMOTE "$REPO"
   否则
     $GIT -C "$REPO" -c http.sslBackend=openssl pull $REMOTE HEAD:master
   （pull 保证 rules.json / history.jsonl / sector_cache.json 等取数状态最新）

2) 在 $REPO 内运行：python3 screen_open.py
   确认生成 $REPO/candidates_$(date +%F).md 与 .json，且 size>0；否则告警并结束。

3) 提交并推送（取代原 tdrive 上传）：
     cd "$REPO"
     $GIT -c http.sslBackend=openssl add -A
     $GIT -c http.sslBackend=openssl -c user.email=bot@workbuddy.local -c user.name=WorkBuddy \
       commit -m "候选 $(date +%F)：开盘前筛选产物"
     $GIT -c http.sslBackend=openssl push $REMOTE HEAD:master
   校验退出码=0；失败必须输出「任务①产物推送失败」+原因，禁止静默结束。

4) 校验与下游地址：
     $GIT -C "$REPO" ls-files | grep "$(date +%F)"
     团队成员经 jsDelivr 读取：
       https://cdn.jsdelivr.net/gh/william-smith/zhangting-guilv-self-learning@master/candidates_$(date +%F).md
       https://cdn.jsdelivr.net/gh/william-smith/zhangting-guilv-self-learning@master/candidates_$(date +%F).json
```

---

## 任务②：收盘复盘与自学习发布（GitHub 版）

**触发**：每日 15:40（脚本内置非交易日跳过逻辑）。

**Prompt 正文（覆盖原网盘上传步骤）**：
```
1) 准备仓库：同任务① Step1（clone 或 pull 最新）。

2) 在 $REPO 内运行：python3 review_close.py
   （更新 rules.json、追加 history.jsonl、生成 review_$(date +%F).md；自学习权重/阈值）

3) 提交并推送（取代原 tdrive 上传）：
     cd "$REPO"
     $GIT -c http.sslBackend=openssl add -A
     $GIT -c http.sslBackend=openssl -c user.email=bot@workbuddy.local -c user.name=WorkBuddy \
       commit -m "复盘 $(date +%F)：收盘复盘与自学习"
     $GIT -c http.sslBackend=openssl push $REMOTE HEAD:master
   校验退出码=0；失败必须输出「任务②产物推送失败」+原因，禁止静默结束。

4) 校验与下游地址：
     $GIT -C "$REPO" ls-files | grep "$(date +%F)"
     团队成员经 jsDelivr 读取：
       https://cdn.jsdelivr.net/gh/william-smith/zhangting-guilv-self-learning@master/review_$(date +%F).md
       https://cdn.jsdelivr.net/gh/william-smith/zhangting-guilv-self-learning@master/rules.json
```

---

## 迁移要点（替代了哪些网盘操作）
- 取消：`WaitForMcpServers(["netdrive"])` 探针、`tdrive.dir_list` / `tdrive.file_upload` /
  `tdrive.file_upload_complete`、`curl -X PUT` 到 COS、`tdrive.file_delete`。
- 改为：git clone/pull → 脚本产出落到本地仓库副本 → `git add -A` + commit + push（PAT）。
- 读取侧从「网盘目录直链」改为「jsDelivr CDN 链接」。
- 仓库内 `gitignore`（不带点）不生效，故 `candidates_*.{json,md}`、`review_*.md`、`rules.json`、
  `history.jsonl` 均会随 `git add -A` 入库，符合「全部材料上 GitHub」意图。
