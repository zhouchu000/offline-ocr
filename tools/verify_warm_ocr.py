"""Measure real process reuse with public fixtures and local OCR models."""
import argparse
import hashlib
import json
import multiprocessing
from pathlib import Path
import time


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('fixtures',type=Path)
    parser.add_argument('output',type=Path)
    parser.add_argument('--parallel',action='store_true')
    args=parser.parse_args()
    from offline_word.domain import ImageJob
    from offline_word.security import SessionStore,configure_offline_env,deny_network
    from offline_word.warm_worker import WarmWorkerCache
    root=Path.cwd()
    paths=sorted(args.fixtures.resolve().glob('*.png'))[:2]
    assert len(paths)==2
    results=[]
    with SessionStore() as session:
        configure_offline_env(root/'models',session.path)
        deny_network()
        cache=WarmWorkerCache(root/'models',session.path)
        try:
            previous=None
            for turn in range(2):
                jobs=[ImageJob(str(i),str(path),diagnostics=True) for i,path in enumerate(paths)]
                worker=cache.batch(jobs,parallel=args.parallel)
                start=time.monotonic();worker.start(jobs)
                pids=[lane.worker._process.pid for lane in worker.lanes]
                if previous is not None: assert pids==previous
                previous=pids
                events=[]
                while worker.busy and time.monotonic()-start<240:
                    events.extend(worker.poll())
                    time.sleep(.02)
                elapsed=time.monotonic()-start
                assert not worker.busy and all(job.status=='done' for job in jobs)
                hashes=[]
                for job in jobs:
                    text=''.join(block.text for block in job.result.blocks if block.kind!='image')
                    assert '不得误删' in text and '二维码说明文字' in text
                    hashes.append(hashlib.sha256(text.encode()).hexdigest())
                initialization=sum(e.get('stage')=='engine_init' for e in events)
                diagnostics=[{k:e[k] for k in ('stage','elapsed_ms','rss_mb')} for e in events if e.get('event')=='diagnostic']
                stages={e['stage'] for e in diagnostics}
                if not turn:
                    assert {'model_validate','module_import','model_create','first_inference'} <= stages
                else:
                    assert not ({'model_create','first_inference'} & stages)
                results.append({'seconds':round(elapsed,3),'lanes':len(pids),'engine_initializations':initialization,'text_sha256':hashes,'diagnostics':diagnostics})
                worker.close()
                if turn: assert initialization==0 and hashes==results[0]['text_sha256']
            region=cache.region(jobs[0])
            assert region._process.pid in previous
            block=next(block for block in jobs[0].result.blocks if block.kind=='text' and '不得误删' in block.text)
            start=time.monotonic();region.start_region(jobs[0],block.bbox)
            events=[]
            while region.busy and time.monotonic()-start<120:
                events.extend(region.poll());time.sleep(.02)
            assert any(e.get('event')=='region_done' and e.get('candidates') for e in events),events
            region.close()
            report={'pass':True,'batches':results,'region_reused_process':True,'region_seconds':round(time.monotonic()-start,3)}
        finally:
            cache.close()
    report['session_cleaned']=not session.path.exists()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report),flush=True)


if __name__=='__main__':
    multiprocessing.freeze_support()
    main()
