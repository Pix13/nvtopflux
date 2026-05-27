# AGENTS.md — Feature: nvtop loop JSON exporter to InfluxDB + Grafana dashboard

## Goal

Implement a Python exporter that reads the JSON stream produced by `nvtop` in loop mode and exports **all GPU and process data** to InfluxDB once per second.

At the end of the development, the agent must also write a `README.md` describing usage, then provide the complete InfluxDB and Grafana configuration needed to generate a dashboard.

## Feature summary

`nvtop` is expected to emit a JSON payload every second, similar to:

```json
[
  {
    "device_name": "AMD BC-250",
    "gpu_clock": "1000MHz",
    "mem_clock": "450MHz",
    "temp": "36C",
    "fan_speed": "CPU Fan",
    "power_draw": "40W",
    "gpu_util": null,
    "encode": null,
    "decode": null,
    "mem_util": "0%",
    "mem_total": "17716740096",
    "mem_used": "18620416",
    "mem_free": "17698119680",
    "processes": [
      {
        "pid": "229655",
        "cmdline": "nvtop -s",
        "kind": "graphic",
        "user": "dev",
        "gpu_usage": null,
        "gpu_mem_bytes_alloc": "12288",
        "gpu_mem_usage": "0%",
        "encode": null,
        "decode": null
      }
    ]
  }
]
```

The exporter must ingest every emitted JSON object and write metrics to InfluxDB.

## Responsibilities of the implementation agent

The implementation agent must:

0. Always use the uv tool to create Python venv
1. Create a Python exporter.
2. Read `nvtop` JSON data continuously from either:
   - `stdin`, for piping from `nvtop`, or
   - a subprocess running the configured `nvtop` command.
3. Parse and normalize every field in the JSON.
4. Export GPU-level data to InfluxDB.
5. Export process-level data to InfluxDB.
6. Preserve null fields as absent InfluxDB fields, not as string values.
7. Provide Docker Compose configuration for InfluxDB and Grafana.
8. Provide Grafana provisioning files.
9. Provide an automatically provisioned dashboard.
10. Write a final `README.md` explaining installation, configuration, execution, and dashboard usage.

## Expected repository layout

The final repository should follow this structure:

```text
.
├── AGENTS.md
├── README.md
├── requirements.txt
├── docker-compose.yml
├── .env.example
├── src/
│   └── nvtop_influx_exporter.py
├── config/
│   └── exporter.example.yaml
└── grafana/
    └── provisioning/
        ├── datasources/
        │   └── influxdb.yml
        └── dashboards/
            ├── dashboards.yml
            └── nvtop-dashboard.json
```

## Python exporter requirements

### Runtime

Use Python 3.11 or newer.

Required dependencies:

```text
influxdb-client
PyYAML
python-dotenv
```

The exporter must be able to run with:

```bash
python3 src/nvtop_influx_exporter.py --config config/exporter.yaml
```

It must also support piping:

```bash
nvtop -s | python3 src/nvtop_influx_exporter.py --stdin --config config/exporter.yaml
```

If the exact `nvtop` JSON loop command differs, make it configurable.

## Configuration file

Create `config/exporter.example.yaml`:

```yaml
influxdb:
  url: "http://localhost:8086"
  token: "${INFLUXDB_TOKEN}"
  org: "gpu-monitoring"
  bucket: "nvtop"

nvtop:
  command: ["nvtop", "-s"]
  interval_seconds: 1

exporter:
  hostname_tag: true
  flush_interval_ms: 1000
  log_level: "INFO"
```

Environment variables in the YAML file must be expanded.

## InfluxDB data model

Use two measurements:

1. `gpu_stats`
2. `gpu_process_stats`

### Measurement: gpu_stats

One point per GPU per loop iteration.

#### Tags

Use the following tags:

| Tag | Source |
|---|---|
| `device_name` | GPU `device_name` |
| `host` | system hostname, if enabled |

#### Fields

Export all GPU-level JSON fields except `processes`.

