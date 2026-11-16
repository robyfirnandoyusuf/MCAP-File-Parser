#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import collections
import json
import os
import re
import sys
from datetime import datetime, timezone

from mcap.reader import make_reader


def decoder_factories():
    facs = []
    try:
        from mcap_ros2.decoder import DecoderFactory as Ros2Factory
        facs.append(Ros2Factory())
    except ImportError:
        pass
    try:
        from mcap_protobuf.decoder import DecoderFactory as PbFactory
        facs.append(PbFactory())
    except ImportError:
        pass
    return facs


def to_plain(obj, max_list):
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (bytes, bytearray, memoryview)):
        b = bytes(obj)
        return f"<bytes len={len(b)}>" if len(b) > max_list else base64.b64encode(b).decode()
    if isinstance(obj, dict):
        return {k: to_plain(v, max_list) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)) or type(obj).__name__ == "ndarray":
        seq = list(obj)
        if len(seq) > max_list:
            return f"<list len={len(seq)}>"
        return [to_plain(v, max_list) for v in seq]
    if hasattr(obj, "__slots__"):
        return {k: to_plain(getattr(obj, k), max_list) for k in obj.__slots__}
    if hasattr(obj, "ListFields"):
        from google.protobuf.json_format import MessageToDict
        return to_plain(MessageToDict(obj, preserving_proto_field_name=True), max_list)
    if hasattr(obj, "__dict__"):
        return {k: to_plain(v, max_list) for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def flatten(d, prefix=""):
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(flatten(v, f"{prefix}{k}."))
    elif isinstance(d, list):
        for i, v in enumerate(d):
            out.update(flatten(v, f"{prefix}{i}."))
    else:
        out[prefix[:-1] if prefix else "value"] = d
    return out


def decode(schema, channel, message, decoded, max_list):
    enc = channel.message_encoding
    if decoded is not None:
        return to_plain(decoded, max_list)
    if enc == "json":
        try:
            return json.loads(message.data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"_raw": message.data[:200].decode("utf-8", "replace")}
    return {"_undecoded_bytes": len(message.data)}


def iter_rows(reader, factories, topics, max_list):
    cache = {}
    for schema, channel, message in reader.iter_messages(topics=topics, log_time_order=True):
        if channel.id not in cache:
            dec = None
            for fac in factories:
                dec = fac.decoder_for(channel.message_encoding, schema)
                if dec is not None:
                    break
            cache[channel.id] = dec
            if dec is None and channel.message_encoding != "json":
                print(f"[warn] {channel.topic}: '{channel.message_encoding}' 디코더 없음, 바이트 길이만 기록",
                      file=sys.stderr)
        dec = cache[channel.id]
        decoded = dec(message.data) if dec is not None else None
        yield channel.topic, row_of(schema, channel, message, decoded, max_list)


def row_of(schema, channel, message, decoded, max_list):
    body = decode(schema, channel, message, decoded, max_list)
    row = {
        "log_time_ns": message.log_time,
        "publish_time_ns": message.publish_time,
        "log_time_utc": ns_to_iso(message.log_time),
        "sequence": message.sequence,
    }
    row.update(flatten(body if isinstance(body, (dict, list)) else {"value": body}))
    return row


def ns_to_iso(ns):
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat(timespec="microseconds")


def cmd_info(path):
    with open(path, "rb") as f:
        reader = make_reader(f)
        header = reader.get_header()
        summary = reader.get_summary()
        print(f"file      : {path}")
        print(f"profile   : {header.profile or '(none)'}   library: {header.library}")
        if summary is None or summary.statistics is None:
            print("summary 섹션이 없음 → export로 전체를 읽어야 함")
            return
        st = summary.statistics
        print(f"messages  : {st.message_count:,}")
        print(f"time (UTC): {ns_to_iso(st.message_start_time)} ~ {ns_to_iso(st.message_end_time)}"
              f"  ({(st.message_end_time - st.message_start_time) / 1e9:.1f} s)")
        comp = collections.Counter(ci.compression or "none" for ci in summary.chunk_indexes)
        print(f"chunks    : {len(summary.chunk_indexes)} {dict(comp)}   attachments: {st.attachment_count}   metadata: {st.metadata_count}")
        print(f"\n{'count':>9}  {'msg_enc':<9} {'schema(enc)':<38} topic")
        for cid, ch in sorted(summary.channels.items(), key=lambda kv: kv[1].topic):
            sc = summary.schemas.get(ch.schema_id)
            sname = f"{sc.name}({sc.encoding})" if sc else "(schemaless)"
            n = st.channel_message_counts.get(cid, 0)
            print(f"{n:>9,}  {ch.message_encoding:<9} {sname[:38]:<38} {ch.topic}")


def safe_name(topic):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", topic.strip("/")) or "root"


def cmd_export(path, out, topics, max_list):
    import pandas as pd
    os.makedirs(out, exist_ok=True)
    buckets = collections.defaultdict(list)
    with open(path, "rb") as f:
        reader = make_reader(f)
        for topic, row in iter_rows(reader, decoder_factories(), topics, max_list):
            buckets[topic].append(row)
    for t in (topics or []):
        if t not in buckets:
            print(f"[warn] 요청한 토픽에 메시지가 없음: {t}  (info로 토픽 이름을 확인하세요)", file=sys.stderr)
    index = []
    for topic, rows in sorted(buckets.items()):
        df = pd.DataFrame(rows)
        fn = os.path.join(out, safe_name(topic) + ".csv")
        df.to_csv(fn, index=False, encoding="utf-8-sig")
        index.append({"topic": topic, "rows": len(df), "columns": df.shape[1], "csv": os.path.basename(fn)})
        print(f"{len(df):>9,} rows  {df.shape[1]:>4} cols  {topic} -> {fn}")
    pd.DataFrame(index).to_csv(os.path.join(out, "_topics.csv"), index=False, encoding="utf-8-sig")


def main():
    ap = argparse.ArgumentParser(description="MCAP info / 토픽별 CSV export")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("info"); a.add_argument("mcap")
    b = sub.add_parser("export"); b.add_argument("mcap"); b.add_argument("--out", required=True)
    b.add_argument("--topics", nargs="*", default=None, help="이 토픽만 변환 (기본: 전체)")
    b.add_argument("--max-list", type=int, default=32,
                   help="이보다 긴 배열·바이트(이미지, 포인트클라우드 등)는 길이만 기록")
    args = ap.parse_args()
    if args.cmd == "info":
        cmd_info(args.mcap)
    else:
        cmd_export(args.mcap, args.out, args.topics, args.max_list)


if __name__ == "__main__":
    main()
