"""Run only the accepted storage matrix; keep every individual result on failure."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--only')
    args = parser.parse_args()
    evidence = args.evidence
    results = []
    for profile, dataset in [('snapshot', 'scale-1-snapshot'), ('snapshot', 'scale-10-snapshot'),
                             ('identified', 'scale-1-identified-v2'), ('identified', 'scale-10-identified-v2')]:
        for viewers in (1, 4):
            name = dataset + '-v' + str(viewers) + ('-' + args.only if args.only else '')
            output = evidence / ('final-http-' + name + '.json')
            with (evidence / ('final-http-' + name + '.log')).open('w') as log:
                completed = subprocess.run([sys.executable, '-m', 'ops.qualification.storage_http',
                    '--dataset', str(evidence / dataset), '--viewers', str(viewers),
                    '--requests', '100', '--warmups', '5', '--output', str(output)] +
                    (['--only', args.only] if args.only else []), stdout=log, stderr=log)
            results.append({'profile': profile, 'dataset': dataset, 'viewers': viewers,
                            'exit_code': completed.returncode, 'result': str(output)})
            (evidence / ('storage-matrix-progress' + ('-' + args.only if args.only else '') + '.json')).write_text(json.dumps(results, indent=2) + '\n')
            print(json.dumps(results[-1]), flush=True)
        for number in range(0 if args.only else 5):
            name = dataset + '-cold-' + str(number + 1)
            output = evidence / ('final-http-' + name + '.json')
            with (evidence / ('final-http-' + name + '.log')).open('w') as log:
                completed = subprocess.run([sys.executable, '-m', 'ops.qualification.storage_http',
                    '--dataset', str(evidence / dataset), '--viewers', '1', '--cold', '--output', str(output)],
                    stdout=log, stderr=log)
            results.append({'dataset': dataset, 'fresh_process': number + 1,
                            'exit_code': completed.returncode, 'result': str(output)})
            (evidence / 'storage-matrix-progress.json').write_text(json.dumps(results, indent=2) + '\n')
            print(json.dumps(results[-1]), flush=True)
    if any(value['exit_code'] for value in results):
        raise SystemExit(2)


if __name__ == '__main__':
    main()
