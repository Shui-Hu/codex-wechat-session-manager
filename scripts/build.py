"""Build the Windows GUI; private configuration is never bundled."""
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parent.parent


def main():
    if os.name != 'nt':
        raise SystemExit('This GUI build has only been prepared for Windows.')
    from PyInstaller.__main__ import run
    run([
        '--onefile', '--windowed', '--noupx', '--noconfirm',
        '--name', 'CodexWeChatSessionManager',
        '--distpath', str(ROOT / 'dist'), '--workpath', str(ROOT / 'build'),
        '--specpath', str(ROOT / 'build'), '--paths', str(ROOT / 'src'),
        '--exclude-module', 'numpy', str(ROOT / 'src' / 'session_manager.py'),
    ])
    print('Built: ' + str(ROOT / 'dist' / 'CodexWeChatSessionManager.exe'))
    print('Keep LICENSE, NOTICE.md and corresponding source available when distributing it.')


if __name__ == '__main__':
    main()
