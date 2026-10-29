"""Check actual Word-rendered public documents for loss, duplication and overlap."""
import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess

from docx import Document
import pdfplumber
import pypdfium2


def normalized(value):
    return ''.join(value.split())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('documents', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    report = {}
    for source in sorted(args.documents.glob('*.docx')):
        folder = args.output/source.stem
        folder.mkdir()
        pdf = folder/'word.pdf'
        result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-File',
            str(Path(__file__).with_name('render_word.ps1')), '-InputDocx',str(source.resolve()),
            '-OutputPdf',str(pdf.resolve())], capture_output=True, timeout=120)
        if result.returncode:
            (folder/'render-error.txt').write_bytes(result.stderr)
            raise RuntimeError(result.stderr.decode('utf-8', errors='replace'))
        metadata = json.loads(result.stdout.decode('utf-8-sig'))
        expected = normalized(''.join(node.text or '' for node in Document(source).element.iter(
            '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t')))
        overlap = 0
        with pdfplumber.open(pdf) as document:
            actual = normalized(''.join(page.extract_text() or '' for page in document.pages))
            assert Counter(actual) == Counter(expected), 'Word text coverage mismatch'
            for page in document.pages:
                chars = [c for c in page.chars if c['text'].strip()]
                assert all(-.1 <= c['x0'] <= c['x1'] <= page.width+.1
                           and -.1 <= c['top'] <= c['bottom'] <= page.height+.1 for c in chars)
                active = []
                for char in sorted(chars, key=lambda c:c['top']):
                    active = [c for c in active if c['bottom'] > char['top']]
                    for other in active:
                        area = max(.01, (char['x1']-char['x0'])*(char['bottom']-char['top']))
                        intersection = (max(0,min(char['x1'],other['x1'])-max(char['x0'],other['x0']))
                            * max(0,min(char['bottom'],other['bottom'])-max(char['top'],other['top'])))
                        overlap += intersection/area > .65
                    active.append(char)
            assert overlap == 0, 'Word glyph overlap detected'
            report[source.stem] = {'pages':len(document.pages), 'characters':len(actual),
                'text_counts_match':True, 'overlapping_glyph_pairs':overlap, 'within_pages':True,
                'owned_word_instance':metadata['owned_instance']}
        rendered = pypdfium2.PdfDocument(pdf)
        for index in range(len(rendered)):
            page = rendered[index]
            bitmap = page.render(scale=1.5)
            bitmap.to_pil().save(folder/f'page-{index+1}.png')
            bitmap.close()
            page.close()
        rendered.close()
    assert report
    (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__ == '__main__':
    main()
