# MinerU 批量解析下载工具 — 实现计划

## 一、需求概述

递归扫描指定根目录下所有 MinerU 支持的文档，通过 TUI 界面让用户选择文件，
批量提交精准解析 API（vlm 模型），轮询等待结果，下载 ZIP 并解压到原文件所在目录。

支持文件类型：`.pdf` / `.png` / `.jpg` / `.jpeg` / `.jp2` / `.webp` / `.gif` / `.bmp`
                / `.doc` / `.docx` / `.ppt` / `.pptx`

---

## 二、项目结构

```
MinerUDownloader/
├── main.py          # 入口：解析参数 → 分发到 TUI 或 CLI 模式
├── tui.py           # Textual TUI 应用（文件树 + 选项面板）
├── cli.py           # 纯命令行交互模式（rich 格式化输出 + input() 向导）
├── scanner.py       # 递归扫描，构建文件树数据结构
├── api.py           # MinerU API 封装（上传、批量提交、轮询）
├── processor.py     # ZIP 下载、解压、文件整理逻辑
├── config.py        # YAML 配置加载 / 保存工具
├── mineru_config.yaml  # 用户持久化配置（自动生成，不提交 VCS）
├── requirements.txt # 依赖清单
└── PLAN.md          # 本文件
```

---

## 三、依赖清单（requirements.txt）

```
textual>=0.60.0   # TUI 框架
requests>=2.31.0  # HTTP 客户端
rich>=13.0.0      # 终端渲染（textual / cli 共用）
pyyaml>=6.0.0     # YAML 配置读写
```

Python 版本要求：≥ 3.10

---

## 四、入口（main.py）

```
python main.py [ROOT_DIR] [OPTIONS]

参数：
  ROOT_DIR            要扫描的根目录（默认：当前目录）

模式选项（二选一）：
  --tui               启动 Textual TUI 界面（默认）
  --cli               启动纯命令行交互模式（无 TUI，适合 SSH / 脚本）

其他选项：
  --token TEXT        MinerU API Token（也可通过 MINERU_TOKEN 环境变量设置）
  --proxy TEXT        HTTP 代理地址，如 http://127.0.0.1:7890
  --no-proxy          绕开系统代理（忽略环境变量中的代理设置）
  --language TEXT     文档语言（默认：ch，可覆盖 YAML 中的保存值）
  --batch-size INT    每批文件数上限（默认：20，API 上限：50）
  --poll-interval INT 轮询间隔秒数（默认：8）
  --timeout INT       单批次超时秒数（默认：1800）
  --save-config       将本次命令行参数写回 mineru_config.yaml
```

**配置优先级**（高 → 低）：命令行参数 > `mineru_config.yaml` > 程序内置默认值

**Token 查找顺序**：`--token` 参数 → `MINERU_TOKEN` 环境变量 → `mineru_config.yaml` → TUI/CLI 提示输入

---

## 五、文件扫描模块（scanner.py）

### 数据结构

```python
from enum import Enum

class DuplicateAction(Enum):
    NONE = "none"         # 尚未决定（初始状态，已存在 .md 时设置）
    OVERWRITE = "overwrite"   # 覆盖已有 .md
    SKIP = "skip"         # 跳过不解析
    RENAME = "rename"     # 输出重命名为 {stem}_1.md、{stem}_2.md …

@dataclass
class FileNode:
    path: Path                       # 绝对路径
    rel_path: Path                   # 相对于根目录的路径
    size: int                        # 文件大小（字节）
    selected: bool = True
    existing_md: Path | None = None  # 若已有同名 .md，记录其路径
    duplicate_action: DuplicateAction = DuplicateAction.NONE

@dataclass
class DirNode:
    path: Path
    rel_path: Path
    children: list[FileNode | DirNode]  # 混合子节点
```

扫描完成后，额外检测每个 `FileNode` 对应的 `{path.parent}/{path.stem}.md` 是否存在，若存在则填充 `existing_md`。

### 扫描逻辑

- 使用 `Path.rglob()` 递归扫描，跳过隐藏目录（以 `.` 开头）
- 仅保留支持的扩展名（大小写不敏感）
- 按目录分组，构建嵌套树结构
- 空目录不显示

---

## 六、TUI 界面设计（tui.py）

### 布局

