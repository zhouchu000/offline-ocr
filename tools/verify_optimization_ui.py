"""Native Qt evidence and public-document measurements for 0.5.4."""
import argparse
import json
import statistics
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication
from offline_word import theme
from offline_word.domain import Block, FilteredText, ImageJob, PageResult, TextLine
from offline_word.exporter import export_docx
from offline_word.gui import MainWindow
from offline_word.security import SessionStore, deny_network
from tools.capture_glass_ui import populate, public_document


def run(output):
    output.mkdir(parents=True, exist_ok=False)
    app = QApplication([])
    theme.QSettings = lambda *a: QSettings(str(output/'appearance.ini'), QSettings.Format.IniFormat)
    theme.load_ui_font()
    source = output/'public.png'
    public_document(source)
    session = SessionStore()
    deny_network()
    before = time.perf_counter()
    w = MainWindow(Path(__file__).resolve().parents[1]/'models', session.path)
    report = {'constructor_ms': (time.perf_counter()-before)*1000, 'scope': 'Public synthetic documents; development PC, native Qt.', 'captures': []}
    w._confirm_discard_results = lambda *a, **k: True
    theme.set_effect(w, 'motion', False, persist=False)
    w.show()

    def capture(name, widget=w):
        assert widget.grab().save(str(output/(name+'.png')))
        report['captures'].append(name)

    def jobs_for(count):
        jobs = []
        for number in range(count):
            blocks = []
            for n in range(20):
                y = 50+n*54
                text = f'第{n+1}段公开测试文字，用于检查界面响应与文档输出，不包含个人文档。'
                lines = [TextLine(text,(60,y,780,y+20),.99), TextLine('原文内容需要完整保留，修改后应及时显示复核状态。',(60,y+23,780,y+43),.99)]
                blocks.append(Block(f'p{number}-b{n}','text',(60,y,780,y+43),'\n'.join(line.text for line in lines),confidence=.85 if n%5==0 else .99,text_lines=lines))
            page = PageResult(f'p{number}',number,840,1188,str(source),blocks)
            jobs.append(ImageJob(page.id,str(source),status='done',result=page))
        return jobs

    def flow():
        yield 100
        capture('01-first-frame')
        while w._model_task is not None:
            yield 50
        populate(w, source)
        w.show_document()
        w.select_block('body')
        for appearance in ('dark', 'light'):
            theme.set_appearance(w, appearance, persist=False)
            for width, height in ((1440,900),(960,560),(860,540)):
                w.resize(width,height)
                yield 150
                assert w.width() == width and w.height() == height, (w.size(), width,height)
                for control in (w.proofread_panel.next,w.proofread_panel.confirm,w.proofread_panel.apply):
                    assert control.isVisible() and w.rect().contains(control.mapTo(w,control.rect().bottomRight()))
                for control in (w.previous_page_button,w.page_combo,w.next_page_button,w.full_document_button):
                    assert not w.drawer.geometry().contains(control.mapTo(w.views,control.rect().center()))
                capture(f'02-review-{appearance}-{width}')
        w.resize(1100,680)
        theme.set_appearance(w,'dark',persist=False)
        w.current_job.result.filtered_text=[FilteredText('public-filter','待核对的原始过滤文字',(60,382,780,454),'body')]
        w.proofread_panel.refresh()
        w.proofread_panel.filtered.click()
        yield 100
        capture('03-filtered',w._filtered_dialog)
        w._filtered_dialog.accept()
        w.proofread_panel.confirm_and_next()
        w._locate_review_item('public-1','body')
        w.proofread_panel.edit.insertPlainText('修改')
        yield 50
        report['review_counts']={'header':w.review_summary_button.text(),'actual':len(w.review_items())}
        assert w.review_summary_button.text() == f'待复核 {len(w.review_items())}'
        w.appearance_button.click()
        yield 100
        capture('04-settings',w.appearance_dialog)
        w.appearance_dialog.reject()
        report['editing'] = []
        for count in (1,10,50,100):
            jobs=jobs_for(count)
            w.set_jobs(jobs)
            w.show_document()
            w.select_block(jobs[0].result.blocks[0].id)
            yield 50
            values=[]
            for _ in range(3):
                start=time.perf_counter()
                w.document_editor.insertPlainText('文')
                w._flush_review_changes()
                values.append((time.perf_counter()-start)*1000)
            report['editing'].append({'pages':count,'median_ms':statistics.median(values),'samples_ms':values})
        jobs=jobs_for(50)
        w.set_jobs(jobs)
        start=time.perf_counter()
        export_docx([job.result for job in jobs],session.path/'fifty.docx')
        report['source_export_50_ms']=(time.perf_counter()-start)*1000
        ticks=[]
        heartbeat=QTimer(w)
        heartbeat.setInterval(10)
        heartbeat.timeout.connect(lambda:ticks.append(time.perf_counter()))
        heartbeat.start()
        started=time.perf_counter()
        ticks.append(started)
        w.show_export_preview()
        yield 50
        if w.export_controller.dialog:
            capture('05-preview-progress',w.export_controller.dialog)
        while w.export_controller.busy:
            yield 25
        ticks.append(time.perf_counter())
        heartbeat.stop()
        report['preview_50_ms']=(time.perf_counter()-started)*1000
        report['preview_heartbeat_count']=len(ticks)
        report['preview_max_event_gap_ms']=max(b-a for a,b in zip(ticks,ticks[1:]))*1000
        assert w.views.currentWidget() is w.preview_view, w.status_label.text()
        assert w.preview_page_list.count()==len(w.preview_canvas._pages)==50
        capture('06-preview')
        report['pass']=True

    steps=flow()
    def advance():
        try:
            QTimer.singleShot(next(steps), advance)
        except StopIteration:
            w.close()
            app.quit()
        except Exception:
            report['error']=traceback.format_exc()
            w.close()
            app.quit()
    QTimer.singleShot(0,advance)
    app.exec()
    session.close()
    report['session_cleaned']=not session.path.exists()
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    return 0 if report.get('pass') else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    raise SystemExit(run(parser.parse_args().output.resolve()))
