"""Real, offline serial/parallel comparison on public image fixtures only."""
import argparse
import hashlib
import json
import multiprocessing
from pathlib import Path
import time


def run(root, paths, profile, output):
    import psutil
    from offline_word.domain import ImageJob
    from offline_word.parallel_worker import ParallelOCRWorker
    from offline_word.security import SessionStore, configure_offline_env, deny_network
    from offline_word.worker import OCRWorker

    session = SessionStore()
    worker = None
    try:
        configure_offline_env(root/'models', session.path)
        deny_network()
        jobs = [ImageJob(f'public-{index}', str(path), performance_profile=profile, diagnostics=True) for index, path in enumerate(paths)]
        worker = (ParallelOCRWorker if profile == 'parallel' else OCRWorker)(root/'models', session.path)
        from offline_word.performance import available_memory_gib, working_set_gib
        admission = {'available_gib':available_memory_gib(), 'per_lane_gib':working_set_gib(jobs)}
        begin = time.monotonic()
        worker.start(jobs)
        print(json.dumps({'started':profile, 'lanes':getattr(worker,'lane_count',1), **admission}),flush=True)
        peak = 0
        process = psutil.Process()
        events = []
        finished = False
        active = set()
        maximum_active = 0
        while worker.busy and time.monotonic()-begin < 480:
            batch = worker.poll()
            for event in batch:
                kind = event['event']
                if kind == 'page_started': active.add(event['job_id'])
                if kind in {'page_done','page_error'}: active.discard(event['job_id'])
                maximum_active = max(maximum_active, len(active))
                if kind == 'diagnostic':
                    events.append({key:event[key] for key in ('stage','elapsed_ms','rss_mb')})
                if kind == 'finished': finished = True
            rss = process.memory_info().rss
            for child in process.children(recursive=True):
                try: rss += child.memory_info().rss
                except psutil.Error: pass
            peak = max(peak, rss)
            if finished: break
            time.sleep(.04)
        elapsed = time.monotonic()-begin
        assert finished and all(job.status == 'done' for job in jobs), [job.error_code for job in jobs]
        texts = []
        for job in jobs:
            text = ''.join(block.text if block.kind != 'table' else ''.join(cell.text for cell in block.cells) for block in job.result.blocks if block.kind != 'image')
            for expected in ('本地文档识别验证','正常文字','二维码说明文字','128.50','不得误删'):
                assert expected in text, expected
            texts.append(''.join(text.split()))
        result = {'profile':profile, 'elapsed_seconds':round(elapsed,3), 'peak_process_tree_rss_bytes':peak,
                  'admission':admission,
                  'lanes':getattr(worker,'lane_count',1), 'maximum_active_pages':maximum_active,
                  'page_characters':[len(text) for text in texts],
                  'text_sha256':[hashlib.sha256(text.encode()).hexdigest() for text in texts],
                  'diagnostic_stages':events, 'completed_pages':len(jobs)}
        output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        return result
    finally:
        if worker is not None: worker.close()
        session.close()
        assert not session.path.exists()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('fixtures',type=Path)
    parser.add_argument('output',type=Path)
    parser.add_argument('--profile', choices=['sequential','parallel'], required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    paths = sorted(args.fixtures.resolve().glob('*.png'))
    assert len(paths) == 6, 'Use the six public barcode regression fixtures'
    result = run(Path.cwd(),paths,args.profile,args.output)
    print(json.dumps({key:result[key] for key in ('profile','elapsed_seconds','peak_process_tree_rss_bytes','maximum_active_pages','completed_pages')},ensure_ascii=False))


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
