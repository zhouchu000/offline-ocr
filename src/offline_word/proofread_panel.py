"""Inline proofreading controls; all edits go through the existing editor."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QListView, QListWidget, QListWidgetItem,
    QFrame, QMenu, QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QTabBar, QVBoxLayout, QWidget,
)
from .domain import Block, Cell
from .review_widgets import DifferenceDelegate, SourceCrop


class ProofreadPanel(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self._syncing = False
        self._key = ''
        self._options = []
        self.setObjectName('sidebar')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content = QWidget()
        self.scroll.setWidget(self.content)
        outer.addWidget(self.scroll, 1)
        layout = QVBoxLayout(self.content)
        layout.setContentsMargins(16, 8, 16, 10)
        layout.setSpacing(6)
        self.context = QLabel('选择原图中的文字区域')
        self.context.setObjectName('caption')
        self.context.setWordWrap(True)
        self.context.setParent(self)
        self.context.hide()
        self.tabs = QTabBar()
        self.tabs.addTab('文字')
        self.tabs.addTab('表格')
        self.tabs.setExpanding(True)
        self.tabs.setDrawBase(False)
        self.tabs.currentChanged.connect(self._change_kind)
        self.tabs.setObjectName('proofreadTabs')
        self.tabs.setFixedWidth(92)
        self.target_picker = QComboBox()
        self.target_picker.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.target_picker.setMinimumContentsLength(10)
        self.target_picker.setAccessibleName('校对段落或单元格')
        self.target_picker.currentIndexChanged.connect(self._select_target)
        target_row = QHBoxLayout()
        target_row.setSpacing(8)
        target_row.addWidget(self.target_picker, 1)
        target_row.addWidget(self.tabs)
        layout.addLayout(target_row)
        crop_heading = QHBoxLayout()
        crop_title = QLabel('原图局部')
        crop_title.setObjectName('caption')
        crop_heading.addWidget(crop_title, 1)
        self.source_crop = SourceCrop()
        self.source_crop.setMinimumHeight(66)
        self.source_crop.setMaximumHeight(16777215)
        for title, callback in [('缩小', lambda: self.source_crop.zoom(1/1.3)),
                                ('放大', lambda: self.source_crop.zoom(1.3)),
                                ('适合', self.source_crop.fit_source)]:
            control = QPushButton(title)
            control.setObjectName('cropTool')
            control.clicked.connect(callback)
            crop_heading.addWidget(control)
        layout.addLayout(crop_heading)
        layout.addWidget(self.source_crop)
        edit_heading = QHBoxLayout()
        edit_title = QLabel('当前文字')
        edit_title.setObjectName('caption')
        edit_heading.addWidget(edit_title, 1)
        self.original_toggle = QPushButton('查看原始识别')
        self.original_toggle.setObjectName('cropTool')
        self.original_toggle.setCheckable(True)
        self.original_toggle.toggled.connect(self._toggle_original)
        edit_heading.addWidget(self.original_toggle)
        layout.addLayout(edit_heading)
        self.edit = QPlainTextEdit()
        self.edit.setObjectName('proofreadEdit')
        self.edit.setAccessibleName('当前文字校对')
        self.edit.setPlaceholderText('点击原图中的文字，开始校对。')
        self.edit.setMinimumHeight(64)
        self.edit.textChanged.connect(self._write)
        layout.addWidget(self.edit)
        self.original = QPlainTextEdit()
        self.original.setAccessibleName('当前文字的原始识别')
        self.original.setReadOnly(True)
        self.original.setFixedHeight(56)
        layout.addWidget(self.original)
        self.original.hide()
        self.candidate_title = QLabel('候选 · 高亮为变化，删除线为删去')
        self.candidate_title.setObjectName('caption')
        layout.addWidget(self.candidate_title)
        self.candidates = QListWidget()
        self.candidates.setAccessibleName('当前文字候选列表')
        self.candidates.setWordWrap(True)
        self.candidates.setResizeMode(QListView.ResizeMode.Adjust)
        self.candidates.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.candidates.setMinimumHeight(60)
        self.candidates.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.candidates.setObjectName('candidateList')
        self.difference_delegate = DifferenceDelegate(self.candidates)
        self.candidates.setItemDelegate(self.difference_delegate)
        self.candidates.currentItemChanged.connect(self._candidate_changed)
        layout.addWidget(self.candidates)
        self.no_candidates = QLabel('暂无候选，可直接修改文字。')
        self.no_candidates.setObjectName('caption')
        self.no_candidates.setWordWrap(True)
        layout.addWidget(self.no_candidates)
        self.apply = QPushButton('采用候选')
        self.apply.setToolTip('采用当前候选；原始识别保留，确认操作独立进行')
        self.apply.setObjectName('primary')
        self.apply_menu = QMenu(self.apply)
        self.adopt_only_action = self.apply_menu.addAction('仅采用候选', self._apply)
        self.adopt_confirm_action = self.apply_menu.addAction('采用并确认本段', lambda: self._apply(confirm=True))
        self.apply.setMenu(self.apply_menu)
        self.rerecognize = QPushButton('局部重新识别')
        self.rerecognize.setObjectName('quiet')
        self.rerecognize.setProperty('iconName', 'zoomIn')
        self.rerecognize.clicked.connect(window._rerecognize_selected)
        secondary = QHBoxLayout()
        secondary.setSpacing(4)
        secondary.addWidget(self.rerecognize, 1)
        self.expand = QPushButton('完整对照')
        self.expand.setToolTip('查看完整原始识别与候选文字')
        self.expand.setObjectName('quiet')
        self.expand.clicked.connect(window.show_text_details)
        secondary.addWidget(self.expand, 1)
        layout.addLayout(secondary)
        self.filtered = QPushButton('查看过滤文字')
        self.filtered.setObjectName('quiet')
        self.filtered.clicked.connect(self._show_filtered)
        layout.addWidget(self.filtered)
        self.regions = QSplitter(Qt.Orientation.Vertical)
        self.regions.setObjectName('proofreadRegions')
        self.regions.setChildrenCollapsible(False)
        self.regions.setHandleWidth(7)
        groups = [([crop_heading], [self.source_crop]),
                  ([edit_heading], [self.edit, self.original]),
                  ([], [self.candidate_title, self.candidates])]
        for headings, controls in groups:
            group = QWidget()
            group_layout = QVBoxLayout(group)
            group_layout.setContentsMargins(0, 0, 0, 0)
            group_layout.setSpacing(5)
            for heading in headings:
                layout.removeItem(heading)
                group_layout.addLayout(heading)
            for control in controls:
                layout.removeWidget(control)
                group_layout.addWidget(control, 0 if isinstance(control, QLabel) or control is self.original else 1)
            self.regions.addWidget(group)
        self.candidate_region = self.regions.widget(2)
        layout.insertWidget(1, self.regions, 1)
        self.regions.setSizes([150, 170, 150])
        self.footer = QFrame()
        self.footer.setObjectName('proofreadFooter')
        footer = QVBoxLayout(self.footer)
        footer.setContentsMargins(16, 8, 16, 12)
        footer.setSpacing(8)
        self.progress = QLabel('')
        self.progress.setObjectName('caption')
        footer.addWidget(self.progress)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.next = QPushButton('下一处')
        self.next.setToolTip('F6 下一处，Shift+F6 上一处')
        self.next.clicked.connect(self.next_review)
        actions.addWidget(self.next, 1)
        self.confirm = QPushButton('确认并继续')
        self.confirm.setToolTip('确认当前整段或整张表格 · Ctrl+Shift+Enter')
        self.confirm.clicked.connect(self.confirm_and_next)
        actions.addWidget(self.confirm, 1)
        actions.addWidget(self.apply, 1)
        footer.addLayout(actions)
        outer.addWidget(self.footer)
        self.refresh()

    def _show_filtered(self):
        from .filtered_text_ui import show_filtered_text
        show_filtered_text(self.window)

    def _toggle_original(self, expanded):
        self.original.setVisible(expanded)
        self.original_toggle.setText('收起原始识别' if expanded else '查看原始识别')

    def refresh(self):
        if self._syncing:
            return
        self._syncing = True
        try:
            editor = self.window.document_editor
            target = editor.current_target
            self._key = editor.current_key
            self.context.setText(self.window.context_label.text() if target else '选择原图中的文字区域')
            self.source_crop.setToolTip(self.context.text() + ' · 仅放大查看，不裁切原图')
            desired_tab = 1 if isinstance(target, Cell) else 0
            self.tabs.setCurrentIndex(desired_tab)
            entries = []
            job = self.window.current_job
            block = next((b for b in job.result.blocks if b.id == self.window._selected_block_id), None) if job and job.result else None
            bbox = target.bbox if target is not None and target.bbox else block.bbox if block else None
            import statistics
            heights = [line.bbox[3]-line.bbox[1] for line in target.text_lines if line.bbox[3] > line.bbox[1]] if isinstance(target, Block) else []
            self.source_crop.set_source(job.result.processed_image_path if job and job.result else None,
                                        bbox, (job.result.width, job.result.height) if job and job.result else (0, 0),
                                        line_height=statistics.median(heights) if heights else None,
                                        image=self.window.processed_preview._image if job and job.result and
                                        self.window.processed_preview._identity == (job.result.processed_image_path, 0) else None)
            self.filtered.setVisible(bool(job and job.result and job.result.filtered_text))
            if job and job.result:
                for i, block in enumerate(job.result.blocks, 1):
                    if block.kind == 'table' and desired_tab:
                        for cell in block.cells:
                            entries.append((f'表格 {i} · 第 {cell.row+1} 行，第 {cell.col+1} 列', editor.cell_key(block.id, cell)))
                    elif block.kind != 'image' and block.kind != 'table' and not desired_tab:
                        entries.append((f'{self.window._kind_label(block.kind)} {i} · {block.text[:18]}', block.id))
            if entries != self._options:
                self._options = entries
                self.target_picker.clear()
                for title, key in entries:
                    self.target_picker.addItem(title, key)
            self.target_picker.setCurrentIndex(self.target_picker.findData(self._key))
            self.target_picker.setToolTip(self.target_picker.currentText())
            value = target.text if target else ''
            if self.edit.toPlainText() != value:
                self.edit.setPlainText(value)
            original = target.original_text if target else ''
            if self.original.toPlainText() != original:
                self.original.setPlainText(original)
            choices = target.candidates if target else []
            comparison_changed = self.difference_delegate.original != value
            if comparison_changed:
                self.difference_delegate.original = value
            old = [self.candidates.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.candidates.count())]
            if old != list(choices):
                self.candidates.clear()
                for value in choices:
                    item = QListWidgetItem(value)
                    item.setData(Qt.ItemDataRole.UserRole, value)
                    item.setToolTip(value)
                    self.candidates.addItem(item)
                if choices:
                    self.candidates.setCurrentRow(0)
            self.candidates.setToolTip('候选需手动采用' if choices else '当前文字暂无候选，可直接编辑或局部重识。')
            self.candidates.setVisible(bool(choices))
            self.candidate_title.setVisible(bool(choices))
            self.candidate_region.setVisible(bool(choices))
            self.no_candidates.setVisible(not choices)
            self.apply.setVisible(bool(choices))
            self.edit.setEnabled(target is not None)
            self.apply.setEnabled(bool(choices))
            self.expand.setEnabled(target is not None)
            self.confirm.setEnabled(target is not None)
            self.rerecognize.setEnabled(isinstance(target, Block) and target.kind in {'text', 'title'} and self.window._worker is None and self.window._models_ready)
            pending = self.window.review_items(sort_by='risk')
            item = next((item for item in pending if item.job_id == self.window._current_job_id and item.block.id == self.window._selected_block_id), None)
            safe_confirmation = not item or set(item.categories) <= {'low', 'conflict', 'other'}
            self.adopt_confirm_action.setEnabled(isinstance(target, Block) and target.kind in {'text', 'title'} and safe_confirmation)
            current = next((i for i, item in enumerate(pending, 1) if item.job_id == self.window._current_job_id and item.block.id == self.window._selected_block_id), None)
            self.progress.setText(f'待复核 {current} / {len(pending)}' if current else f'剩余 {len(pending)} 处待复核' if pending else '全部复核完成')
            self.next.setEnabled(bool(pending))
        finally:
            self._syncing = False
        if comparison_changed or old != list(choices):
            self.candidates.doItemsLayout()
            self.candidates.viewport().update()

    def _write(self):
        if self._syncing or not self._key:
            return
        self._syncing = True
        try:
            self.window.document_editor.replace_target(self._key, self.edit.toPlainText())
        finally:
            self._syncing = False
        self.difference_delegate.original = self.edit.toPlainText()
        self.candidates.doItemsLayout()
        self.candidates.viewport().update()

    def _select_target(self, _index):
        if not self._syncing and (key := self.target_picker.currentData()):
            self.window.document_editor.select_target(key)

    def _change_kind(self, index):
        if self._syncing:
            return
        job = self.window.current_job
        if not job or not job.result:
            return
        block = next((b for b in job.result.blocks if (b.kind == 'table' if index else b.kind in {'text', 'title'})), None)
        if block:
            self.window.select_block(block.id)
        else:
            self.refresh()
            self.window._set_status('本页没有表格。' if index else '本页没有独立文字段落。')

    def _candidate_changed(self, item, _previous):
        self.apply.setEnabled(item is not None)

    def _apply(self, _checked=False, *, confirm=False):
        item = self.candidates.currentItem()
        target = self.window.document_editor.current_target
        if item is None or target is None:
            return
        value = item.data(Qt.ItemDataRole.UserRole)
        if self.window.document_editor.replace_target(self._key, value):
            target.proofread_source = 'manual_candidate'
            target.needs_review = False
            self.window.review_changed(self.window._current_job_id)
            if confirm and isinstance(target, Block) and self.adopt_confirm_action.isEnabled():
                self.confirm_and_next()
            else:
                self.window._set_status('已采用候选；核对无误后可确认。原始识别保留。')
            self.refresh()

    def next_review(self, _checked=False, *, direction=1):
        items = self.window.review_items(sort_by='risk')
        if not items:
            self.window._set_status('没有待复核文字。')
            return
        current = next((i for i, item in enumerate(items) if item.job_id == self.window._current_job_id and item.block.id == self.window._selected_block_id), -1)
        target = items[(current+direction) % len(items) if current >= 0 else (0 if direction > 0 else -1)]
        self.window._locate_review_item(target.job_id, target.block.id)

    def confirm_and_next(self):
        if self.window.review_index.confirm(self.window.jobs, self.window._current_job_id, self.window._selected_block_id):
            self.window._set_status('已确认本处，修改文字或识别结果后会重新提示。')
            self.window._sync_queue_items()
            self.window._refresh_action_states()
            self.next_review()
