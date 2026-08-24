"""
config_settings_dialogs.py — 配置设置弹窗（原「⚙️ 配置」标签页，2026-08-22 迁入）
================================================================================
背景：配置标签页删除，内容迁入总览页「⚙️ 配置面板」下方的「⚙️ 其他设置」按钮。
- 弹窗两列（用户偏好：多字段弹窗拆两列且左右均衡，每 GroupBox 带明确标题+项数）：
  - 左列「进程与端口（5 项）」：监听 host/port（需重启）+ 控制 API host/port（热生效）
    + 数据目录 data_dir（需重启）
  - 右列「资产与调试（5 项）」：文件资产 3 路径（热生效+资源重载）
    + 调试 2 开关（save_batch_text / batch_endpoint 断点续跑）
- 数字框：无上下箭头 + 禁滚轮（用户全局偏好，widgets.no_wheel_spin + NoArrowSpinBox）
- 保存链路（与原配置页一致）：写 yaml → POST /config 热重载 → 报告热生效/需重启
  （需重启项弹确认，确认后走总览页 _restart）
- _collect 写 listen / control_api / assets / debug / paths 五段
  + bot 段 3 个行为开关（auto_approve_friend / echo_repeat / reply_to_quotes，
  08-23/08-24）；
  其余键（bot.qq/max_history 等、llm/comfyui/scheduler/msg）保持
  deepcopy 原值不动——其他页/弹窗管理的键不得被本弹窗覆盖（Pitfall 13）
"""

import os

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLineEdit, QCheckBox, QPushButton, QLabel, QMessageBox,
)

import api_client
from widgets import flash_button, no_wheel_spin, NoArrowSpinBox
from worker import Worker


