"""python -m unified_navigation_v3 INPUT.json (or - for stdin)."""
import sys
from . import decide_json, InputError


def main():
    if len(sys.argv) != 2:
        print('usage: python -m unified_navigation_v3 INPUT.json|-', file=sys.stderr)
        return 2
    try:
        if sys.argv[1] == '-':
            payload = sys.stdin.buffer.read(16 * 1024 * 1024 + 1)
        else:
            with open(sys.argv[1], 'rb') as source:
                payload = source.read(16 * 1024 * 1024 + 1)
        encoded = decide_json(payload)
    except (InputError, OSError) as exc:
        print(f'InputError: {exc}', file=sys.stderr)
        return 2
    sys.stdout.buffer.write(encoded + b'\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
