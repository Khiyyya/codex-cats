"""Pure response.create incremental planner. Never deletes, summarizes, or rewrites images.

No inferred server state: only a completed response on the currently LIVE upstream
WebSocket can establish a continuation baseline. An uploaded file_id, a hash, or an
MCP archive is NOT evidence the server can retrieve an image.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any


def strict_json(value):
    """Refuse ambiguous/nonstandard JSON before any semantic comparison."""
    def unique_object(pairs):
        obj={}
        for key,item in pairs:
            if key in obj:raise ValueError('duplicate_json_key')
            obj[key]=item
        return obj
    def invalid_constant(value):
        raise ValueError('nonfinite_json_number')
    def finite_float(value):
        number=float(value)
        if not math.isfinite(number):raise ValueError('nonfinite_json_number')
        return number
    return json.loads(value,object_pairs_hook=unique_object,
                      parse_constant=invalid_constant,parse_float=finite_float)


def canonical_sha(value: Any) -> bytes:
    data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(data).digest()


def identity(request: dict) -> tuple[str, str] | None:
    meta = request.get('client_metadata')
    if not isinstance(meta, dict):
        return None
    thread = meta.get('thread_id')
    window = meta.get('x-codex-window-id')
    if not window and isinstance(meta.get('x-codex-turn-metadata'), str):
        try:
            turn = json.loads(meta['x-codex-turn-metadata'])
            window = turn.get('window_id') if isinstance(turn, dict) else None
        except (ValueError, TypeError):
            pass
    if not (isinstance(thread, str) and 1 <= len(thread) <= 160):
        return None
    if not (isinstance(window, str) and 1 <= len(window) <= 160):
        return None
    return thread, window


def request_properties(body: dict) -> bytes:
    # The upstream recognizes client_metadata as per-response metadata, not history.
    # Do not ignore ANY other fields (including tools and model).
    return canonical_sha({k: v for k, v in body.items()
                          if k not in ('input', 'previous_response_id', 'client_metadata')
                          and not (k == 'stream' and v is True)})


@dataclass(frozen=True)
class Baseline:
    identity: tuple[str, str]
    properties: bytes
    input_hashes: tuple[bytes, ...]
    response_hashes: tuple[bytes, ...]
    response_id: str
    response_input_hashes: tuple[bytes, ...] = ()
    input_alternative_hashes: tuple[bytes, ...] = ()
    response_variant_hashes: tuple[tuple[bytes, ...], ...] = ()
    input_variant_hashes: tuple[tuple[bytes, ...], ...] = ()


def output_as_input(item):
    """Narrow, observed official-core output-to-input serialization projection.

    Preserve IDs, call relations, image/text content and nonempty annotations.
    This narrow projection never applies to prior INPUT or image content.
    """
    if (isinstance(item, dict) and item.get('type') in ('function_call','message')
            and item.get('status') == 'completed'):
        result={k:v for k,v in item.items() if k != 'status'}
        if item.get('type')=='message' and isinstance(item.get('content'),list):
            result['content']=[{k:v for k,v in part.items() if k!='annotations'}
                               if isinstance(part,dict) and part.get('type')=='output_text'
                               and part.get('annotations')==[] else part for part in item['content']]
        return result
    return item


def output_variants(item):
    """Exact observed serializations, only for server-confirmed OUTPUT items.

    No wildcard metadata exclusions: duplicate turn_id removal requires an
    identical retained copy. The observed one-text-part kind is pinned literally;
    unknown shapes and existing content_item_kinds are never generalized.
    """
    variants = [item, output_as_input(item)]
    if not isinstance(item, dict) or item.get('status') != 'completed':
        return variants
    if item.get('type') not in ('message', 'function_call', 'custom_tool_call'):
        return variants
    metadata = item.get('metadata')
    internal = item.get('internal_chat_message_metadata_passthrough')
    if (not isinstance(metadata, dict) or set(metadata) != {'turn_id'}
            or not isinstance(metadata['turn_id'], str) or not metadata['turn_id']
            or not isinstance(internal, dict)
            or internal.get('turn_id') != metadata['turn_id']):
        return variants
    duplicate_removed = {k: v for k, v in item.items() if k != 'metadata'}
    variants.append(duplicate_removed)
    content = item.get('content')
    if (item.get('type') == 'message' and item.get('role') == 'assistant'
            and isinstance(content, list) and len(content) == 1
            and isinstance(content[0], dict) and content[0].get('type') == 'output_text'
            and isinstance(content[0].get('text'), str)
            and set(internal) == {'turn_id', 'create_time'}
            and type(internal['create_time']) in (int, float)):
        variants.append({**duplicate_removed,
                         'internal_chat_message_metadata_passthrough':
                         {**internal, 'content_item_kinds': ['unknown']}})
    return variants


def baseline_after_success(sent_request: dict, event: dict, previous: Baseline | None = None) -> Baseline | None:
    if (not isinstance(sent_request, dict) or not isinstance(event, dict)
            or event.get('type') != 'response.completed'
            or not isinstance(sent_request.get('input'), list)):
        return None
    who = identity(sent_request)
    response = event.get('response')
    if not isinstance(response, dict) or response.get('status', 'completed') != 'completed':
        return None
    rid, outputs = response.get('id'), response.get('output')
    if not (who and isinstance(rid, str) and rid and isinstance(outputs, list)):
        return None
    if sent_request.get('generate', True) is False and outputs:
        return None  # A real warmup must not have generated model output.
    inputs=tuple(map(canonical_sha,sent_request['input']))
    alternatives=inputs
    input_variants=tuple((h,) for h in inputs)
    prior_id=sent_request.get('previous_response_id')
    if prior_id:
        if previous is None or prior_id != previous.response_id or who != previous.identity:
            return None
        # Only follow a server-confirmed parent on THIS live upstream session.
        # No inference from an arbitrary response ID supplied by a client.
        input_variants=(previous.input_variant_hashes or tuple((h,) for h in previous.input_hashes)) + (previous.response_variant_hashes or tuple((h,) for h in previous.response_hashes)) + input_variants
        inputs=previous.input_hashes+previous.response_hashes+inputs
        alternatives=(previous.input_alternative_hashes or previous.input_hashes)+(previous.response_input_hashes or previous.response_hashes)+alternatives
    return Baseline(who, request_properties(sent_request),
                    inputs,
                    tuple(map(canonical_sha, outputs)), rid,
                    tuple(canonical_sha(output_as_input(item)) for item in outputs),alternatives,
                    tuple(tuple(dict.fromkeys(canonical_sha(v) for v in output_variants(item))) for item in outputs),
                    input_variants)


def plan(full: dict, baseline: Baseline | None, *, upstream_live: bool) -> tuple[dict, str]:
    """Return unchanged original or a semantically equivalent server-cache continuation.

    Proof requires the exact prior INPUT + complete server OUTPUT to be a prefix
    of current INPUT, and matching request properties and conversation identity.
    A severed upstream socket invalidates the proof, regardless of response_id.
    """
    if not (isinstance(full, dict) and full.get('type') == 'response.create'
            and isinstance(full.get('input'), list)
            and full.get('generate', True) is not False):
        return full, 'not_generation'
    if full.get('previous_response_id'):
        return full, 'already_incremental'
    if not upstream_live or baseline is None:
        return full, 'no_live_baseline'
    if identity(full) != baseline.identity:
        return full, 'identity_mismatch'
    if request_properties(full) != baseline.properties:
        return full, 'properties_changed'
    prefix = baseline.input_hashes + baseline.response_hashes
    items = full['input']
    if len(items) < len(prefix):
        return full, 'input_shortened'
    for index,(item, h) in enumerate(zip(items, prefix)):
        actual=canonical_sha(item)
        projected_index=index-len(baseline.input_hashes)
        projected=(baseline.response_input_hashes[projected_index]
                   if 0 <= projected_index < len(baseline.response_input_hashes)
                   else baseline.input_alternative_hashes[index] if index < len(baseline.input_alternative_hashes) else h)
        variants=(baseline.response_variant_hashes[projected_index]
                  if 0 <= projected_index < len(baseline.response_variant_hashes)
                  else baseline.input_variant_hashes[index] if index < len(baseline.input_variant_hashes) else ())
        if actual != h and actual != projected and actual not in variants:
            return full, 'history_mismatch'
    result = dict(full)
    result['previous_response_id'] = baseline.response_id
    result['input'] = items[len(prefix):]
    return result, 'live_continuation'


def response_event(message: str | bytes) -> dict | None:
    try:
        obj = strict_json(message)
        return obj if isinstance(obj, dict) else None
    except (ValueError, UnicodeDecodeError, TypeError):
        return None


def terminal_event(event: dict) -> bool:
    return event.get('type') in ('response.completed', 'response.failed',
                                 'response.incomplete', 'error')
