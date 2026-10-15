import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.utils.capacity_profile import load_profile

parser = argparse.ArgumentParser(description='Validate declared dual-container commitments before starting them.')
parser.add_argument('--profile', required=True)
parser.add_argument('--host-total', type=int, required=True)
parser.add_argument('--host-available', type=int, required=True)
parser.add_argument('--host-cpus', type=float, required=True)
args = parser.parse_args()
profile = load_profile(args.profile)
committed = profile.check_host(args.host_total, args.host_available, args.host_cpus)
print(json.dumps({'profile_sha256': profile.fingerprint(), 'committed_bytes': committed,
                  'production_verified': False}))