class ConfigSettingsDialog(QDialog):
    """配置设置弹窗：config.yaml 五段（listen/control_api/assets/debug/paths）。"""

    def __init__(self, mw):
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle("⚙️ 其他设置（config.yaml）")
        self.setFixedWidth(800)
        self.setMinimumHeight(700)
        # yaml 副本：在 deepcopy 上改，取消不污染主窗口状态
        import copy
        self.yaml = copy.deepcopy(mw.yaml_cfg)
        self._build()
        self._load_values()

    # ------------------------------------------------------------
    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(10)

        cols = QHBoxLayout()
        cols.setSpacing(10)
        cols.addWidget(self._build_left())
        cols.addWidget(self._build_right())
        v.addLayout(cols, 1)

        # ---- 底部通栏：保存 + 打开 yaml + 结果提示 ----
        ops = QHBoxLayout()
        ops.setSpacing(8)
        self.btn_save = QPushButton("💾 保存并热加载")
        self.btn_save.setMinimumHeight(38)
        self.btn_save.setStyleSheet("font-weight: bold;")
        # 动态宽度（emoji 会让 sizeHint 算窄切字，Pitfall 12 配方）
        self.btn_save.setMinimumWidth(
            self.btn_save.fontMetrics().horizontalAdvance(self.btn_save.text()) + 44)
        self.btn_open_yaml = QPushButton("📝 打开 config.yaml")
        self.btn_open_yaml.setMinimumHeight(38)
        self.btn_open_yaml.setMinimumWidth(
            self.btn_open_yaml.fontMetrics().horizontalAdvance(self.btn_open_yaml.text()) + 44)
        self.lbl_result = QLabel("")
        self.lbl_result.setStyleSheet("font-size: 12px; color: #7f8c8d;")
        ops.addWidget(self.btn_save)
        ops.addWidget(self.btn_open_yaml)
        ops.addWidget(self.lbl_result, 1)
        v.addLayout(ops)

        self.btn_save.clicked.connect(self._save)
        self.btn_open_yaml.clicked.connect(self._open_yaml)

    def _build_left(self) -> QGroupBox:
        """左列：进程与端口（5 项）——监听 2 + 控制 API 2 + 数据目录 1
        + 机器人行为（3 项，08-23：好友申请自动通过 / 复读+1 全局开关；
        08-24：回复引用消息开关）。

        08-22：NapCat 守护 3 项原计划放本列，实测左列 4 组框 vs 右列 2 组框
        重心失衡（~250px），移至右列后两列 3 组框、内容高度差 ~20px。
        08-23：机器人行为 2 项加回左列（4 组框 7 项 vs 右列 3 组框 8 项，均衡）。
        08-24：回复引用消息入列（4 组框 8 项 vs 右列 3 组框 8 项，项数持平）。
        """
        gb = QGroupBox("进程与端口 / 机器人行为（8 项）")
        lay = QVBoxLayout(gb)
        lay.setSpacing(10)

        gb_listen = QGroupBox("监听（2 项 · 改动需重启 bot）")
        f = QFormLayout(gb_listen)
        self.ed_host = QLineEdit()
        self.ed_host.setPlaceholderText("bot WebSocket 监听地址（如 0.0.0.0）")
        # 数字框无上下箭头 + 禁滚轮（用户全局偏好）
        self.ed_port = no_wheel_spin(NoArrowSpinBox())
        self.ed_port.setRange(1, 65535)
        f.addRow("主机", self.ed_host)
        f.addRow("端口", self.ed_port)
        lay.addWidget(gb_listen)

        gb_ctrl = QGroupBox("控制 API（2 项 · GUI 通道，勿改 127.0.0.1）")
        f = QFormLayout(gb_ctrl)
        self.ed_ctrl_host = QLineEdit()
        self.ed_ctrl_host.setPlaceholderText("控制 API 地址（保持 127.0.0.1）")
        self.ed_ctrl_port = no_wheel_spin(NoArrowSpinBox())
        self.ed_ctrl_port.setRange(1, 65535)
        f.addRow("主机", self.ed_ctrl_host)
        f.addRow("端口", self.ed_ctrl_port)
        lay.addWidget(gb_ctrl)

        gb_paths = QGroupBox("数据目录（1 项 · 改动需重启）")
        f = QFormLayout(gb_paths)
        self.ed_data_dir = QLineEdit()
        self.ed_data_dir.setPlaceholderText("数据库存放目录（旧数据不迁移）")
        f.addRow("data_dir", self.ed_data_dir)
        lay.addWidget(gb_paths)

        # 机器人行为（08-23）：好友申请自动通过 + 复读+1 全局开关（保存即热生效）
        # 08-24：+ 回复引用消息开关（默认关，保持现状：群聊引用消息不触发 AI 聊天）
        gb_bot = QGroupBox("机器人行为（3 项 · 保存即热生效）")
        f = QFormLayout(gb_bot)
        self.cb_auto_approve_friend = QCheckBox()
        self.cb_auto_approve_friend.setChecked(True)
        self.cb_auto_approve_friend.setToolTip(
            "开（默认）=收到好友申请自动通过。\n"
            "关=不自动通过，申请保留等手动处理。")
        f.addRow("好友申请自动通过", self.cb_auto_approve_friend)
        self.cb_echo_repeat = QCheckBox()
        self.cb_echo_repeat.setChecked(True)
        self.cb_echo_repeat.setToolTip(
            "开（默认）=群内连续 3 条相同纯文本时 bot 跟一条（模拟 +1）。\n"
            "关=完全不触发复读跟发。")
        f.addRow("复读 +1", self.cb_echo_repeat)
        self.cb_reply_to_quotes = QCheckBox()
        self.cb_reply_to_quotes.setChecked(False)
        self.cb_reply_to_quotes.setToolTip(
            "QQ 手机端引用消息会自动附带 @bot，但用户引用 bot 消息\n"
            "一般不希望被回复 → 默认关=引用消息不触发 AI 聊天；\n"
            "开=引用消息与 @bot 消息同等待遇（会回复）。\n"
            "引用+命令/游戏指令（如引用消息里发 /投票）不受本开关影响。")
        f.addRow("回复引用消息", self.cb_reply_to_quotes)
        lay.addWidget(gb_bot)

        lay.addStretch(1)
        return gb

    def _build_right(self) -> QGroupBox:
        """右列：资产 / 调试 / NapCat 守护（8 项）——文件资产 3 + 调试 2 +
        NapCat 守护 3（08-22：探活/阈值/自动重启）。"""
        gb = QGroupBox("资产 / 调试 / NapCat 守护（8 项）")
        lay = QVBoxLayout(gb)
        lay.setSpacing(10)

        gb_assets = QGroupBox("文件型资产路径（3 项 · 留空=功能禁用）")
        f = QFormLayout(gb_assets)
        self.ed_pun_dir = QLineEdit()
        self.ed_pun_dir.setPlaceholderText("谐音梗题库目录（含 pinyin.txt、文字题库.csv）")
        self.ed_sensitive = QLineEdit()
        self.ed_sensitive.setPlaceholderText("敏感词表 .txt")
        self.ed_cosplay_db = QLineEdit()
        self.ed_cosplay_db.setPlaceholderText("外部图包搜索库 cosplay.db（SQLite 路径）")
        f.addRow("谐音梗题库目录", self.ed_pun_dir)
        f.addRow("敏感词表", self.ed_sensitive)
        f.addRow("cosplay 图库", self.ed_cosplay_db)
        lay.addWidget(gb_assets)

        gb_debug = QGroupBox("调试（2 项）")
        f = QFormLayout(gb_debug)
        self.cb_batch_text = QCheckBox()
        self.cb_batch_text.setToolTip("开启后联合更新 Map 批次把喂给 LLM 的原文一并存库（排查用）")
        f.addRow("批次保存喂给 LLM 的原文", self.cb_batch_text)
        # batch 端点开关（debug.batch_endpoint，默认开=保持现状）：
        # 开=联合更新 Map 批次结果写库、中断后断点续跑；关=不写库、每次全量处理
        self.cb_batch_endpoint = QCheckBox()
        self.cb_batch_endpoint.setChecked(True)
        self.cb_batch_endpoint.setToolTip(
            "开=联合更新 Map 批次结果记录进数据库，中断后下次在已记录批次基础上继续处理（断点续跑）。\n"
            "关=不记录批次结果，每次都按最后一次更新人设画像后的全部消息重新处理。")
        f.addRow("batch 端点（断点续跑）", self.cb_batch_endpoint)
        lay.addWidget(gb_debug)

        # NapCat 守护（08-22 半死态复盘）：HTTP 服务探活 + 告警 + 可选自动重启。
        # 热生效（watchdog 每 tick 读 CONFIG，/config 重载后立即生效，无需重启 bot）。
        # 放右列：左列 4 组框 vs 右列 2 组框重心失衡，移至本列后两列 3 组框、
        # 内容高度差 ~20px（08-22 截图实测）
        gb_watchdog = QGroupBox("NapCat 守护（3 项 · 保存即热生效）")
        f = QFormLayout(gb_watchdog)
        self.ed_wd_interval = no_wheel_spin(NoArrowSpinBox())
        self.ed_wd_interval.setRange(10, 3600)
        self.ed_wd_interval.setToolTip(
            "探活间隔（秒）：每 N 秒向 NapCat HTTP 服务 /get_login_info 探活一次。"
            "半死态（QQ 客户端活着但 HTTP 服务挂死）无任何告警，本守护是兜底。")
        f.addRow("探活间隔（秒）", self.ed_wd_interval)
        self.ed_wd_threshold = no_wheel_spin(NoArrowSpinBox())
        self.ed_wd_threshold.setRange(1, 50)
        self.ed_wd_threshold.setToolTip(
            "连续失败几次判定不健康：达到后 WARNING 告警；"
            "开了自动重启且处于冷却期外则触发 docker restart。")
        f.addRow("失败阈值（次）", self.ed_wd_threshold)
        self.cb_wd_auto = QCheckBox()
        self.cb_wd_auto.setChecked(False)
        self.cb_wd_auto.setToolTip(
            "开=NapCat HTTP 服务持续异常时自动 docker restart（30 分钟冷却防死循环）。\n"
            "关（默认）=只告警不动手——手动关闭 NapCat 时不会被自动拉起。")
        f.addRow("自动重启", self.cb_wd_auto)
        lay.addWidget(gb_watchdog)

        lay.addStretch(1)
        return gb

    # ------------------------------------------------------------
    def _load_values(self):
        y = self.yaml
        self.ed_host.setText(str(y.get("listen", {}).get("host", "0.0.0.0")))
        self.ed_port.setValue(int(y.get("listen", {}).get("port", 8696)))
        self.ed_ctrl_host.setText(str(y.get("control_api", {}).get("host", "127.0.0.1")))
        self.ed_ctrl_port.setValue(int(y.get("control_api", {}).get("port", 8697)))
        # bot.qq 不从 GUI 管理（08-22：运行时身份由 NapCat 连接派生，yaml 值仅兜底）
        assets = y.get("assets", {})
        self.ed_pun_dir.setText(str(assets.get("pun_dir", "")))
        self.ed_sensitive.setText(str(assets.get("sensitive_words", "")))
        self.ed_cosplay_db.setText(str(assets.get("cosplay_db", "")))
        self.cb_batch_text.setChecked(bool(y.get("debug", {}).get("save_batch_text", False)))
        # batch 端点（断点续跑）：yaml 无键时默认开（保持现状）
        self.cb_batch_endpoint.setChecked(
            bool(y.get("debug", {}).get("batch_endpoint", True)))
        self.ed_data_dir.setText(str(y.get("paths", {}).get("data_dir", "data")))
        # 机器人行为（08-23）：yaml 无键时默认开（保持现状）
        bt = y.get("bot", {})
        self.cb_auto_approve_friend.setChecked(
            bool(bt.get("auto_approve_friend", True)))
        self.cb_echo_repeat.setChecked(
            bool(bt.get("echo_repeat", True)))
        # 回复引用消息（08-24）：yaml 无键时默认关（保持现状：引用不触发 AI 聊天）
        self.cb_reply_to_quotes.setChecked(
            bool(bt.get("reply_to_quotes", False)))
        # NapCat 守护（08-22）：yaml 无键时回 DEFAULTS（60/3/关）
        nc = y.get("napcat", {})
        self.ed_wd_interval.setValue(int(nc.get("watchdog_interval", 60)))
        self.ed_wd_threshold.setValue(int(nc.get("watchdog_threshold", 3)))
        self.cb_wd_auto.setChecked(bool(nc.get("watchdog_auto_restart", False)))

    def _collect(self) -> tuple[dict, dict]:
        """表单 → (yaml dict, env dict)。只写五段，其余键保持原值（deepcopy）。"""
        y = self.yaml
        y.setdefault("listen", {})["host"] = self.ed_host.text().strip()
        y["listen"]["port"] = self.ed_port.value()
        y.setdefault("control_api", {})["host"] = self.ed_ctrl_host.text().strip()
        y["control_api"]["port"] = self.ed_ctrl_port.value()
        # bot.qq / max_history / cooldown_seconds 等 bot 段键：本弹窗不动
        y.setdefault("assets", {})["pun_dir"] = self.ed_pun_dir.text().strip()
        y["assets"]["sensitive_words"] = self.ed_sensitive.text().strip()
        y["assets"]["cosplay_db"] = self.ed_cosplay_db.text().strip()
        # llm / comfyui / scheduler / msg 段：其他页/弹窗管理，本弹窗不动
        y.setdefault("debug", {})["save_batch_text"] = self.cb_batch_text.isChecked()
        y["debug"]["batch_endpoint"] = self.cb_batch_endpoint.isChecked()
        y.setdefault("paths", {})["data_dir"] = self.ed_data_dir.text().strip() or "data"
        # 08-23：管理 bot 段 2 个行为开关（GUI 其他设置弹窗，保存即热生效）
        bt = y.setdefault("bot", {})
        bt["auto_approve_friend"] = self.cb_auto_approve_friend.isChecked()
        bt["echo_repeat"] = self.cb_echo_repeat.isChecked()
        bt["reply_to_quotes"] = self.cb_reply_to_quotes.isChecked()
        # NapCat 守护（08-22 半死态复盘）：写 napcat 段 3 键（热生效）。
        # 其余 napcat 键（mode/container/ws_token 等）保持 deepcopy 原值不动
        nc = y.setdefault("napcat", {})
        nc["watchdog_interval"] = int(self.ed_wd_interval.value())
        nc["watchdog_threshold"] = int(self.ed_wd_threshold.value())
        nc["watchdog_auto_restart"] = bool(self.cb_wd_auto.isChecked())
        return y, {}

    # ------------------------------------------------------------
    def _save(self):
        y, _env = self._collect()
        if not y["listen"]["host"]:
            QMessageBox.warning(self, "配置错误", "listen.host 不能为空")
            return

        # 写 yaml（.env 密钥由总览页 LLM 板块管理，本弹窗不动）
        api_client.save_yaml(y)
        self.mw.yaml_cfg = y
        self._refresh_mw_cfg()

        def _do():
            return api_client.reload_config(self.mw.cfg)

        def _ok(report):
            flash_button(self.btn_save)
            applied = report.get("applied", {})
            restart = report.get("restart_required", [])
            errors = report.get("errors", [])
            parts = []
            if errors:
                parts.append("❌ " + "; ".join(errors))
            if applied:
                keys = ", ".join(list(applied.keys())[:8]) + ("…" if len(applied) > 8 else "")
                parts.append(f"✅ 热生效 {len(applied)} 项: {keys}")
            if restart:
                parts.append(f"🔁 需重启: {', '.join(restart)}")
            if not applied and not restart and not errors:
                parts.append("✅ 已保存（无变更）")
            self.lbl_result.setText(" | ".join(parts))
            # 资产路径变了 → 顺带重载资源
            asset_keys = {"ASSET_PUN_DIR", "ASSET_SENSITIVE_WORDS", "ASSET_COSPLAY_DB"}
            if asset_keys & set(applied.keys()):
                w2 = Worker(api_client.reload_resources, self.mw.cfg, "all")
                w2.finished_ok.connect(lambda r: self.lbl_result.setText(
                    self.lbl_result.text() + f" | 资源已重载: {r.get('result', {})}"))
                w2.start()
                self.mw._workers.append(w2)
            if restart:
                if self.mw.confirm("需要重启", f"以下配置需重启 bot 生效:\n{', '.join(restart)}\n现在重启？"):
                    self.mw.tab_overview._restart()

        def _err(msg):
            self.lbl_result.setText(
                f"⚠️ 已保存到磁盘，但 bot 未运行或未响应（{msg}）——启动后生效")

        w = Worker(_do)
        w.finished_ok.connect(_ok)
        w.finished_err.connect(_err)
        w.start()
        self.mw._track(w)

    def _refresh_mw_cfg(self):
        from core.config import flatten_yaml_tree
        new_cfg = flatten_yaml_tree(self.yaml)
        # 密钥以现有 env_cfg 为准（本弹窗不管理密钥）
        env = getattr(self.mw, "env_cfg", None) or {}
        new_cfg["REMOTE_API_KEY"] = env.get("REMOTE_API_KEY", env.get("DEEPSEEK_API_KEY", ""))
        new_cfg["LLM_API_KEY"] = env.get("LLM_API_KEY", "")
        self.mw.cfg.clear()
        self.mw.cfg.update(new_cfg)

    def _open_yaml(self):
        import subprocess
        import sys
        path = os.path.join(api_client.PROJECT_ROOT, "config.yaml")
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            QMessageBox.warning(self, "打开失败", str(e))
