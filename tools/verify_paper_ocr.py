"""Check real OCR before/after an explicitly accepted public paper boundary."""
import argparse
import hashlib
import json
import multiprocessing
from pathlib import Path
import time

from PIL import Image, ImageDraw, ImageFont


def make_fixture(output, assets):
    font = ImageFont.truetype(str(assets/'MiSans-Regular.ttf'),32)
    heading = ImageFont.truetype(str(assets/'MiSans-Medium.ttf'),40)
    page = Image.new('RGB',(820,1120),'white')
    draw = ImageDraw.Draw(page)
    lines = ['纸面文字完整性验证','正文第一行必须完整保留','价格为128.50元','左边缘文字保留','右边缘文字保留','确认范围后才裁切','原始图片不得修改']
    draw.text((35,65),lines[0],font=heading,fill='black')
    for index,text in enumerate(lines[1:]):
        x = 5 if index == 2 else 820-draw.textlength(text,font=font)-5 if index == 3 else 40
        draw.text((x,190+index*98),text,font=font,fill='black')
    photo = Image.new('RGB',(1320,1440),'#505965')
    photo.paste(page,(250,90))
    draw = ImageDraw.Draw(photo)
    draw.rectangle((36,1310,900,1370),fill='#ffffff')
    draw.text((52,1315),'OUTSIDE 4321 纸外测试文字',font=font,fill='black')
    photo.save(output)
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    import offline_word
    from offline_word.domain import ImageJob
    from offline_word.paper import detect_paper_quad
    from offline_word.security import SessionStore,configure_offline_env,deny_network
    from offline_word.worker import OCRWorker
    source = args.output/'public-paper.png'
    expected = make_fixture(source,Path(offline_word.__file__).parent/'assets')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with Image.open(source) as image: detection = detect_paper_quad(image)
    assert detection.status == 'suggested'
    session = SessionStore()
    root = Path.cwd()
    configure_offline_env(root/'models',session.path)
    deny_network()
    worker = OCRWorker(root/'models',session.path)
    jobs = [ImageJob('whole',str(source.resolve()),mode='photo'),
            ImageJob('confirmed',str(source.resolve()),mode='photo',perspective=detection.quad,
                     paper_quad=detection.quad,paper_status='confirmed')]
    start = time.monotonic()
    try:
        worker.start(jobs)
        finished = False
        while worker.busy and time.monotonic()-start < 240:
            events = worker.poll()
            if any(event['event']=='finished' for event in events):
                finished = True
                break
            time.sleep(.04)
        assert finished and all(job.status=='done' for job in jobs), [job.error_code for job in jobs]
        text = [''.join(''.join(block.text.split()) for block in job.result.blocks if block.kind not in {'image','table'}) for job in jobs]
        for value in text:
            for phrase in expected:
                assert phrase in value, phrase
        assert 'OUTSIDE4321' in text[0] and 'OUTSIDE4321' not in text[1]
        assert jobs[0].result.paper_status == 'suggested' and jobs[0].result.paper_review_count > 0
        assert jobs[1].result.width < jobs[0].result.width
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
        report = {'pass':True,'sample_class':'synthetic_public_photo','expected_phrases_preserved':len(expected),
                  'whole_image_outside_text_preserved':True,'confirmed_outside_text_removed':True,
                  'unconfirmed_review_count':jobs[0].result.paper_review_count,'original_unchanged':True,
                  'elapsed_seconds':round(time.monotonic()-start,3)}
    finally:
        worker.close()
        session.close()
    report['session_cleaned'] = not session.path.exists()
    (args.output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report),flush=True)


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
