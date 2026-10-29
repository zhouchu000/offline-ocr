"""Reproducible, offline OCR comparison on generated degraded office pages."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import multiprocessing
from pathlib import Path
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from offline_word.domain import ImageJob
from offline_word.security import SessionStore, configure_offline_env, deny_network
from offline_word.version import __version__
from offline_word.warm_worker import WarmWorkerCache
from tools.benchmark import cer, extract_effective_text


TEXTS = (
    '资料归档后应检查页码、附件和原始编号。',
    '采购金额为1,024.50元，审批编号为AB-2026。',
    '检测记录应保留，不得覆盖人工修订内容。',
    '请在2个工作日内核对数据，并填写复核结论。',
    '离线识别支持中文、English及版本号V3.12。',
    '办公设备每月检查一次，异常情况及时登记。',
)
HOLDOUT_TEXTS = (
    '请保留票据原件，复印件应标注用途及日期。',
    '样本编号为XZ-4087，温度记录为23.6摄氏度。',
    '会议安排在星期五上午九点，地点另行通知。',
    '复核范围包含正文、批注、表格及附件目录。',
    '库存数量是375件，实付金额为8,062.15元。',
    '设备型号为RX-17，英文标识为Sample Test。',
)
CASES = ('clear', 'small', 'defocus', 'motion', 'low_contrast', 'shadow_noise', 'jpeg', 'faint_blur')


def generate(output: Path, *, holdout: bool = False) -> list[dict]:
    """No external documents, network assets or private reference text."""
    output.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    font_path = root / 'src/offline_word/assets/MiSans-Regular.ttf'
    texts = HOLDOUT_TEXTS if holdout else TEXTS
    rows = []
    for kind in CASES:
        size = 12 if kind == 'small' else 20
        font = ImageFont.truetype(str(font_path), size)
        width = int(max(font.getlength(line) for line in texts)) + 64
        image = Image.new('RGB', (width, 6 * (size + 18) + 64), 'white')
        draw = ImageDraw.Draw(image)
        for index, line in enumerate(texts):
            draw.text((32, 32 + index * (size + 18)), line, font=font,
                      fill=(243, 243, 243) if kind in {'low_contrast', 'faint_blur'} else (25, 25, 25))
        if kind == 'defocus':
            image = image.filter(ImageFilter.GaussianBlur(2.0))
        elif kind == 'motion':
            image = image.filter(ImageFilter.Kernel((5, 5), [0] * 10 + [1] * 5 + [0] * 10, scale=5))
        elif kind == 'shadow_noise':
            a = np.asarray(image).astype(np.float32)
            shade = np.linspace(140, 230, image.width)[None, :, None]
            a = shade + (a - 255) * .20 + np.random.default_rng(104).normal(0, 2, a.shape)
            image = Image.fromarray(np.clip(a, 0, 255).astype('uint8'))
        elif kind == 'jpeg':
            data = io.BytesIO()
            image.save(data, format='JPEG', quality=15)
            data.seek(0)
            image = Image.open(data).convert('RGB')
        elif kind == 'faint_blur':
            image = image.filter(ImageFilter.GaussianBlur(.7))
        path = output / (kind + '.png')
        image.save(path)
        rows.append({'id': kind, 'path': path, 'reference': '\n'.join(texts),
                     'mode': 'photo' if kind == 'shadow_noise' else 'scan'})
    return rows


def run(output: Path, models: Path, *, holdout: bool = False, precise: bool = False, text_only: bool = True) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    cases = generate(output / 'fixtures', holdout=holdout)
    jobs = [ImageJob(case['id'], str(case['path'].resolve()), mode=case['mode'],
                     text_only=text_only, auto_proofread=precise) for case in cases]
    started = time.monotonic()
    rows = []
    with SessionStore() as session:
        configure_offline_env(models, session.path)
        deny_network()
        cache = WarmWorkerCache(models, session.path)
        try:
            worker = cache.batch(jobs)
            worker.start(jobs)
            completed = set()
            while worker.busy:
                worker.poll()
                for job, case in zip(jobs, cases):
                    if job.id not in completed and job.status in {'done', 'failed'}:
                        completed.add(job.id)
                        print(json.dumps({'case': job.id, 'status': job.status}), flush=True)
                if time.monotonic() - started > 900:
                    raise TimeoutError('degraded_benchmark_timeout')
                time.sleep(.025)
            worker.poll()
            for job, case in zip(jobs, cases):
                text = extract_effective_text(job.result) if job.result else ''
                score = cer(case['reference'], text)
                rows.append({'case': job.id, 'status': job.status, **score,
                             'input_sha256': hashlib.sha256(case['path'].read_bytes()).hexdigest(),
                             'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
                             'reviewed_regions': job.result.detail_reviewed_regions if job.result else 0})
                # These fixtures are generated public prose, safe for local debugging.
                (output / (job.id + '.txt')).write_text(text, encoding='utf-8')
        finally:
            cache.close()
    report = {'version': __version__, 'cases': rows, 'holdout': holdout, 'precise': precise,
              'text_only': text_only, 'errors': sum(row['errors'] for row in rows),
              'reference': sum(row['reference'] for row in rows),
              'failed': sum(row['status'] != 'done' for row in rows),
              'elapsed_seconds': round(time.monotonic() - started, 3),
              'session_cleaned': not session.path.exists(), 'development_cpu_only': True,
              'synthetic_data_only': True}
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--models', type=Path, default=Path('models'))
    parser.add_argument('--holdout', action='store_true')
    parser.add_argument('--precise', action='store_true')
    parser.add_argument('--layout', action='store_true')
    args = parser.parse_args()
    report = run(args.output, args.models.resolve(), holdout=args.holdout, precise=args.precise, text_only=not args.layout)
    print(json.dumps(report), flush=True)
    if report['failed']:
        raise SystemExit(1)


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
