"""Native Qt checks of manual encrypted projects and focused review controls."""
import argparse
import json
from pathlib import Path
import traceback

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from offline_word.domain import Block, ImageJob, PageResult, TextLine
from offline_word.gui import MainWindow
from offline_word.project_ui import PasswordDialog
from offline_word.security import SessionStore, deny_network
from offline_word.theme import set_appearance
from tools.capture_glass_ui import public_document


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    app = QApplication([])
    session = SessionStore()
    deny_network()
    source = args.output/'public.png'
    public_document(source)
    window = MainWindow(Path('models'), session.path)
    window._confirm_discard_results = lambda *a, **k: True
    window.resize(1100,680)
    window.set_jobs([ImageJob('public', str(source))])
    body = '这是一段公开测试文字，用于验证校对和加密项目保存。'
    page = PageResult('public',0,840,1188,str(source),[
        Block('public:0','text',(50,160,760,190),body, confidence=.85,
              text_lines=[TextLine(body,(50,160,760,190))]),
        Block('public:recovered-0','text',(52,166,760,196),body, recognition_source='recovered',
              text_lines=[TextLine(body,(52,166,760,196))])])
    window.apply_page_result('public', page)
    window.show()
    report = {'pass':False,'states':[]}
    def capture(name, widget=window):
        assert widget.grab().save(str(args.output/(name+'.png')))
        report['states'].append(name)
    def steps():
        for theme in ('light','dark'):
            set_appearance(window,theme,persist=False)
            yield 250
            capture('workspace-'+theme)
            password = PasswordDialog(window,saving=True)
            password.open()
            yield 200
            capture('password-'+theme,password)
            password.reject()
            password.deleteLater()
            window._render_review_summary()
            yield 200
            capture('review-'+theme,window._review_dialog)
            assert window.review_list.count()==2
            window._review_dialog.reject()
            window.request_export(separate=False)
            yield 200
            capture('export-check-'+theme,window._export_check_dialog)
            window._export_check_dialog.reject()
        controller=window.project_controller
        target=args.output/'public.ocrproj'
        controller.start(target,'公开验收项目口令-050',saving=True)
        while controller.busy: yield 50
        assert target.is_file() and not controller.dirty()
        window.jobs[0].result.blocks[0].edited_text='人工校对后的公开内容'
        controller.start(target,'错误口令-1234567890',saving=False)
        while controller.busy: yield 50
        assert window.jobs[0].result.blocks[0].text=='人工校对后的公开内容'
        controller.start(target,'公开验收项目口令-050',saving=False)
        while controller.busy: yield 50
        assert window.jobs[0].result.blocks[0].text==body
        assert window.jobs[0].display_name=='public.png'
        window._locate_review_item('public','public:recovered-0')
        window.proofread_panel.confirm_and_next()
        assert window.jobs[0].result.blocks[1].review_confirmed_signature
        yield 250
        capture('restored-project')
        report['pass']=True
    iterator=steps()
    def advance():
        try:
            delay=next(iterator)
        except StopIteration:
            finish()
        except Exception:
            report['error']=traceback.format_exc()
            finish()
        else:
            QTimer.singleShot(delay,advance)
    def finish():
        window.close()
        app.quit()
    QTimer.singleShot(200,advance)
    app.exec()
    session.close()
    report['session_cleaned']=not session.path.exists()
    (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    return 0 if report['pass'] else 1


if __name__=='__main__':
    raise SystemExit(main())
