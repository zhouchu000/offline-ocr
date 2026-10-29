"""Render the public changelog from the application's offline release catalog."""
import argparse
from pathlib import Path

from offline_word.releases import load_releases


def render_changelog(releases):
    lines = ['# 更新记录', '', '版本记录与应用内更新日志使用同一份数据。', '']
    for release in releases:
        lines.extend([f'## {release.version} · {release.date}', '', release.title, ''])
        lines.extend(f'- {change}' for change in release.changes)
        lines.append('')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    destination = Path(__file__).resolve().parents[1] / 'CHANGELOG.md'
    expected = render_changelog(load_releases())
    if args.check:
        if not destination.is_file() or destination.read_text(encoding='utf-8') != expected:
            raise SystemExit('CHANGELOG.md differs from releases.json; run tools.sync_changelog.')
    else:
        destination.write_text(expected, encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
