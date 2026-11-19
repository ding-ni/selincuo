"""Pass Inno definitions as exact arguments, including Windows paths."""
import argparse
import subprocess
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument('--compiler',required=True)
parser.add_argument('--source',type=Path,required=True)
parser.add_argument('--version',required=True)
parser.add_argument('--verification',action='store_true')
parser.add_argument('--output-dir',type=Path,help='本次发行目录')
args=parser.parse_args()
root=Path(__file__).resolve().parents[1]
out=(args.output_dir or root/'releases').resolve()
suffix='-Verification-Setup.exe' if args.verification else '-Setup.exe'
target=out/('色林错预警预报平台-v'+args.version+suffix)
if target.exists():raise SystemExit('安装器已经存在，请指定新的发行目录：'+str(target))
out.mkdir(parents=True,exist_ok=True)
command=[args.compiler,'/O'+str(out),'/DAppSource='+str(args.source.resolve()),'/DAppVersion='+args.version]
if args.verification:command.append('/DVerificationInstall=1')
command.append(str(Path(__file__).resolve().parents[1]/'installer/selinco.iss'))
raise SystemExit(subprocess.call(command))
