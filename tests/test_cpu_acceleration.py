from pathlib import Path

import pytest
from PIL import Image

from offline_word import worker


@pytest.mark.parametrize("options", [worker.pipeline_options, worker.plain_ocr_options, worker.detail_recognition_options])
def test_safe_retry_keeps_models_resolution_and_cpu_bound(tmp_path, options):
    accelerated = options(tmp_path, platform="nt")
    safe = options(tmp_path, platform="nt", safe_cpu=True)
    assert safe["enable_mkldnn"] is False and "engine_config" not in safe
    assert {key: value for key, value in accelerated.items() if key.endswith(("_dir", "_name"))} == {
        key: value for key, value in safe.items() if key.endswith(("_dir", "_name"))}
    assert accelerated.get("text_det_limit_side_len") == safe.get("text_det_limit_side_len")
    assert 1 <= safe["cpu_threads"] <= 4
    assert accelerated["engine_config"]["cpu_threads"] == safe["cpu_threads"]


@pytest.mark.skipif(worker.os.name != "nt", reason="Windows inference compatibility fallback")
@pytest.mark.parametrize("mode", ["plain", "structure", "detail"])
def test_accelerator_failure_retries_same_model_once_and_reuses_safe_engine(tmp_path, monkeypatch, mode):
    image = tmp_path / "line.png"
    Image.new("RGB", (100, 30), "white").save(image)
    calls = []
    class Engine:
        def __init__(self, safe): self.safe = safe
        def predict(self, _):
            calls.append(self.safe)
            if not self.safe:
                raise NotImplementedError("ConvertPirAttribute2RuntimeAttribute / oneDNN")
            yield {"rec_text": "原有文字保持完整", "rec_score": .99, "rec_texts": ["原有文字保持完整"]}
    def factory(*_, safe_cpu=False): return Engine(safe_cpu)
    monkeypatch.setattr(worker, "_create_plain_engine", factory)
    monkeypatch.setattr(worker, "_create_engine", factory)
    monkeypatch.setattr(worker, "_create_detail_engine", factory)
    runtime = worker._PredictionRuntime(tmp_path, tmp_path, Engine(False) if mode == "structure" else None)
    for _ in range(2):
        if mode == "detail":
            assert runtime.predict_detail(image).text == "原有文字保持完整"
        else:
            actual_mode, prediction = runtime.predict_baseline(image, text_only=mode == "plain")
            assert actual_mode == mode and prediction["rec_texts"] == ["原有文字保持完整"]
    assert calls == [False, True, True]


@pytest.mark.skipif(worker.os.name != "nt", reason="Windows inference compatibility fallback")
def test_cpu_initialization_failure_can_also_fall_back(tmp_path):
    calls = []
    def constructor(**options):
        calls.append(options["enable_mkldnn"])
        if options["enable_mkldnn"]:
            raise RuntimeError("oneDNN could not initialize")
        return options
    result = worker._build_cpu_engine(constructor, worker.plain_ocr_options, tmp_path, safe_cpu=False)
    assert calls == [True, False] and result["device"] == "cpu"
    assert result["text_recognition_model_dir"].endswith("PP-OCRv6_medium_rec")
