from docx import Document
from docx.oxml.ns import qn
from offline_word.domain import Block, Cell, PageResult, TextLine
from offline_word.exporter import export_docx
from offline_word.table_geometry import attach_table_geometry


def sample_table():
    html = '<table><tr><td colspan="2">汇总标题</td></tr><tr><td>名称</td><td>详细说明</td></tr></table>'
    return Block('table', 'table', (100, 400, 900, 640), original_text=html, rows=2, cols=2,
                 cells=[Cell(0, 0, '汇总标题', colspan=2), Cell(1, 0, '名称'), Cell(1, 1, '详细说明')],
                 text_lines=[TextLine('汇总标题', (140, 420, 240, 450)),
                             TextLine('名称', (120, 510, 170, 540)),
                             TextLine('详细说明', (330, 510, 430, 540))])


def test_cell_geometry_matches_html_and_location_preserving_unequal_columns_and_spans(tmp_path):
    block = sample_table()
    boxes = [[100, 400, 900, 480], [100, 480, 300, 640], [300, 480, 900, 640]]
    attach_table_geometry(block, [
        {'pred_html': block.original_text, 'cell_box_list': [[100, 800, 900, 900]]*3},
        {'pred_html': block.original_text, 'cell_box_list': boxes},
    ])
    assert [cell.bbox for cell in block.cells] == [tuple(b) for b in boxes]
    page = PageResult('p', 0, 1000, 1400, 'unused', [block])
    doc = Document(export_docx([page], tmp_path/'table.docx'))
    table = doc.tables[0]
    assert abs(table.columns[1].width/table.columns[0].width-3) < .01
    assert table.cell(0, 0)._tc is table.cell(0, 1)._tc
    assert table.cell(1, 1).text == '详细说明'
    assert table._tbl.tblPr.find(qn('w:tblpPr')) is not None
    assert abs(table.rows[1].height/table.rows[0].height-2) < .01
    block.cells[1].edited_text = '人工增加完整的单元格说明'*20
    edited = Document(export_docx([page], tmp_path/'edited.docx'))
    assert edited.tables[0]._tbl.tblPr.find(qn('w:tblpPr')) is None
    assert edited.tables[0].cell(1, 0).text == block.cells[1].text


def test_contradictory_or_ambiguous_table_geometry_is_not_used():
    block = sample_table()
    boxes = [[100, 400, 900, 480], [100, 480, 300, 640], [500, 480, 900, 640]]
    attach_table_geometry(block, [{'pred_html': block.original_text, 'cell_box_list': boxes}])
    assert all(cell.bbox is None for cell in block.cells)
    boxes[2][0] = 300
    result = {'pred_html': block.original_text, 'cell_box_list': boxes}
    attach_table_geometry(block, [result, result])
    assert all(cell.bbox is None for cell in block.cells)
