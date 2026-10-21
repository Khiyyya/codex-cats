"""Statistics contain counts only, never credentials or conversation text."""
import json
from pathlib import Path
from datetime import datetime, timezone


CONTENT_ENCODINGS = frozenset(('identity', 'gzip', 'zstd', 'deflate', 'br'))
WS_COMPRESSIONS = frozenset(('none', 'permessage-deflate'))


def _fixed_enum(value, allowed):
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.lower()
        if normalized in allowed:
            return normalized
    return 'other'


def status(file):
    data={'requests':0,'optimized':0,'no_optimization':0,
          'full_ws_payload_bytes':0,'actual_ws_payload_bytes':0,'saved_ws_payload_bytes':0,
          'reasons':{}}
    file=Path(file)
    if not file.exists():return data
    for raw in file.read_text(encoding='utf-8').splitlines():
        try:row=json.loads(raw)
        except (ValueError,TypeError):continue
        data['requests']+=1
        key=row.get('reason','unknown')
        data['reasons'][key]=data['reasons'].get(key,0)+1
        if key=='live_continuation':data['optimized']+=1
        else:data['no_optimization']+=1
        if key.startswith('http_native') or key.startswith('http_fallback') or key=='http_busy_native_passthrough':
            continue
        data['full_ws_payload_bytes']+=row.get('application_bytes_before',0)
        data['actual_ws_payload_bytes']+=row.get('application_bytes_after',0)
        data['saved_ws_payload_bytes']+=row.get('bytes_saved',0)
    return data


def log(path, *, reason, before, after, input_before, input_after,
        ingress=None, upstream=None, received_body_bytes=None, content_encoding=None,
        images_before=None, images_after=None, warmup_count=0, ws_compression=None):
    if not path:
        return
    record = {'time': datetime.now(timezone.utc).isoformat(), 'reason': reason,
              'application_bytes_before': before, 'application_bytes_after': after,
              'bytes_saved': before-after,
              'input_items_before': input_before, 'input_items_after': input_after}
    record.update({'ingress':ingress,'upstream':upstream,
                   'received_body_bytes':received_body_bytes,
                   'content_encoding':_fixed_enum(content_encoding, CONTENT_ENCODINGS),
                   'images_before':images_before,'images_after':images_after,
                   'warmup_count':warmup_count,
                   'ws_compression':_fixed_enum(ws_compression, WS_COMPRESSIONS)})
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open('a', encoding='utf-8') as f:
            f.write(json.dumps(record, separators=(',', ':'))+'\n')
    except OSError:
        pass
