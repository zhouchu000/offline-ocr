"""A continuous document editor bound to the OCR's original text and table cells.

Qt paragraph formats carry stable domain identities through typing, splitting,
joining and undo. The domain remains authoritative for Word layout; only the
editable text is synchronized. Table/image structure is protected from accidental
cross-boundary deletion, while ordinary paragraphs support normal text editing.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QUrl
from PySide6.QtGui import (
    QColor, QFont, QImage, QKeySequence, QTextBlockFormat, QTextCharFormat,
    QTextCursor, QTextDocument, QTextFormat, QTextFrameFormat, QTextImageFormat, QTextLength, QTextTableFormat,
)
from PySide6.QtWidgets import QMenu, QTextEdit

from .domain import Block, Cell, PageResult

OWNER = int(QTextFormat.Property.UserProperty) + 17


class DocumentEditor(QTextEdit):
    target_changed = Signal(str)
    edited = Signal()
    notice = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('documentEditor')
        self.setAccessibleName('文档正文')
        self.setAcceptRichText(False)
        self.setAcceptDrops(False)
        self.setTabChangesFocus(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumWidth(340)
        self._page = None
        self._targets: dict[str, Block | Cell] = {}
        self._owners: dict[str, str] = {}
        self._loading = False
        self.document().setDocumentMargin(44)
        font = QFont('Microsoft YaHei UI')
        font.setPixelSize(16)
        self.document().setDefaultFont(font)
        self.textChanged.connect(self._sync)
        self.cursorPositionChanged.connect(self._cursor_changed)
        self.setPlaceholderText('识别完成后可编辑正文。')

    @staticmethod
    def cell_key(block_id: str, cell: Cell) -> str:
        return f'{block_id}:cell:{cell.row}:{cell.col}'

    @property
    def current_key(self) -> str:
        return str(self.textCursor().blockFormat().property(OWNER) or '')

    @property
    def current_target(self) -> Block | Cell | None:
        return self._targets.get(self.current_key)

    @property
    def current_block_id(self) -> str:
        return self._owners.get(self.current_key, '')

    def _format(self, key: str, *, title=False, cell=False):
        fmt = QTextBlockFormat()
        fmt.setProperty(OWNER, key)
        fmt.setTopMargin(0 if cell else 8)
        fmt.setBottomMargin(0 if cell else 18 if title else 12)
        fmt.setLineHeight(150, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        if title:
            fmt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return fmt

    def _insert_text(self, cursor, key, value, *, title=False, cell=False, bold=False):
        cursor.setBlockFormat(self._format(key, title=title, cell=cell))
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Weight.DemiBold if title or bold else QFont.Weight.Normal)
        fmt.setProperty(QTextFormat.Property.FontPixelSize, 22 if title else 16)
        cursor.setCharFormat(fmt)
        # A printed newline belongs to the same domain paragraph. Enter creates
        # another Qt block with the same identity, which also round-trips as \n.
        cursor.insertText(value.replace('\r\n', '\n').replace('\n', '\u2028'), fmt)

    def set_page(self, page: PageResult | None):
        if page is self._page:
            return
        self._loading = True
        self._page = page
        self._targets = {}
        self._owners = {}
        self.clear()
        self.setReadOnly(page is None)
        cursor = QTextCursor(self.document())
        if page:
            for index, block in enumerate(page.blocks):
                if index:
                    cursor.insertBlock()
                if block.kind == 'table' and block.rows > 0 and block.cols > 0:
                    cursor.setBlockFormat(self._format('structure:table'))
                    fmt = QTextTableFormat()
                    fmt.setCellPadding(9)
                    fmt.setCellSpacing(0)
                    fmt.setBorder(1)
                    fmt.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
                    fmt.setBorderBrush(QColor('#D7DBD5'))
                    fmt.setWidth(QTextLength(QTextLength.Type.PercentageLength, 100))
                    table = cursor.insertTable(block.rows, block.cols, fmt)
                    for cell in block.cells:
                        if cell.rowspan > 1 or cell.colspan > 1:
                            table.mergeCells(cell.row, cell.col, cell.rowspan, cell.colspan)
                        qt_cell = table.cellAt(cell.row, cell.col)
                        cell_format = qt_cell.format().toTableCellFormat()
                        cell_format.setBorder(1)
                        cell_format.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
                        cell_format.setBorderBrush(QColor('#D7DBD5'))
                        qt_cell.setFormat(cell_format)
                        key = self.cell_key(block.id, cell)
                        self._targets[key] = cell
                        self._owners[key] = block.id
                        self._insert_text(table.cellAt(cell.row, cell.col).firstCursorPosition(), key, cell.text, cell=True)
                    cursor = table.lastCursorPosition()
                    cursor.movePosition(QTextCursor.MoveOperation.NextBlock)
                    cursor.setBlockFormat(self._format('structure:table'))
                elif block.kind == 'image':
                    cursor.setBlockFormat(self._format('structure:image'))
                    cursor.setBlockFormat(self._center_format(cursor.blockFormat()))
                    preview = QImage(block.image_path or '')
                    if not preview.isNull():
                        url = QUrl(f'offline-image:{index}')
                        self.document().addResource(QTextDocument.ResourceType.ImageResource, url, preview)
                        size = preview.size().scaled(380, 200, Qt.AspectRatioMode.KeepAspectRatio)
                        fmt = QTextImageFormat()
                        fmt.setName(url.toString())
                        fmt.setWidth(size.width())
                        fmt.setHeight(size.height())
                        cursor.insertImage(fmt)
                    else:
                        cursor.insertText('〔图片〕')
                else:
                    self._targets[block.id] = block
                    self._owners[block.id] = block.id
                    self._insert_text(cursor, block.id, block.text, title=block.kind == 'title', bold=block.font_bold)
        self.document().clearUndoRedoStacks()
        self.moveCursor(QTextCursor.MoveOperation.Start)
        self._loading = False
        self._cursor_changed()

    @staticmethod
    def _center_format(fmt):
        fmt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return fmt

    def _sync(self):
        if self._loading or self._page is None:
            return
        values = {key: [] for key in self._targets}
        block = self.document().begin()
        while block.isValid():
            key = str(block.blockFormat().property(OWNER) or '')
            if key in values:
                values[key].append(block.text().replace('\u2028', '\n'))
            block = block.next()
        for key, target in self._targets.items():
            value = '\n'.join(values[key])
            target.edited_text = None if value == target.original_text else value
        self.edited.emit()

    def _cursor_changed(self):
        if not self._loading:
            self.target_changed.emit(self.current_block_id)

    def target_cursor(self, key: str) -> QTextCursor | None:
        first = last = None
        block = self.document().begin()
        while block.isValid():
            if str(block.blockFormat().property(OWNER) or '') == key:
                if first is None:
                    first = block.position()
                last = block.position() + block.length() - 1
            block = block.next()
        if first is None:
            return None
        cursor = QTextCursor(self.document())
        cursor.setPosition(first)
        cursor.setPosition(last, QTextCursor.MoveMode.KeepAnchor)
        return cursor

    def select_target(self, key: str, *, select_text=False):
        cursor = self.target_cursor(key)
        if cursor is None:
            # Table review targets a block; enter its first cell.
            key = next((k for k, owner in self._owners.items() if owner == key), key)
            cursor = self.target_cursor(key)
        if cursor is None:
            return False
        if not select_text:
            cursor.setPosition(cursor.selectionStart())
        self.setTextCursor(cursor)
        self.ensureCursorVisible()
        return True

    def replace_target(self, key: str, value: str):
        cursor = self.target_cursor(key)
        if cursor is None or key not in self._targets:
            return False
        cursor.beginEditBlock()
        cursor.insertText(value.replace('\r\n', '\n').replace('\n', '\u2028'))
        cursor.endEditBlock()
        self.setTextCursor(cursor)
        return True

    def _can_edit(self, cursor=None, *, backward=False, forward=False):
        cursor = QTextCursor(cursor or self.textCursor())
        if not cursor.hasSelection():
            if backward:
                cursor.movePosition(QTextCursor.MoveOperation.PreviousCharacter, QTextCursor.MoveMode.KeepAnchor)
            elif forward:
                cursor.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor)
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        block = self.document().findBlock(start)
        keys = set()
        while block.isValid() and block.position() <= end:
            # The exclusive end at the start of another block does not erase its
            # text, but DOES erase the structural separator; protect that too.
            keys.add(str(block.blockFormat().property(OWNER) or ''))
            block = block.next()
        valid = bool(keys) and keys.issubset(self._targets)
        if valid and any(isinstance(self._targets[k], Cell) for k in keys):
            valid = len(keys) == 1
        if not valid:
            self.notice.emit('仅可编辑段落与单元格文字；表格和图片结构保留。')
        return valid

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Paste):
            self.paste()
            return
        if event.matches(QKeySequence.StandardKey.Cut):
            self.cut()
            return
        if event.key() == Qt.Key.Key_Tab and not event.modifiers():
            cursor = self.textCursor()
            table = cursor.currentTable()
            if table:
                cell = table.cellAt(cursor)
                cells = []
                seen = set()
                for row in range(table.rows()):
                    for col in range(table.columns()):
                        other = table.cellAt(row, col)
                        identity = (other.row(), other.column())
                        if identity not in seen:
                            seen.add(identity)
                            cells.append(other)
                position = next(i for i, c in enumerate(cells) if c.row() == cell.row() and c.column() == cell.column())
                if position + 1 < len(cells):
                    self.setTextCursor(cells[position + 1].firstCursorPosition())
                else:
                    self.focusNextChild()
                return
        backward = event.key() == Qt.Key.Key_Backspace
        forward = event.key() == Qt.Key.Key_Delete
        typing = bool(event.text()) and not (event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier))
        cursor = self.textCursor()
        if (backward or forward) and not cursor.hasSelection() and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            cursor.movePosition(QTextCursor.MoveOperation.PreviousWord if backward else QTextCursor.MoveOperation.NextWord, QTextCursor.MoveMode.KeepAnchor)
        if (backward or forward or typing) and not self._can_edit(cursor, backward=backward, forward=forward):
            return
        super().keyPressEvent(event)

    def inputMethodEvent(self, event):
        cursor = self.textCursor()
        if event.replacementLength():
            start = max(0, min(self.document().characterCount()-1, cursor.position() + event.replacementStart()))
            end = min(self.document().characterCount()-1, start + event.replacementLength())
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        if (event.commitString() or event.preeditString() or event.replacementLength()) and not self._can_edit(cursor):
            event.ignore()
            return
        super().inputMethodEvent(event)

    def insertFromMimeData(self, source):
        if source.hasText() and self._can_edit():
            self.textCursor().insertText(source.text().replace('\r\n', '\n'))

    def cut(self):
        if self._can_edit():
            super().cut()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        for name, callback, enabled in (
            ('撤销', self.undo, self.document().isUndoAvailable()),
            ('重做', self.redo, self.document().isRedoAvailable()),
            ('剪切', self.cut, self.textCursor().hasSelection()),
            ('复制', self.copy, self.textCursor().hasSelection()),
            ('粘贴', self.paste, self.canPaste()),
            ('全选', self.selectAll, True),
        ):
            action = menu.addAction(name)
            action.setEnabled(enabled)
            action.triggered.connect(callback)
        menu.exec(event.globalPos())
