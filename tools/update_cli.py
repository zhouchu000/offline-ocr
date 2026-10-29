"""Standalone trusted updater entry; all messages are fixed, text-free status."""
import argparse
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--package', type=Path)
    parser.add_argument('--rollback', action='store_true')
    parser.add_argument('--status-file', type=Path)
    args = parser.parse_args()
    def report(message):
        if args.status_file is not None:
            # NSIS pipes use the Windows ANSI code page, which corrupts UTF-8.
            # The wizard reads this fixed message explicitly as UTF-16 LE.
            args.status_file.write_text(message+'\r\n', encoding='utf-16-le')
        else:
            print(message, flush=True)
    from offline_word.security import deny_network
    deny_network()
    from offline_word.offline_update import apply_update, rollback_update
    messages = {'checking':'正在核对旧版本与全部文件…', 'preparing':'正在验证更新内容并备份…',
                'applying':'正在更新…', 'verifying':'正在核对更新结果…'}
    try:
        if args.rollback:
            rollback_update(args.target)
            report('已恢复更新前版本。')
        elif args.package:
            apply_update(args.package, args.target, progress=lambda stage: report(messages[stage]))
            report('更新完成，原版本备份已保留。')
        else:
            return 2
    except Exception as error:
        code = getattr(error, 'code', 'update_io_failed')
        explanations = {'application_or_update_running':'请关闭离线 OCR 及识别任务后重试。',
            'invalid_update_signature':'更新包签名无效，请重新取得完整更新包。',
            'update_base_files_changed':'原版本或文件不匹配，请使用对应版本的更新包或完整安装程序。',
            'update_wrong_version':'此更新包不适用于当前版本。',
            'update_payload_damaged':'更新包内容损坏，原版本未被替换。',
            'update_rollback_pending':'恢复尚未完成。请保留备份，再运行本更新程序完成恢复。',
            'update_backup_missing':'没有可恢复的更新备份。',
            'update_backup_damaged':'更新备份损坏，已停止恢复。',
            'update_disk_space':'可用磁盘空间不足。'}
        report(explanations.get(code, '未完成更新，请检查文件权限并重试。') + f'（{code}）')
        return 1
    return 0


if __name__ == '__main__':
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
