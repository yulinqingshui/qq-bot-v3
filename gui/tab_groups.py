"""
tab_groups.py — 群组集群页
=========================
- 集群列表：group_clusters + 成员
- 群级开关：group_cluster_members 的 enable_* 列（GUI 勾选 → 写库，bot 定时读取）
- 集群管理：把群加入/移出集群、新建/删除集群
- 管理员 & 黑名单：08-20 自消息管理页移入（admin_users / user_blocklist）
"""

import sqlite3
import time
import uuid

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QHeaderView,
    QLabel, QGroupBox, QCheckBox, QMessageBox, QGridLayout,
)

import api_client
from worker import Worker


def _ts(t) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(t)))
    except Exception:
        return str(t)


# 群级定时任务开关（与 schema 列对应）
SWITCHES = [
    ("enable_persona_update", "人设更新"),
    ("enable_profile_update", "画像更新"),
    ("enable_question_refill", "题库补充"),
    ("enable_evaluation", "评选报告"),
    ("enable_summary", "总结报告"),
    ("enable_member_notify", "进退群通知"),
    ("enable_mimic", "赛博模仿"),
]


class TabGroups(QWidget):
    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self._build()
        self._load()

    def _build(self):
        v = QVBoxLayout(self)

        # ---------------- 集群列表 ----------------
        gb_clusters = QGroupBox("集群（视为同一个群的多群组）")
        box = QVBoxLayout(gb_clusters)
        row = QHBoxLayout()
        self.btn_cluster_refresh = QPushButton("🔄 刷新")
        self.ed_new_cluster = QLineEdit()
        self.ed_new_cluster.setPlaceholderText("新建集群：填主群群号")
        self.ed_new_cluster.setFixedWidth(180)
        self.btn_new_cluster = QPushButton("➕ 新建集群")
        self.ed_cluster_pick = QLineEdit()
        self.ed_cluster_pick.setPlaceholderText("选中下方集群后，用下面的开关管理成员群")
        self.btn_del_cluster = QPushButton("🗑 删除集群")
        row.addWidget(self.btn_cluster_refresh)
        row.addWidget(self.ed_new_cluster)
        row.addWidget(self.btn_new_cluster)
        row.addWidget(self.ed_cluster_pick)
        row.addWidget(self.btn_del_cluster)
        box.addLayout(row)
        self.tbl_cluster = QTableWidget(0, 3)
        self.tbl_cluster.setHorizontalHeaderLabels(["集群ID", "主群", "创建时间"])
        self.tbl_cluster.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tbl_cluster.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_cluster.setFixedHeight(110)
        self.tbl_cluster.itemSelectionChanged.connect(self._on_cluster_select)
        box.addWidget(self.tbl_cluster)
        v.addWidget(gb_clusters)

        # ---------------- 集群成员 + 群级开关 ----------------
        gb_members = QGroupBox("集群成员群 & 群级开关")
        box = QVBoxLayout(gb_members)
        self.tbl_member = QTableWidget(0, 9)
        headers = ["群号"] + [name for _, name in SWITCHES] + [""]
        self.tbl_member.setHorizontalHeaderLabels(headers)
        self.tbl_member.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tbl_member.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_member.setFixedHeight(110)
        box.addWidget(self.tbl_member)
        row = QHBoxLayout()
        self.btn_apply_switch = QPushButton("💾 应用勾选")
        self.ed_add_group = QLineEdit()
        self.ed_add_group.setPlaceholderText("新群号")
        self.ed_add_group.setFixedWidth(150)
        self.btn_add_group = QPushButton("➕ 加入集群")
        self.btn_rm_group = QPushButton("➖ 移出集群")
        row.addWidget(self.btn_apply_switch)
        row.addWidget(self.ed_add_group)
        row.addWidget(self.btn_add_group)
        row.addWidget(self.btn_rm_group)
        row.addWidget(QLabel("勾选/取消上表开关后点「应用勾选」生效；加入/移出集群需该群已在 bot 存档中"))
        row.addStretch(1)
        box.addLayout(row)
        v.addWidget(gb_members)

        # ---------------- 管理员 & 黑名单（自消息管理页移入，08-20） ----------------
        gb_users = QGroupBox("管理员 & 黑名单")
        grid = QGridLayout(gb_users)

        # 左列：管理员
        self.ed_admin_uid = QLineEdit()
        self.ed_admin_uid.setPlaceholderText("用户QQ")
        self.ed_admin_nick = QLineEdit()
        self.ed_admin_nick.setPlaceholderText("昵称（可选）")
        self.btn_add_admin = QPushButton("➕ 添加")
        self.btn_rm_admin = QPushButton("➖ 移除选中")
        self.btn_reload_admin = QPushButton("🔄 刷新")
        admin_row = QHBoxLayout()
        admin_row.addWidget(self.ed_admin_uid)
        admin_row.addWidget(self.ed_admin_nick)
        admin_row.addWidget(self.btn_add_admin)
        admin_row.addWidget(self.btn_rm_admin)
        admin_row.addWidget(self.btn_reload_admin)
        self.tbl_admin = QTableWidget(0, 3)
        self.tbl_admin.setHorizontalHeaderLabels(["用户QQ", "昵称", "添加时间"])
        self.tbl_admin.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tbl_admin.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_admin.setFixedHeight(90)
        grid.addWidget(QLabel("<b>管理员</b>"), 0, 0)
        grid.addLayout(admin_row, 1, 0)
        grid.addWidget(self.tbl_admin, 2, 0)

        # 右列：黑名单
        self.ed_block_uid = QLineEdit()
        self.ed_block_uid.setPlaceholderText("用户QQ")
        self.ed_block_nick = QLineEdit()
        self.ed_block_nick.setPlaceholderText("昵称（可选）")
        self.btn_block = QPushButton("➕ 拉黑")
        self.btn_unblock = QPushButton("➖ 取消拉黑")
        self.btn_reload_block = QPushButton("🔄 刷新")
        block_row = QHBoxLayout()
        block_row.addWidget(self.ed_block_uid)
        block_row.addWidget(self.ed_block_nick)
        block_row.addWidget(self.btn_block)
        block_row.addWidget(self.btn_unblock)
        block_row.addWidget(self.btn_reload_block)
        self.tbl_block = QTableWidget(0, 4)
        self.tbl_block.setHorizontalHeaderLabels(["用户QQ", "昵称", "拉黑时间", "操作者"])
        self.tbl_block.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tbl_block.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_block.setFixedHeight(90)
        grid.addWidget(QLabel("<b>黑名单</b>"), 0, 1)
        grid.addLayout(block_row, 1, 1)
        grid.addWidget(self.tbl_block, 2, 1)

        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        v.addWidget(gb_users)

        self.btn_cluster_refresh.clicked.connect(self._load)
        self.btn_new_cluster.clicked.connect(self._create_cluster)
        self.btn_del_cluster.clicked.connect(self._delete_cluster)
        self.btn_apply_switch.clicked.connect(self._apply_switch)
        self.btn_add_group.clicked.connect(self._add_group)
        self.btn_rm_group.clicked.connect(self._rm_group)
        # 管理员 & 黑名单
        self.btn_add_admin.clicked.connect(self._add_admin)
        self.btn_rm_admin.clicked.connect(self._rm_admin)
        self.btn_reload_admin.clicked.connect(self._load_admin)
        self.btn_block.clicked.connect(self._block)
        self.btn_unblock.clicked.connect(self._unblock)
        self.btn_reload_block.clicked.connect(self._load_block)

        # 复选框句柄
        self._switch_boxes: list[list[QCheckBox]] = []

        # 初始加载管理员 & 黑名单
        self._load_admin()
        self._load_block()

    def showEvent(self, e):
        # 每次切到本页刷新数据表
        self._load_admin()
        self._load_block()
        super().showEvent(e)

    def _load(self):
        w = Worker(api_client.query, self.mw.cfg, "settings",
                   "SELECT cluster_id, master_group_id, created_at FROM group_clusters ORDER BY created_at DESC")

        def _ok(rows):
            self._clusters = rows
            self.tbl_cluster.setRowCount(len(rows))
            for i, r in enumerate(rows):
                self.tbl_cluster.setItem(i, 0, QTableWidgetItem(r["cluster_id"]))
                self.tbl_cluster.setItem(i, 1, QTableWidgetItem(str(r["master_group_id"])))
                self.tbl_cluster.setItem(i, 2, QTableWidgetItem(_ts(r["created_at"])))
            if rows:
                self._load_members(rows[0]["cluster_id"])
            self.mw.statusBar().showMessage(f"集群：{len(rows)} 个")

        w.finished_ok.connect(_ok)
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"加载失败: {e}"))
        w.start()
        self.mw._track(w)

    def _on_cluster_select(self):
        row = self.tbl_cluster.currentRow()
        if row < 0 or not getattr(self, "_clusters", []):
            return
        cid = self._clusters[row]["cluster_id"]
        self.ed_cluster_pick.setText(cid)
        self._load_members(cid)

    def _create_cluster(self):
        gid = self.ed_new_cluster.text().strip()
        if not gid.isdigit() or int(gid) <= 0:
            self.mw.statusBar().showMessage("请填写主群群号（纯数字）")
            return

        def _do():
            # 校验：群不在任何已有集群
            rows = api_client.query(self.mw.cfg, "settings",
                                    "SELECT cluster_id FROM group_cluster_members WHERE group_id = ?",
                                    (int(gid),))
            if rows:
                raise ValueError(f"群 {gid} 已在集群 {rows[0]['cluster_id'][:12]}… 中，不能重复建集群")
            # 校验：群已在 bot 存档（与成员管理口径一致）
            rows = api_client.query(self.mw.cfg, "chat",
                                    "SELECT COUNT(*) AS n FROM group_chat_cache WHERE group_id = ?",
                                    (int(gid),))
            if not rows or not rows[0]["n"]:
                raise ValueError(f"群 {gid} 不在 bot 存档中（无聊天记录），请等 bot 收到该群消息后再建集群")
            # 与 core/database.py create_cluster 同格式：cluster_{主群}_{uuid8}
            cid = f"cluster_{gid}_{uuid.uuid4().hex[:8]}"
            now = time.time()
            api_client.query(self.mw.cfg, "settings",
                             "INSERT INTO group_clusters (cluster_id, master_group_id, created_at) VALUES (?,?,?)",
                             (cid, int(gid), now), write=True)
            api_client.query(self.mw.cfg, "settings",
                             "INSERT INTO group_cluster_members (cluster_id, group_id, created_at) VALUES (?,?,?)",
                             (cid, int(gid), now), write=True)
            return cid

        w = Worker(_do)
        w.finished_ok.connect(lambda cid: (
            self.ed_new_cluster.clear(),
            self._load(),
            self.mw.statusBar().showMessage(f"集群已创建：{cid}（主群 {gid}）"),
        ))
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"建集群失败: {e}"))
        w.start()
        self.mw._track(w)

    def _delete_cluster(self):
        row = self.tbl_cluster.currentRow()
        if row < 0 or not getattr(self, "_clusters", []):
            self.mw.statusBar().showMessage("先选中要删除的集群")
            return
        cid = self._clusters[row]["cluster_id"]
        # 成员数查询 + 确认弹窗在主线程（模态框不能放 QThread）
        rows = api_client.query(self.mw.cfg, "settings",
                                "SELECT COUNT(*) AS n FROM group_cluster_members WHERE cluster_id = ?",
                                (cid,))
        n = rows[0]["n"] if rows else 0
        if not self.mw.confirm_danger("删除集群",
                                      f"集群 {cid}\n含 {n} 个成员群。删除后群级开关、人设合并、评选/总结合并等全部失效，且不可恢复。"):
            return

        def _do():
            # 事务内先删成员再删集群，不留孤儿数据
            path = api_client.db_path(self.mw.cfg, "settings")
            conn = sqlite3.connect(path, timeout=15)
            try:
                conn.execute("DELETE FROM group_cluster_members WHERE cluster_id = ?", (cid,))
                conn.execute("DELETE FROM group_clusters WHERE cluster_id = ?", (cid,))
                conn.commit()
            finally:
                conn.close()

        w = Worker(_do)
        w.finished_ok.connect(lambda *_: (
            self.ed_cluster_pick.clear(),
            self._load(),
            self.mw.statusBar().showMessage(f"集群 {cid[:12]}… 已删除"),
        ))
        w.finished_err.connect(lambda e: self.mw.statusBar().showMessage(f"删除失败: {e}"))
        w.start()
        self.mw._track(w)

    def _load_members(self, cluster_id: str):
        w = Worker(api_client.query, self.mw.cfg, "settings",
                   "SELECT group_id, " + ", ".join(k for k, _ in SWITCHES) +
                   " FROM group_cluster_members WHERE cluster_id = ? ORDER BY group_id",
                   (cluster_id,))

        def _ok(rows):
            self._cur_cluster = cluster_id
            self._member_rows = rows
            self.tbl_member.setRowCount(len(rows))
            self._switch_boxes = []
            for i, r in enumerate(rows):
                self.tbl_member.setItem(i, 0, QTableWidgetItem(str(r["group_id"])))
                boxes = []
                for j, (key, _) in enumerate(SWITCHES):
                    cb = QCheckBox()
                    cb.setChecked(bool(r.get(key)))
                    cb.setProperty("key", key)
                    cb.setProperty("group_id", r["group_id"])
                    self.tbl_member.setCellWidget(i, j + 1, cb)
                    boxes.append(cb)
                self._switch_boxes.append(boxes)
            self.mw.statusBar().showMessage(f"集群 {cluster_id[:8]}…：{len(rows)} 个成员群")

        w.finished_ok.connect(_ok)
        w.start()
        self.mw._track(w)

    def _apply_switch(self):
        cid = getattr(self, "_cur_cluster", None)
        if not cid or not getattr(self, "_member_rows", []):
            self.mw.statusBar().showMessage("先选中集群")
            return
        # 收集所有勾选项
        updates = []
        for i, r in enumerate(self._member_rows):
            for j, (key, _) in enumerate(SWITCHES):
                cb = self._switch_boxes[i][j]
                if cb.isChecked() != bool(r.get(key)):
                    updates.append((key, int(cb.property("group_id")), 1 if cb.isChecked() else 0))
        if not updates:
            self.mw.statusBar().showMessage("无变更")
            return
        if not self.mw.confirm("应用群级开关", f"将修改 {len(updates)} 项群级开关？"):
            return

        def _do():
            for key, gid, val in updates:
                api_client.query(self.mw.cfg, "settings",
                                 f"UPDATE group_cluster_members SET {key} = ? WHERE cluster_id = ? AND group_id = ?",
                                 (val, cid, gid), write=True)
            return True
        w = Worker(_do)
        w.finished_ok.connect(lambda *_: (self._load_members(cid), self.mw.statusBar().showMessage("群级开关已应用")))
        w.start()
        self.mw._track(w)

    def _add_group(self):
        cid = self.ed_cluster_pick.text().strip()
        gid = self.ed_add_group.text().strip()
        if not cid or not gid.isdigit():
            self.mw.statusBar().showMessage("需选中集群 + 填群号")
            return
        if not self.mw.confirm("加入集群", f"群 {gid} 加入集群 {cid[:8]}…？\n（该群将与主群视为同一个群）"):
            return

        def _do():
            import time as _t
            return api_client.query(self.mw.cfg, "settings",
                                    "INSERT OR IGNORE INTO group_cluster_members (cluster_id, group_id, created_at) VALUES (?,?,?)",
                                    (cid, int(gid), _t.time()), write=True)
        w = Worker(_do)
        w.finished_ok.connect(lambda *_: (self._load_members(cid), self.mw.statusBar().showMessage(f"群 {gid} 已加入集群")))
        w.start()
        self.mw._track(w)

    def _rm_group(self):
        cid = self.ed_cluster_pick.text().strip()
        row = self.tbl_member.currentRow()
        if not cid or row < 0:
            self.mw.statusBar().showMessage("需选中集群 + 选中成员群")
            return
        item = self.tbl_member.item(row, 0)
        gid = item.text() if item else ""
        # 主群不能移出
        cur_row = self.tbl_cluster.currentRow()
        if self._clusters and 0 <= cur_row < len(self._clusters):
            if gid == str(self._clusters[cur_row]["master_group_id"]):
                self.mw.statusBar().showMessage("主群不能移出")
                return
        if not self.mw.confirm_danger("移出集群", f"群 {gid} 移出集群 {cid[:8]}…？"):
            return

        def _do():
            return api_client.query(self.mw.cfg, "settings",
                                    "DELETE FROM group_cluster_members WHERE cluster_id = ? AND group_id = ?",
                                    (cid, int(gid)), write=True)
        w = Worker(_do)
        w.finished_ok.connect(lambda *_: (self._load_members(cid), self.mw.statusBar().showMessage(f"群 {gid} 已移出集群")))
        w.start()
        self.mw._track(w)

    # ------------------------------------------------------------
    #  管理员（自消息管理页移入，08-20）
    # ------------------------------------------------------------
    def _load_admin(self):
        w = Worker(api_client.query, self.mw.cfg, "settings",
                   "SELECT user_id, nickname, added_at FROM admin_users ORDER BY added_at DESC")

        def _ok(rows):
            self.tbl_admin.setRowCount(len(rows))
            for i, r in enumerate(rows):
                self.tbl_admin.setItem(i, 0, QTableWidgetItem(str(r["user_id"])))
                self.tbl_admin.setItem(i, 1, QTableWidgetItem(r["nickname"] or ""))
                self.tbl_admin.setItem(i, 2, QTableWidgetItem(_ts(r["added_at"])))

        w.finished_ok.connect(_ok)
        w.start()
        self.mw._track(w)

    def _add_admin(self):
        uid = self.ed_admin_uid.text().strip()
        if not uid.isdigit():
            self.mw.statusBar().showMessage("用户QQ 需为数字")
            return
        if not self.mw.confirm("添加管理员", f"确认添加 {uid} 为管理员？\n管理员可使用 /智能体 之外的所有管理指令。"):
            return
        nick = self.ed_admin_nick.text().strip()

        def _do():
            return api_client.query(self.mw.cfg, "settings",
                                    "INSERT OR IGNORE INTO admin_users (user_id, nickname, added_at) VALUES (?,?,?)",
                                    (int(uid), nick, time.time()), write=True)
        w = Worker(_do)
        w.finished_ok.connect(lambda *_: (self._load_admin(), self.mw.statusBar().showMessage(f"已添加管理员 {uid}")))
        w.start()
        self.mw._track(w)

    def _rm_admin(self):
        row = self.tbl_admin.currentRow()
        if row < 0:
            self.mw.statusBar().showMessage("请先选中一行")
            return
        item = self.tbl_admin.item(row, 0)
        uid = item.text() if item else ""
        if not self.mw.confirm_danger("移除管理员", f"移除管理员 {uid}？\n该用户将失去管理权限。"):
            return

        def _do():
            return api_client.query(self.mw.cfg, "settings",
                                    "DELETE FROM admin_users WHERE user_id = ?", (int(uid),), write=True)
        w = Worker(_do)
        w.finished_ok.connect(lambda *_: (self._load_admin(), self.mw.statusBar().showMessage(f"已移除管理员 {uid}")))
        w.start()
        self.mw._track(w)

    # ------------------------------------------------------------
    #  黑名单（自消息管理页移入，08-20）
    # ------------------------------------------------------------
    def _load_block(self):
        w = Worker(api_client.query, self.mw.cfg, "settings",
                   "SELECT user_id, nickname, blocked_at, blocked_by FROM user_blocklist ORDER BY blocked_at DESC")

        def _ok(rows):
            self.tbl_block.setRowCount(len(rows))
            for i, r in enumerate(rows):
                self.tbl_block.setItem(i, 0, QTableWidgetItem(str(r["user_id"])))
                self.tbl_block.setItem(i, 1, QTableWidgetItem(r["nickname"] or ""))
                self.tbl_block.setItem(i, 2, QTableWidgetItem(_ts(r["blocked_at"])))
                self.tbl_block.setItem(i, 3, QTableWidgetItem(r["blocked_by"] or ""))
            self._block_rows = rows

        w.finished_ok.connect(_ok)
        w.start()
        self.mw._track(w)

    def _block(self):
        uid = self.ed_block_uid.text().strip()
        if not uid.isdigit():
            self.mw.statusBar().showMessage("用户QQ 需为数字")
            return
        if not self.mw.confirm("拉黑", f"确认拉黑用户 {uid}？\n该用户消息将被 bot 忽略。"):
            return
        nick = self.ed_block_nick.text().strip()

        def _do():
            return api_client.query(self.mw.cfg, "settings",
                                    "INSERT OR IGNORE INTO user_blocklist (user_id, nickname, blocked_at, blocked_by) VALUES (?,?,?,?)",
                                    (int(uid), nick, time.time(), "GUI"), write=True)
        w = Worker(_do)
        w.finished_ok.connect(lambda *_: (self._load_block(), self.mw.statusBar().showMessage(f"已拉黑 {uid}")))
        w.start()
        self.mw._track(w)

    def _unblock(self):
        uid = self.ed_block_uid.text().strip()
        if not uid.isdigit():
            self.mw.statusBar().showMessage("用户QQ 需为数字")
            return
        if not self.mw.confirm("取消拉黑", f"确认取消拉黑 {uid}？"):
            return

        def _do():
            return api_client.query(self.mw.cfg, "settings",
                                    "DELETE FROM user_blocklist WHERE user_id = ?", (int(uid),), write=True)
        w = Worker(_do)
        w.finished_ok.connect(lambda *_: (self._load_block(), self.mw.statusBar().showMessage(f"已取消拉黑 {uid}")))
        w.start()
        self.mw._track(w)
