# MinerU 批量解析下载工具

调用 [MinerU](https://mineru.net) API，将本地目录中的 PDF / Word / PPT / 图片批量解析为结构化 Markdown，公式保留为 LaTeX，表格保留结构，支持 OCR 扫描件。

提供三种运行方式：
- **TUI 模式**（默认）— 基于 [Textual](https://github.com/Textualize/textual) 的终端 UI，直观选择文件、配置参数、实时查看进度
- **CLI 模式** — 一次性非交互执行，适合 SSH / 脚本 / agent；所有参数通过命令行或配置给出
- **交互向导** — 旧版分步提问式 CLI，使用 `--interactive`（仅人类使用）

> Agent / 脚本的非交互使用说明见 [`skills/skill.md`](skills/skill.md)。

---

## 功能特性

- 递归扫描目录，支持 `.pdf` `.docx` `.pptx` `.png` `.jpg` 等格式
- 批量提交（每批最多 50 个文件）并发上传，并发下载结果 ZIP
- **多 Token 负载均衡**：逗号分隔多个 Token，轮询分配批次；Token 401/429 时自动切换，限流/网络错误指数退避重试
- 统一错误分类：AUTH / RATE_LIMIT / TRANSIENT / FILE_ERROR 等，失败自动重试或切换
- 重复 `.md` 处理：覆盖 / 跳过 / 改名，三种策略
- 后台任务：`--background` 启动，`--job-status` / `--job-wait` / `--job-result` 查询结果
- 配置持久化到 `mineru_config.yaml`，支持环境变量 `MINERU_TOKEN`
- Token 优先级：`mineru_config.yaml` < `MINERU_TOKEN` 环境变量 < `--token` 参数 < 交互向导内部输入

---

## 快速开始

### 1. 环境要求

- Python 3.10+
- Windows 10 / 11、Linux 或 macOS
- 系统 `curl`：Windows 10 / 11 内置 `curl.exe`，Linux / macOS 通常已预装（缺失时见下方安装提示）

> OSS 上传统一通过系统 `curl` 完成，用于规避 Windows 上阿里云 OSS 的 SSL 问题；程序会自动查找 `curl.exe` / `curl`。

Linux 安装 curl：

```bash
# Debian / Ubuntu
sudo apt install curl
# Fedora
sudo dnf install curl
# Arch Linux
sudo pacman -S curl
```

### 2. 安装依赖

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. 获取 Token

前往 [mineru.net](https://mineru.net) 注册账号，在个人中心获取 API Token（以 `eyJ...` 开头的 JWT 字符串）。

### 4. 运行

```bash
# TUI 模式（扫描指定目录）
python main.py D:\docs

# TUI 模式（当前目录）
python main.py

# CLI 一次性非交互执行
python main.py D:\docs --cli

# 旧版交互向导
python main.py D:\docs --interactive

# 只扫描并输出 JSON，不调用 API
python main.py D:\docs --cli --dry-run --json

# 指定 Token 并使用 CLI 模式
python main.py D:\docs --cli --token eyJ...

# 关闭代理并保存配置
python main.py . --no-proxy --save-config
```

---

## 命令行参数

| 参数 | 说明 |
|------|------|
| `ROOT_DIR` | 要扫描的根目录（默认：当前目录）|
| `--cli` | 一次性非交互执行（agent / 脚本；所有参数需一次给全）|
| `--interactive` | 旧版交互式 CLI 向导（仅人类使用）|
| `--tui` | 使用 TUI 模式（默认）|
| `--token TOKEN` | MinerU API Token，多个用逗号分隔 |
| `--proxy URL` | HTTP 代理地址，如 `http://127.0.0.1:7890` |
| `--no-proxy` | 强制不使用任何代理 |
| `--language LANG` | 文档语言（默认 `ch`，可选 `en` `japan` `korean` `latin`）|
| `--batch-size N` | 每批文件数上限（1~50，默认 20）|
| `--poll-interval SEC` | 轮询间隔秒数（默认 8）|
| `--timeout SEC` | 单批次超时秒数（默认 1800）|
| `--dry-run` | 只扫描并输出将处理的文件，不调用 API、不需要 Token |
| `--json` | 以 JSON 输出结果（stdout 只有 JSON）|
| `--duplicate {overwrite,skip,rename}` | 重复 `.md` 的处理策略（默认读取配置，否则 `skip`）|
| `--include GLOB` | 只处理匹配的文件（可重复；无斜杠时匹配文件名）|
| `--exclude GLOB` | 排除匹配的文件（可重复；无斜杠时匹配文件名）|
| `--file-list PATH` | 只处理清单中的文件（一行一个；相对根目录或绝对路径）|
| `--background` | 后台执行一次性任务，立即返回 `job_id` |
| `--job-status ID` | 查看后台任务状态（输出 JSON）|
| `--job-wait ID` | 等待后台任务结束并输出结果（输出 JSON）|
| `--job-result ID` | 读取已完成后台任务的结果（输出 JSON）|
| `--job-timeout SEC` | `--job-wait` 超时秒数（默认 3600，0 表示一直等待）|
| `--save-config` | 将本次参数写回 `mineru_config.yaml` |

> `-y` / `--yes` 已废弃：`--cli` 现在默认就是一次性非交互模式。

---

## 非交互 / Agent 用法

`--cli` 是一次性执行：不读 stdin、不弹确认，参数必须一次给全。建议先 dry-run 获取扫描结果，再执行：

```bash
export MINERU_TOKEN="eyJ..."   # Windows: set MINERU_TOKEN=eyJ...

# 1. 预扫描：不调用 API、不需要 Token、不会写配置
python main.py /path/to/docs --cli --dry-run --json > plan.json

# 2. 执行全部可处理文件（已有 .md 默认跳过）
python main.py /path/to/docs --cli --json --no-proxy --duplicate skip
```

如果 dry-run 之后才发现只想处理部分文件（例如按 `existing_md` 手工筛选），把选中的路径写入清单，再执行：

```bash
# files.txt 一行一个，支持相对根目录或绝对路径，# 开头为注释
python main.py /path/to/docs --cli --json --file-list files.txt
```

其他常用组合：

```bash
# 只处理 PDF，排除草稿目录
python main.py /path/to/docs --cli --json \
  --include "*.pdf" --exclude "draft/*"

# 只覆盖部分文件：用清单 + overwrite
python main.py /path/to/docs --cli --json \
  --file-list reparse.txt --duplicate overwrite
```

`--json` 模式下 stdout 只包含一个 JSON 对象，进度与日志走 stderr。实际执行的 JSON 包含
`success`、`summary`（`done` / `failed` / `skipped`）和 `files[]`（`status`、`message`、`output`）。

运行后才能知道的信息这样处理：

- 文件清单 / 重复 `.md`：先 `--dry-run --json`，再用 `--file-list` 或 `--duplicate` 决定
- Token 无效 / 限流：写入 `files[].message` 与 `mineru_errors.log`，用退出码区分
- 超页 PDF：自动拆分并合并，无需交互
- 批次失败 / 超时：该文件标记为 `failed`，退出码 `1`
- 输出路径：成功文件的 `files[].output` 给出绝对路径

退出码：`0` 全部成功（或全部跳过），`1` 至少一个文件失败，`2` 参数 / 扫描 / Token 配置错误。

---

## 后台任务

长任务可以放到后台，启动命令会立即返回 `job_id`：

```bash
# 启动（--background 会自动给子进程加 --json）
python main.py /path/to/docs --cli --background --no-proxy

# 查看状态（running / done / failed）
python main.py --job-status <job_id>

# 等待结束并输出结果（默认最多等 3600 秒，0 表示一直等）
python main.py --job-wait <job_id> --job-timeout 600

# 任务结束后读取完整结果
python main.py --job-result <job_id>
```

后台控制命令始终输出 JSON。每个任务在 `.mineru_jobs/<job_id>/` 下保存：

| 文件 | 内容 |
|------|------|
| `job.json` | 状态、PID、退出码、summary、错误信息 |
| `stdout.json` | 子进程的完整 JSON 结果（与前台 `--json` 相同）|
| `stderr.log` | 进度、警告和异常堆栈 |

`--job-wait` / `--job-result` 的退出码与任务本身一致：`0` 成功，`1` 有文件失败，`2` 预检/配置错误；`--job-wait` 超时、任务不存在或任务仍在运行时也返回 `2`。

注意：

- `--background` 不能与 `--interactive`、`--tui`、`--dry-run` 或 job 控制参数同时使用
- 子进程会脱离当前终端，父进程退出后继续运行
- `job.json` 会遮罩 `--token`，但操作系统进程列表仍可能看到命令行参数；优先用 `MINERU_TOKEN`
- `.mineru_jobs/` 已加入 `.gitignore`；任务完成后不会自动删除
- 每个后台进程独立限流；不要同时启动多个后台任务处理大量文件，否则仍可能触发 API 限流
- 可用环境变量 `MINERU_JOB_DIR` 覆盖任务目录

---

## 并发与限流

- **批次提交串行**：申请上传 URL 的请求在同一进程内串行执行，两次请求之间至少间隔 2 秒
- **同批上传并发**：同一批次内的文件通过 `curl` 上传到 OSS，最多 4 个并发（`MAX_UPLOAD_WORKERS`）
- **轮询 / 下载并发**：多个批次并发轮询；单个文件解析完成即进入下载队列，结果 ZIP 最多 4 个并发下载
- **超时语义**：轮询超时只影响未完成文件；已完成并下载的文件仍计为成功
- **429 / 网络错误**：有其他可用 Token 时优先切换；没有其他 Token 时指数退避重试最多 5 次，支持 `Retry-After`；仍失败才标记 Token 限流
- TUI 和 CLI 共用 `api.py`，上传调度行为一致；仓库内没有独立的 GUI 模块

如果第一个批次就持续 429，通常不是“多批次并发”导致的，而是账号配额或全局限流。检查错误码 `-60018` / `-60019`，或换 Token 后重试。

---

## 多 Token 负载均衡

输入多个 Token（逗号分隔）时可启用负载均衡，将不同批次（注意不是每一个，而是基于 batch_size 的“轮次”）轮流分配给不同 Token：

**TUI 模式**：在 Token 输入框中填写 `eyJtoken1...,eyJtoken2...`，勾选"启用负载均衡"复选框。

**交互向导**：输入多个 Token 后，程序自动询问是否启用负载均衡。

**一次性 CLI**：通过 `MINERU_TOKEN` 或 `--token` 传入逗号分隔的多个 Token，默认启用负载均衡。

**命令行**：
```bash
python main.py D:\docs --token "eyJtoken1...,eyJtoken2..."
```

**容错行为**：
- 某 Token 返回 401 → 标记为 `INVALID`，自动切换到下一个
- 某 Token 返回 429（限速）→ 有其他可用 Token 时立即标记该 Token 并切换；没有其他 Token 时按 `Retry-After` / 指数退避重试（最多 5 次），仍失败才报错
- 所有 Token 均失效 → 报错退出

---

## 配置文件

首次运行后自动生成 `mineru_config.yaml`，也可手动编辑：

```yaml
# 单个 Token
token: eyJ...

# 或多个 Token（列表）
token:
  - eyJtoken1...
  - eyJtoken2...

language: ch          # 文档语言
proxy_mode: system    # system | custom | none
proxy_url: ''         # 自定义代理地址（proxy_mode=custom 时生效）
batch_size: 20        # 每批文件数（1~50）
poll_interval: 8      # 轮询间隔（秒）
timeout: 1800         # 单批次超时（秒）
keep_zip: false       # 是否保留下载的 ZIP
keep_json: false      # 是否保留解析 JSON
lb_enabled: true      # 负载均衡开关（多 Token 时自动开启）
```

也可通过环境变量提供 Token（会覆盖配置文件，但低于 `--token` 参数）：

```bash
set MINERU_TOKEN=eyJ...
python main.py D:\docs
```

---

## 支持的文件格式

| 类型 | 扩展名 |
|------|--------|
| PDF | `.pdf` |
| 图片 | `.png` `.jpg` `.jpeg` `.jp2` `.webp` `.gif` `.bmp` |
| Word | `.doc` `.docx` |
| PowerPoint | `.ppt` `.pptx` |

---

## 重复 .md 处理

当目录中已存在与源文件同名的 `.md` 文件时，程序提供三种处理策略：

| 策略 | 说明 |
|------|------|
| **覆盖** | 删除旧 `.md`，重新解析写入 |
| **跳过** | 不提交该文件，保留旧 `.md` |
| **改名** | 输出为 `{stem}_1.md`、`{stem}_2.md` 等 |

TUI 模式：选中有 ⚠ 标记的文件，按 `D` 键设置策略；或按 `G` 开始时弹出批量处理对话框。

---

## 项目结构

```
MinerUDownloader/
├── main.py           # 入口，参数解析，分发 TUI / CLI / 交互向导
├── tui.py            # Textual TUI（SelectScreen + ProgressScreen）
├── cli.py            # 一次性 CLI + 旧交互向导（rich 格式化输出）
├── api.py            # MinerU API 封装（上传、轮询、切换 Token）
├── processor.py      # ZIP 下载、解压、文件整理
├── scanner.py        # 递归目录扫描，构建文件树
├── config.py         # YAML 配置加载 / 保存 / 合并
├── token_manager.py  # 多 Token 管理与负载均衡
├── errors.py         # 统一错误分类与 MinerUApiError
├── jobs.py           # 后台任务目录、状态检测、结果读取
├── requirements.txt
├── skills/
│   └── skill.md      # 给其他 agent 的非交互使用说明
├── .mineru_jobs/     # 后台任务状态与结果（自动生成，已忽略）
└── mineru_config.yaml  # 持久化配置（自动生成）
```

---

## 依赖

| 包 | 用途 |
|----|------|
| `textual >= 0.60.0` | TUI 框架 |
| `requests >= 2.31.0` | HTTP 客户端 |
| `rich >= 13.0.0` | CLI 格式化输出 |
| `pyyaml >= 6.0.0` | 配置文件读写 |
| `pypdf >= 4.0.0` | 超页 PDF 自动拆分 |

系统依赖：`curl`（Windows 10 / 11 内置 `curl.exe`；Linux / macOS 通常预装）

---

## 打包为可执行文件（Windows / Linux / macOS）

项目提供跨平台的 `mineru_downloader.spec`，可在目标平台上用 PyInstaller 打包为免安装 Python 的独立程序。

> PyInstaller 不支持交叉编译：在 Windows 上打包得到 Windows 版，在 Linux 上打包得到 Linux 版。

### 步骤

```bash
# 1. 安装 PyInstaller（仅需一次）
pip install pyinstaller

# 2. 打包（首次）
pyinstaller --clean -y mineru_downloader.spec

# 重新打包（覆盖已有 dist）
pyinstaller --clean -y mineru_downloader.spec
```

打包完成后，输出目录为 `dist/mineru-downloader/`（Windows 下为 `dist\mineru-downloader\`），直接分发整个目录即可。

### 使用打包版本

```bash
cd dist/mineru-downloader

# Windows
.\mineru-downloader.exe D:\docs
.\mineru-downloader.exe D:\docs --cli

# Linux / macOS
./mineru-downloader /path/to/docs
./mineru-downloader /path/to/docs --cli

./mineru-downloader --help
```

> **注意**：打包版本依赖系统的 `curl`（Windows 10 / 11 已内置）。`mineru_config.yaml` 和后台任务目录 `.mineru_jobs/` 会在可执行文件所在目录生成，首次运行需配置 Token。

### 输出说明

| 路径 | 说明 |
|------|------|
| `dist/mineru-downloader/mineru-downloader[.exe]` | 主程序入口（约 6 MB）|
| `dist/mineru-downloader/_internal/` | Python 运行时与依赖库 |
| `build/` | 中间产物，可删除 |
