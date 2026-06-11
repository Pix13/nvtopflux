# nvtop InfluxDB Exporter

Exports GPU metrics from `nvtop` JSON output to InfluxDB, with a pre-configured Grafana dashboard.

## Architecture

```
┌──────────┐     JSON stream       ┌──────────────┐       Points       ┌──────────┐
│  nvtop   │ ─────────────────────►│  Exporter    │ ──────────────────►│ InfluxDB │
│  (local) │                       │  (Python)    │                     │  :8086   │
└──────────┘                       └──────────────┘                     └────┬─────┘
                                                                              │
                                                                        query │
                                                                        ▼     │
                                                                        ┌─────┴─────┐
                                                                        │  Grafana   │
                                                                        │  :3000     │
                                                                        └───────────┘
```

## Requirements

- Python 3.11+
- `nvtop` installed on the host
- InfluxDB 2.x
- Grafana (optional, for dashboard)
- Docker + Docker Compose (for InfluxDB/Grafana stack)

## Installation

```bash
# Clone the repo
git clone <repo-url>
cd nvtopflux

# Create virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Or use uv
uv venv
uv pip install -r requirements.txt
```

## InfluxDB / Grafana Startup

```bash
# Copy and edit environment variables
cp .env.example .env
# Edit .env with your credentials

# Start InfluxDB and Grafana
docker compose up -d

# Wait for InfluxDB to initialize (~10 seconds)
sleep 10

# Verify InfluxDB is running
curl http://localhost:8086/ping
```

## Exporter Configuration

```bash
# Copy and edit the config
cp etc/config.example.cfg etc/config.cfg
```

Configuration options:

| Key | Description |
|---|---|
| `influxdb.url` | InfluxDB HTTP URL |
| `influxdb.token` | InfluxDB API token (supports `${ENV_VAR}`) |
| `influxdb.org` | InfluxDB organization |
| `influxdb.bucket` | InfluxDB bucket name |
| `nvtop.command` | Command to run nvtop (subprocess mode) |
| `exporter.hostname_tag` | Add hostname as tag (default: `true`) |
| `exporter.log_level` | Log level: DEBUG, INFO, WARNING, ERROR |

Environment variables in the YAML file are expanded automatically. Set `INFLUXDB_TOKEN` in your `.env` file.

## Running the Exporter

### Subprocess mode (default)

The exporter spawns `nvtop` automatically:

```bash
# First, create your config from the example
cp etc/config.example.cfg etc/config.cfg
# Edit etc/config.cfg with your InfluxDB credentials

.venv/bin/python src/nvtop_influx_exporter.py --config etc/config.cfg
```

### Stdin mode (pipe from nvtop)

```bash
nvtop -s -l | .venv/bin/python src/nvtop_influx_exporter.py --stdin --config etc/config.cfg
```

### One-shot mode

Read one JSON batch, export it, then exit:

```bash
.venv/bin/python src/nvtop_influx_exporter.py --config etc/config.cfg --once
```

### Dry-run mode

Parse and print normalized Line Protocol points without writing to InfluxDB:

```bash
cat sample-nvtop.json | .venv/bin/python src/nvtop_influx_exporter.py --stdin --once --dry-run --config etc/config.cfg
```

Example output:

```
gpu_stats,device_name=AMD\ BC-250,host=myhost gpu_clock_mhz=1000,mem_clock_mhz=450,temp_celsius=36,power_draw_watts=40,mem_util_percent=0,mem_total_bytes=17716740096i,mem_used_bytes=18620416i,mem_free_bytes=17698119680i

gpu_process_stats,device_name=AMD\ BC-250,host=myhost,pid=229655,user=dev,kind=graphic cmdline="nvtop -s",gpu_mem_bytes_alloc=12288i,gpu_mem_usage_percent=0
```

## Systemd Service (Recommended for Production)

Install as a systemd service for automatic startup and restart:

```bash
# 1. Create config directory and install config
sudo mkdir -p /etc/nvtopflux
sudo cp etc/config.example.cfg /etc/nvtopflux/config.cfg

# 2. Edit the config with your InfluxDB credentials
sudo editor /etc/nvtopflux/config.cfg

# 3. Install the exporter script
sudo cp src/nvtop_influx_exporter.py /usr/local/bin/nvtopflux
sudo chmod +x /usr/local/bin/nvtopflux

# 4. Install systemd unit
sudo cp systemd/nvtopflux.service /etc/systemd/system/

# 5. Enable and start
sudo systemctl daemon-reload
sudo systemctl enable --now nvtopflux

# 6. Check status
sudo systemctl status nvtopflux
sudo journalctl -u nvtopflux -f
```

The service runs as `root` (required for `nvtop` GPU access), restarts on failure, and logs to the systemd journal.

### Running from venv with systemd

If you prefer to keep the venv-based setup, update `ExecStart` in the unit file:

```ini
[Service]
ExecStart=/path/to/nvtopflux/.venv/bin/python /usr/local/bin/nvtopflux
```

## CLI Options

