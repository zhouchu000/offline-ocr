from pathlib import Path
from docx import Document
from docx.oxml.ns import qn
from offline_word.domain import Block, PageResult, TextLine
from offline_word.exporter import export_docx


def page():
    text = '原稿印刷文字用于排版验证'
    first = Block('recovered', 'text', (100,100,800,130), original_text=text,
                  text_lines=[TextLine(text,(100,100,800,130))])
    body = Block('body','text',(100,160,800,250),original_text=text*2,
                 text_lines=[TextLine(text,(100,160,800,190)), TextLine(text,(100,220,800,250))],
                 font_name='仿宋',bold_ranges=[(0,4)])
    footer = Block('footer','text',(490,1300,510,1320),original_text='7',
                   text_lines=[TextLine('7',(490,1300,510,1320))])
    return PageResult('p',0,1000,1400,'unused',[first,body,footer])


def test_recovered_lines_share_body_pitch_and_footer_stays_at_source_coordinate(tmp_path):
    doc=Document(export_docx([page(),page()],tmp_path/'source.docx'))
    paragraphs=[p for p in doc.paragraphs if p.text]
    assert len(paragraphs)==6
    assert paragraphs[0].paragraph_format.line_spacing == paragraphs[1].paragraph_format.line_spacing
    for p in paragraphs:
        frame=p._p.pPr.find(qn('w:framePr'))
        assert frame is not None
        assert frame.get(qn('w:hRule'))=='auto'
        assert frame.get(qn('w:h')) is None
    footer_frame=paragraphs[2]._p.pPr.find(qn('w:framePr'))
    assert int(footer_frame.get(qn('w:y'))) > 15000
    anchors=[p for p in doc.paragraphs if not p.text]
    assert len(anchors)==2 and anchors[1].paragraph_format.page_break_before
    assert any(r.bold for r in paragraphs[1].runs)


def test_flow_export_and_expanded_manual_edits_remain_editable_without_frames(tmp_path):
    source=page()
    flow=Document(export_docx([source],tmp_path/'flow.docx',layout_mode='flow'))
    assert all(p._p.pPr.find(qn('w:framePr')) is None for p in flow.paragraphs)
    source.blocks[1].edited_text='人工添加的大段正文'*30
    edited=Document(export_docx([source],tmp_path/'edited.docx'))
    assert source.blocks[1].edited_text in [p.text for p in edited.paragraphs]
    assert all(p._p.pPr.find(qn('w:framePr')) is None for p in edited.paragraphs)


def test_manual_line_breaks_cannot_expand_a_frame_over_its_neighbour(tmp_path):
    source = page()
    source.blocks[0].edited_text = '第一行\n第二行\n第三行'
    doc = Document(export_docx([source], tmp_path/'breaks.docx'))
    assert source.blocks[0].edited_text in [p.text for p in doc.paragraphs]
    assert all(p._p.pPr.find(qn('w:framePr')) is None for p in doc.paragraphs)
