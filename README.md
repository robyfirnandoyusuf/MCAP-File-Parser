# MCAP File Parser

<img width="215" height="188" alt="image" src="https://github.com/user-attachments/assets/19ff1151-e25e-448d-8850-3f5c30b8692b" />

A small command-line tool to look inside [MCAP](https://mcap.dev) files and export each topic to a CSV file.

- `info`: shows what is in a file (topics, encodings, message counts, time range) without decoding the messages.
- `export`: decodes the messages and writes one CSV per topic, ready to open in Excel or pandas.

It handles JSON messages (for example UNS / MQTT payloads) and ROS 2 messages (`cdr`, as written by `ros2 bag record -s mcap`).

## Requirements

- Python 3 (tested with Python 3.14.6)
- `mcap` (tested 1.6.0)
- `pandas` (tested 3.1.1, only needed for `export`)
- Optional decoders:
  - `mcap-ros2-support` (tested 0.6.0) for ROS 2 `cdr` messages
  - `mcap-protobuf-support` for protobuf messages (supported in the code, not tested)

```sh
pip install mcap pandas mcap-ros2-support
```

Without `mcap-ros2-support`, ROS 2 messages are not decoded. The tool prints a warning and writes only the byte length of each message (`_undecoded_bytes`).

If a ROS environment sets `PYTHONPATH` and imports fail, run Python in isolated mode (`python -I mcap_parse.py ...`). Isolated mode ignores user site-packages, so install the packages in a virtual environment or the main environment.

## Usage

```sh
# 1. What is in the file?
python mcap_parse.py info data.mcap

# 2. Export all topics to CSV
python mcap_parse.py export data.mcap --out out_dir

# 3. Export only some topics
python mcap_parse.py export data.mcap --out out_dir --topics /joint_states /chatter

# 4. Keep longer arrays (default: arrays or byte fields longer than 32 items are replaced by their length)
python mcap_parse.py export data.mcap --out out_dir --max-list 64
```

### `info` output

```
file      : samples/img_ros2.mcap
profile   : ros2   library: mcap-python/1.6.0 mcap-ros2-support/0.6.0
messages  : 3
time (UTC): 2026-10-23T04:04:32.000000+00:00 ~ 2026-10-23T04:04:34.000000+00:00  (2.0 s)
chunks    : 1 {'zstd': 1}   attachments: 0   metadata: 0

    count  msg_enc   schema(enc)                            topic
        3  cdr       sensor_msgs/msg/Image(ros2msg)         /cam/image_raw
```

- `msg_enc` is the message encoding. It decides which decoder is used.
- `schema(enc)` is the message type and the format of its definition.
- `chunks` shows the compression. zstd and lz4 are opened automatically.

### `export` output

- One CSV per topic. The file name is the topic name with `/` replaced by `_` (for example `/cam/image_raw` becomes `cam_image_raw.csv`).
- `_topics.csv` lists every topic with its row count, column count and CSV name.
- CSVs are written as UTF-8 with BOM, so Excel shows non-ASCII text correctly.
- Rows are in log-time order.
- A requested topic that does not exist gives a warning (check the name with `info`).

Every CSV starts with these columns:

| Column | Meaning |
|---|---|
| `log_time_ns` | Time the recorder received the message, UTC, nanoseconds since epoch |
| `publish_time_ns` | Time the publisher sent the message (same as log time if the writer did not set it) |
| `log_time_utc` | `log_time_ns` as a readable UTC string |
| `sequence` | Message sequence number in the MCAP file |

The message body follows, flattened into columns:

- Nested fields: `header.stamp.sec`
- Array items: `position.0`, `position.1`, ...
- JSON objects: `tags.temperature.value`

Example (`/cam/image_raw` from the sample, with `mcap-ros2-support` installed):

```
log_time_ns,publish_time_ns,log_time_utc,sequence,header.stamp.sec,header.stamp.nanosec,header.frame_id,height,width,encoding,is_bigendian,step,data
1792728272000000000,1792728272000000000,2026-10-23T04:04:32.000000+00:00,0,1792728272,0,cam01,10,100,mono8,0,100,<bytes len=1000>
```

Without `mcap-ros2-support` the same row becomes:

```
log_time_ns,publish_time_ns,log_time_utc,sequence,_undecoded_bytes
1792728272000000000,1792728272000000000,2026-10-23T04:04:32.000000+00:00,0,1052
```

### How each encoding is handled

| Encoding | How it is read | Result |
|---|---|---|
| `json` | `json.loads` | All fields as columns. Invalid JSON is kept as `_raw` (first 200 bytes as text) |
| `cdr` (ROS 2) | `mcap-ros2-support`, using the schema stored in the file | All fields as columns |
| `protobuf` | `mcap-protobuf-support` | All fields as columns (not tested) |
| anything else, or decoder not installed | not decoded | `_undecoded_bytes` = message length, plus a warning |

Long arrays and byte fields (images, point clouds) longer than `--max-list` are written as `<list len=N>` or `<bytes len=N>`, so the CSV stays small. Shorter byte fields are written as base64.

## Sample

`samples/img_ros2.mcap` (1.3 KB) holds 3 `sensor_msgs/msg/Image` messages on `/cam/image_raw` (width 100, height 10, `mono8`, 1,000-byte data, zstd chunk). It was written with Python `mcap` and `mcap-ros2-support` and contains no recorded data.

Try it:

```sh
python mcap_parse.py info   samples/img_ros2.mcap
python mcap_parse.py export samples/img_ros2.mcap --out out_img
python mcap_parse.py export samples/img_ros2.mcap --out out_img_full --max-list 2000
```

The first export writes the image data as `<bytes len=1000>`. With `--max-list 2000` it is written as base64.

## Test results

With the sample in this repository:

| Test | Result |
|---|---|
| `info` | 1 topic, 3 messages, 2.0 s, zstd chunk |
| `export` | 3 rows × 13 columns, image data written as `<bytes len=1000>` |
| `export --max-list 2000` | Image data written as base64 |
| `export` without `mcap-ros2-support` | Warning, 3 rows × 5 columns with `_undecoded_bytes` = 1052 |
| `export --topics` with an unknown topic | Warning, the run continues |

Also tested on files that are not included in this repository:

| File | Result |
|---|---|
| Bag recorded with `ros2 bag record -s mcap` on ROS 2 Jazzy (C++ `libmcap` 1.3.1, uncompressed chunk), `JointState` and `String`, 135 messages | All messages read, values equal to what was published. `publish_time` and `log_time` stored separately (about 0.2 ms apart) |
| ROS 2 `JointState` file, 29,702 messages on 8 topics, zstd | Values equal to the source data. Topics and counts equal to `ros2 bag info` (ROS 2 Jazzy) |
| JSON file with nested payloads, 125,368 messages on 485 topics | 485 CSVs in about 11 s. One topic checked against its source data with no differences |
| Hourly collector file (mcap-go 1.9.0 writer, JSON, zstd, 152,764 messages) | `info` reads it. Exporting one topic gives 1,755 rows × 75 columns in about 1 s |

## Limitations

- `export` keeps all rows of the selected topics in memory before writing. For large files, export only the topics you need with `--topics`.
- Not tested:
  - Encodings other than json, cdr and protobuf (ros1, flatbuffer, cbor): only the byte length is written.
  - Schemas in `ros2idl` format.
  - Files of several GB.
  - Damaged files without a summary section: `info` says so, `export` is not tested on them.
  - `log_time` values that are not epoch times (for example simulation time).
