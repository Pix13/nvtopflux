#!/usr/bin/env python3
"""nvtop JSON stream exporter to InfluxDB."""

import argparse
import json
import logging
import os
import platform
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Iterator, Optional

import yaml
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

DEFAULT_CONFIG = "/etc/nvtopflux/config.cfg"

logger = logging.getLogger("nvtop_exporter")


def load_config(path: str) -> dict:
    """Load YAML config with env var expansion."""
    load_dotenv()
    with open(path) as f:
        raw = f.read()
    # Expand ${VAR} and $VAR patterns from environment
    raw = re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), m.group(0)), raw)
    raw = re.sub(r"\$(\w+)", lambda m: os.environ.get(m.group(1), m.group(0)), raw)
    return yaml.safe_load(raw)


def parse_numeric(value, suffix: Optional[str] = None, as_int: bool = False):
    """Parse nvtop numeric strings such as 1000MHz, 36C, 40W, 0%, or byte strings."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if as_int else float(value)

    s = str(value).strip()
    if not s:
        return None

    if suffix:
        s = s.replace(suffix, "")
    s = s.strip()

    try:
        return int(s) if as_int else float(s)
    except (ValueError, TypeError) as e:
        logger.debug("Failed to parse numeric value %r: %s", value, e)
        return None


def normalize_gpu_fields(gpu: dict) -> dict:
    """Return normalized GPU-level Influx fields."""
    fields = {}

    result = parse_numeric(gpu.get("gpu_clock"), suffix="MHz")
    if result is not None:
        fields["gpu_clock_mhz"] = result

    result = parse_numeric(gpu.get("mem_clock"), suffix="MHz")
    if result is not None:
        fields["mem_clock_mhz"] = result

    result = parse_numeric(gpu.get("temp"), suffix="C")
    if result is not None:
        fields["temp_celsius"] = result

    # fan_speed is a string, preserve as-is (not numeric)
    fan = gpu.get("fan_speed")
    if fan is not None:
        fields["fan_speed"] = str(fan)

    result = parse_numeric(gpu.get("power_draw"), suffix="W")
    if result is not None:
        fields["power_draw_watts"] = result

    result = parse_numeric(gpu.get("gpu_util"), suffix="%")
    if result is not None:
        fields["gpu_util_percent"] = result

    result = parse_numeric(gpu.get("encode"), suffix="%")
    if result is not None:
        fields["encode_percent"] = result

    result = parse_numeric(gpu.get("decode"), suffix="%")
    if result is not None:
        fields["decode_percent"] = result

    result = parse_numeric(gpu.get("mem_util"), suffix="%")
    if result is not None:
        fields["mem_util_percent"] = result

    result = parse_numeric(gpu.get("mem_total"), as_int=True)
    if result is not None:
        fields["mem_total_bytes"] = int(result)

    result = parse_numeric(gpu.get("mem_used"), as_int=True)
    if result is not None:
        fields["mem_used_bytes"] = int(result)

    result = parse_numeric(gpu.get("mem_free"), as_int=True)
    if result is not None:
        fields["mem_free_bytes"] = int(result)

    return fields


def normalize_process_fields(process: dict) -> dict:
    """Return normalized process-level Influx fields."""
    fields = {}

    cmdline = process.get("cmdline")
    if cmdline is not None:
        fields["cmdline"] = str(cmdline)

    result = parse_numeric(process.get("gpu_usage"), suffix="%")
    if result is not None:
        fields["gpu_usage_percent"] = result

    result = parse_numeric(process.get("gpu_mem_bytes_alloc"), as_int=True)
    if result is not None:
        fields["gpu_mem_bytes_alloc"] = int(result)

    result = parse_numeric(process.get("gpu_mem_usage"), suffix="%")
    if result is not None:
        fields["gpu_mem_usage_percent"] = result

    result = parse_numeric(process.get("encode"), suffix="%")
    if result is not None:
        fields["encode_percent"] = result

    result = parse_numeric(process.get("decode"), suffix="%")
    if result is not None:
        fields["decode_percent"] = result

    return fields


def build_gpu_point(gpu: dict, host: Optional[str], timestamp) -> Point:
    """Build one gpu_stats point."""
    point = Point("gpu_stats").time(timestamp, "ns")
    if gpu.get("device_name"):
        point.tag("device_name", gpu["device_name"])
    if host:
        point.tag("host", host)

    for field_name, field_value in normalize_gpu_fields(gpu).items():
        point.field(field_name, field_value)

    return point


def build_process_points(gpu: dict, host: Optional[str], timestamp) -> list:
    """Build gpu_process_stats points for all processes attached to a GPU."""
    points = []
    processes = gpu.get("processes")
    if not processes:
        return points

    device_name = gpu.get("device_name", "unknown")
    for proc in processes:
        point = Point("gpu_process_stats").time(timestamp, "ns")
        point.tag("device_name", device_name)
        if host:
            point.tag("host", host)
        if proc.get("pid"):
            point.tag("pid", str(proc["pid"]))
        if proc.get("user"):
            point.tag("user", proc["user"])
        if proc.get("kind"):
            point.tag("kind", proc["kind"])

        for field_name, field_value in normalize_process_fields(proc).items():
            point.field(field_name, field_value)

        points.append(point)

    return points


def iter_json_batches(stream) -> Iterator[list]:
    """Yield decoded nvtop JSON arrays from a continuous stream."""
    buffer = ""
    depth = 0

    for line in stream:
        line = line.rstrip("\n\r")
        if not line.strip():
            continue

        buffer += line
        for ch in line:
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1

        if depth <= 0 and depth != 0:
            # Went negative, reset
            buffer = ""
            depth = 0
            continue

        if depth == 0 and buffer.strip():
            try:
                data = json.loads(buffer)
                buffer = ""
                yield data
            except json.JSONDecodeError as e:
                logger.error("JSON parse error: %s (buffer: %r)", e, buffer[:200])
                buffer = ""
                depth = 0
                continue


def setup_logging(level_str: str) -> None:
    """Configure root logger."""
    level = getattr(logging, level_str.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def run_exporter(config: dict, use_stdin: bool = False, once: bool = False, dry_run: bool = False) -> None:
    """Main exporter loop."""
    influx_cfg = config.get("influxdb", {})
    nvtop_cfg = config.get("nvtop", {})
    exporter_cfg = config.get("exporter", {})

    url = influx_cfg.get("url", "http://localhost:8086")
    token = influx_cfg.get("token", "")
    org = influx_cfg.get("org", "gpu-monitoring")
    bucket = influx_cfg.get("bucket", "nvtop")

    hostname = platform.node() if exporter_cfg.get("hostname_tag", True) else None
    flush_interval = exporter_cfg.get("flush_interval_ms", 1000)

    # Log startup config (mask token)
    masked_token = token[:6] + "..." if len(token) > 6 else "****"
    logger.info("Starting nvtop exporter")
    logger.info("InfluxDB target: %s (org=%s, bucket=%s, token=%s)", url, org, bucket, masked_token)
    logger.info("Mode: %s", "stdin" if use_stdin else "subprocess")
    if once:
        logger.info("One-shot mode: will exit after first batch")

    # Initialize InfluxDB client (skip in dry-run)
    write_client = None
    write_api = None
    if not dry_run:
        try:
            write_client = InfluxDBClient(url=url, token=token, org=org)
            write_api = write_client.write_api(
                write_options=SYNCHRONOUS,
            )
            # Test connection
            write_client.ping()
            logger.info("Connected to InfluxDB at %s", url)
        except Exception as e:
            logger.error("Failed to connect to InfluxDB: %s", e)
            logger.error("Check url=%s, org=%s, token configuration", url, org)
            sys.exit(1)

    # Determine input stream
    if use_stdin:
        stream = sys.stdin
        subprocess_handle = None
    else:
        cmd = nvtop_cfg.get("command", ["nvtop", "-s"])
        logger.info("Spawning nvtop: %s", " ".join(cmd))
        try:
            subprocess_handle = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stream = subprocess_handle.stdout
        except FileNotFoundError:
            logger.error("nvtop command not found: %s", cmd[0])
            sys.exit(1)
        except Exception as e:
            logger.error("Failed to start nvtop subprocess: %s", e)
            sys.exit(1)

    # Signal handling for clean shutdown
    shutdown = False

    def handle_signal(signum, frame):
        nonlocal shutdown
        sig_name = signal.Signals(signum).name
        logger.info("Received %s, initiating shutdown...", sig_name)
        shutdown = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    batch_count = 0

    try:
        for batch in iter_json_batches(stream):
            if shutdown:
                break

            batch_count += 1
            timestamp = datetime.now(timezone.utc)

            gpu_points = []
            process_points = []

            for gpu in batch:
                gpu_points.append(build_gpu_point(gpu, hostname, timestamp))
                process_points.extend(build_process_points(gpu, hostname, timestamp))

            all_points = gpu_points + process_points

            logger.debug(
                "Batch %d: %d GPU points, %d process points",
                batch_count,
                len(gpu_points),
                len(process_points),
            )

            if dry_run:
                for pt in all_points:
                    print(pt)
                print()  # blank line between batches
            else:
                try:
                    write_api.write(bucket=bucket, org=org, record=all_points)
                    logger.debug("Wrote %d points to InfluxDB", len(all_points))
                except Exception as e:
                    logger.error("InfluxDB write error: %s", e)

            if once:
                break

    except KeyboardInterrupt:
        logger.info("Interrupted by keyboard")
    finally:
        # Cleanup
        if write_api:
            try:
                write_api.flush()
            except Exception:
                pass
        if write_client:
            write_client.close()
        if subprocess_handle:
            if subprocess_handle.poll() is None:
                subprocess_handle.terminate()
                try:
                    subprocess_handle.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    subprocess_handle.kill()

        logger.info("Exporter stopped after %d batches", batch_count)


def main():
    parser = argparse.ArgumentParser(description="nvtop JSON stream exporter to InfluxDB")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"Path to YAML config file (default: {DEFAULT_CONFIG})")
    parser.add_argument("--stdin", action="store_true", dest="use_stdin", help="Read nvtop JSON from stdin")
    parser.add_argument("--once", action="store_true", help="Read one batch then exit")
    parser.add_argument("--dry-run", action="store_true", help="Parse and print points without writing")
    parser.add_argument("--log-level", default=None, help="Override log level")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        logger.error("Config file not found: %s", args.config)
        sys.exit(1)

    config = load_config(args.config)

    log_level = args.log_level or config.get("exporter", {}).get("log_level", "INFO")
    setup_logging(log_level)

    run_exporter(
        config=config,
        use_stdin=args.use_stdin,
        once=args.once,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
