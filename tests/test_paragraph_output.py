from copy import deepcopy

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
import pytest

from offline_word.domain import Block, Cell, PageResult, TextLine
from offline_word.exporter import export_docx
from offline_word.paragraph_output import paragraph_page, continues_on_next_page
from offline_word.text_output import unwrap_text


def block(id, rows, *, kind="text", bold=False):
    lines = [TextLine(text,box) for text,box in rows]
    box = (min(line.bbox[0] for line in lines),min(line.bbox[1] for line in lines),
           max(line.bbox[2] for line in lines),max(line.bbox[3] for line in lines))
    return Block(id,kind,box,"\n".join(line.text for line in lines),text_lines=lines,font_name="仿宋",font_bold=bold)


def page(blocks, index=0):
    return PageResult(f"p{index}",index,1000,1400,"unused",blocks)


def test_paragraph_mode_preserves_styles_and_paragraphs_but_not_printed_wraps(tmp_path):
    heading = block("title",[("文档标题",(300,100,700,150))],kind="title",bold=True)
    body = block("body",[("正文第一行需要保留",(160,220,480,250)),("段落结构，后续内容自动换行。",(100,275,480,305))])
    body.bold_ranges = [(0,4)]
    next_body = block("next",[("这是另一个大段落。",(160,380,880,410))])
    pages = [page([heading,body,next_body])]
    before = deepcopy(pages)
    doc = Document(export_docx(pages,tmp_path/'paragraph.docx',layout_mode='paragraph'))
    assert [p.text for p in doc.paragraphs] == ['文档标题','正文第一行需要保留段落结构，后续内容自动换行。','这是另一个大段落。']
    assert doc.paragraphs[0].style.name == 'Heading 1'
    assert doc.paragraphs[0].alignment == WD_ALIGN_PARAGRAPH.CENTER
    assert doc.paragraphs[1].paragraph_format.first_line_indent.pt > 0
    assert doc.paragraphs[1].paragraph_format.line_spacing_rule == WD_LINE_SPACING.AT_LEAST
    assert ''.join(run.text for run in doc.paragraphs[1].runs if run.bold) == '正文第一'
    assert all(run.font.name == '仿宋' for p in doc.paragraphs for run in p.runs)
    assert '<w:br' not in doc.element.xml and '<w:framePr' not in doc.element.xml
    assert pages == before


def test_paragraph_mode_keeps_manual_breaks_and_table_structure(tmp_path):
    body = block('body',[("原始第一行",(100,200,900,230)),("原始第二行",(100,250,900,280))])
    body.edited_text = '手动分段\n请保留此换行'
    table = Block('table','table',(100,500,900,800),rows=1,cols=2,cells=[Cell(0,0,'数量'),Cell(0,1,'12')])
    doc = Document(export_docx([page([body,table])],tmp_path/'edited.docx',layout_mode='paragraph'))
    assert doc.paragraphs[0].text == body.edited_text
    assert len(doc.tables) == 1 and doc.tables[0].cell(0,1).text == '12'


def test_recovered_rows_rejoin_the_original_paragraph_without_mutating_or_losing_bold():
    recovered = [block(f'p:recovered-{i}',[(f'恢复的第{i+1}行继续', (100,100+i*50,900,140+i*50))]) for i in range(3)]
    tail = block('tail',[("原段落最后一行。",(100,250,650,290))])
    tail.bold_ranges = [(4,7)]
    next_body = block('new',[("下一段的正文。",(180,305,900,345))])
    original = page([*recovered,tail,next_body])
    before = deepcopy(original)
    joined = paragraph_page(original)
    assert len(joined.blocks) == 2
    first = joined.blocks[0]
    assert unwrap_text(first.text) == ''.join(unwrap_text(b.text) for b in recovered+[tail])
    assert len(first.text_lines) == 4
    assert ''.join(first.text[a:b] for a,b in first.bold_ranges) == '后一行'
    assert original == before


def test_recovered_middle_row_is_inserted_inside_a_paragraph():
    body = block('body',[("前面一行正文",(100,100,900,130)),("最后一行正文。",(100,200,900,230))])
    recovered = block('p:recovered-0',[("中间恢复的文字",(100,150,900,180))])
    joined = paragraph_page(page([body,recovered]))
    assert len(joined.blocks) == 1
    assert joined.blocks[0].text == '前面一行正文\n中间恢复的文字\n最后一行正文。'


