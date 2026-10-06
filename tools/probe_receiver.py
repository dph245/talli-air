"""Read-only finite capture for decoder validation; not application storage."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import time

import pyModeS

from talli_flug.input import LineBuffer, parse_line

parser = argparse.ArgumentParser()
parser.add_argument('--host', required=True)
parser.add_argument('--port', type=int, default=47806)
parser.add_argument('--seconds', type=float, default=60)
parser.add_argument('--output', required=True)
args = parser.parse_args()
counts = Counter()
examples = {}
records = []
start = time.monotonic()
started = datetime.now(timezone.utc).isoformat()
with socket.create_connection((args.host, args.port), timeout=5) as sock:
    sock.settimeout(1)
    buffer = LineBuffer()
    while time.monotonic() - start < args.seconds:
        try:
            chunk = sock.recv(16384)
        except socket.timeout:
            continue
        if not chunk:
            break
        for line in buffer.feed(chunk):
            f = parse_line(line, 'receiver-1')
            if f is None:
                continue
            records.append({'raw': f.raw, 'metadata': f.metadata, 'received_at': f.received_at,
                            'offset': f.received_monotonic - start})
            try:
                d = pyModeS.decode(f.raw, include_meteo=True)
            except ValueError:
                counts['undecodable'] += 1
                continue
            counts[f'DF{d["df"]}'] += 1
            if d.get('crc_valid') is False:
                counts['CRC invalid'] += 1
                continue
            if d['df'] in (17, 18):
                key = f'ADS-B TC{d.get("typecode")} subtype {d.get("subtype", "n/a")}'
            elif d['df'] in (20, 21):
                candidates = d.get('bds_candidates', [d['bds']] if 'bds' in d else [])
                key = 'Comm-B ' + ('unique ' + candidates[0] if len(candidates) == 1 else 'ambiguous ' + '/'.join(candidates) if candidates else 'unidentified')
            else:
                continue
            counts[key] += 1
            examples.setdefault(key, f.raw)
summary = {'started_utc': started, 'duration_seconds': time.monotonic() - start,
           'receiver': f'{args.host}:{args.port}', 'frames': len(records),
           'counts': dict(sorted(counts.items())), 'examples': examples}
Path(args.output).write_text(json.dumps({'summary': summary, 'records': records}, indent=2) + '\n')
print(json.dumps(summary, indent=2))
