"""Render public native UI evidence; never import user documents or OCR models."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAbstractTextDocumentLayout, QColor, QFont, QImage, QPainter, QPalette, QTextDocument
from PySide6.QtWidgets import QApplication
from offline_word.domain import Block, Cell, ImageJob, PageResult, TextLine
from offline_word.gui import MainWindow
from offline_word.security import SessionStore, deny_network
from offline_word.theme import load_ui_font, set_appearance, set_effect


def public_document(path):
    doc = QTextDocument()
    doc.setDefaultFont(QFont('Noto Sans SC',14))
    doc.setHtml('''<style>body{font-family:Microsoft YaHei UI;font-size:20px;color:#25282b}
    h1{text-align:center;font-family:SimSun;font-size:38px} h3{font-size:22px} p{line-height:160%}</style>
    <h1>服务合作协议</h1><p align="center">合同编号：HT-2024-0892</p><br>
    <p>甲方：北京恒信科技有限公司<br>乙方：上海睿诚信息技术有限公司</p>
    <h3>第一条　合作内容</h3><p>1.1　乙方根据甲方的需求，向甲方提供图像文字识别服务，包括但不限于算法部署、技术支持及相关培训。</p>
    <h3>第二条　合作期限</h3><p>2.1　本协议自双方签字盖章之日起生效，有效期为 12 个月。<br>2.2　如需续签，双方应另行协商。</p>
    <h3>第三条　服务费用</h3><p>3.1　本项目服务费用总计人民币伍拾万元整。</p>
    <table border="1" cellspacing="0" cellpadding="10" width="100%"><tr><td>付款阶段</td><td>比例</td><td>金额（元）</td></tr>
    <tr><td>第一期</td><td>30%</td><td>150,000</td></tr><tr><td>第二期</td><td>40%</td><td>200,000</td></tr>
    <tr><td>第三期</td><td>30%</td><td>150,000</td></tr></table><p align="center">— 2 —</p>''')
    doc.setTextWidth(720)
    image=QImage(840,1188,QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    paint=QPainter(image)
    paint.translate(60,50)
    context=QAbstractTextDocumentLayout.PaintContext()
    context.palette.setColor(QPalette.ColorRole.Text,QColor('#25282b'))
    doc.documentLayout().draw(paint,context)
    paint.end()
    assert image.save(str(path))


def populate(window, source):
    window.set_jobs([ImageJob(f'public-{i}',str(source)) for i in range(4)])
    for i, job in enumerate(window.jobs):
        blocks=[Block('title','title',(80,50,760,115),'服务合作协议',confidence=.99,
                      text_lines=[TextLine('服务合作协议',(80,50,760,115))]),
                Block('body','text',(60,382,780,454),
                      '乙方根据甲方的需求，向甲方提供图像文字识别服务，包括但不限于昇算算法部署、技术支持及相关培训。',
                      confidence=.85,needs_review=True,candidates=['乙方根据甲方的需求，向甲方提供图像文字识别服务，包括但不限于算法部署、技术支持及相关培训。']),
                Block('table','table',(60,850,780,1100),cells=[Cell(0,0,'付款阶段'),Cell(0,1,'比例'),Cell(0,2,'金额（元）'),
                      Cell(1,0,'第一期'),Cell(1,1,'30%'),Cell(1,2,'150,000')],rows=2,cols=3)]
        window.apply_page_result(job.id,PageResult(job.id,i,840,1188,str(source),blocks))
    window.queue_list.setCurrentRow(1)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    app=QApplication([])
    load_ui_font()
    public_document(args.output/'public-contract.png')
    session=SessionStore()
    deny_network()
    window=MainWindow(Path(__file__).resolve().parents[1]/'models',session.path)
    window._confirm_discard_results=lambda *a,**k:True
    populate(window,args.output/'public-contract.png')
    window.resize(1280,828)
    window.show()
    window.raise_()
    window.activateWindow()
    states=iter([(theme,view,size) for theme in ('light','dark')
                 for size in ((1280,828),(1100,680))
                 for view in ('document','proofread','settings','preview','empty')])
    report={'states':[]}
    def advance():
        try:
            theme,view,size=next(states)
        except StopIteration:
            window.close()
            session.close()
            report['session_cleaned']=not session.path.exists()
            (args.output/'capture.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            app.quit()
            return
        window.resize(*size)
        set_appearance(window,theme,persist=False)
        set_effect(window,'motion',False,persist=False)
        if view == 'empty':
            window.show_gallery()
            window.source_stack.setCurrentWidget(window.empty_stage)
        elif view in ('document','proofread'):
            window.show_document()
            if view=='proofread':window.select_block('body')
        elif view=='settings':window.show_recognition_settings()
        else:window.show_export_preview()
        def capture():
            name=f'{view}-{theme}-{size[0]}'
            assert window.grab().toImage().scaled(window.size(),Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation).save(str(args.output/f'{name}.png'))
            if window._native_frame:
                # Layered HWND BitBlt is black on Windows. Capture only our own
                # foreground rectangle from the composed screen instead.
                import ctypes
                ctypes.windll.user32.GetForegroundWindow.restype=ctypes.c_void_p
                if ctypes.windll.user32.GetForegroundWindow()==int(window.winId()):
                    region=window.frameGeometry()
                    assert window.screen().grabWindow(0,region.x(),region.y(),region.width(),region.height()).save(str(args.output/f'{name}-composed.png'))
            report['states'].append({'name':name,'size':[window.width(),window.height()],
                                     'dwm':window._dwm_result,'drawer':window.drawer.isVisible(),
                                     'dpi':window.devicePixelRatioF()})
            QTimer.singleShot(20,advance)
        QTimer.singleShot(250,capture)
    QTimer.singleShot(250,advance)
    QTimer.singleShot(30000,app.quit)
    result=app.exec()
    print(json.dumps(report,ensure_ascii=False))
    return result


if __name__=='__main__':raise SystemExit(main())