@pytest.mark.parametrize('condition',['stop','heading','indented','edited','column','gap'])
def test_recovery_does_not_merge_distinct_or_untrusted_paragraphs(condition):
    recovered = block('p:recovered-0',[("前一段恢复的文字",(100,100,900,140))])
    body = block('body',[("后一段正文内容",(100,150,900,190))])
    if condition == 'stop':
        recovered.original_text += '。'
        recovered.text_lines[0].text += '。'
    elif condition == 'heading':
        body.font_bold = True
    elif condition == 'indented':
        body.text_lines[0].bbox = (180,150,900,190)
        body.bbox = (180,150,900,190)
    elif condition == 'edited':
        recovered.edited_text = '用户已改写的内容'
    elif condition == 'column':
        body.bbox = (600,150,950,190)
        body.text_lines[0].bbox = body.bbox
    else:
        body.bbox = (100,250,900,290)
        body.text_lines[0].bbox = body.bbox
    assert len(paragraph_page(page([recovered,body])).blocks) == 2


def test_cross_image_continuation_has_no_page_or_paragraph_break(tmp_path):
    prior = block('prior',[("跨图片的大段落保持连",(100,1100,900,1140))])
    following = block('following',[("续，标题与新段落仍然保留。",(100,100,900,140))])
    doc = Document(export_docx([page([prior]),page([following],1)],tmp_path/'joined.docx',layout_mode='paragraph'))
    assert len(doc.paragraphs) == 1
    assert doc.paragraphs[0].text == '跨图片的大段落保持连续，标题与新段落仍然保留。'
    assert '<w:pageBreakBefore' not in doc.element.xml and '<w:br' not in doc.element.xml


def test_distinct_paragraphs_on_different_images_still_use_natural_pagination(tmp_path):
    prior = block('prior',[("这是前一页的完整段落。",(100,1100,900,1140))])
    following = block('following',[("这是下一页的新段落。",(180,100,900,140))])
    assert not continues_on_next_page(prior,following)
    doc = Document(export_docx([page([prior]),page([following],1)],tmp_path/'separate.docx',layout_mode='paragraph'))
    assert len(doc.paragraphs) == 2
    assert '<w:pageBreakBefore' not in doc.element.xml
    assert doc.paragraphs[1].paragraph_format.space_before.pt == 0


def test_english_word_spaces_survive_source_line_unwrapping_and_page_continuation(tmp_path):
    assert unwrap_text('Hello\nworld\n中文\n段落') == 'Hello world中文段落'
    prior = block('prior',[("The document continues",(100,1000,900,1040))])
    following = block('following',[("with words kept intact.",(100,100,900,140))])
    doc = Document(export_docx([page([prior]),page([following],1)],tmp_path/'english.docx',layout_mode='paragraph'))
    assert doc.paragraphs[0].text == 'The document continues with words kept intact.'


def test_paragraph_mode_supports_optional_page_number_removal(tmp_path):
    body = block('body',[("本页保留 2026 年与 12 项。",(100,200,900,240))])
    number = block('number',[("12",(480,1320,520,1340))])
    doc = Document(export_docx([page([body,number])],tmp_path/'numbers.docx',layout_mode='paragraph',remove_page_numbers=True))
    assert [p.text for p in doc.paragraphs] == [body.text]


def test_single_row_at_page_bottom_uses_measured_continuation_leading(tmp_path):
    first = block('first',[("这段文字在前一页底部开始并且继续",(160,1100,640,1130))])
    following = block('next',[("下一页延续的正文应当保持正常行距",(100,100,640,130)),
                              ("不能因为前面只有一行就挤在一起。",(100,150,640,180))])
    doc = Document(export_docx([page([first]),page([following],1)],tmp_path/'leading.docx',layout_mode='paragraph'))
    assert len(doc.paragraphs) == 1
    pf = doc.paragraphs[0].paragraph_format
    assert pf.line_spacing.pt >= 29
    assert pf.line_spacing_rule == WD_LINE_SPACING.AT_LEAST
    assert pf.first_line_indent.pt > 0


def test_ambiguous_recovery_between_two_paragraphs_stays_independent():
    before = block('before',[("前一段可能还在继续",(100,100,900,140))])
    recovered = block('p:recovered-0',[("这一行归属不明确",(100,150,900,190))])
    after = block('after',[("后一段也可能有关联",(100,200,900,240))])
    assert len(paragraph_page(page([before,recovered,after])).blocks) == 3


def test_reflow_does_not_split_known_words_at_geometry_only_line_boundaries(tmp_path):
    body = block('body',[("Interna",(100,200,500,230)),("tional cooperation continues",(100,250,900,280))])
    body.original_text = 'International cooperation continues'
    doc = Document(export_docx([page([body])],tmp_path/'known-word.docx',layout_mode='paragraph'))
    assert doc.paragraphs[0].text == body.original_text
    recovered = block('p:recovered-0',[("随后补回的完整文字。",(100,300,900,330))])
    joined = paragraph_page(page([body,recovered]))
    assert len(joined.blocks) == 1
    assert 'International' in joined.blocks[0].text
    assert 'Interna\ntional' not in joined.blocks[0].text
