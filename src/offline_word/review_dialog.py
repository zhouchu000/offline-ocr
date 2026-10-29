"""Cross-page review presentation; filtering keeps the existing review rules."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QDialog, QListWidget, QListWidgetItem, QPlainTextEdit
from .review_widgets import ReviewRowDelegate, SourceCrop


CATEGORIES = [('全部需复核', 'all'), ('低置信度', 'low'), ('补回文字', 'recovered'),
              ('候选与分歧', 'conflict'), ('位置交叠', 'overlap'), ('疑似重复', 'duplicate'),
              ('页面边缘', 'boundary'), ('图码附近', 'code_nearby'), ('表格', 'table')]


def build_review_dialog(w):
    from .workspace_ui import button, column, label, row_layout
    dialog = QDialog(w)
    dialog.setWindowTitle('待复核文字')
    dialog.resize(760, 580)
    layout = column(dialog, (24, 24, 24, 24), 14)
    w.review_summary_label = label('', 'viewTitle')
    layout.addWidget(w.review_summary_label)
    filters = row_layout()
    w.review_filter_combo = QComboBox()
    w.review_filter_combo.setAccessibleName('复核问题分类')
    for title, key in CATEGORIES:
        w.review_filter_combo.addItem(title, key)
    w.review_filter_combo.currentIndexChanged.connect(w._populate_review_list)
    filters.addWidget(w.review_filter_combo, 1)
    w.review_sort_combo = QComboBox()
    w.review_sort_combo.setAccessibleName('复核排序')
    w.review_sort_combo.addItem('风险优先', 'risk')
    w.review_sort_combo.addItem('按图片顺序', 'page')
    w.review_sort_combo.currentIndexChanged.connect(w._populate_review_list)
    filters.addWidget(w.review_sort_combo, 1)
    layout.addLayout(filters)
    w.review_list = QListWidget()
    w.review_list.setObjectName('reviewList')
    w.review_list.setAccessibleName('待复核文字列表')
    w.review_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    w.review_list.setItemDelegate(ReviewRowDelegate(w.review_list))
    w.review_list.itemDoubleClicked.connect(w._open_review_selection)
    w.review_list.currentItemChanged.connect(lambda *_: show_review_detail(w))
    layout.addWidget(w.review_list, 1)
    w.review_detail_heading = label('选择复核项', 'caption')
    w.review_detail_heading.setWordWrap(True)
    layout.addWidget(w.review_detail_heading)
    detail = row_layout(spacing=16)
    w.review_source_crop = SourceCrop()
    detail.addWidget(w.review_source_crop, 1)
    w.review_detail_text = QPlainTextEdit()
    w.review_detail_text.setAccessibleName('选中复核项的完整文字')
    w.review_detail_text.setReadOnly(True)
    w.review_detail_text.setFixedHeight(108)
    detail.addWidget(w.review_detail_text, 1)
    layout.addLayout(detail)
    actions = row_layout()
    actions.addWidget(label('双击定位原图', 'caption'), 1)
    actions.addWidget(button('关闭', dialog.reject, 'quiet'))
    w.review_locate_button = button('定位并校对', w._open_review_selection, 'primary')
    actions.addWidget(w.review_locate_button)
    layout.addLayout(actions)
    w._review_dialog = dialog


def populate_review_list(w):
    selected = w.review_list.currentItem()
    previous = selected.data(Qt.ItemDataRole.UserRole) if selected else None
    w.review_list.clear()
    all_items = w.review_items(sort_by=w.review_sort_combo.currentData())
    for index, (title, category) in enumerate(CATEGORIES):
        count = sum(category == 'all' or category in item.categories for item in all_items)
        w.review_filter_combo.setItemText(index, f'{title}  {count}')
    category = w.review_filter_combo.currentData()
    items = [item for item in all_items if category == 'all' or category in item.categories]
    w._visible_review_items = {(item.job_id, item.block.id): item for item in items}
    current = 0
    for index, item in enumerate(items):
        text = item.block.text or ' · '.join(c.text for c in item.block.cells)
        kinds = ' · '.join(title for title, key in CATEGORIES[1:] if key in item.categories) or '文字待核对'
        row = QListWidgetItem(f'第 {item.page_number} 页 · {kinds}\n' + ' '.join(text.split()))
        row.setData(Qt.ItemDataRole.UserRole, (item.job_id, item.block.id))
        row.setData(Qt.ItemDataRole.AccessibleTextRole, f'第 {item.page_number} 页，{kinds}，{text}')
        w.review_list.addItem(row)
        if (item.job_id, item.block.id) == previous:
            current = index
    w.review_summary_label.setText(f'{len(items)} 处文字待复核' if items else '当前分类暂无待复核项')
    w.review_locate_button.setEnabled(bool(items))
    if items:
        w.review_list.setCurrentRow(current)
    else:
        show_review_detail(w)


def show_review_detail(w):
    selected = w.review_list.currentItem()
    item = getattr(w, '_visible_review_items', {}).get(selected.data(Qt.ItemDataRole.UserRole)) if selected else None
    if item is None:
        w.review_detail_heading.setText('选择复核项')
        w.review_detail_text.clear()
        w.review_source_crop.set_source(None, None, (0, 0))
        return
    job = next((job for job in w.jobs if job.id == item.job_id), None)
    w.review_detail_heading.setText(' · '.join(item.reasons))
    w.review_detail_text.setPlainText(item.block.text or '\n'.join(cell.text for cell in item.block.cells))
    if job and job.result:
        w.review_source_crop.set_source(item.processed_image_path, item.block.bbox, (job.result.width, job.result.height))
