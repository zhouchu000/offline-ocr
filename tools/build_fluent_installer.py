"""Compile a Windows-native, offline WPF shell around a verified NSIS backend."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

from offline_word.version import __version__


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def application_palettes(root):
    module = ast.parse((root/'src/offline_word/theme.py').read_text(encoding='utf-8'))
    values = {}
    for statement in module.body:
        if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Call):
            for name in statement.targets:
                if isinstance(name, ast.Name) and name.id in {'LIGHT', 'DARK'}:
                    values[name.id.lower()] = {part.arg: ast.literal_eval(part.value) for part in statement.value.keywords}
    if set(values) != {'light', 'dark'}:
        raise ValueError('application_palette_missing')
    return values


def wrap(backend, output, *, kind, version=__version__, base_version='', payload=None, qa=False):
    root = Path(__file__).resolve().parents[1]
    backend, output = Path(backend).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError('fluent_installer_output_exists')
    if kind not in {'install', 'update'} or not backend.is_file():
        raise ValueError('invalid_backend')
    if not re.fullmatch(r'\d{1,4}\.\d{1,4}\.\d{1,4}', version):
        raise ValueError('invalid_installer_version')
    framework = Path(os.environ.get('WINDIR', 'C:/Windows'))/'Microsoft.NET/Framework64/v4.0.30319'
    compiler = framework/'csc.exe'
    if not compiler.is_file():
        raise FileNotFoundError('Windows .NET Framework compiler unavailable')
    source = root/'packaging/windows/fluent'
    releases = json.loads((root/'src/offline_word/releases.json').read_text(encoding='utf-8'))
    changes = next((r['changes'] for r in releases if r['version'] == version), ['统一安装与应用的浅深主题、磨砂窗口和动效。'])
    info = dict(version=version, baseVersion=base_version, kind=kind,
                backendSha256=digest(backend), backendBytes=backend.stat().st_size,
                installedBytes=0, changes=changes, **application_palettes(root))
    index = {}
    if payload is not None:
        payload = Path(payload).resolve()
        index = json.loads((payload/'checksums.json').read_text(encoding='utf-8'))
        index['checksums.json'] = digest(payload/'checksums.json')
        info['installedBytes'] = sum((payload/name).stat().st_size for name in index)
    elif kind == 'install' and not qa:
        raise ValueError('full_installer_requires_verified_payload')
    output.parent.mkdir(parents=True, exist_ok=True)
    # TemporaryDirectory is created by this build, never an existing output.
    with tempfile.TemporaryDirectory(prefix='.fluent-installer-', dir=output.parent) as temporary:
        stage = Path(temporary)
        config = stage/'setup.json'
        config.write_text(json.dumps(info, ensure_ascii=False), encoding='utf-8')
        (stage/'payload.json').write_text(json.dumps(index, ensure_ascii=False), encoding='utf-8')
        resource_builder = stage/'Resources.exe'
        subprocess.run([str(compiler), '/nologo', '/target:exe', '/out:'+str(resource_builder), str(source/'Resources.cs')], check=True)
        resources = stage/'FluentInstaller.g.resources'
        font_args = []
        for font in ('MiSans-Regular.ttf', 'MiSans-Medium.ttf'):
            font_args += ['assets/'+font.lower(), str(root/'src/offline_word/assets'/font)]
        subprocess.run([str(resource_builder), str(resources), *font_args], check=True)
        stub = stage/'FluentInstaller.exe'
        metadata = stage/'AssemblyInfo.cs'
        metadata.write_text('using System.Reflection;\n'
            '[assembly: AssemblyTitle("离线 OCR 安装程序")]\n'
            '[assembly: AssemblyProduct("离线 OCR")]\n'
            '[assembly: AssemblyCompany("OfflineWord")]\n'
            f'[assembly: AssemblyVersion("{version}.0")]\n'
            f'[assembly: AssemblyFileVersion("{version}.0")]\n'
            f'[assembly: AssemblyInformationalVersion("{version}")]\n', encoding='utf-8')
        references = [framework/name for name in ('System.dll','System.Core.dll','System.Xaml.dll','System.Runtime.Serialization.dll')]
        references += [framework/'WPF'/name for name in ('PresentationCore.dll','PresentationFramework.dll','WindowsBase.dll')]
        command = [str(compiler), '/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/warnaserror+',
            '/out:'+str(stub), '/win32manifest:'+str(source/'Installer.manifest'),
            '/win32icon:'+str(root/'src/offline_word/assets/app.ico'),
            '/resource:'+str(config)+',setup.json', '/resource:'+str(stage/'payload.json')+',payload.json',
            '/resource:'+str(source/'Window.xaml')+',window.xaml',
            '/resource:'+str(resources)+',FluentInstaller.g.resources',
            *['/reference:'+str(p) for p in references],
            *[str(source/name) for name in ('Bundle.cs','Native.cs','Window.cs')], str(metadata)]
        if qa:
            command += ['/define:QA', '/reference:'+str(framework/'System.Drawing.dll'), str(source/'Qa.cs')]
        subprocess.run(command, check=True)
        offset = stub.stat().st_size
        with stub.open('ab') as outgoing, backend.open('rb') as incoming:
            shutil.copyfileobj(incoming, outgoing, 1024*1024)
            outgoing.write(struct.pack('<8sqq', b'OWSETUP1', offset, info['backendBytes']))
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if output.exists():
            raise FileExistsError('fluent_installer_output_appeared')
        stub.rename(output)
    return dict(fluent_frontend=True, backend_sha256=info['backendSha256'],
                frontend_bytes=offset+24, installer_bytes=output.stat().st_size,
                installer_sha256=digest(output), qa_build=qa)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--kind', choices=['install','update'], required=True)
    parser.add_argument('--version', default=__version__)
    parser.add_argument('--base-version', default='')
    parser.add_argument('--payload', type=Path)
    parser.add_argument('--qa', action='store_true')
    args = parser.parse_args()
    result = wrap(args.backend, args.output, kind=args.kind, version=args.version,
                  base_version=args.base_version, payload=args.payload, qa=args.qa)
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
