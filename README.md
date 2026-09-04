# Bilibili Favorites Classifier  B站收藏夹AI分类整理工具

[![Python](https://img.shields.io/badge/Python-3.7+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

一个使用 AI 智能分析 Bilibili 收藏夹内容，并自动将视频移动到指定收藏夹的自动化工具。

---

## ✨ 功能特性

- **🤖 智能分类**：利用大语言模型（如 OpenAI GPT 系列、Gemini 等）分析视频的标题和简介，从你指定的收藏夹列表中，为视频匹配最合适的分类。
- **📂 全自动整理**：根据 AI 的分类结果，自动将视频从源收藏夹移动到目标收藏夹，整个过程在你的 B站账号内完成，无需本地下载。
- **⚡ 流水线处理**：AI 分类可以并行准备结果，Bilibili 移动保持单通道限速，减少等待。
- **💾 可恢复进度**：每批分类和每次移动都会写入本地 CSV，临时失败后可以继续处理未完成项目。
- **✅ 实际核实**：移动后回读来源和目标收藏夹，连续两次结果一致才记录为已确认。
- **🔒 安全登录**：支持通过扫描二维码登录B站，无需手动填写和暴露复杂的 Cookie，安全又便捷。
- **🎮 交互式体验**：通过美观、直观的命令行界面 (CLI) 与用户交互，每一步操作都有清晰的指引。
- **⚙️ 灵活配置**：通过简单的配置文件 (`.env` 和 `ai_config.json`) 管理个人凭证和 AI 服务，易于修改和维护。

## 🚀 快速开始

### 1. 环境准备

- 确保你的电脑已经安装了 Python 3.7 或更高版本。
- 准备好你的 Bilibili 账号和一个能够提供大语言模型服务的 API Key。

### 2. 克隆与安装

首先，将项目克隆到你的本地：
```bash
git clone https://github.com/atri1011/Bilibili-Favorites-Classifier.git
cd Bilibili-Favorites-Classifier
```

然后，创建虚拟环境并安装所有必需的依赖库：
```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 3. 配置

在第一次运行前，你需要创建两个配置文件。项目已经为你准备好了模板，你只需要复制并修改即可。

1.  **创建B站配置文件**：
    复制 `.env.example` 并重命名为 `.env`。暂时将 `BILIBILI_COOKIE` 的值留空，程序会在首次运行时引导你通过扫码登录来自动填充。

2.  **创建AI服务配置文件**：
    复制 `ai_config.json.example` 并重命名为 `ai_config.json`。然后，填入你的 AI 服务信息：
    ```json
    {
      "openai_api_key": "YOUR_OPENAI_API_KEY",
      "openai_base_url": "YOUR_OPENAI_BASE_URL",
      "model_name": "YOUR_MODEL_NAME"
    }
    ```

### 4. 运行程序

一切准备就绪！现在，运行主程序：
```bash
.venv\Scripts\python.exe main.py
```

## 📖 使用流程

程序启动后，会引导你完成以下步骤：

1.  **登录B站**：如果这是你第一次运行，程序会提示你通过扫描命令行中出现的二维码来登录你的B站账号。
2.  **选择源收藏夹**：程序会列出你所有的收藏夹，你需要输入一个序号，选择你想要整理的那个收藏夹。
3.  **选择目标收藏夹**：接下来，你需要再次输入一个或多个收藏夹的序号（用英文逗号隔开），告诉 AI 只能在这些你指定的收藏夹里做选择。
4.  **流水线处理**：AI 按较大批次连续分类并立即记录 CSV；已完成的分类结果进入移动队列。
5.  **自动重试与核实**：Bilibili API 始终顺序调用并带节流；网络临时错误会有限重试，达到核实批次大小后回读实际归属。412、429、鉴权等全局错误会保存进度并安全停止。
6.  **查看结果**：终端显示汇总，详细状态保存在项目目录下的 `bilibili_favorites_progress_*.csv`。

AI 无法可靠分类的视频会留在原收藏夹，不会被强行移动。

## 进度文件与本地清理

CSV 是机器生成的进度记录，不要手动修改分类来驱动程序。重新运行同一来源和目标组合时，已确认的项目不会重复提交，失败或未确认项目可以继续处理。

如果项目目录中只有一个能匹配当前账号收藏夹 ID 的进度 CSV，重新启动时会自动识别来源和目标收藏夹并继续；存在多个任务时才会要求重新选择。

默认流水线参数为：AI 每批 50 个视频、最多 4 个 AI 请求并发、每累计 50 个需要移动的视频进行一次双回读核实。可以在 `.env` 中通过 `AI_BATCH_SIZE`、`AI_CONCURRENCY` 和 `VERIFY_BATCH_SIZE` 调整。

`.env`、`ai_config.json`、虚拟环境、本地缓存和 CSV 都被 `.gitignore` 忽略，不应提交到 GitHub。整理完成后可由用户手动删除本地配置和进度文件。

也可以使用显式的清理命令。程序会先列出文件并要求确认：

```bash
.venv\Scripts\python.exe main.py --cleanup
```

---

希望这份文档能帮助你更好地使用和分享这个项目！( ´ ▽ ` )ﾉ
