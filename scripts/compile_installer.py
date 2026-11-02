"""Pass Inno definitions as exact arguments, including Windows paths."""
import argparse
import subprocess
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument('--compiler',required=True)
parser.add_argument('--source',type=Path,required=True)
parser.add_argument('--version',required=True)
parser.add_argument('--verification',action='store_true')
args=parser.parse_args()
command=[args.compiler,'/DAppSource='+str(args.source.resolve()),'/DAppVersion='+args.version]
if args.verification:command.append('/DVerificationInstall=1')
command.append(str(Path(__file__).resolve().parents[1]/'installer/selinco.iss'))
raise SystemExit(subprocess.call(command))
