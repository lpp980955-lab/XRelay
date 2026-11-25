"""Run the distributable synthetic/unit test subset without private data."""
import argparse,os,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
os.environ['QT_QPA_PLATFORM']='offscreen'
sys.path[:0]=[str(ROOT/'src'),str(ROOT),str(ROOT/'tests')]
(ROOT/'artifacts/test_tmp').mkdir(parents=True,exist_ok=True)
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--ui',action='store_true');args=parser.parse_args()
    folder=ROOT/('tests_ui' if args.ui else 'tests')
    tests=unittest.defaultTestLoader.discover(str(folder))
    result=unittest.TextTestRunner(verbosity=2).run(tests)
    raise SystemExit(0 if result.wasSuccessful() else 1)
