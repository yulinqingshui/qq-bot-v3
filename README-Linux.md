# QQ Bot v3 — Ubuntu 版使用说明

## 这是什么

QQ 群 AI 机器人 + 图形控制台，**完全免安装、免装任何运行时**。
内置了 Python、NapCat、QQ 客户端、Xvfb、ffmpeg、中文字体——
解压后 `./start.sh` 直接跑，不需要装 Python / Docker / Node / pip。

**系统要求**：Ubuntu 24.04 或更新的桌面版（x86-64）。
桌面环境自带的 X11 库（libX11/libxcb 等）即可，无需额外安装。

## 快速开始

```bash
# 1. 解压（路径不要有中文/空格）
unzip qq-bot-v3-ubuntu-x64.zip -d ~/qq-bot
cd ~/qq-bot/qq-bot-v3-ubuntu-x64

# 2. 填 API 密钥（首次必须，见下）
nano .env

# 3. 启动
./start.sh
```

`start.sh` 会打开图形控制台。**首次运行**：总览页点「启动 bot」
→ NapCat 显示**扫码二维码** → 用手机 QQ 扫码登录机器人账号。
登录态存 `data/napcat_linux/home/`，以后重启不用再扫。

> QQ 客户端和 NapCat 已内置在 `data/napcat_linux/`，**无需联网下载**，
> 解压即可用。

## 配置

**`.env`**（与 `config.yaml` 同目录，首次需创建）：
```bash
# 远程 LLM（必填，二选一）
REMOTE_API_KEY=你的API密钥

# 本地 ComfyUI 画图（可选，不用画图可留空）
COMFYUI_URL=http://你的机器IP:8188
```

**`config.yaml`**：`llm.remote_api`（如 `https://api.deepseek.com/v1`）、
`llm.remote_model`（如 `deepseek-chat`）等。其余参数都有默认值，不懂可不改。
改完在 GUI 点「重载配置」或重启生效。

## 目录结构

```
qq-bot-v3-ubuntu-x64/
├── start.sh               ← 启动入口（双击或终端跑）
├── stop.sh                ← 停止（停 bot + 关界面）
├── launcher.py            ← GUI 入口（start.sh 调用）
├── gui_launcher.py        ← GUI 主程序
├── config.yaml            ← 配置文件（文本编辑器打开改）
├── .env                   ← API 密钥（首次创建）
├── requirements.txt       ← Python 依赖清单（已装，仅供参考）
├── core/  gui/  utils/  scripts/   ← 程序代码
├── python/                ← 内置 Python 运行时（不要动）
├── libs/                  ← GUI 系统库兜底（不要动）
└── data/
    ├── question_bank/     题库（真心话大冒险）
    └── napcat_linux/      内置 QQ 客户端 + NapCat + Xvfb + ffmpeg
        ├── QQ/            QQ Linux 客户端（已注入 NapCat hook）
        ├── napcat/        NapCat.Shell（Node 运行时）
        ├── bin/           Xvfb + ffmpeg + ffprobe
        ├── libs/          QQ/Xvfb 系统库闭包
        ├── fonts/         中文字体
        └── home/          QQ 登录态（扫码后生成）
```

## 工作原理（技术说明）

- **Linux 绿色版 NapCat**：与 Windows 发行版一致的自包含方案。
  QQ Linux 客户端（Electron）已预注入 NapCat hook，进程内直接加载
  `napcat.mjs`，无需独立 Node 服务；Xvfb 提供虚拟显示（内置，`DISPLAY=:97`），
  登录态与数据全落包内 `data/napcat_linux/home/`。
- **双进程隔离**：
  - QQ/NapCat 进程：用 `data/napcat_linux/libs/`（22.04 自洽闭包）
  - GUI 进程：用 `libs/`（24.04 闭包）+ PySide6 6.11
  - 两套库互不干扰，无版本冲突。
- **GUI 语音播放**：PySide6 6.11 自带 ffmpeg 媒体插件（`libffmpegmediaplugin.so`
  + `libav*.so.61` 自包含，FFmpeg 7.1.5），不依赖系统 gstreamer。
- **Python**：内置 python-build-standalone CPython 3.14.7（stripped），
  `LD_LIBRARY_PATH` 由 start.sh 设好，RPATH 自动解析子目录库。
- bot 主进程 = `python/bin/python3 gui_launcher.py --start`
  （GUI 内嵌控制 API 8697，bot WS 监听 8696）。

## 常见问题

**Q: `./start.sh` 报错「glibc 版本太低」？**
确认是 Ubuntu 24.04+（`lsb_release -a`）。22.04 的 glibc（2.35）低于
PySide6 要求的 2.34 之上的传递依赖，请用 24.04。

**Q: GUI 起不来 / 黑屏？**
确认是桌面版（有 X11）。纯 Server 版无桌面环境，GUI 起不来。
可先跑 `./python/bin/python3 -c "from PySide6.QtWidgets import QApplication"`
验证 PySide6 能加载。

**Q: 扫码后没反应 / WebUI 打不开？**
看 `data/napcat_linux/home/napcat_linux.log`（运行日志）。
常见：端口 6099 被占（`ss -ltnp | grep 6099`），杀掉占用进程重启。

**Q: 能装在移动硬盘/任意目录吗？**
可以（绿色免安装）。登录态跟着 `data/` 走，换位置不受影响。
路径尽量短、无中文/空格。

**Q: 想彻底卸载？**
① `./stop.sh` 停止 → ② 删整个文件夹。聊天记录/登录态都在 `data/`，
删了就没有；想保留先备份 `data/`。

## 体积说明

整包解压后约 1.3G（zip 约 622M），其中 QQ Linux 客户端 562M（内置的必然代价）、
Python 运行时 491M（含 PySide6 GUI 框架 295M）。
比 Windows 版大是因为 Linux 版连 QQ 客户端都打包了（Windows 版
复用系统已装的 QQ，只内置 NapCat）。