```
┌─────────────────────────────────┬──────────────────────┐
│  📁 文件树（左侧，可滚动）          │  ⚙ 选项面板（右侧）    │
│                                  │                      │
│  ▶ [x] 根目录/                   │  Token: [_________]  │
│    ▶ [x] subdir/                 │  语言:  [ch       ▼] │
│        [x] file1.pdf  1.2MB      │                      │
│        [x] ⚠ file2.docx 0.8MB  │  ☑ 保留 ZIP 压缩包   │
│            └─ 已有 file2.md      │  ☑ 保留 JSON 文件    │
│               [覆盖][跳过][改名] │                      │
│      [x] note.pptx   3.5MB      │  代理设置：           │
│                                  │  ○ 使用系统代理       │
│                                  │  ● 自定义代理         │
│                                  │  ○ 不使用代理         │
│                                  │  [http://127.0.0.1:] │
├─────────────────────────────────┴──────────────────────┤
│  [A]全选  [N]全不选  [Space]切换  [Enter]确认开始  [Q]退出  │
│  已选: 3 个文件，共 5.5 MB    ⚠ 1 个文件有已存在的 .md      │
└──────────────────────────────────────────────────────────┘
```

**⚠ 重复文件处理**：扫描完成后自动检测，在文件树中用黄色 `⚠` 标记有已存在 `.md` 的文件，
光标移到该行按 `D` 键弹出内联操作条，可选：
- **覆盖**（Overwrite）：删除旧 `.md`，重新解析写入
- **跳过**（Skip）：该文件在确认后不提交解析
- **改名**（Rename）：输出为 `{stem}_1.md`（若 `_1` 也存在则自动递增）

若用户确认前仍有文件处于未决定状态（`NONE`），`Enter` 键会提示批量设置默认动作（覆盖 / 跳过 / 改名）。

### 交互逻辑

| 按键 | 行为 |
|------|------|
| ↑ / ↓ | 移动光标 |
| Space | 切换当前项选中状态（文件或整个目录） |
| → / ← | 展开 / 折叠目录 |
| A | 全选所有文件 |
| N | 取消所有选中 |
| Enter | 确认选择，进入处理流程（在同一 TUI 窗口展示进度） |
| Q / Esc | 退出程序 |

选中目录时，切换其下所有文件的选中状态。

---

## 七、API 模块（api.py）

### 并发与限流策略

- **批次大小**：每批 ≤ `batch_size`（默认 20，API 限制 50）
- **上传并发**：使用 `ThreadPoolExecutor(max_workers=4)` 并发上传同批文件
- **上传方式**：调用 `subprocess.run(["curl.exe", "-s", "-X", "PUT", "-T", file, url])`
  （绕开 Python requests 在 Windows 上对阿里云 OSS 的 SSL EOF 问题）
- **批次间延迟**：每批次提交后等待 2 秒再提交下一批，避免触发频控
- **轮询并发**：所有 batch_id 并发轮询，用 `threading.Event` 协调完成状态

### 关键 API 端点

```
POST https://mineru.net/api/v4/file-urls/batch      → 申请上传 URL
GET  https://mineru.net/api/v4/extract-results/batch/{batch_id} → 轮询结果
```

### 函数签名

```python
def apply_upload_urls(files: list[dict], token: str,
                      model_version="vlm", language="ch",
                      proxies=None) -> tuple[str, list[str]]:
    """申请预签名上传 URL，返回 (batch_id, [upload_url, ...]）"""

def upload_file_curl(local_path: Path, upload_url: str) -> bool:
    """用 curl.exe 上传单个文件，返回是否成功"""

def poll_batch(batch_id: str, token: str,
               interval: int, timeout: int,
               proxies=None,
               on_progress=None) -> list[dict]:
    """
    轮询批次结果直到全部完成或超时。
    on_progress(results) 回调用于更新 TUI 进度。
    返回 extract_result 列表。
    """
```

### 代理配置

```python
def build_proxies(mode: str, custom_url: str = "") -> dict | None:
    """
    mode: "system"  → None（requests 默认读取环境变量）
          "custom"  → {"http": url, "https": url}
          "none"    → {"http": "", "https": ""}（强制不走代理）
    """
```

> **注意**：`curl.exe` 上传不经过 Python requests，代理需单独通过 `-x` 参数传入。

---

## 八、处理模块（processor.py）

### 目录内共享 images/ 原则

同一目录下的多个文件解析结果共用一个 `images/` 子目录：

