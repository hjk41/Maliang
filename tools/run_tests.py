"""Run the complete offline suite; missing dependencies/resources must not skip."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run_suite(suite, stream=None):
    stream = stream or sys.stderr
    result = unittest.TextTestRunner(verbosity=2, stream=stream).run(suite)
    if result.skipped:
        print('Complete verification requires zero skipped tests. Install .[dev,agent] '
              'and include all repository resources; inspect the skip reasons above.', file=stream)
    return 0 if result.wasSuccessful() and not result.skipped else 1


if __name__ == '__main__':
    raise SystemExit(run_suite(unittest.defaultTestLoader.discover(str(ROOT / 'tests'))))
