# ai-workbench · Agnes 免费 AI 工作台

把 Agnes AI 免费接口做成一个**本地运行的网页工作台**：文生图、图生图·多图合成、视频生成、**长视频接力（最长 120 秒）**、图像理解、文本聊天，全部带任务队列、历史资产管理与提示词回看。纯本地部署，数据不出本机。

> 技术栈：Python + FastAPI 后端（单文件 `app.py`） + 原生 HTML/JS 前端（单文件 `index.html`） + ffmpeg（长视频拼接）。无 Node、无数据库、无外部依赖服务。

---

## 功能一览

| 模块 | 说明 |
|---|---|
| 文生图 | agnes-image-2.5-flash，档位 1K/2K/3K/4K，比例 8 种，单次 1~4 张 |
| 图生图 · 多图合成 | 上传/历史选图 1~5 张参考图，按指令改图或合成 |
| 视频生成 | agnes-video-2.5-flash，时长 4/5/6/8/10/12 秒，图生/文生；输出 720p 24fps |
| **长视频接力** | 分镜多段（每段 4/5/6/8/10/12s，最多 10 段=120 秒），**尾帧自动接力**保证镜头衔接 + ffmpeg xfade 交叉淡化拼接 |
| 图像理解 | agnes-2.5-flash 读图问答 |
| 聊天 | agnes-3.0-flash 文本对话 |
| 任务中心 | 耗时任务持久化队列（服务重启不丢），增量渲染不整页刷新，自动滚到最新 |
| 历史资产 | 左右分栏：左侧资产列表，右侧完整提示词（长视频逐段展示分镜） |

**视频任务容错策略**：上游「队列满 / 5xx / 限流」自动重试（间隔 2 分钟，最多 10 次，全失败才判失败）；参数/额度类错误直接失败不空转。参考图一律转 Base64 提交，不需要任何云空间。

---

## 目录结构

```
ai-workbench/
├── app.py                 # 后端：全部 API + 任务队列 worker（单文件）
├── index.html             # 前端：整个工作台界面（单文件）
├── config.json            # 接口配置（API key / base_url），部署时必填
├── requirements.txt       # Python 依赖（fastapi / uvicorn / httpx）
├── Dockerfile             # Docker 镜像（内置 ffmpeg）
├── docker-compose.yml     # 一键容器启动（挂载 config + data）
├── start.bat              # Windows 一键启动
├── data/
│   ├── output/            # 生成的图片
│   ├── videos/            # 生成的视频（含长视频成片 *_long.mp4）
│   ├── history.json       # 历史资产记录（含完整提示词）
│   └── tasks.json         # 任务队列持久化
└── tools/ffmpeg.../       # （可选）本地 ffmpeg 二进制；无则用系统 ffmpeg
```

---

## 配置：`config.json`

```json
{
  "image": {
    "base_url": "https://api.agnes-ai.cn",
    "api_key": "你的 API Key"
  }
}
```

- `api_key` 为必填；缺失或错误时页面会提示「配置异常」。
- **此文件含密钥**：不要提交到公开仓库；Docker 部署用挂载方式注入（见下）。

---

## 本地启动（Windows）

```bat
:: 方式一：一键脚本（已带 .venv 时）
start.bat

:: 方式二：手动
cd ai-workbench
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python app.py
```

启动后浏览器访问 **http://127.0.0.1:8010/**。
Linux / macOS 同理，把 `.venv\Scripts\` 换成 `.venv/bin/`。

---

## ffmpeg 安装（长视频接力必需）

长视频接力依赖 ffmpeg（抽尾帧 + 分段拼接）。后端 `_find_ffmpeg()` 会**优先使用系统 PATH 中的 ffmpeg，找不到再回退到 `tools/` 目录**。

| 平台 | 安装方式 |
|---|---|
| Windows | 方式 A：下载 [ffmpeg essentials build](https://www.gyan.dev/ffmpeg/builds/) 解压，把 `bin\ffmpeg.exe` 与 `ffprobe.exe` 放入 `tools\ffmpeg...\bin\`；方式 B：解压后把 `bin` 加入系统 PATH |
| macOS | `brew install ffmpeg` |
| Linux | `sudo apt install ffmpeg`（Docker 镜像已内置，无需手动装） |

验证：`ffmpeg -version` 能输出版本即 OK。缺 ffmpeg 时，普通图/视频生成不受影响，仅长视频接力的「拼接」步骤会失败并提示。

---

## Docker 启动

**前置**：准备好 `config.json`（含你的 API key）和 `data/` 目录（可留空，会自动创建）。

```bash
# 方式一：docker compose（推荐）
docker compose up -d --build
# 访问 http://127.0.0.1:8010/

# 方式二：纯 docker
docker build -t ai-workbench .
docker run -d --name ai-workbench -p 8010:8010 \
  -v "$(pwd)/config.json:/app/config.json:ro" \
  -v "$(pwd)/data:/app/data" \
  ai-workbench
```

要点：
- 镜像基于 `python:3.11-slim`，**已内置 ffmpeg**（`apt install ffmpeg`），长视频拼接直接可用。
- `config.json`（密钥）与 `data/`（资产/队列）通过**卷挂载**，容器重建不丢数据；`data/` 会被容器内的 python 进程写（非只读）。
- 日志：`docker logs -f ai-workbench`；停止：`docker compose down`（加 `-v` 会删卷，勿加）。

---

## 使用指南

### 长视频接力（重点功能）
1. 「视频生成」页 → 长视频接力面板。
2. 每行写一段动作描述（每行=一个镜头）；支持直接粘贴带「第N段（0-10秒）」标题的剧本——**标题行自动识别并忽略，秒数自动提取**（如 0-10 → 每段 10 秒）。
3. 可选：上传首段锚定图（1~5 张，锁定角色/场景）。
4. 提交后任务中心显示「第 X/Y 段 · 生成中」+ 当前段描述；全段完成后自动拼接出片。
5. 提示词里锚定「同一角色 + 同一场景」描述可进一步降低跨镜漂移。

### 任务中心
- 视频/长视频等耗时任务排队执行；**刷新页面、重启服务不丢任务**。
- 队列满自动重试（2 分钟×10 次）；参数/额度类错误直接失败。
- 列表上限 200 条，自动裁剪最旧的终态任务——重要资产的提示词请在「历史资产」页查看/备份。

### 历史资产
- 左侧资产列表（图片/视频/长视频），右侧显示完整提示词与参数；长视频逐段展示分镜。
- 点击删除单个资产；`data/history.json` 是全量备份文件。

---

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| 提示「素材必须是公网 URL 或 Base64」 | 参考图已自动转 Base64，正常不会出现；如出现请确认前端为最新版（Ctrl+F5） |
| 页面没显示新功能 | 浏览器缓存旧版，**Ctrl+F5 强刷** |
| 视频模糊 | 上游固定输出 720p 24fps，无分辨率参数；需要更高清可后期超分 |
| 长视频拼接失败「未找到 ffmpeg」 | 按上文安装 ffmpeg 到 PATH 或 tools/ 目录 |
| 任务一直排队不跑 | 上游队列满，属正常现象，最多自动重试 10 次（约 20 分钟）后失败 |

---

## 数据与备份

- 资产记录：`data/history.json`（含每个资产的提示词/分镜/参数）
- 任务队列：`data/tasks.json`
- 生成文件：`data/output/*.png`、`data/videos/*.mp4`
- 备份：直接复制整个 `data/` 目录即可。