| Field | Example input | Influx field type | Normalized value |
|---|---:|---|---:|
| `gpu_clock_mhz` | `1000MHz` | float | `1000` |
| `mem_clock_mhz` | `450MHz` | float | `450` |
| `temp_celsius` | `36C` | float | `36` |
| `fan_speed` | `CPU Fan` | string | `CPU Fan` |
| `power_draw_watts` | `40W` | float | `40` |
| `gpu_util_percent` | `null` | float | omitted when null |
| `encode_percent` | `null` | float | omitted when null |
| `decode_percent` | `null` | float | omitted when null |
| `mem_util_percent` | `0%` | float | `0` |
| `mem_total_bytes` | `17716740096` | int | `17716740096` |
| `mem_used_bytes` | `18620416` | int | `18620416` |
| `mem_free_bytes` | `17698119680` | int | `17698119680` |

### Measurement: gpu_process_stats

One point per GPU process per loop iteration.

#### Tags

Use the following tags:

| Tag | Source |
|---|---|
| `device_name` | parent GPU `device_name` |
| `host` | system hostname, if enabled |
| `pid` | process `pid` |
| `user` | process `user` |
| `kind` | process `kind` |

Do **not** use `cmdline` as a tag, because it may have high cardinality.

#### Fields

| Field | Example input | Influx field type | Normalized value |
|---|---:|---|---:|
| `cmdline` | `nvtop -s` | string | `nvtop -s` |
| `gpu_usage_percent` | `null` | float | omitted when null |
| `gpu_mem_bytes_alloc` | `12288` | int | `12288` |
| `gpu_mem_usage_percent` | `0%` | float | `0` |
| `encode_percent` | `null` | float | omitted when null |
| `decode_percent` | `null` | float | omitted when null |

## Parsing and normalization rules

The exporter must include safe parsers for values returned by `nvtop`:

- `"1000MHz"` -> `1000.0`
- `"450MHz"` -> `450.0`
- `"36C"` -> `36.0`
- `"40W"` -> `40.0`
- `"0%"` -> `0.0`
- `"17716740096"` -> `17716740096`
- `null` -> omitted field
- unknown strings such as `"CPU Fan"` -> preserved as strings only where expected

The parser must be defensive:

- Do not crash on missing fields.
- Do not crash on unknown extra fields.
- Log malformed JSON and continue.
- Log conversion errors and skip only the invalid field.
- Keep the exporter running unless InfluxDB configuration is invalid.

## JSON stream handling

The exporter must support looped JSON output.

Depending on how `nvtop` emits JSON, the stream may contain:

1. One complete JSON array per line.
2. Pretty-printed multi-line JSON arrays.
3. Multiple arrays emitted sequentially.

The implementation must handle at least line-delimited JSON. If multi-line JSON is required, implement a small buffering parser that waits until brackets are balanced before decoding.

Recommended approach:

- Read from `stdin` or subprocess stdout.
- Accumulate text into a buffer.
- Track bracket depth for `[` and `]`.
- When depth returns to zero, parse the buffered JSON.
- Reset buffer.

## InfluxDB writing behavior

Use the official `influxdb-client` Python library.

The exporter must:

- Create one `Point` per GPU.
- Create one `Point` per process.
- Use a single timestamp for all points from the same nvtop JSON batch.
- Write points to the configured org and bucket.
- Batch writes where possible.
- Flush at least every second.
- Log successful connection at startup.
- Log write errors with enough detail to debug token, org, bucket, or network issues.

## Suggested Python implementation details

Create these functions in `src/nvtop_influx_exporter.py`:

```python
def parse_numeric(value: object, suffix: str | None = None, as_int: bool = False) -> int | float | None:
    """Parse nvtop numeric strings such as 1000MHz, 36C, 40W, 0%, or byte strings."""


def normalize_gpu_fields(gpu: dict) -> dict:
    """Return normalized GPU-level Influx fields."""


def normalize_process_fields(process: dict) -> dict:
    """Return normalized process-level Influx fields."""


def build_gpu_point(gpu: dict, host: str | None, timestamp) -> Point:
    """Build one gpu_stats point."""


def build_process_points(gpu: dict, host: str | None, timestamp) -> list[Point]:
    """Build gpu_process_stats points for all processes attached to a GPU."""


def iter_json_batches(stream) -> Iterator[list[dict]]:
    """Yield decoded nvtop JSON arrays from a continuous stream."""


def run_exporter(config: dict, use_stdin: bool = False) -> None:
    """Main exporter loop."""
```

