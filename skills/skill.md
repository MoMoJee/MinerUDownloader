---
name: mineru-downloader
description: 使用本仓库的 MinerU Downloader 将 PDF / Word / PPT / 图片批量解析为 Markdown。适用于 agent 或脚本进行非交互式文档转换、批量 OCR、公式与表格保留。
---

# MinerU Downloader 使用技能

本文件面向其他 agent，说明如何以**非交互方式**调用本仓库的 MinerU 批量解析工具。
人类用户也可以使用，但 agent 不应启动 TUI。

## 这个工具做什么

调用 [MinerU](https://mineru.net) API，递归扫描一个目录，把支持的文件解析为 Markdown：

- 输出 `.md` 写在**源文件所在目录**，文件名与源文件相同（`报告.pdf` -> `报告.md`）
- 解析出的图片写入同目录的 `images/`（MinerU 使用 UUID 文件名，多个文档共享该目录）
- 公式保留为 LaTeX，表格保留结构，扫描件自动 OCR
- 超过页数限制的 PDF 会自动拆分、分别解析，再按顺序合并为一个 `.md`
- 支持格式：`.pdf`、`.png` `.jpg` `.jpeg` `.jp2` `.webp` `.gif` `.bmp`、`.doc` `.docx`、`.ppt` `.pptx`

不支持的格式：Excel（`.xls` / `.xlsx`）、HTML、纯文本、压缩包等。

## 先获取工具

这个 skill 可以独立分发，不要求用户本机已经安装仓库。先确认用户要处理的目录（下文用 `<文档目录>` 表示），再按平台选择一种方式获取工具。

### Windows：下载发布包（无需 Python，推荐）

```powershell
$AppDir = Join-Path $env:TEMP "mineru-downloader"
$Exe = Join-Path $AppDir "mineru-downloader.exe"
if (-not (Test-Path -LiteralPath $Exe)) {
    $Zip = Join-Path $env:TEMP "mineru-downloader.zip"
    Invoke-WebRequest `
      -Uri "https://github.com/MoMoJee/MinerUDownloader/releases/latest/download/mineru-downloader.zip" `
      -OutFile $Zip
    Expand-Archive -LiteralPath $Zip -DestinationPath $env:TEMP -Force
}
cd $AppDir
.\mineru-downloader.exe --help
```

发布包是 Windows x64；需要系统 `curl.exe`（Windows 10 / 11 自带），不需要 Python。

### Windows / macOS / Linux：克隆源码

PowerShell：

```powershell
$AppDir = Join-Path $env:TEMP "MinerUDownloader"
if (-not (Test-Path -LiteralPath (Join-Path $AppDir "main.py"))) {
    git clone --depth 1 https://github.com/MoMoJee/MinerUDownloader.git $AppDir
}
$PY = Join-Path $AppDir ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PY)) {
    python -m venv (Join-Path $AppDir ".venv")
    & $PY -m pip install -r (Join-Path $AppDir "requirements.txt")
}
cd $AppDir
& $PY main.py --help
```

bash：

```bash
APP_DIR="${TMPDIR:-/tmp}/MinerUDownloader"
if [ ! -f "$APP_DIR/main.py" ]; then
  git clone --depth 1 https://github.com/MoMoJee/MinerUDownloader.git "$APP_DIR"
fi
PY="$APP_DIR/.venv/bin/python"
if [ ! -x "$PY" ]; then
  python3 -m venv "$APP_DIR/.venv"
  "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
fi
cd "$APP_DIR"
"$PY" main.py --help
```

需要 Python 3.10+。如果没有 `git`，可以下载
`https://github.com/MoMoJee/MinerUDownloader/archive/refs/heads/master.zip`，解压到 `$APP_DIR` 后继续安装依赖。

### 如果已经有工具

如果当前环境里已经存在本仓库或解压好的发布包，直接复用即可，不必重新下载。后面的命令默认当前目录是工具目录。

## 如何获取 MinerU API Token

先检查环境变量 `MINERU_TOKEN` 是否已经设置；没有就向用户索取，不要自行注册或猜测 Token。

1. 打开 [mineru.net](https://mineru.net) 并注册 / 登录
2. 进入个人中心，找到 API Token（以 `eyJ` 开头的 JWT 字符串）
3. 通过环境变量提供给工具，避免写进命令行或提交到仓库：

```powershell
$env:MINERU_TOKEN = "eyJ..."
```

```bash
export MINERU_TOKEN="eyJ..."
```

Token 有时效性，失效后 HTTP 401 / `A0202` / `A0211`，需要重新获取。

## 运行要求

- Python 3.10+（仅源码方式）
- 工具依赖：`textual`、`requests`、`rich`、`pyyaml`、`pypdf`
- 实际提交任务时需要 `curl` / `curl.exe` 在 `PATH` 中（`--dry-run` 不需要）
- 一个 MinerU API Token（以 `eyJ` 开头的 JWT）
- 能访问 `mineru.net` 与阿里云 OSS

下文用 `$PY` 表示解释器：

- Windows PowerShell：`$PY = ".\.venv\Scripts\python.exe"`
- bash：`PY=".venv/bin/python"`

如果使用发布包，把命令中的 `$PY main.py` 替换为 `.\mineru-downloader.exe`（在 `$AppDir` 下）。如果每次命令都是新 shell，先重新 `cd $AppDir`，或使用绝对路径。

## 最重要的规则

1. **不要直接运行 `python main.py`**。默认会启动 TUI，agent 无法操作。
2. **使用 `--cli`**。它是一次性非交互模式：不读 stdin、不等待确认，所有参数必须一次给全。旧版向导只给人类用（`--interactive`），agent 不要使用。
3. **先 dry-run**，确认扫描到的文件和重复 `.md` 策略，再真正提交。
4. **用 `--json` 获取机器可读结果**。stdout 只有一个 JSON 对象，进度和日志在 stderr。
5. **不要把 Token 写进命令行历史或日志**。优先用环境变量 `MINERU_TOKEN`；`--token` 仅在不落盘的环境中使用。
6. **检查退出码**：`0` 成功，`1` 有文件失败，`2` 参数 / 扫描 / Token 配置错误。
7. **已有同名 `.md` 默认跳过**。需要重新解析时显式使用 `--duplicate overwrite`（会先删除旧 `.md`）。
8. **需要扫描后才决定处理哪些文件时，使用两阶段流程**：先 `--dry-run --json`，把选中的 `files[].path` 写入清单，再用 `--file-list` 执行。
9. **长任务使用后台模式**：`--background` 立即返回 `job_id`，之后用 `--job-wait` 或 `--job-result` 取结果；job 控制命令始终输出 JSON。

## 推荐工作流

### 第一步：dry-run

不调用 API、不需要 Token、不会创建配置文件：

```powershell
$env:MINERU_TOKEN = "eyJ..."   # 可选；dry-run 不检查
& $PY main.py "D:\docs" --cli --dry-run --json
```

```bash
MINERU_TOKEN="eyJ..." "$PY" main.py "/data/docs" --cli --dry-run --json
```

输出示例（实际 JSON 中非 ASCII 字符会被转义为 `\uXXXX`）：

```json
{
  "success": true,
  "dry_run": true,
  "root": "D:\\docs",
  "config": {
    "token_configured": true,
    "language": "ch",
    "batch_size": 20,
    "poll_interval": 8,
    "timeout": 1800,
    "keep_zip": false,
    "keep_json": false,
    "proxy_mode": "none",
    "lb_enabled": true,
    "duplicate_policy": "skip"
  },
  "summary": {
    "total": 3,
    "will_process": 2,
    "skipped": 1,
    "batches": 1,
    "selected_size": 123456
  },
  "files": [
    {
      "index": 1,
      "path": "D:\\docs\\a.pdf",
      "rel_path": "a.pdf",
      "size": 123456,
      "existing_md": null,
      "duplicate_action": "none",
      "will_process": true
    }
  ]
}
```

检查 `summary.will_process` 和 `files[].will_process` 是否符合预期。

### 可选：扫描后再决定处理哪些文件

一次性模式不能在运行时提问。若要根据扫描结果筛选（例如只重跑没有 `.md` 的文件、只处理某些路径），先 dry-run 生成清单：

```powershell
$plan = & $PY main.py "D:\docs" --cli --dry-run --json | ConvertFrom-Json
$plan.files | Where-Object { $_.will_process } |
  ForEach-Object { $_.path } |
  Set-Content -LiteralPath "files.txt" -Encoding UTF8

& $PY main.py "D:\docs" --cli --json --file-list "files.txt"
```

```bash
"$PY" main.py "/data/docs" --cli --dry-run --json > plan.json
# 用任意脚本筛选出要处理的 path，一行一个写入 files.txt
"$PY" main.py "/data/docs" --cli --json --file-list files.txt
```

`--file-list` 规则：

- 一行一个路径；绝对路径，或相对于扫描根目录的路径
- `#` 开头为注释，空行忽略
- 清单中任一路径不在扫描结果中时，退出码 `2`
- 与 `--include` / `--exclude` 同时使用时取交集

运行后才知道的信息：

- Token 失效 / 限额：`files[].message`，退出码 `1`
- 超页 PDF：自动拆分并合并，不需要运行时决策
- 轮询超时 / 上传失败：对应文件 `status` 为 `failed`
- 输出位置：成功文件的 `files[].output`

### 长任务：后台执行

如果任务可能超过工具调用超时，用 `--background` 启动，父进程立即返回：

```powershell
$start = & $PY main.py "D:\docs" --cli --background --no-proxy | ConvertFrom-Json
$jobId = $start.job_id

& $PY main.py --job-status $jobId
& $PY main.py --job-wait $jobId --job-timeout 600
& $PY main.py --job-result $jobId
```

```bash
job_id="$("$PY" main.py /data/docs --cli --background --no-proxy | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')"
"$PY" main.py --job-status "$job_id"
"$PY" main.py --job-wait "$job_id" --job-timeout 600
"$PY" main.py --job-result "$job_id"
```

job 控制命令始终输出 JSON，不需要 `--json`。任务文件位于 `.mineru_jobs/<job_id>/`：

- `job.json`：状态、PID、退出码、summary、错误
- `stdout.json`：完整结果（与前台 `--json` 相同）
- `stderr.log`：进度、警告、异常堆栈

`--job-wait` 默认最多等 3600 秒，`--job-timeout 0` 表示一直等；超时、任务不存在、任务仍在运行时退出码为 `2`。`job.json` 会遮罩 `--token`，但仍建议用 `MINERU_TOKEN`。

`--background` 不能与 `--interactive`、`--dry-run` 或 job 控制参数同时使用；多个后台任务会各自限流，不要同时启动太多。

### 第二步：实际执行

```powershell
$env:MINERU_TOKEN = "eyJ..."
& $PY main.py "D:\docs" --cli --no-proxy --duplicate skip --json
```

```bash
MINERU_TOKEN="eyJ..." "$PY" main.py "/data/docs" \
  --cli --no-proxy --duplicate skip --json
```

实际执行的 JSON：

```json
{
  "success": true,
  "root": "D:\\docs",
  "config": {
    "token_configured": true,
    "language": "ch",
    "batch_size": 20,
    "poll_interval": 8,
    "timeout": 1800,
    "keep_zip": false,
    "keep_json": false,
    "proxy_mode": "none",
    "lb_enabled": true
  },
  "summary": {
    "total": 3,
    "done": 2,
    "failed": 0,
    "skipped": 1,
    "batches": 1,
    "selected_size": 123456
  },
  "files": [
    {
      "path": "D:\\docs\\a.pdf",
      "rel_path": "a.pdf",
      "size": 123456,
      "status": "done",
      "message": "→ a.md",
      "output": "D:\\docs\\a.md"
    }
  ]
}
```

`files[].status` 取值：

- `done`：成功，`output` 是生成的 `.md` 绝对路径
- `failed`：失败，`message` 是原因
- `skipped`：已有同名 `.md`，按策略跳过

`--json` 模式不会实时输出进度，结果在全部处理结束后才写 stdout。调用方需要设置足够长的超时。

## 常用参数

| 参数 | 说明 |
|------|------|
| `ROOT_DIR` | 要扫描的根目录（位置参数，默认当前目录） |
| `--cli` | 一次性非交互执行（agent 必须加；所有参数一次给全） |
| `--interactive` | 旧版交互向导（仅人类使用，agent 不要用） |
| `--dry-run` | 只扫描并输出计划，不调用 API、不需要 Token |
| `--json` | 输出单个 JSON 对象到 stdout |
| `--duplicate overwrite` | 重新解析并覆盖已有 `.md` |
| `--duplicate skip` | 跳过已有 `.md`（默认） |
| `--duplicate rename` | 输出为 `{stem}_1.md`、`{stem}_2.md` 等 |
| `--include GLOB` | 只处理匹配文件，可重复 |
| `--exclude GLOB` | 排除匹配文件，可重复 |
| `--file-list PATH` | 只处理清单中的文件（一行一个；相对根目录或绝对路径） |
| `--background` | 后台执行一次性任务，立即返回 `job_id` |
| `--job-status ID` | 查看后台任务状态（输出 JSON） |
| `--job-wait ID` | 等待后台任务结束并输出结果（输出 JSON） |
| `--job-result ID` | 读取已完成后台任务的结果（输出 JSON） |
| `--job-timeout SEC` | `--job-wait` 超时秒数（默认 3600，0 表示一直等待） |
| `--language LANG` | 文档语言：`ch`（默认）、`en`、`japan`、`korean`、`latin` |
| `--batch-size N` | 每批文件数，1~50，默认 20 |
| `--poll-interval SEC` | 轮询间隔秒数，默认 8 |
| `--timeout SEC` | 单批次最长等待秒数，默认 1800 |
| `--no-proxy` | 强制不使用代理 |
| `--proxy URL` | 使用指定 HTTP 代理，如 `http://127.0.0.1:7890` |
| `--token TOKEN` | 直接指定 Token（多个用逗号分隔）；优先用环境变量 |
| `--save-config` | 把本次参数写回 `mineru_config.yaml`（会持久化 Token，谨慎使用） |

> `-y` / `--yes` 已废弃：`--cli` 现在默认就是一次性非交互模式。

### 文件过滤

`--include` / `--exclude` 可以重复使用：

```powershell
& $PY main.py "D:\docs" --cli --json `
  --include "*.pdf" `
  --include "report-*.docx" `
  --exclude "draft/*"
```

匹配规则：

- 模式不含 `/` 时匹配文件名，例如 `*.pdf`、`report-*.docx`
- 模式含 `/` 时匹配相对于根目录的路径，例如 `papers/2026/*`
- Windows 上大小写不敏感；`*` 可以跨目录分隔符
- 支持过滤后为空：返回退出码 `2`

## Token 使用

优先级从低到高：

1. `mineru_config.yaml` 中的 `token`
2. 环境变量 `MINERU_TOKEN`
3. `--token` 参数
4. 交互向导内部输入

agent 推荐用环境变量：

```powershell
$env:MINERU_TOKEN = "eyJ..."
& $PY main.py "D:\docs" --cli --json
```

```bash
export MINERU_TOKEN="eyJ..."
"$PY" main.py "/data/docs" --cli --json
```

多 Token 用逗号分隔，程序会按批次轮询并自动切换失效 Token：

```powershell
$env:MINERU_TOKEN = "eyJ_token1...,eyJ_token2..."
```

注意：

- 配置文件位于**仓库目录**（`main.py` 同级）的 `mineru_config.yaml`，不是当前工作目录
- 该文件已被 `.gitignore` 忽略，但仍包含明文 Token，不要提交或上传
- `--save-config` 会把当前 Token 写入该文件
- Token 失效（HTTP 401 / `A0202` / `A0211`）时不会重试同一个 Token，应更换 Token
- HTTP 429 会优先切换到其他可用 Token；没有其他 Token 时按 `Retry-After` / 指数退避重试（最多 5 次），仍失败才标记限流并报错
- 同一进程内申请上传 URL 的请求串行执行，并有 2 秒最小间隔；不要同时启动多个进程处理大批量文件

## 重复 `.md` 策略

非交互模式下，优先级为：

1. `--duplicate` 参数
2. `mineru_config.yaml` 的 `duplicate_default`
3. `skip`

| 策略 | 行为 |
|------|------|
| `skip` | 保留旧 `.md`，不提交该文件（默认，最安全） |
| `overwrite` | 提交前删除旧 `.md`，解析完成后写入新文件 |
| `rename` | 写入 `{stem}_1.md`、`{stem}_2.md`，不碰旧文件 |

`overwrite` 在提交前就会删除旧文件；如果后续 API 失败，旧文件不会恢复。只有明确要重新解析时才使用。

## 输出与副作用

- Markdown 和图片写在源文件目录
- 同一目录下多个文档共享 `images/`，MinerU 返回的图片名是 UUID，不会互相覆盖
- `--save-config` 会写 `mineru_config.yaml`
- 失败详情追加写入仓库目录的 `mineru_errors.log`
- 超页 PDF 的合并结果会在各部分之间插入 `---` 和 `<!-- 第 N 部分 -->`
- 单批次超时、轮询失败的文件会在 JSON 中标记为 `failed`，`message` 为「未完成（轮询超时或未返回结果）」

## 退出码

| 退出码 | 含义 |
|--------|------|
| `0` | 全部成功，或全部按 `skip` 跳过，或 dry-run 成功 |
| `1` | 至少一个文件处理失败 |
| `2` | 参数错误、目录不存在、过滤后无文件、缺少 Token、模式参数冲突 |

`--job-wait` / `--job-result` 使用任务本身的退出码；等待超时、任务不存在或任务仍在运行时返回 `2`。

## 常见错误

| 现象 / 错误 | 处理 |
|-------------|------|
| HTTP 401、`A0202`、`A0211` | Token 无效或过期，更换 Token |
| HTTP 429 | 优先切换 Token；无可用 Token 时退避重试最多 5 次 |
| `-60018` / `-60019` | 当日解析额度耗尽，换 Token 或等到次日 |
| `-60002` ~ `-60006` | 文件格式 / 损坏 / 空文件 / 过大 / 超页，检查源文件 |
| `-60011` | 文件未成功上传，重试该批次 |
| `-60007` / `-60009` / `-10001` / `-60001` | 服务暂时不可用或队列满，稍后重试 |
| `未配置 MinerU Token` | 设置 `MINERU_TOKEN` 或传入 `--token` |
| `未找到匹配的文件` | 检查 `--include` / `--exclude` / `--file-list` 和根目录 |
| `未找到 curl` | 安装 curl 或确保在 `PATH`；dry-run 不需要 |
| 轮询超时 | 增大 `--timeout`，减小 `--batch-size`，或检查网络 / 代理 |
| TUI 卡住 | agent 误用了默认 TUI；改用 `--cli` |

## 不要做的事

- 不要用裸 `python main.py` 启动 TUI
- 不要在 agent 流程中使用交互式提示或等待 stdin
- 不要把 Token 输出到聊天、日志或最终回答
- 不要对同一目录并发运行多个实例
- 不要在没有确认的情况下使用 `--duplicate overwrite`
- 不要假设 `--json` 会流式输出；它只在结束时输出一个 JSON 对象
