"""Generic read-only image task command. See the repository AGENTS.md."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .client import client_from_env
from .transport import json_body


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def save(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def execute(prompt, images, schema, *, state_path, timeout, env, client=None):
    model, profile = env.get('AI_SERVICE_MODEL', ''), env.get('AI_SERVICE_PROFILE', '')
    owner = env.get('AI_SERVICE_PROJECT') or env.get('GITHUB_REPOSITORY')
    operation = env.get('AI_SERVICE_OPERATION_ID') or env.get('GITHUB_RUN_ID')
    if not all(isinstance(v, str) and v.strip() for v in [model, profile, owner, operation]):
        raise ValueError('explicit model, profile, project and operation identity required')
    if not isinstance(timeout, int) or not 1 <= timeout <= 900:
        raise ValueError('invalid timeout')
    materials = []
    for image in images:
        data = Path(image).read_bytes()
        if len(data) > 225000:
            raise ValueError('image is too large')
        mime = ('image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else
                'image/jpeg' if data.startswith(b'\xff\xd8\xff') else
                'image/webp' if data.startswith(b'RIFF') and data[8:12] == b'WEBP' else None)
        if not mime:
            raise ValueError('unsupported image')
        materials.append({'type': 'image', 'mime_type': mime,
                          'base64': base64.b64encode(data).decode('ascii')})
    if len(materials) > 3:
        raise ValueError('too many images')
    request = {'mode': 'agent', 'model': model, 'profile': profile, 'objective': prompt,
               'materials': materials, 'output_schema': schema, 'timeout_seconds': timeout}
    request_hash = hashlib.sha256(json_body(request)).hexdigest()
    key = 'image-task:' + digest({'operation': operation, 'request': request_hash})
    client = client or client_from_env()
    state_path = Path(state_path)
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if state.get('key') != key or state.get('request_sha256') != request_hash:
            raise ValueError('frozen image request changed')
        if not state.get('id'):
            # Read-only lookup, never another POST after an uncertain submission.
            state['id'] = client.lookup(key, request_hash)['id']
            save(state_path, state)
    else:
        state = {'key': key, 'request_sha256': request_hash, 'id': None}
        save(state_path, state)
        state['id'] = client.submit(request, key)['id']
        save(state_path, state)
    record = client.wait(state['id'], timeout_seconds=timeout)
    if (record.get('id') != state['id'] or record.get('owner') != owner
            or record.get('request') != request or record.get('status') != 'completed'
            or record.get('result_kind') != 'advisory'):
        raise ValueError('image task is incomplete or result binding changed')
    return record['output']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prompt-file', type=Path, required=True)
    parser.add_argument('--image', type=Path, action='append', default=[])
    parser.add_argument('--output-schema', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--timeout-seconds', type=int, default=60)
    parser.add_argument('--sandbox', choices=['read-only'], default='read-only')
    parser.add_argument('--ask-for-approval', choices=['never'], default='never')
    # Existing command adapters retain these labels; routing is explicitly
    # configured by this project's service policy, not a legacy provider chain.
    parser.add_argument('--task')
    parser.add_argument('--complexity')
    parser.add_argument('--providers')
    args = parser.parse_args()
    try:
        output = execute(args.prompt_file.read_text(), args.image,
                         json.loads(args.output_schema.read_text()),
                         state_path=Path(str(args.out) + '.state.json'),
                         timeout=args.timeout_seconds, env=os.environ)
        save(args.out, output)
    except Exception:
        parser.exit(3, 'AI image task unavailable; retain its state and reuse its original identity.\n')


if __name__ == '__main__':
    main()