## Required CLI options

The script must support:

```text
--config PATH       Path to YAML config file
--stdin             Read nvtop JSON from stdin instead of spawning nvtop
--once              Read and export one JSON batch, then exit
--dry-run           Parse and print normalized points without writing to InfluxDB
--log-level LEVEL   Override configured log level
```

## Docker Compose requirements

Create `docker-compose.yml` with:

- InfluxDB 2.x
- Grafana
- persistent volumes
- ports:
  - InfluxDB: `8086:8086`
  - Grafana: `3000:3000`

Use environment variables from `.env`.

Required services:

```yaml
services:
  influxdb:
    image: influxdb:2
    container_name: nvtop-influxdb
    ports:
      - "8086:8086"
    environment:
      DOCKER_INFLUXDB_INIT_MODE: setup
      DOCKER_INFLUXDB_INIT_USERNAME: ${INFLUXDB_USERNAME}
      DOCKER_INFLUXDB_INIT_PASSWORD: ${INFLUXDB_PASSWORD}
      DOCKER_INFLUXDB_INIT_ORG: ${INFLUXDB_ORG}
      DOCKER_INFLUXDB_INIT_BUCKET: ${INFLUXDB_BUCKET}
      DOCKER_INFLUXDB_INIT_ADMIN_TOKEN: ${INFLUXDB_TOKEN}
    volumes:
      - influxdb-data:/var/lib/influxdb2
      - influxdb-config:/etc/influxdb2

  grafana:
    image: grafana/grafana:latest
    container_name: nvtop-grafana
    ports:
      - "3000:3000"
    environment:
      GF_SECURITY_ADMIN_USER: ${GRAFANA_ADMIN_USER}
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD}
    volumes:
      - grafana-data:/var/lib/grafana
      - ./grafana/provisioning:/etc/grafana/provisioning:ro
    depends_on:
      - influxdb

volumes:
  influxdb-data:
  influxdb-config:
  grafana-data:
```

## `.env.example`

Create:

```dotenv
INFLUXDB_USERNAME=admin
INFLUXDB_PASSWORD=change-me-strong-password
INFLUXDB_ORG=gpu-monitoring
INFLUXDB_BUCKET=nvtop
INFLUXDB_TOKEN=change-me-super-secret-token

GRAFANA_ADMIN_USER=admin
GRAFANA_ADMIN_PASSWORD=admin
```

## Grafana datasource provisioning

Create `grafana/provisioning/datasources/influxdb.yml`:

```yaml
apiVersion: 1

datasources:
  - name: InfluxDB-nvtop
    type: influxdb
    access: proxy
    url: http://influxdb:8086
    jsonData:
      version: Flux
      organization: gpu-monitoring
      defaultBucket: nvtop
      tlsSkipVerify: true
    secureJsonData:
      token: ${INFLUXDB_TOKEN}
    editable: true
```

If Grafana environment variable expansion does not work for `secureJsonData.token`, document that the token can be entered manually in Grafana or provisioned with an explicit generated file.

## Grafana dashboard requirements

Create `grafana/provisioning/dashboards/dashboards.yml`:

```yaml
apiVersion: 1

providers:
  - name: nvtop
    orgId: 1
    folder: GPU Monitoring
    type: file
    disableDeletion: false
    updateIntervalSeconds: 10
    allowUiUpdates: true
    options:
      path: /etc/grafana/provisioning/dashboards
```

Create `grafana/provisioning/dashboards/nvtop-dashboard.json`.

The dashboard must include panels for:

1. GPU temperature in Celsius.
2. GPU power draw in watts.
3. GPU clock in MHz.
4. GPU memory clock in MHz.
5. GPU memory used in bytes.
6. GPU memory free in bytes.
7. GPU memory utilization percent.
8. GPU utilization percent, when available.
9. Encode utilization percent, when available.
10. Decode utilization percent, when available.
11. Per-process allocated GPU memory.
12. Per-process GPU memory usage percent.
13. Per-process GPU usage percent, when available.
14. Process table containing `pid`, `user`, `kind`, `cmdline`, and memory usage.

