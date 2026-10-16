# 开盘前候选产物上传 Runbook（项目资产网盘）

适用：每日开盘前 `zhangting-guilv-self-learning` 系统筛出 `candidates_YYYY-MM-DD.{md,json}` 后，
将其存入**项目资产网盘根目录**（`QknZxBQNxaGn`，即各 `.py` 脚本平铺所在的同步根目录），
供团队成员查看使用。

> 变更记录（2026-09-25）：目录整理后原 `cache` 子目录（`QjMEDRBpayWp`）已删除，全部文件平铺到
> 根目录。故**上传目标从 `cache` 子目录改为根目录 `QknZxBQNxaGn`**；定时任务工作目录同样改为
> 根同步目录（见 README「部署为定时任务」章节与自动化任务 prompt）。下文所有 `cache` 引用均替换为根目录。

---

## ★ Step 0 前置探针（必做，本次新增）

> 教训：2026-09-18 早盘，`netdrive`(tdrive) MCP 连接器在会话中**未连接**，所有
> `tdrive.file_*` 调用均返回 `Tool not found in deferred tools index` / `Failed to connect`，
> 导致"脚本跑了但项目盘里没东西"。上传前必须先确认通道可用。

1. 调用 `WaitForMcpServers(["netdrive"])`（或等价连接器就绪检查）。
2. 返回 `ready: true. Connected (their tools are now available)` → **继续 Step 1–4 上传**。
3. 返回 `Failed to connect: netdrive` / `no MCP server with this name is configured` →
   - **禁止尝试上传**。立即输出告警：
     `任务①产物上传失败：netdrive 连接器未连接，云端通道不可用，产物仅落本地同步根目录`。
   - 指数退避重试探针（建议最多 3 次，间隔 30–60s）；仍不通则等待连接器恢复后由人工/定时重跑。
   - **连接器恢复（探针 ready）后，再执行 Step 1–4**。
4. 探针始终不通：产物保留在本地同步根目录，日志明确标注"待重传"，**禁止静默结束**。

---

## Step 1 确认本地产物已生成

脚本的 `WORKDIR = 脚本自身所在目录`，产物直接写到**脚本同步根目录**（即网盘根 `QknZxBQNxaGn`
的本地同步副本，例如 `/workspace` 或你的同步根）。

```bash
SYNC_DIR=<脚本同步根目录>   # 各 .py 文件所在目录，与网盘根 QknZxBQNxaGn 对应
ls -l "$SYNC_DIR/candidates_$(date +%F).md" "$SYNC_DIR/candidates_$(date +%F).json"
```

两文件必须存在且 `size > 0`；否则先跑 `python3 screen_open.py`（内置非交易日跳过逻辑）。

## Step 2 定位项目盘目标目录

上传目标为**根目录**，无需再查找 `cache` 子目录：

`tdrive.dir_list(dir_id="QknZxBQNxaGn")` → 直接使用根目录 `dir_id = QknZxBQNxaGn` 作为上传目标。

（原 `cache` 子目录 `QjMEDRBpayWp` 已删除，请勿再引用。）

## Step 3 逐文件上传（md、json 各一次）

对 `candidates_YYYY-MM-DD.md` 与 `.json` 分别执行：

1. `tdrive.file_upload(dir_id="QknZxBQNxaGn", file_name, file_size, conflict_strategy="overwrite")`
   → 返回 `domain` / `path` / `headers` / `confirm_key` / `task_id`。
2. shell 用 curl 流式 PUT 上传本地文件，**原样**带上返回的所有 headers：
   ```bash
   curl -sSL -X PUT -T "$SYNC_DIR/<file>" \
     -H "Authorization: <返回>" -H "Cache-Control: ..." -H "Content-Type: ..." \
     -H "x-cos-acl: default" -H "x-cos-crc32c-flag: cosn" \
     -H "x-cos-security-token: <返回>" -H "x-cos-storage-class: INTELLIGENT_TIERING" \
     "https://<domain><path>" -w "\nHTTP_CODE=%{http_code}\n"
   ```
   - ⚠ **不要额外加 `Host` 头**（会让 COS 签名不匹配 → `InvalidAccessKeyId`）。
   - ⚠ 路径前缀必须照抄返回值 `smh2mf8nq7i9cemv`，**别打成 `sm2…`**（→ `SignatureDoesNotMatch`）。
   - ⚠ 用 `-T` 读文件流式上传；不要把路径字符串当 body（`--data-binary "/path"` 会存成路径文本）。
   - 校验返回 `HTTP_CODE=200`。
3. `tdrive.file_upload_complete(dir_id="QknZxBQNxaGn", file_name, file_size, confirm_key, task_id)` → 落库。
4. 临时凭证约 2h 过期且一次性，**每张文件重新 `file_upload` 取新凭证，勿复用旧 URL**。

## Step 4 上传校验（必做）

重新 `tdrive.dir_list(dir_id="QknZxBQNxaGn")`，确认 `candidates_YYYY-MM-DD.{md,json}`
均已存在且 `size > 0`。任一缺失/为空 → 重新生成并重传一次；重试仍失败 →
输出明确失败告警（含「**任务①产物上传失败**」字样 + 原因），不得静默结束。

## 旧产物清理

- 本地同步根目录：删除文件名日期 `< 今天` 的 `candidates_*.{md,json}`；保留 `review_*.md`、`rules.json`、`history.jsonl` 等。
- 项目盘根目录：默认保留历史候选作对照，需清理时单独确认后再 `tdrive.file_delete`。

## 踩坑记录

- 2026-09-18 早：netdrive 未连接 → 云上传失败，仅落本地；晚连接器恢复后重传成功。
- 2026-09-18 晚：多带 `Host` 头 / 路径 `sm2` 笔误 → 403，修正即通。
- `upload.sh` 内含 2026-08-26 的**静态过期凭证**，已失效，**禁止再用**；正式上传一律走
  `tdrive.file_upload` 动态临时凭证流程。
- 2026-09-25：目录整理删除 `cache` 子目录，上传目标统一改为网盘根目录 `QknZxBQNxaGn`。
