"""Local visual preview of the actual generated DOCX, without requiring Word.

The same export produces the preview input and the delivered file. Qt draws its
text/tables and the source-positioned frames; Word's pagination may differ.
Nothing here changes the OCR domain or the export layout algorithm.
"""
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from PySide6.QtCore import QRectF, QSizeF, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor, QFont, QImage, QPainter, QTextBlockFormat, QTextCharFormat,
    QTextCursor, QTextDocument, QTextFormat, QTextFrameFormat, QTextImageFormat,
    QTextLength, QTextTableFormat,
)
from PySide6.QtWidgets import QGraphicsItem, QGraphicsScene, QGraphicsView


def _number(element, tag, attribute='w:val', default=0):
    child = element.find(qn(tag)) if element is not None else None
    try:
        return float(child.get(qn(attribute))) if child is not None else default
    except (TypeError, ValueError):
        return default


def _paragraph(cursor, element, package):
    props = element.find(qn('w:pPr'))
    fmt = QTextBlockFormat()
    align = props.find(qn('w:jc')) if props is not None else None
    if align is not None:
        fmt.setAlignment({'center': Qt.AlignmentFlag.AlignHCenter, 'right': Qt.AlignmentFlag.AlignRight,
                          'both': Qt.AlignmentFlag.AlignJustify}.get(align.get(qn('w:val')), Qt.AlignmentFlag.AlignLeft))
    spacing = props.find(qn('w:spacing')) if props is not None else None
    if spacing is not None:
        fmt.setTopMargin(float(spacing.get(qn('w:before'), '0'))/20)
        fmt.setBottomMargin(float(spacing.get(qn('w:after'), '0'))/20)
        line = spacing.get(qn('w:line'))
        if line:
            if spacing.get(qn('w:lineRule')) == 'exact':
                fmt.setLineHeight(float(line)/20, QTextBlockFormat.LineHeightTypes.FixedHeight.value)
            else:
                fmt.setLineHeight(float(line)/240*100, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
    indent = props.find(qn('w:ind')) if props is not None else None
    if indent is not None:
        fmt.setLeftMargin(float(indent.get(qn('w:left'), '0'))/20)
        fmt.setRightMargin(float(indent.get(qn('w:right'), '0'))/20)
        fmt.setTextIndent((float(indent.get(qn('w:firstLine'), '0'))-float(indent.get(qn('w:hanging'), '0')))/20)
    cursor.setBlockFormat(fmt)
    for run in element.findall(qn('w:r')):
        rp = run.find(qn('w:rPr'))
        cf = QTextCharFormat()
        cf.setForeground(QColor('#211F1A'))
        cf.setFontFamilies(['宋体'])
        cf.setProperty(QTextFormat.Property.FontPixelSize, _number(rp, 'w:sz', default=24)/2)
        if rp is not None:
            if rp.find(qn('w:vanish')) is not None:
                continue
            fonts = rp.find(qn('w:rFonts'))
            if fonts is not None:
                cf.setFontFamilies([fonts.get(qn('w:eastAsia')) or fonts.get(qn('w:ascii')) or '宋体'])
            bold = rp.find(qn('w:b'))
            cf.setFontWeight(QFont.Weight.Bold if bold is not None and bold.get(qn('w:val'), '1') not in {'0', 'false'} else QFont.Weight.Normal)
            cf.setFontItalic(rp.find(qn('w:i')) is not None)
        for child in run:
            if child.tag == qn('w:t'):
                cursor.insertText(child.text or '', cf)
            elif child.tag == qn('w:br'):
                cursor.insertText('\u2028', cf)
            elif child.tag == qn('w:tab'):
                cursor.insertText('\t', cf)
            elif child.tag == qn('w:drawing'):
                blip = next(iter(child.iter(qn('a:blip'))), None)
                extent = next(iter(child.iter(qn('wp:extent'))), None)
                if blip is None or extent is None:
                    continue
                relationship = blip.get(qn('r:embed'))
                part = package.part.related_parts.get(relationship)
                if part is None:
                    continue
                image = QImage.fromData(part.blob)
                if image.isNull():
                    continue
                url = QUrl(f'preview-image:{relationship}')
                cursor.document().addResource(QTextDocument.ResourceType.ImageResource, url, image)
                image_format = QTextImageFormat()
                image_format.setName(url.toString())
                image_format.setWidth(float(extent.get('cx'))/12700)
                image_format.setHeight(float(extent.get('cy'))/12700)
                cursor.insertImage(image_format)


def _table(cursor, element, package):
    rows = element.findall(qn('w:tr'))
    grid = element.find(qn('w:tblGrid'))
    widths = [float(c.get(qn('w:w'), '1000'))/20 for c in grid] if grid is not None else []
    columns = len(widths) or max((len(r.findall(qn('w:tc'))) for r in rows), default=1)
    fmt = QTextTableFormat()
    fmt.setBorder(0.6)
    fmt.setBorderBrush(QColor('#D3CFC0'))
    fmt.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
    fmt.setCellPadding(3)
    fmt.setCellSpacing(0)
    if widths:
        fmt.setColumnWidthConstraints([QTextLength(QTextLength.Type.FixedLength, width) for width in widths])
        fmt.setWidth(QTextLength(QTextLength.Type.FixedLength, sum(widths)))
    table = cursor.insertTable(max(1, len(rows)), columns, fmt)
    merges = {}
    for row_index, row in enumerate(rows):
        col = 0
        for cell in row.findall(qn('w:tc')):
            cp = cell.find(qn('w:tcPr'))
            span = max(1, int(_number(cp, 'w:gridSpan', default=1)))
            span = min(span, columns-col)
            if span < 1:
                break
            vertical = cp.find(qn('w:vMerge')) if cp is not None else None
            if vertical is not None and vertical.get(qn('w:val')) != 'restart' and col in merges:
                start = merges[col]
                table.mergeCells(start, col, row_index-start+1, span)
            else:
                merges.pop(col, None)
                if vertical is not None:
                    merges[col] = row_index
                if span > 1:
                    table.mergeCells(row_index, col, 1, span)
                qt_cell = table.cellAt(row_index, col)
                cell_format = qt_cell.format().toTableCellFormat()
                cell_format.setBorder(0.6)
                cell_format.setBorderBrush(QColor('#D3CFC0'))
                cell_format.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
                qt_cell.setFormat(cell_format)
                cc = qt_cell.firstCursorPosition()
                for i, paragraph in enumerate(cell.findall(qn('w:p'))):
                    if i:
                        cc.insertBlock()
                    _paragraph(cc, paragraph, package)
            col += span
    cursor.setPosition(table.lastPosition()+1)


class PaperItem(QGraphicsItem):
    def __init__(self, size, document, page_index, overlays=()):
        super().__init__()
        self.size, self.document, self.page_index = size, document, page_index
        self.overlays = overlays

    def boundingRect(self):
        return QRectF(0, 0, self.size.width(), self.size.height())

    def paint(self, painter, option, widget=None):
        painter.fillRect(self.boundingRect(), QColor('#FFFFFF'))
        painter.save()
        painter.setClipRect(self.boundingRect())
        painter.translate(0, -self.page_index*self.size.height())
        self.document.drawContents(painter, QRectF(0, self.page_index*self.size.height(), self.size.width(), self.size.height()))
        painter.restore()
        for x, y, document in self.overlays:
            painter.save()
            painter.setClipRect(self.boundingRect())
            painter.translate(x, y)
            document.drawContents(painter)
            painter.restore()


def iter_docx_pages(path):
    """GUI-thread iterator: yield between elements and completed pages."""
    package = Document(str(path)) if isinstance(path, (str, Path)) else path
    section = package.sections[0]
    size = QSizeF(section.page_width.pt, section.page_height.pt)
    def new_document(*, positioned=False, width=None):
        document = QTextDocument()
        document.setUndoRedoEnabled(False)
        font = QFont('宋体')
        font.setPixelSize(12)
        document.setDefaultFont(font)
        document.setDocumentMargin(0)
        if positioned:
            document.setTextWidth(width or size.width())
        else:
            document.setPageSize(size)
            root = document.rootFrame().frameFormat()
            root.setTopMargin(section.top_margin.pt)
            root.setBottomMargin(section.bottom_margin.pt)
            root.setLeftMargin(section.left_margin.pt)
            root.setRightMargin(section.right_margin.pt)
            document.rootFrame().setFrameFormat(root)
        return document
    document = new_document()
    cursor = QTextCursor(document)
    overlays = []
    has_content = False
    def finish():
        count = max(1, document.pageCount())
        for index in range(count):
            yield PaperItem(size, document, index, list(overlays) if index == 0 else [])
    for element in package.element.body:
        yield None
        if element.tag not in {qn('w:p'), qn('w:tbl')}:
            continue
        props = element.find(qn('w:pPr'))
        page_break = props.find(qn('w:pageBreakBefore')) if props is not None else None
        if page_break is not None and page_break.get(qn('w:val'),'1') not in {'0','false','off'} and (has_content or overlays):
            yield from finish()
            document, overlays, has_content = new_document(), [], False
            cursor = QTextCursor(document)
        frame = props.find(qn('w:framePr')) if props is not None else None
        if element.tag == qn('w:tbl'):
            tp = element.find(qn('w:tblPr'))
            frame = tp.find(qn('w:tblpPr')) if tp is not None else None
        if frame is not None:
            is_table = element.tag == qn('w:tbl')
            x = float(frame.get(qn('w:tblpX' if is_table else 'w:x'), '0'))/20
            y = float(frame.get(qn('w:tblpY' if is_table else 'w:y'), '0'))/20
            width = float(frame.get(qn('w:w'), str(size.width()*20)))/20
            piece = new_document(positioned=True, width=width)
            pc = QTextCursor(piece)
            (_table if is_table else _paragraph)(pc, element, package)
            overlays.append((x, y, piece))
        else:
            # Empty anchor paragraphs carry source-page boundaries only.
            if element.tag == qn('w:p') and next(element.iter(qn('w:t')), None) is None and next(element.iter(qn('w:drawing')), None) is None:
                continue
            if has_content:
                cursor.insertBlock()
            (_table if element.tag == qn('w:tbl') else _paragraph)(cursor, element, package)
            has_content = True
    yield from finish()


def load_docx(path):
    return [page for page in iter_docx_pages(path) if page is not None]


class ExportCanvas(QGraphicsView):
    page_changed = Signal(int, int)
    zoom_changed = Signal(int)

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setObjectName('exportCanvas')
        self.setAccessibleName('Word 排版预览')
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._pages = []
        self._index = 0
        self._fit = 'page'

    def set_theme(self, palette):
        from PySide6.QtGui import QBrush
        self.setBackgroundBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.viewport().setAutoFillBackground(False)

    def load(self, path):
        paths = path if isinstance(path, (list, tuple)) else [path]
        pages = [page for item in paths for page in load_docx(item)]
        self.scene().clear()
        self._pages = pages
        self._index = 0
        for page in pages:
            self.scene().addItem(page)
            page.setVisible(False)
        self.show_page(0)

    def clear_pages(self):
        self.scene().clear()
        self._pages = []
        self._index = 0

    def append_page(self, page):
        self._pages.append(page)
        self.scene().addItem(page)
        page.setVisible(False)
        self.show_page(self._index)

    def show_page(self, index):
        if not self._pages:
            return
        self._pages[self._index].setVisible(False)
        self._index = max(0, min(index, len(self._pages)-1))
        paper = self._pages[self._index]
        paper.setVisible(True)
        self.scene().setSceneRect(paper.boundingRect().adjusted(-24, -24, 24, 24))
        self.fit_page(self._fit)
        self.page_changed.emit(self._index, len(self._pages))

    def fit_page(self, mode='page'):
        self._fit = mode
        if not self._pages:
            return
        rect = self._pages[self._index].boundingRect()
        self.resetTransform()
        if mode == 'width':
            factor = max(.05, (self.viewport().width()-56)/rect.width())
            self.scale(factor, factor)
            self.centerOn(rect.center().x(), min(rect.center().y(), self.viewport().height()/factor/2))
        else:
            self.fitInView(rect.adjusted(-24,-24,24,24), Qt.AspectRatioMode.KeepAspectRatio)
        self.zoom_changed.emit(round(self.transform().m11()*100))

    def zoom(self, factor):
        scale = self.transform().m11()*factor
        if .1 <= scale <= 5:
            self._fit = None
            self.scale(factor, factor)
            self.zoom_changed.emit(round(scale*100))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit:
            self.fit_page(self._fit)
