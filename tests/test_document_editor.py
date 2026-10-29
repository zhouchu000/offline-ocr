from pathlib import Path
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from offline_word.document_editor import DocumentEditor, OWNER
from offline_word.domain import Block, Cell, PageResult


@pytest.fixture
def editor(qtbot):
    value = DocumentEditor()
    value.resize(780, 700)
    qtbot.addWidget(value)
    value.show()
    return value


def page(blocks):
    return PageResult('page', 0, 1000, 1400, '', blocks)


def test_round_trip_multiline_title_table_and_originals(editor):
    a = Block('a', 'title', (0, 0, 20, 10), '文档标题')
    b = Block('b', 'text', (0, 20, 20, 40), '第一行\n第二行')
    c = Cell(0, 0, '原单元格\n下一行', rowspan=2)
    table = Block('t', 'table', (0, 40, 20, 80), rows=2, cols=2, cells=[c, Cell(0, 1, '右上'), Cell(1, 1, '右下')])
    editor.set_page(page([a, b, table]))
    assert a.edited_text is None and b.edited_text is None and c.edited_text is None
    editor.replace_target('b', '手动改字\n保留段落')
    key = editor.cell_key('t', c)
    assert editor.replace_target(key, '表格修改\n保留换行')
    assert b.text == '手动改字\n保留段落' and b.original_text == '第一行\n第二行'
    assert c.text == '表格修改\n保留换行' and c.original_text == '原单元格\n下一行'
    assert a.text == '文档标题'
    editor.undo()
    assert c.edited_text is None
    editor.redo()
    assert c.text == '表格修改\n保留换行'
    assert editor.textCursor().currentTable().cellAt(0, 0).rowSpan() == 2


def test_typing_split_join_delete_and_undo_do_not_lose_text(editor, qtbot):
    a = Block('a', 'text', (0, 0, 10, 10), '甲乙')
    b = Block('b', 'text', (0, 20, 10, 30), '丙丁')
    editor.set_page(page([a, b]))
    editor.select_target('a')
    qtbot.keyClick(editor, Qt.Key.Key_Right)
    qtbot.keyClick(editor, Qt.Key.Key_Return)
    assert a.text == '甲\n乙' and b.text == '丙丁'
    editor.undo()
    assert a.edited_text is None
    editor.select_target('b')
    qtbot.keyClick(editor, Qt.Key.Key_Backspace)
    assert a.text == '甲乙丙丁' and b.text == ''
    editor.undo()
    assert a.text == '甲乙' and b.text == '丙丁'
    editor.selectAll()
    qtbot.keyClicks(editor, 'replacement')
    assert a.text + b.text == 'replacement'
    editor.undo()
    assert a.text == '甲乙' and b.text == '丙丁'


def test_paste_uses_plain_text_and_round_trips_new_paragraphs(editor, qtbot):
    from PySide6.QtCore import QMimeData
    a = Block('a', 'text', (0, 0, 10, 10), '旧文')
    editor.set_page(page([a]))
    editor.select_target('a', select_text=True)
    mime = QMimeData()
    mime.setText('第一段\n第二段')
    mime.setHtml('<p>Unwanted rich formatting</p>')
    editor.insertFromMimeData(mime)
    assert a.text == '第一段\n第二段'
    editor.undo()
    assert a.edited_text is None


def test_table_structure_cannot_be_removed_by_cross_boundary_delete_or_tab(editor, qtbot):
    a = Block('a', 'text', (0, 0, 10, 10), '正文')
    c = Cell(0, 0, '单元格')
    t = Block('t', 'table', (0, 20, 10, 40), rows=1, cols=1, cells=[c])
    editor.set_page(page([a, t]))
    editor.selectAll()
    qtbot.keyClick(editor, Qt.Key.Key_Delete)
    assert a.text == '正文' and c.text == '单元格'
    editor.select_target(editor.cell_key('t', c))
    table = editor.textCursor().currentTable()
    qtbot.keyClick(editor, Qt.Key.Key_Tab)
    assert table.rows() == 1 and table.columns() == 1
    editor.select_target(editor.cell_key('t', c))
    qtbot.keyClick(editor, Qt.Key.Key_Backspace)
    assert c.text == '单元格'


def test_export_reads_latest_continuous_document_and_table_edits(editor, tmp_path):
    from offline_word.exporter import export_docx
    from docx import Document
    a = Block('a', 'text', (10, 10, 950, 80), '旧正文')
    c = Cell(0, 0, '旧表格')
    result = page([a, Block('t', 'table', (10, 100, 950, 200), rows=1, cols=1, cells=[c])])
    editor.set_page(result)
    editor.replace_target('a', '正文修订')
    editor.replace_target(editor.cell_key('t', c), '表格修订')
    for mode in ('source', 'flow', 'paragraph', 'text_only'):
        doc = Document(export_docx([result], tmp_path/f'{mode}.docx', layout_mode=mode))
        text = ''.join(p.text for p in doc.paragraphs) + ''.join(cell.text for table in doc.tables for row in table.rows for cell in row.cells)
        assert '正文修订' in text and '表格修订' in text
        assert '旧正文' not in text and '旧表格' not in text