```
path/to/               ← 这里假设有 a.pdf、b.pdf、c.docx
├── a.pdf
├── a.md               ← 解压后生成
├── b.pdf
├── b.md               ← 解压后生成
├── c.docx
├── c.md               ← 解压后生成
└── images/            ← 三个文件的图片合并到这里
    ├── abc123.jpg     ← 来自 a.pdf
    ├── def456.png     ← 来自 b.pdf
    └── ghi789.jpg     ← 来自 c.docx
```

每个 `{stem}.md` 中的图片引用路径为 `images/xxx.jpg`（相对路径），
与共享 `images/` 同级，因此**路径无需修改**。

### 解压逻辑

```python
def extract_result(zip_url: str, file_path: Path,
                   keep_zip: bool, keep_json: bool,
                   output_stem: str,
                   proxies=None) -> None:
    """
    output_stem: 输出文件名主干（通常 = file_path.stem，改名时为 stem_1 等）

    1. 下载 ZIP（streaming，避免大文件内存溢出）
    2. 按需保存 .zip 到原文件目录（文件名 = {output_stem}.zip）
    3. 遍历 ZIP 内文件：
       - full.md          → 写为 {output_stem}.md，放在 file_path.parent
       - images/*         → 写入 file_path.parent / "images" /
                            （MinerU 图片以 UUID 命名，理论上无重名冲突）
       - *.json           → 仅当 keep_json=True 时写入 file_path.parent
       - *_origin.pdf     → 始终丢弃（与源文件重复）
       - 其他文件         → 丢弃
    4. full.md 中图片引用已是相对路径 images/xxx，与共享 images/ 同级，
       无需修改路径
    """
```

### 下载并发

- 使用 `ThreadPoolExecutor(max_workers=4)` 并发下载多个 ZIP
- 每个文件独立错误处理，失败时记录错误日志，不影响其他文件

---

## 九、进度展示（TUI 处理阶段）

确认开始后，左侧文件树变为进度列表，每行显示：

```
[✓] file1.pdf          → 上传完成
[⠿] file2.docx         → 解析中 (第3/10页)
[✓] file3.pdf          → 下载完成
[✗] file4.pptx         → 失败: 文件页数超出限制
[…] file5.jpg          → 排队中
```

状态图标：`…`排队 / `↑`上传中 / `⠿`解析中 / `↓`下载中 / `✓`完成 / `✗`失败

右侧显示：
- 当前批次进度（第 X / 总 Y 批）
- 总文件进度（已完成 X / 总 Y）
- 已耗时 / 预估剩余时间
- 实时日志（最后 10 条）

全部处理完成后，显示汇总报告，提示按 Q 退出。

---

## 十、错误处理策略

| 场景 | 处理方式 |
|------|---------|
| Token 缺失 / 无效 | TUI 提示重新输入，不退出程序 |
| 上传失败（curl 返回非 0） | 标记该文件为 `失败-上传`，跳过，继续其他文件 |
| curl.exe 不存在 | 启动时检测，缺失则提示错误并退出（Windows 10+ 内置） |
| API 返回非 0 code | 解析错误信息，展示给用户，标记对应文件失败 |
| 解析失败（state=failed） | 记录 err_msg，标记文件失败，不影响同批其他文件 |
| 轮询超时 | 提示用户，记录 batch_id 供手动重试，继续处理其他批次 |
| ZIP 下载失败 | 重试最多 3 次（指数退避），仍失败则标记文件失败 |
| 解压时图片路径冲突 | 自动添加随机后缀重命名，更新 md 中的引用 |
| 批次提交 429 限流 | 指数退避重试（最多 5 次），超出则记录失败 |
| 网络连接超时 | requests timeout=30，超时则重试最多 3 次 |

所有失败信息写入 `mineru_errors.log`（追加模式），格式：
```
[2026-05-01 12:34:56] ERROR file=path/to/file.pdf stage=upload msg=...
```

---

## 十一、配置模块（config.py + mineru_config.yaml）

### mineru_config.yaml（自动生成，位于项目目录）

```yaml
# MinerU Downloader 持久化配置
# 由程序自动生成/更新，也可手动编辑

token: ""                 # MinerU API Token（留空则每次启动提示）
language: ch              # 文档语言
proxy_mode: system        # system / custom / none
proxy_url: ""             # 自定义代理地址，proxy_mode=custom 时有效
batch_size: 20            # 每批文件数（1~50）
poll_interval: 8          # 轮询间隔（秒）
timeout: 1800             # 单批超时（秒）
keep_zip: false           # 是否保留下载的 ZIP 压缩包
keep_json: false          # 是否保留解析 JSON 文件
duplicate_default: null   # 重复文件默认动作：overwrite / skip / rename / null（每次询问）
```