Use Flux queries against the `nvtop` bucket.

Example GPU temperature Flux query:

```flux
from(bucket: "nvtop")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "gpu_stats")
  |> filter(fn: (r) => r._field == "temp_celsius")
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> yield(name: "mean")
```

Example process memory query:

```flux
from(bucket: "nvtop")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "gpu_process_stats")
  |> filter(fn: (r) => r._field == "gpu_mem_bytes_alloc")
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> yield(name: "mean")
```

## README.md requirements

At the end of the development, the agent must write a complete `README.md`.

The README must include:

1. Project overview.
2. Architecture diagram in text form.
3. Requirements.
4. Installation steps.
5. InfluxDB/Grafana startup instructions.
6. Exporter configuration.
7. Running the exporter from a subprocess.
8. Running the exporter from stdin.
9. Dry-run mode.
10. Example `nvtop` JSON input.
11. InfluxDB measurements and fields.
12. Grafana dashboard provisioning.
13. Troubleshooting.
14. Security notes about tokens and passwords.
15. Known limitations.

## Acceptance criteria

The feature is complete only when all of these criteria are met:

- A Python exporter exists under `src/nvtop_influx_exporter.py`.
- The exporter reads looped JSON from `nvtop`.
- The exporter supports stdin mode.
- The exporter exports GPU-level metrics to `gpu_stats`.
- The exporter exports process-level metrics to `gpu_process_stats`.
- All JSON fields from the sample are represented in InfluxDB as either tags or fields.
- Null values are omitted, not stringified.
- Units are normalized into numeric fields.
- `cmdline` is stored as a field, not a tag.
- The exporter can run in dry-run mode without InfluxDB.
- Docker Compose starts InfluxDB and Grafana.
- Grafana datasource provisioning exists.
- Grafana dashboard provisioning exists.
- A dashboard JSON file exists and contains panels for GPU and process metrics.
- `README.md` explains the full usage flow.

## Testing requirements

The agent must add basic tests or at minimum a testable dry-run workflow.

Recommended test command:

```bash
cat sample-nvtop.json | python3 src/nvtop_influx_exporter.py --stdin --once --dry-run --config config/exporter.example.yaml
```

Expected dry-run output must show normalized points similar to:

```text
gpu_stats,device_name=AMD BC-250 gpu_clock_mhz=1000,mem_clock_mhz=450,temp_celsius=36,power_draw_watts=40,mem_util_percent=0,mem_total_bytes=17716740096,mem_used_bytes=18620416,mem_free_bytes=17698119680

gpu_process_stats,device_name=AMD BC-250,pid=229655,user=dev,kind=graphic cmdline="nvtop -s",gpu_mem_bytes_alloc=12288,gpu_mem_usage_percent=0
```

## Edge cases to handle

The exporter must handle:

- Several GPUs in the same JSON array.
- GPUs without running processes.
- Missing `processes` key.
- Empty `processes` list.
- Unknown future fields from `nvtop`.
- Null GPU utilization values.
- Null encode/decode values.
- InfluxDB temporarily unavailable.
- Malformed JSON batch.
- Interrupted subprocess.
- SIGINT/SIGTERM shutdown with clean flush.

## Logging requirements

The exporter must log:

- Startup configuration summary, excluding secrets.
- InfluxDB target URL, org, and bucket.
- Whether stdin or subprocess mode is used.
- Number of GPU points and process points written per batch at debug level.
- JSON parse errors.
- InfluxDB write errors.
- Clean shutdown.

Do not log the InfluxDB token.

## Security requirements

- Do not commit real tokens.
- Use `.env` for secrets.
- Provide `.env.example` only.
- Mask tokens in logs.
- Document that Grafana and InfluxDB default passwords must be changed.

## Implementation guidance

Prefer correctness and maintainability over over-engineering.

The exporter should be small, readable, and easy to run on a GPU host. It does not need to run inside Docker by default, because it needs access to `nvtop` and the local GPU stack. Docker is only required for InfluxDB and Grafana unless the project later adds a privileged GPU-aware exporter container.

