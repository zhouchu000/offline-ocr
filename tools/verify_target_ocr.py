"""Regression for a user-selected missed-line page; never log its OCR text."""
import argparse
import hashlib
import json
import multiprocessing
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('image', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--phrases-file', type=Path, required=True,
                        help='Local UTF-8 reference phrases, one per line; never include private references in source control')
    args = parser.parse_args()
    phrases = [''.join(line.split()) for line in args.phrases_file.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    if not phrases or len(phrases) > 100 or any(len(phrase) > 1000 for phrase in phrases):
        raise ValueError('invalid_reference_phrases')
    args.output.mkdir(parents=True, exist_ok=False)
    from offline_word.domain import ImageJob
    from offline_word.exporter import export_docx
    from offline_word.project_store import ProjectSnapshot, load_project, save_project
    from offline_word.security import SessionStore, configure_offline_env, deny_network
    from offline_word.warm_worker import WarmWorkerCache
    def digest(path):
        with path.open('rb') as stream:
            return hashlib.file_digest(stream,'sha256').hexdigest()
    before=digest(args.image)
    with SessionStore() as session:
        root=Path.cwd()
        configure_offline_env(root/'models',session.path)
        deny_network()
        job=ImageJob('target',str(args.image.resolve()),mode='photo',diagnostics=True)
        cache=WarmWorkerCache(root/'models',session.path)
        started=time.monotonic()
        events=[]
        try:
            worker=cache.batch([job])
            worker.start([job])
            while worker.busy and time.monotonic()-started<300:
                events.extend(worker.poll())
                time.sleep(.025)
            assert not worker.busy and job.result and job.status=='done'
            seconds=round(time.monotonic()-started,3)
            text=''.join(''.join(b.text.split()) for b in job.result.blocks if b.kind!='image')
            assert all(text.count(phrase)==1 for phrase in phrases), 'Target phrase coverage mismatch'
            encrypted=session.path/'roundtrip.ocrproj'
            password='临时验收口令-only-in-session-050'
            save_project(ProjectSnapshot([job],{'layout_mode':'source'}),encrypted,password)
            loaded=load_project(encrypted,password,session.path)
            restored=''.join(''.join(b.text.split()) for b in loaded.jobs[0].result.blocks if b.kind!='image')
            assert restored==text
            for mode in ('source','paragraph'):
                export_docx([loaded.jobs[0].result],args.output/f'{mode}.docx',layout_mode=mode)
            report={'pass':True,'seconds':seconds,'characters':len(text),'previously_missing_phrases_once':True,
                'text_sha256':hashlib.sha256(text.encode()).hexdigest(),
                'project_roundtrip':True,'original_unchanged':before==digest(args.image),
                'recovered_blocks':sum(b.recognition_source=='recovered' for b in job.result.blocks),
                'development_cpu_only':True}
        finally:
            cache.close()
    report['session_cleaned']=not session.path.exists()
    (args.output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))


if __name__=='__main__':
    multiprocessing.freeze_support()
    main()
