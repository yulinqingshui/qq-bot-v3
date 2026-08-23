# QQ Bot v3（GUI 版）

基于 OneBot 11 + NapCat 的 QQ 群机器人，附 PySide6 GUI 控制台。
GUI 控制台管理全部功能，Linux/Windows 双平台部署（当前在 Ubuntu 开发验证）。

## 架构

```
┌─────────────────────────────────────────────┐
│  GUI（PySide6，独立进程）                    │
│  gui_launcher.py → gui/main_window.py        │
│  8 标签页 + 状态灯 + 日志面板 + 确认框        │
└──────┬──────────────────────────────┬───────┘
       │ subprocess 启停              │ 2s 轮询 / 控制 API
       ▼                             ▼
┌─────────────────────────────────────────────┐
│  bot 子进程（core/bot.py，asyncio）          │
│  ├─ WebSocket 服务 :8696（NapCat 反向连入）  │
│  ├─ 控制 API :8697（仅 127.0.0.1，GUI 专用） │
│  └─ NapCat 集成层 napcat_manager.py          │
│     ├─ Linux:  管理 docker 容器（现状）      │
│     └─ Windows: 拉起内置绿色版子进程（自动   │
│        下载 111MB 自包含包 + 注入 WS 配置）   │
└─────────────────────────────────────────────┘
```

- **数据流**：GUI 改配置 → 写 `config.yaml`/`.env` → `POST /config` 通知 bot 热重载 →
  bot 原地更新内存 CONFIG（引用共享，天然生效）→ 返回 `{applied, restart_required}`
- **DB 直读**：消息/人设/群组等数据面板由 GUI 直接读写 SQLite（WAL 并发安全）
- **状态**：bot 写 `data/napcat_status.txt` + 控制 API `/status`，GUI 轮询驱动状态灯
- **NapCat 集成**：bot 启动时 `napcat_manager.ensure_running()` 确保协议端在跑
  （Linux=docker 容器 / Windows=内置绿色版自动下载+拉起 / off=外部自管）。
  GUI 总览页「NapCat 登录」卡片经 `GET /napcat`、`POST /napcat/restart` 统一管理
  二维码与登录态，两种平台同一套接口。

## 目录

```
qq-bot-v3/
├── core/            # 机器人核心（fork 自 qq-bot，含 v2 改造）
│   ├── bot.py       # 入口：WS 服务 + 控制 API + 跨平台信号 + 日志落盘
│   ├── config.py    # 配置外部化：config.yaml + .env → CONFIG 活对象 + 热加载
│   ├── control_api.py  # 内嵌控制 API（aiohttp，127.0.0.1）
│   ├── napcat_manager.py  # NapCat 平台抽象层（docker / win 绿色版 / off）
│   ├── archive.py   # 存档三开关（save_images / save_recall_messages / save_recall_images）
│   ├── llm.py       # LLM 后端按 llm.backend 显式选择（deepseek/local，热切换）
│   └── ...
├── games/           # 游戏模块（10 个，路径全部配置化）
├── gui/             # GUI（PySide6）
│   ├── main_window.py   # 主窗口 + 状态轮询 + 确认框
│   ├── process_manager.py  # bot 子进程生命周期
│   ├── api_client.py    # 控制 API 客户端 + SQLite 直读
│   ├── tab_overview.py  # 总览（状态灯/启停/运行信息）
│   ├── tab_config.py    # 配置（YAML 表单化 + 密钥 + 连接测试）
│   ├── tab_messages.py  # 消息管理（历史/黑名单/管理员/会话清除）
│   ├── tab_personas.py  # 人设画像（用户人设/画像/好感度）
│   ├── tab_groups.py    # 群组集群（集群 + 群级开关）
│   ├── tab_games.py     # 游戏（题库状态/重载/模仿黑名单）
│   ├── tab_imagegen.py  # 画图（ComfyUI 连接/生成历史）
│   └── tab_logs.py      # 日志（tail + 过滤 + 搜索）
├── gui_launcher.py  # GUI 入口（--start 自动拉起 bot）
├── config.yaml      # 全部运行配置
├── .env.example   # 密钥模板（cp .env.example .env 后填写）
├── requirements.txt
└── data/            # 运行数据（自动创建：数据库 + 存档 + 日志）
```

## 快速开始（Ubuntu 桌面）

