# MinerU 批量解析下载工具

调用 [MinerU](https://mineru.net) API，将本地目录中的 PDF / Word / PPT / 图片批量解析为结构化 Markdown，公式保留为 LaTeX，表格保留结构，支持 OCR 扫描件。

提供两种交互模式：
- **TUI 模式**（默认）— 基于 [Textual](https://github.com/Textualize/textual) 的终端 UI，直观选择文件、配置参数、实时查看进度
- **CLI 模式** — 纯命令行向导，适合 SSH / 脚本场景

---

## 功能特性

- 递归扫描目录，支持 `.pdf` `.docx` `.pptx` `.png` `.jpg` 等格式
- 批量提交（每批最多 50 个文件）并发上传，并发下载结果 ZIP
- **多 Token 负载均衡**：逗号分隔多个 Token，轮询分配批次；Token 401/429 时自动切换
- 统一错误分类：AUTH / RATE_LIMIT / TRANSIENT / FILE_ERROR 等，失败自动重试或切换
- 重复 `.md` 处理：覆盖 / 跳过 / 改名，三种策略
- 配置持久化到 `mineru_config.yaml`，支持环境变量 `MINERU_TOKEN`
- Token 优先级：`MINERU_TOKEN` 环境变量 < `mineru_config.yaml` < `--token` 参数 < TUI/CLI 内部输入

---

## 快速开始

### 1. 环境要求

- Python 3.10+
- Windows 10 / 11（需要内置 `curl.exe`，用于规避 Windows 上阿里云 OSS 的 SSL 问题）

> macOS / Linux 可用，但 OSS 上传回退到 `curl` 命令，需确保已安装。

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

# CLI 模式
python main.py D:\docs --cli

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
| `--cli` | 使用纯 CLI 向导模式 |
| `--tui` | 使用 TUI 模式（默认）|
| `--token TOKEN` | MinerU API Token，多个用逗号分隔 |
| `--proxy URL` | HTTP 代理地址，如 `http://127.0.0.1:7890` |
| `--no-proxy` | 强制不使用任何代理 |
| `--language LANG` | 文档语言（默认 `ch`，可选 `en` `japan` `korean` `latin`）|
| `--batch-size N` | 每批文件数上限（1~50，默认 20）|
| `--poll-interval SEC` | 轮询间隔秒数（默认 8）|
| `--timeout SEC` | 单批次超时秒数（默认 1800）|
| `--save-config` | 将本次参数写回 `mineru_config.yaml` |

---

## 多 Token 负载均衡

输入多个 Token（逗号分隔）时可启用负载均衡，将不同批次（注意不是每一个，而是基于 batch_size 的“轮次”）轮流分配给不同 Token：

**TUI 模式**：在 Token 输入框中填写 `eyJtoken1...,eyJtoken2...`，勾选"启用负载均衡"复选框。

**CLI 模式**：向导中输入多个 Token 后，程序自动询问是否启用负载均衡。

**命令行**：
```bash
python main.py D:\docs --token "eyJtoken1...,eyJtoken2..."
```

**容错行为**：
- 某 Token 返回 401 → 标记为 `INVALID`，自动切换到下一个
- 某 Token 返回 429（限速）→ 标记为 `RATE_LIMITED`，自动切换
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

也可通过环境变量提供 Token（优先级低于配置文件）：

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
├── main.py           # 入口，参数解析，分发 TUI / CLI
├── tui.py            # Textual TUI（SelectScreen + ProgressScreen）
├── cli.py            # 纯 CLI 向导（rich 格式化输出）
├── api.py            # MinerU API 封装（上传、轮询、切换 Token）
├── processor.py      # ZIP 下载、解压、文件整理
├── scanner.py        # 递归目录扫描，构建文件树
├── config.py         # YAML 配置加载 / 保存 / 合并
├── token_manager.py  # 多 Token 管理与负载均衡
├── errors.py         # 统一错误分类与 MinerUApiError
├── requirements.txt
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

系统依赖：`curl.exe`（Windows 10 / 11 内置）

---

## 打包为 Windows 可执行文件

项目提供 `mineru_downloader.spec`，可用 PyInstaller 打包为免安装 Python 的独立程序。

### 步骤

```bash
# 1. 安装 PyInstaller（仅需一次）
pip install pyinstaller

# 2. 打包（首次）
pyinstaller --clean -y mineru_downloader.spec

# 重新打包（覆盖已有 dist）
pyinstaller --clean -y mineru_downloader.spec
```

打包完成后，输出目录为 `dist\mineru-downloader\`，直接分发整个目录即可。

### 使用打包版本

```bash
# 将 dist\mineru-downloader\ 复制到目标机器后：
cd mineru-downloader
.\mineru-downloader.exe D:\docs
.\mineru-downloader.exe D:\docs --cli
.\mineru-downloader.exe --help
```

> **注意**：打包版本仍依赖系统的 `curl.exe`（Windows 10 / 11 已内置）。`mineru_config.yaml` 会在 exe 所在目录生成，首次运行需配置 Token。

### 输出说明

| 路径 | 说明 |
|------|------|
| `dist\mineru-downloader\mineru-downloader.exe` | 主程序入口（约 6 MB）|
| `dist\mineru-downloader\_internal\` | Python 运行时与依赖库 |
| `build\` | 中间产物，可删除 |