### config.py — 加载 / 保存工具

```python
CONFIG_FILE = Path("mineru_config.yaml")   # 始终在程序工作目录

SUPPORTED_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".jp2",
    ".webp", ".gif", ".bmp", ".doc", ".docx", ".ppt", ".pptx"
}

API_BASE = "https://mineru.net"
BATCH_URL = f"{API_BASE}/api/v4/file-urls/batch"
RESULT_URL = f"{API_BASE}/api/v4/extract-results/batch"

MAX_UPLOAD_WORKERS = 4
MAX_DOWNLOAD_WORKERS = 4
DOWNLOAD_RETRY = 3
INTER_BATCH_DELAY = 2

def load_config() -> dict:
    """读取 YAML 配置，若文件不存在则写入默认值后返回。"""

def save_config(cfg: dict) -> None:
    """将配置字典写入 YAML 文件。"""

def merge_cli_args(cfg: dict, args) -> dict:
    """用命令行参数覆盖 YAML 配置，返回最终生效配置字典。"""
```

---

## 十二、CLI 模式（cli.py）

`--cli` 模式提供与 TUI 完全等价的功能，通过 `rich` 输出和 `input()` 向导交互。
适合 SSH 远程连接、脚本调用、或不需要全屏 TUI 的场景。

---

### 阶段一：文件扫描与展示

启动后自动扫描 ROOT_DIR，以 `rich.Tree` 形式打印目录结构，每个文件附带序号、大小、⚠ 重复标记：

```
 MinerU Downloader  v1.1
══════════════════════════════════════════════════════
 扫描目录: D:\docs
 找到 7 个可解析文件（共 12.4 MB）
──────────────────────────────────────────────────────
 📁 docs/
 ├── 📁 papers/
 │   ├──  1  paper1.pdf                      3.2 MB
 │   └──  2  paper2.pdf                      4.1 MB
 ├── 📁 slides/
 │   ├──  3  ⚠ lecture.pptx  [已有 lecture.md]   1.4 MB
 │   └──  4  demo.pptx                       0.9 MB
 ├──  5  ⚠ report.docx     [已有 report.md]      0.8 MB
 ├──  6  scan1.jpg                           1.2 MB
 └──  7  scan2.png                           0.8 MB
──────────────────────────────────────────────────────
```

---

### 阶段二：文件选择

```
[选择文件]
输入要【排除】的序号（支持逗号和范围，如 "3,6-7"），直接回车保持全选:
> 
```

输入示例：
- 直接回车 → 全选 7 个文件
- `3,5` → 排除序号 3 和 5，选中剩余 5 个
- `6-7` → 排除序号 6~7，选中前 5 个
- `all` → 全不选（重新输入）

选择完毕后打印确认摘要：

```
 已选中 5 个文件（10.4 MB）：
   paper1.pdf、paper2.pdf、lecture.pptx ⚠、demo.pptx、report.docx ⚠
```

---

### 阶段三：重复文件处理

若已选中的文件中有 `⚠` 标记（`existing_md` 不为 None），进入此阶段：

**情况 A：仅有少量重复文件（≤5 个），逐一询问**

```
[重复文件处理]
以下文件已有对应的 .md 输出，请选择处理方式：

  [1/2] slides/lecture.pptx → 已有 slides/lecture.md
        (O) 覆盖  删除旧 .md，重新解析并写入
        (S) 跳过  不提交此文件，保留旧 .md
        (R) 改名  输出为 lecture_1.md（不影响旧 .md）
  选择 [O/S/R]: 
```

用户输入 `O`/`S`/`R` 后继续下一个，输入非法字符则重新提示。

**情况 B：重复文件较多（>5 个），先询问是否批量处理**

```
  有 8 个文件存在重复 .md。
  (A) 全部覆盖   (K) 全部跳过   (N) 全部改名   (M) 逐一决定
  选择 [A/K/N/M]: 
```

选择 `M` 则退回逐一询问流程。

---

### 阶段四：解析选项确认

展示当前生效配置，允许用户逐项修改：

```
[解析选项]
当前配置（来源：mineru_config.yaml）：

  语言        : ch
  模型        : vlm
  保留 ZIP    : 否
  保留 JSON   : 否
  代理模式    : system（使用系统代理）
  每批文件数  : 20
  Token       : sk-****...****（已配置）

是否修改任意选项？(y/N): 
```

