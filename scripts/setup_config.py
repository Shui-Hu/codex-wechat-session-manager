"""Create a private local configuration without overwriting an existing one."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))
from manager_core import DEFAULT_CONFIG


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        parser.error('The workspace must be an existing directory.')
    destination = args.config.expanduser().resolve()
    data = json.loads((ROOT / 'config.example.json').read_text(encoding='utf-8'))
    data['codex']['workspace'] = str(workspace)
    data['state_dir'] = str(destination.parent / 'state')
    data['download_dir'] = str(workspace / 'incoming')
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open('x', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    except FileExistsError:
        raise SystemExit('Existing configuration was preserved: ' + str(destination))
    print('Private configuration created: ' + str(destination))


if __name__ == '__main__':
    main()
