from dataclasses import asdict

from PIL import Image
import pytest
from PySide6.QtCore import QSettings

from offline_word import theme
from offline_word.domain import Block, ImageJob, PageResult
from offline_word.gui import MainWindow
from offline_word.paper import PaperDetection


@pytest.fixture
def window(qtbot,tmp_path,monkeypatch):
    monkeypatch.setattr(theme,'QSettings',lambda *args:QSettings(str(tmp_path/'appearance.ini'),QSettings.Format.IniFormat))
    source = tmp_path/'source.png'
    Image.new('RGB',(300,400),'white').save(source)
    w = MainWindow(tmp_path/'models',tmp_path/'session')
    qtbot.waitUntil(lambda: w._model_task is None)
    w._confirm_discard_results = lambda *a,**k:True
    w._models_ready = True
    w.set_jobs([ImageJob('p',str(source))])
    w.show()
    qtbot.addWidget(w)
    yield w
    w._worker = None
    w._confirm_discard_results = lambda *a,**k:True
    w.close()


def test_parallel_settings_preserve_precise_and_edited_text(window):
    w = window
    job = w.current_job
    job.result = PageResult(job.id,0,300,400,job.source_path,[Block('b','text',(20,20,200,100),'原文',edited_text='人工修改')])
    before = asdict(job.result)
    w.auto_proofread_checkbox.setChecked(True)
    w.profile_combo.setCurrentIndex(w.profile_combo.findData('parallel'))
    w.recognition_controls.apply_to_job(job)
    assert job.auto_proofread and not job.text_only
    assert job.performance_profile == 'parallel'
    assert asdict(job.result) == before
    assert w.auto_proofread_checkbox.isEnabled()


def test_confirm_paper_is_the_only_automatic_geometry_application(window):
    w = window
    job = w.current_job
    job.mode = 'photo'
    job.paper_status = 'suggested'
    job.paper_quad = [(20,20),(270,20),(270,370),(20,370)]
    w._refresh_current_page()
    assert job.crop is None and job.perspective is None
    assert w.paper_bar.isVisible()
    assert w.source_preview._paper_quad == job.paper_quad
    w.accept_paper_button.click()
    assert job.paper_status == 'confirmed'
    assert job.perspective == job.paper_quad
    assert not w.paper_bar.isVisible()


def test_declining_invalidation_preserves_edits_and_unconfirmed_paper(window):
    w = window
    job = w.current_job
    job.mode = 'photo'
    job.paper_status = 'suggested'
    job.paper_quad = [(20,20),(270,20),(270,370),(20,370)]
    job.result = PageResult(job.id,0,300,400,job.source_path,[Block('b','text',(20,20,200,100),'原文',edited_text='人工修改')])
    w._confirm_invalidation = lambda:False
    w.recognition_controls.accept_paper()
    assert job.perspective is None and job.paper_status == 'suggested'
    assert job.result.blocks[0].text == '人工修改'


def test_stale_detector_cannot_overwrite_manual_crop_or_removed_job(window):
    w = window
    job = w.current_job
    job.mode, job.paper_status = 'photo','detecting'
    result = PaperDetection([(20,20),(270,20),(270,370),(20,370)],.9,'suggested')
    assert w.apply_crop((5,5,295,395))
    w.recognition_controls._detected(job,result)
    assert job.paper_status == 'manual' and job.paper_quad is None
    w.set_jobs([])
    w.recognition_controls._detected(job,result)
    assert w.current_job is None


def test_keep_whole_image_and_scan_mode_clear_photo_hint(window):
    w = window
    job = w.current_job
    job.mode,job.paper_status = 'photo','suggested'
    job.paper_quad = [(20,20),(270,20),(270,370),(20,370)]
    w._refresh_current_page()
    w.keep_paper_button.click()
    assert job.paper_status == 'kept' and job.perspective is None
    w.mode_combo.setCurrentIndex(0)
    assert job.mode == 'scan' and job.paper_quad is None and not w.paper_bar.isVisible()


def test_parallel_selection_starts_pool_and_locks_settings_but_keeps_import(window,monkeypatch):
    w = window
    created = []
    class Pool:
        lane_count = 2
        busy = True
        def __init__(self,*args,**kwargs): created.append(self)
        def start(self,jobs): self.jobs = jobs
    monkeypatch.setattr('offline_word.parallel_worker.ParallelOCRWorker',Pool)
    w.profile_combo.setCurrentIndex(w.profile_combo.findData('parallel'))
    w.start_processing()
    assert w._worker is created[0]
    assert w._worker.jobs[0].performance_profile == 'parallel'
    assert not w.profile_combo.isEnabled() and not w.auto_proofread_checkbox.isEnabled()
    assert w.import_button.isEnabled()
    w._poll_timer.stop()


def test_diagnostics_event_gate_and_disabling_clears_records(window):
    event = {'event':'diagnostic','stage':'recognize','elapsed_ms':3,'rss_mb':1024,'text':'私密正文'}
    window._handle_worker_event(event)
    assert not window.recognition_controls.diagnostics.events
    window.diagnostics_checkbox.setChecked(True)
    window._handle_worker_event(event)
    assert len(window.recognition_controls.diagnostics.events) == 1
    assert '私密' not in window.recognition_controls.diagnostics.text()
    window.recognition_controls.show_diagnostics()
    dialog = window.recognition_controls.diagnostic_dialog
    window.recognition_controls.show_diagnostics()
    assert window.recognition_controls.diagnostic_dialog is dialog
    window.diagnostics_checkbox.setChecked(False)
    assert not window.recognition_controls.diagnostics.events
    assert window.recognition_controls.diagnostic_dialog is None