| Option | Description |
|---|---|
| `--config PATH` | Path to YAML config file (default: `/etc/nvtopflux/config.cfg`) |
| `--stdin` | Read nvtop JSON from stdin instead of spawning nvtop |
| `--once` | Read one JSON batch then exit |
| `--dry-run` | Parse and print points without writing to InfluxDB |
| `--log-level LEVEL` | Override log level (DEBUG, INFO, WARNING, ERROR) |

## InfluxDB Measurements

### gpu_stats

GPU-level metrics, one point per GPU per iteration.

**Tags:** `device_name`, `host`

**Fields:**

| Field | Type | Description |
|---|---|---|
| `gpu_clock_mhz` | float | GPU core clock in MHz |
| `mem_clock_mhz` | float | GPU memory clock in MHz |
| `temp_celsius` | float | GPU temperature in Celsius |
| `fan_speed` | string | Fan speed description |
| `power_draw_watts` | float | Power draw in watts |
| `gpu_util_percent` | float | GPU utilization percentage |
| `encode_percent` | float | Encoder utilization percentage |
| `decode_percent` | float | Decoder utilization percentage |
| `mem_util_percent` | float | Memory utilization percentage |
| `mem_total_bytes` | int | Total GPU memory in bytes |
| `mem_used_bytes` | int | Used GPU memory in bytes |
| `mem_free_bytes` | int | Free GPU memory in bytes |

### gpu_process_stats

Per-process GPU metrics, one point per process per iteration.

**Tags:** `device_name`, `host`, `pid`, `user`, `kind`

**Fields:**

| Field | Type | Description |
|---|---|---|
| `cmdline` | string | Process command line |
| `gpu_usage_percent` | float | Per-process GPU usage |
| `gpu_mem_bytes_alloc` | int | Allocated GPU memory in bytes |
| `gpu_mem_usage_percent` | float | Per-process GPU memory usage |
| `encode_percent` | float | Per-process encoder usage |
| `decode_percent` | float | Per-process decoder usage |

## Grafana Dashboard

The dashboard is auto-provisioned at startup. Access it at `http://localhost:3000` (default credentials: `admin` / `admin`).

Panels include:

1. **GPU Temperature** - stat panel with color thresholds
2. **GPU Power Draw** - stat panel in watts
3. **GPU Utilization %** - time series
4. **GPU Memory Utilization %** - time series
5. **GPU Clock (MHz)** - time series
6. **GPU Memory Clock (MHz)** - time series
7. **GPU Memory Used** - time series in bytes
8. **GPU Memory Free** - time series in bytes
9. **GPU Memory Total** - time series in bytes
10. **GPU Encode Utilization %** - time series
11. **GPU Decode Utilization %** - time series
12. **Per-Process GPU Memory Allocated** - time series
13. **Per-Process GPU Memory Usage %** - time series
14. **Per-Process GPU Usage %** - time series
15. **GPU Process Table** - table with PID, user, kind, command, and memory

### Grafana Token Note

Grafana provisioning may not expand `${INFLUXDB_TOKEN}` in `secureJsonData.token`. If the datasource shows as "Not Connected", enter the token manually in Grafana:

1. Go to **Configuration > Data Sources > InfluxDB-nvtop**
2. Enter your InfluxDB token in the **Token** field
3. Click **Save & Test**

## Example nvtop JSON Input

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

## Troubleshooting

### "nvtop command not found"

Install nvtop:
```bash
# Ubuntu/Debian
sudo apt install nvtop

# Arch
sudo pacman -S nvtop
```

### InfluxDB connection refused

Verify InfluxDB is running:
```bash
docker compose ps
curl http://localhost:8086/ping
```

Check your config:
- URL is correct (`http://localhost:8086`)
- Token matches what you set in `.env`
- Organization and bucket names match

### Grafana datasource "Not Connected"

The token may not have been expanded in the provisioning file. Enter it manually in the Grafana UI (see **Grafana Token Note** above).

### No data in Grafana

1. Verify the exporter is running and not logging errors
2. Check the InfluxDB bucket has data:
   ```bash
   # Use InfluxDB CLI or UI to query
   ```
3. Confirm the Grafana time range covers your data
4. Check that the bucket name matches between exporter config and Grafana datasource

### Exporter crashes on malformed JSON

The exporter logs the error and continues. Check logs for details:
```bash
.venv/bin/python src/nvtop_influx_exporter.py --config etc/config.cfg --log-level DEBUG
```

## Security Notes

- **Never commit real tokens or passwords.** Use `.env` files (gitignored) for secrets.
- Change default InfluxDB and Grafana passwords in `.env`.
- The InfluxDB token is masked in logs.
- Restrict InfluxDB and Grafana network access in production.

## Known Limitations

- The exporter runs on the host (not in Docker) to access `nvtop` and the local GPU stack.
- `nvtop` JSON output format may vary between versions; the exporter handles known formats but may need updates for new fields.
- Multi-GPU systems are supported; each GPU produces its own data points.
- `cmdline` is stored as a field (not a tag) to avoid high cardinality issues.