```bash
# 0) 系统依赖（仅首次）
sudo apt install -y python3-venv libgl1 libegl1 libxcb-cursor0 \
                    libxkbcommon0 libdbus-1-3 libfontconfig1 libglib2.0-0

# 1) 一键启动（自动创建 .venv 并安装依赖，首次约 1-3 分钟）
./start.sh

# 2) （可选）桌面/应用菜单快捷方式
./start.sh install     # 应用菜单/桌面出现「QQ Bot」图标，双击即启
./start.sh remove      # 移除快捷方式
./start.sh status      # 查看 bot / GUI 运行状态
./start.sh stop        # 停止 bot（GUI 可保留）

# 3) 首次运行：在 GUI 总览页点「启动 bot」，NapCat 连入后状态灯变绿
#    （NapCat 需先扫码登录 QQ——Linux docker 自动部署 / Windows 内置绿色版自动下载）

# 填配置（可选，GUI 内「配置」页也可改）:
#    config.yaml: llm.backend / llm.remote_api / llm.remote_model /
#                 assets.* 资产路径 / comfyui.url
#    .env:        cp .env.example .env 后填 REMOTE_API_KEY
#    （无 LLM 也可跑：llm.enabled: false，游戏/存档/查询类功能不依赖 LLM）
```

## 配置热生效规则

| 改动 | 生效方式 |
|------|---------|
| LLM 后端/模型/key/并发、ComfyUI 地址、存档三开关、资产路径、冷却、上下文条数 | **热生效**（GUI 保存即通知 bot 重载） |
| 资产路径变更 | 热生效 + 自动重载题库/敏感词 |
| `listen.host` / `listen.port` | **需重启 bot**（GUI 弹确认）；`bot.qq` 留空时由 NapCat 登录号自动回写 |
| 数据面板改动（人设/黑名单/管理员/好感度/群开关） | 直接写 SQLite，bot 实时读取 |

## 文件型资产（不随程序分发）

| 资产 | 配置项 | 不配置的影响 |
|------|--------|------------|
| 谐音梗题库目录（pinyin.txt + 文字题库.csv + 图片题库/） | `assets.pun_dir` | 谐音梗游戏无题 |
| 敏感词表 | `assets.sensitive_words` | 用内置词库兜底 |
| cosplay.db（外部图包搜索库） | `assets.cosplay_db` | cosplay 搜索/猜老婆不可用 |
| 题库（真心话/大冒险/卧底/海龟汤） | 固定 `data/question_bank/`（仓库自带，可改） | 空库时 LLM 自动补题（真心话大冒险）；卧底/海龟汤无题 |

## Windows 部署（集成层已完成，待真机验证）

**NapCat 已集成进 bot 程序**（`core/napcat_manager.py` 平台抽象层）：
- `napcat.mode: auto`（默认）→ Linux 用 docker / Windows 用内置绿色版，**两种平台都是全自动**
- **Linux（含新机器）自动部署**：容器不存在时 bot 自动 pull 镜像（1.4G）→
  建数据目录（程序目录内）→ 注入 onebot11 配置（WS 连宿主机网关 + token）→
  起容器（参数与生产 compose 对齐，端口可配 `napcat.docker_host_ports`）。
  已实测：新容器从零部署到出二维码 6 秒
- **Windows 内置绿色版**：官方 `NapCat.Shell.Windows.Node.zip`（111MB 自包含：
  node.exe + QQ 运行时 + NapCat，**无需另装 QQ/Node**），自动下载（缓存
  `data/napcat_win/`）→ 拉起子进程（`node.exe ./index.js`）
- 两平台数据/扫码登录态（passkey.json）都落程序目录 → **一次扫码，重启免扫**
- 自动注入 onebot11 配置（WS 连 bot 8696 + token，与生产配置同结构）
- GUI 总览页二维码卡片/刷新按钮 三平台同一套接口

**待真机验证项**（需一台 Windows 机器实测）：
- `NAPCAT_WORKDIR` 是否被 v4.18 尊重为数据根（逆向确认 env 存在，需实测落点）
- QR 码实际路径（代码已做数据目录递归扫描兜底）
- PyInstaller `--onedir` 打包 `gui_launcher.py`（第三方依赖均纯 Python，无 C 扩展障碍）
- 首次运行引导：建库 → GUI 扫码 → 启动