若输入 `y`，进入逐项修改子向导：

```
  [1] 语言       当前: ch     → 输入新值（回车保持）: 
  [2] 保留 ZIP   当前: 否     → (y/N): 
  [3] 保留 JSON  当前: 否     → (y/N): 
  [4] 代理模式   当前: system → (system/custom/none): 
  [5] Token      当前: 已配置 → 输入新 Token（回车保持）: 

  是否将以上修改保存到 mineru_config.yaml？(y/N): 
```

---

### 阶段五：处理确认与开始

```
[确认开始]
 将提交 5 个文件进行解析（分 1 批，每批最多 20 个）
 预计消耗 Token 额度（取决于文档页数）

确认开始解析？(Y/n): 
```

输入 `n` 则退回阶段二（重新选择文件）。

---

### 阶段六：处理进度展示

使用 `rich.Progress` 显示多任务进度，分三层：

```
[处理进度]
──────────────────────────────────────────────────────
 总进度       ████████████░░░░░░░░  5/7 完成  已耗时 02:14
──────────────────────────────────────────────────────
 第 1 批 (5个文件)
   上传        ████████████████████  5/5  完成
   解析        ████████████████░░░░  4/5  解析中...
   下载        ████████████░░░░░░░░  3/5  下载中...
──────────────────────────────────────────────────────
 实时日志（最新 5 条）：
   [02:12] ✓ paper2.pdf 下载完成 → papers/paper2.md
   [02:11] ✓ paper1.pdf 下载完成 → papers/paper1.md
   [02:08] ↑ lecture.pptx 上传完成
   [02:07] ↑ demo.pptx 上传完成
   [02:05] ↑ paper1.pdf 上传完成
──────────────────────────────────────────────────────
```

每个文件的当前状态也可通过追加日志行体现：
- `↑ 上传中...` → `↑ 上传完成` → `⠿ 解析中 (3/10页)` → `↓ 下载中...` → `✓ 完成` / `✗ 失败: <原因>`

---

### 阶段七：完成汇总

```
[完成] 耗时 03:42，共处理 5 个文件：

  状态  文件                    输出
  ✓     papers/paper1.pdf    → papers/paper1.md
  ✓     papers/paper2.pdf    → papers/paper2.md
  ✓     slides/lecture.pptx  → slides/lecture_1.md（改名）
  ✓     slides/demo.pptx     → slides/demo.md
  ✗     report.docx          → 失败: 文件页数超出限制（200页）

  成功: 4  失败: 1  跳过: 0
  详细错误日志已追加到 mineru_errors.log

按回车退出: 
```

---

### 函数签名

```python
def run_cli(root_dir: Path, cfg: dict) -> None:
    """CLI 模式入口，接管后续所有阶段的交互。"""

def _phase_select(nodes: list, cfg: dict) -> list[FileNode]:
    """阶段二：展示文件树并收集用户选择，返回已选 FileNode 列表。"""

def _phase_duplicate(selected: list[FileNode], cfg: dict) -> list[FileNode]:
    """阶段三：处理重复文件决策，返回更新了 duplicate_action 的列表。"""

def _phase_options(cfg: dict) -> dict:
    """阶段四：展示并允许修改解析选项，返回最终生效配置。"""

def _phase_process(selected: list[FileNode], cfg: dict) -> None:
    """阶段五~七：提交、轮询、下载，rich Progress 展示，最终打印汇总。"""
```

---

## 十三、实现顺序（开发路线图）

1. **[Step 1]** `config.py` + `mineru_config.yaml` — YAML 配置加载/保存
2. **[Step 2]** `scanner.py` — 文件扫描、树结构、重复 .md 检测
3. **[Step 3]** `api.py` — MinerU API 封装（上传、轮询、代理）
4. **[Step 4]** `processor.py` — ZIP 下载与解压整理
5. **[Step 5]** `tui.py` — Textual TUI（选择阶段，含重复处理 UI）
6. **[Step 6]** `tui.py` — Textual TUI（处理进度阶段）
7. **[Step 7]** `cli.py` — 纯命令行交互模式
8. **[Step 8]** `main.py` — 入口整合（TUI / CLI 分发）
9. **[Step 9]** `requirements.txt` — 依赖清单
10. **[Step 10]** 安装依赖并测试

---

---

*计划版本：v1.1 | 日期：2026-05-01*

