import json
from pathlib import Path
import tomllib

import pytest
from PySide6.QtCore import QAbstractAnimation, QSettings, Qt
from PySide6.QtWidgets import QApplication, QLabel

from offline_word import theme
from offline_word.fluent import DesktopEffects
from offline_word.gui import MainWindow
from offline_word.release_dialog import ReleasePreferences, show_release_dialog
from offline_word.releases import __version__, load_releases


def test_version_has_notes_and_all_builders_use_the_same_version():
    from tools import build_macos, build_portable, build_windows_installer, windows_dev
    assert build_macos.VERSION == build_portable.VERSION == build_windows_installer.VERSION == windows_dev.__version__ == __version__
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root/'pyproject.toml').read_text(encoding='utf-8'))
    assert 'version' not in project['project']
    assert project['tool']['setuptools']['dynamic']['version']['attr'] == 'offline_word.version.__version__'
    assert load_releases()[0].version == __version__


@pytest.mark.parametrize('change,error', [('missing', 'current_release_notes_missing'), ('duplicate', 'duplicate_release_version'), ('empty', 'invalid_release_changes')])
def test_release_catalog_fails_build_if_notes_are_missing_or_invalid(tmp_path, change, error):
    values = [{'version': __version__, 'date': '2026-09-14', 'title': '版本更新', 'changes': ['更新内容']}]
    if change == 'missing': values[0]['version'] = '0.0.1'
    if change == 'duplicate': values.append(dict(values[0]))
    if change == 'empty': values[0]['changes'] = []
    path = tmp_path/'notes.json'
    path.write_text(json.dumps(values), encoding='utf-8')
    with pytest.raises(ValueError, match=error):
        load_releases(path)


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(theme, 'QSettings', lambda *a: QSettings(str(tmp_path/'appearance.ini'), QSettings.Format.IniFormat))
    w = MainWindow(tmp_path/'models', tmp_path/'session')
    w._effects = DesktopEffects(False, True, True)
    w.show()
    qtbot.addWidget(w)
    yield w
    if getattr(w, 'release_dialog', None): w.release_dialog.reject()
    w.close()


def prefs(tmp_path):
    return ReleasePreferences(QSettings(str(tmp_path/'versions.ini'), QSettings.Format.IniFormat))


@pytest.mark.parametrize('mode', ['light', 'dark'])
def test_first_launch_two_pages_history_and_no_second_prompt(window, qtbot, tmp_path, mode):
    theme.set_appearance(window, mode, persist=False)
    window.resize(1100, 680)
    preferences = prefs(tmp_path)
    dialog = show_release_dialog(window, welcome=True, preferences=preferences)
    qtbot.waitUntil(dialog.isVisible)
    assert dialog.pages.currentIndex() == 0
    assert not preferences.has_seen()
    assert show_release_dialog(window, welcome=True, preferences=preferences) is dialog
    dialog.next_button.click()
    qtbot.waitUntil(lambda: dialog.motion.layer._image.isNull(), timeout=2000)
    assert dialog.pages.currentIndex() == 1
    assert dialog.motion.layer._image.isNull()
    assert dialog.notes_scroll.horizontalScrollBar().maximum() == 0
    dialog.version_combo.setCurrentIndex(dialog.version_combo.findData('0.3.10'))
    assert '玻璃' in dialog.notes_title.text()
    assert any('Windows 11' in label.text() for label in dialog.findChildren(QLabel))
    dialog.back_button.click()
    dialog.next_button.click()
    dialog.next_button.click()
    assert preferences.has_seen()
    assert set(preferences.settings.allKeys()) == {f'seen/{__version__}'}
    assert show_release_dialog(window, welcome=True, preferences=preferences) is None
    history = show_release_dialog(window, preferences=preferences)
    assert history.pages.currentIndex() == 1
    history.reject()


def test_previous_version_seen_does_not_suppress_new_update_and_escape_marks_seen(window, qtbot, tmp_path):
    preferences = prefs(tmp_path)
    preferences.mark_seen('0.3.10')
    window.setProperty('motionEnabled', False)
    dialog = show_release_dialog(window, welcome=True, preferences=preferences)
    assert dialog.entrance.state() == QAbstractAnimation.State.Stopped
    dialog.next_button.click()
    assert dialog.motion.layer._image.isNull()
    qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    assert preferences.has_seen()


def test_history_button_does_not_mark_the_version_seen(window, tmp_path):
    preferences = prefs(tmp_path)
    dialog = show_release_dialog(window, preferences=preferences)
    dialog.reject()
    assert not preferences.has_seen()


@pytest.mark.parametrize('from_settings', [False, True])
@pytest.mark.parametrize('selection_delay', ['immediate', 'settled'])
def test_history_opened_from_settings_accepts_mouse_version_selection(window, qtbot, from_settings, selection_delay):
    window.setProperty('motionEnabled', False)
    if from_settings:
        qtbot.mouseClick(window.appearance_button, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(window.appearance_dialog.isVisible)
        qtbot.mouseClick(window.release_history_button, Qt.MouseButton.LeftButton)
        dialog = window.release_dialog
    else:
        dialog = show_release_dialog(window)
    qtbot.waitUntil(dialog.isVisible)
    assert QApplication.activeModalWidget() is dialog
    combo = dialog.version_combo
    qtbot.mouseClick(combo, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(combo.view().isVisible)
    target = combo.findData('0.3.10')
    index = combo.model().index(target, 0)
    combo.view().scrollTo(index)
    qtbot.wait(50)
    assert combo.view().viewport().rect().contains(combo.view().visualRect(index).center())
    assert combo.view().indexAt(combo.view().visualRect(index).center()) == index
    assert combo.view().viewport().isEnabled()
    qtbot.mouseMove(combo.view().viewport(), combo.view().visualRect(index).center())
    if selection_delay == 'settled':
        qtbot.wait(QApplication.doubleClickInterval() + 20)
    qtbot.mouseClick(combo.view().viewport(), Qt.MouseButton.LeftButton,
                     pos=combo.view().visualRect(index).center())
    qtbot.waitUntil(lambda: combo.currentData() == '0.3.10', timeout=1000)
    assert '玻璃' in dialog.notes_title.text()
    dialog.reject()
    if from_settings:
        qtbot.waitUntil(lambda: QApplication.activeModalWidget() is window.appearance_dialog)
        assert window.appearance_dialog.isVisible()
        window.appearance_dialog.reject()


def test_history_keyboard_selection_updates_title_and_body(window, qtbot):
    window.setProperty('motionEnabled', False)
    window.appearance_dialog.open()
    window.release_history_button.click()
    dialog = window.release_dialog
    qtbot.waitUntil(dialog.isVisible)
    combo = dialog.version_combo
    combo.setFocus()
    qtbot.keyClick(combo, Qt.Key.Key_Down)
    selected = dialog.releases[1]
    assert combo.currentData() == selected.version
    assert dialog.notes_title.text() == selected.title
    body = [label.text() for label in dialog.notes_scroll.widget().findChildren(QLabel)]
    assert all(change in body for change in selected.changes)
    dialog.reject()
    window.appearance_dialog.reject()
