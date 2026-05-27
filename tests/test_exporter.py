#!/usr/bin/env python3
"""Test suite for nvtop_influx_exporter.

Uses an InfluxDB stub that writes Line Protocol to /tmp/nvtop_exporter_test.log
instead of a real InfluxDB instance.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure src/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from nvtop_influx_exporter import (
    build_gpu_point,
    build_process_points,
    iter_json_batches,
    normalize_gpu_fields,
    normalize_process_fields,
    parse_numeric,
    run_exporter,
)

TEST_LOG = "/tmp/nvtop_exporter_test.log"


class InfluxDBStub:
    """Stub InfluxDB client that writes Line Protocol to a file."""

    def __init__(self, url=None, token=None, org=None):
        self.url = url
        self.token = token
        self.org = org
        # Truncate the log file on init
        with open(TEST_LOG, "w") as f:
            f.write("")

    def ping(self):
        """Stub ping — always succeeds."""
        pass

    def write_api(self, **kwargs):
        return StubWriteApi()

    def close(self):
        pass


class StubWriteApi:
    """Stub write API that appends Line Protocol to the log file."""

    def write(self, bucket, org, record):
        with open(TEST_LOG, "a") as f:
            if isinstance(record, list):
                for point in record:
                    f.write(str(point) + "\n")
            else:
                f.write(str(record) + "\n")

    def flush(self):
        pass


class StubClientFactory:
    """Context manager that patches InfluxDBClient with our stub."""

    def __enter__(self):
        self.patcher = patch(
            "nvtop_influx_exporter.InfluxDBClient",
            return_value=InfluxDBStub(),
        )
        self.mock_class = self.patcher.start()
        return self

    def __exit__(self, *args):
        self.patcher.stop()


class TestParseNumeric(unittest.TestCase):
    """Test parse_numeric() with all nvtop value formats."""

    def test_mhz_suffix(self):
        self.assertEqual(parse_numeric("1000MHz", suffix="MHz"), 1000.0)
        self.assertEqual(parse_numeric("1440MHz", suffix="MHz"), 1440.0)

    def test_celsius_suffix(self):
        self.assertEqual(parse_numeric("36C", suffix="C"), 36.0)
        self.assertEqual(parse_numeric("85C", suffix="C"), 85.0)

    def test_watt_suffix(self):
        self.assertEqual(parse_numeric("40W", suffix="W"), 40.0)
        self.assertEqual(parse_numeric("250W", suffix="W"), 250.0)

    def test_percent_suffix(self):
        self.assertEqual(parse_numeric("0%", suffix="%"), 0.0)
        self.assertEqual(parse_numeric("100%", suffix="%"), 100.0)
        self.assertEqual(parse_numeric("50.5%", suffix="%"), 50.5)

    def test_raw_int(self):
        self.assertEqual(parse_numeric("17716740096", as_int=True), 17716740096)
        self.assertEqual(parse_numeric("12288", as_int=True), 12288)

    def test_none_returns_none(self):
        self.assertIsNone(parse_numeric(None))
        self.assertIsNone(parse_numeric(None, suffix="MHz"))
        self.assertIsNone(parse_numeric(None, as_int=True))

    def test_empty_string_returns_none(self):
        self.assertIsNone(parse_numeric(""))
        self.assertIsNone(parse_numeric("  "))

    def test_already_numeric(self):
        self.assertEqual(parse_numeric(100), 100.0)
        self.assertEqual(parse_numeric(100, as_int=True), 100)
        self.assertEqual(parse_numeric(3.14), 3.14)

    def test_unparseable_returns_none(self):
        self.assertIsNone(parse_numeric("CPU Fan", suffix="MHz"))


class TestNormalizeGpuFields(unittest.TestCase):
    """Test GPU field normalization."""

    def test_full_gpu(self):
        gpu = {
            "device_name": "AMD BC-250",
            "gpu_clock": "1000MHz",
            "mem_clock": "450MHz",
            "temp": "36C",
            "fan_speed": "CPU Fan",
            "power_draw": "40W",
            "gpu_util": "50%",
            "encode": "10%",
            "decode": "5%",
            "mem_util": "0%",
            "mem_total": "17716740096",
            "mem_used": "18620416",
            "mem_free": "17698119680",
        }
        fields = normalize_gpu_fields(gpu)
        self.assertEqual(fields["gpu_clock_mhz"], 1000.0)
        self.assertEqual(fields["mem_clock_mhz"], 450.0)
        self.assertEqual(fields["temp_celsius"], 36.0)
        self.assertEqual(fields["fan_speed"], "CPU Fan")
        self.assertEqual(fields["power_draw_watts"], 40.0)
        self.assertEqual(fields["gpu_util_percent"], 50.0)
        self.assertEqual(fields["encode_percent"], 10.0)
        self.assertEqual(fields["decode_percent"], 5.0)
        self.assertEqual(fields["mem_util_percent"], 0.0)
        self.assertEqual(fields["mem_total_bytes"], 17716740096)
        self.assertEqual(fields["mem_used_bytes"], 18620416)
        self.assertEqual(fields["mem_free_bytes"], 17698119680)

    def test_null_fields_omitted(self):
        gpu = {
            "gpu_clock": "1000MHz",
            "gpu_util": None,
            "encode": None,
            "decode": None,
        }
        fields = normalize_gpu_fields(gpu)
        self.assertNotIn("gpu_util_percent", fields)
        self.assertNotIn("encode_percent", fields)
        self.assertNotIn("decode_percent", fields)

    def test_missing_fields_no_crash(self):
        gpu = {}
        fields = normalize_gpu_fields(gpu)
        self.assertEqual(fields, {})

    def test_extra_fields_ignored(self):
        gpu = {
            "gpu_clock": "1000MHz",
            "unknown_field": "whatever",
            "another_new_field": 12345,
        }
        fields = normalize_gpu_fields(gpu)
        self.assertNotIn("unknown_field", fields)
        self.assertNotIn("another_new_field", fields)


class TestNormalizeProcessFields(unittest.TestCase):
    """Test process field normalization."""

    def test_full_process(self):
        proc = {
            "pid": "229655",
            "cmdline": "python train.py",
            "kind": "compute",
            "user": "ml",
            "gpu_usage": "85%",
            "gpu_mem_bytes_alloc": "4000000000",
            "gpu_mem_usage": "25%",
            "encode": "2%",
            "decode": "1%",
        }
        fields = normalize_process_fields(proc)
        self.assertEqual(fields["cmdline"], "python train.py")
        self.assertEqual(fields["gpu_usage_percent"], 85.0)
        self.assertEqual(fields["gpu_mem_bytes_alloc"], 4000000000)
        self.assertEqual(fields["gpu_mem_usage_percent"], 25.0)
        self.assertEqual(fields["encode_percent"], 2.0)
        self.assertEqual(fields["decode_percent"], 1.0)

    def test_null_fields_omitted(self):
        proc = {
            "gpu_usage": None,
            "encode": None,
            "decode": None,
        }
        fields = normalize_process_fields(proc)
        self.assertNotIn("gpu_usage_percent", fields)
        self.assertNotIn("encode_percent", fields)
        self.assertNotIn("decode_percent", fields)

    def test_missing_fields_no_crash(self):
        fields = normalize_process_fields({})
        self.assertEqual(fields, {})


class TestBuildPoints(unittest.TestCase):
    """Test Point construction."""

    def test_gpu_point_tags(self):
        gpu = {"device_name": "NVIDIA GPU"}
        ts = 1000000000000
        point = build_gpu_point(gpu, "testhost", ts)
        self.assertIn("device_name", point._tags)
        self.assertEqual(point._tags["device_name"], "NVIDIA GPU")
        self.assertIn("host", point._tags)
        self.assertEqual(point._tags["host"], "testhost")

    def test_gpu_point_no_host(self):
        gpu = {"device_name": "NVIDIA GPU"}
        ts = 1000000000000
        point = build_gpu_point(gpu, None, ts)
        self.assertNotIn("host", point._tags)

    def test_process_point_tags(self):
        gpu = {
            "device_name": "GPU1",
            "processes": [
                {
                    "pid": "1234",
                    "user": "dev",
                    "kind": "graphic",
                }
            ],
        }
        ts = 1000000000000
        points = build_process_points(gpu, "h1", ts)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]._tags["pid"], "1234")
        self.assertEqual(points[0]._tags["user"], "dev")
        self.assertEqual(points[0]._tags["kind"], "graphic")
        self.assertEqual(points[0]._tags["device_name"], "GPU1")
        self.assertEqual(points[0]._tags["host"], "h1")

    def test_no_processes_returns_empty(self):
        gpu = {"device_name": "GPU1", "processes": []}
        self.assertEqual(build_process_points(gpu, "h", 1000), [])

    def test_missing_processes_key_returns_empty(self):
        gpu = {"device_name": "GPU1"}
        self.assertEqual(build_process_points(gpu, "h", 1000), [])


class TestIterJsonBatches(unittest.TestCase):
    """Test the bracket-buffering JSON parser."""

    def test_single_line_json(self):
        lines = ['[{"a":1}]']
        batches = list(iter_json_batches(iter(lines)))
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0], [{"a": 1}])

    def test_multiline_json(self):
        lines = ['[', '  {"a": 1}', ']']
        batches = list(iter_json_batches(iter(lines)))
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0], [{"a": 1}])

    def test_multiple_batches(self):
        lines = ['[{"a":1}]', '[{"b":2}]']
        batches = list(iter_json_batches(iter(lines)))
        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[0], [{"a": 1}])
        self.assertEqual(batches[1], [{"b": 2}])

    def test_blank_lines_ignored(self):
        lines = ['', '[{"a":1}]', '', '[{"b":2}]', '']
        batches = list(iter_json_batches(iter(lines)))
        self.assertEqual(len(batches), 2)

    def test_malformed_json_continues(self):
        lines = ['[{"a":1}]', 'NOT JSON', '[{"b":2}]']
        batches = list(iter_json_batches(iter(lines)))
        # First batch OK, second malformed (skipped), third OK
        self.assertGreaterEqual(len(batches), 1)
        self.assertEqual(batches[0], [{"a": 1}])


class TestExporterWithStub(unittest.TestCase):
    """Integration tests using the InfluxDB stub."""

    def setUp(self):
        # Clear the stub log
        with open(TEST_LOG, "w") as f:
            f.write("")
        # Create a temp config
        self.config_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.config_dir, "test.yaml")
        with open(self.config_path, "w") as f:
            f.write(
                'influxdb:\n'
                '  url: "http://stub:8086"\n'
                '  token: "stub-token"\n'
                '  org: "test-org"\n'
                '  bucket: "test-bucket"\n'
                "nvtop:\n"
                '  command: ["nvtop", "-s"]\n'
                "exporter:\n"
                "  hostname_tag: false\n"
                '  log_level: "ERROR"\n'
            )

    def tearDown(self):
        shutil.rmtree(self.config_dir, ignore_errors=True)

    def _read_log(self):
        with open(TEST_LOG) as f:
            return f.read().strip()

    def test_stdin_once_writes_gpu_and_process(self):
        """Pipe a JSON batch via stdin and verify stub received it."""
        sample = json.dumps([
            {
                "device_name": "TestGPU",
                "gpu_clock": "800MHz",
                "mem_clock": "200MHz",
                "temp": "45C",
                "power_draw": "25W",
                "mem_util": "10%",
                "mem_total": "8000000000",
                "mem_used": "800000000",
                "mem_free": "7200000000",
                "gpu_util": "60%",
                "encode": None,
                "decode": None,
                "fan_speed": "1000RPM",
                "processes": [
                    {
                        "pid": "9999",
                        "cmdline": "test_proc",
                        "kind": "compute",
                        "user": "tester",
                        "gpu_usage": "55%",
                        "gpu_mem_bytes_alloc": "500000000",
                        "gpu_mem_usage": "15%",
                        "encode": None,
                        "decode": None,
                    }
                ],
            }
        ])

        # Inline stub classes in subprocess to avoid import issues
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                f"""
import sys
sys.path.insert(0, {repr(os.path.join(os.path.dirname(__file__), "..", "src"))})
from nvtop_influx_exporter import run_exporter
from unittest.mock import patch

TEST_LOG = {repr(TEST_LOG)}

class InfluxDBStub:
    def __init__(self, **kw):
        with open(TEST_LOG, "w") as f: f.write("")
    def ping(self): pass
    def write_api(self, **kw): return StubWriteApi()
    def close(self): pass

class StubWriteApi:
    def write(self, bucket, org, record):
        with open(TEST_LOG, "a") as f:
            for p in (record if isinstance(record, list) else [record]):
                f.write(str(p) + "\\n")
    def flush(self): pass

config = {repr({
    "influxdb": {"url": "http://stub:8086", "token": "stub-token", "org": "test-org", "bucket": "test-bucket"},
    "nvtop": {"command": ["nvtop", "-s"]},
    "exporter": {"hostname_tag": False, "log_level": "ERROR"},
})}

with patch("nvtop_influx_exporter.InfluxDBClient", return_value=InfluxDBStub()):
    run_exporter(config=config, use_stdin=True, once=True, dry_run=False)
""",
            ],
            input=sample.encode(),
            capture_output=True,
            cwd=os.path.dirname(__file__),
        )
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr.decode()}")

        log = self._read_log()
        self.assertIn("gpu_stats", log)
        self.assertIn("gpu_process_stats", log)
        self.assertIn("TestGPU", log)
        self.assertIn("gpu_clock_mhz=800", log)
        self.assertIn("temp_celsius=45", log)
        self.assertIn("pid=9999", log)

    def test_dry_run_no_stub_write(self):
        """Dry-run mode should NOT write to the stub log."""
        sample = json.dumps([{"device_name": "DryGPU", "gpu_clock": "100MHz"}])

        proc = subprocess.run(
            [
                sys.executable,
                os.path.join(os.path.dirname(__file__), "..", "src", "nvtop_influx_exporter.py"),
                "--stdin",
                "--once",
                "--dry-run",
                "--config",
                self.config_path,
            ],
            input=sample.encode(),
            capture_output=True,
            cwd=os.path.dirname(__file__),
        )
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr.decode()}")

        stdout = proc.stdout.decode()
        self.assertIn("gpu_stats", stdout)
        self.assertIn("DryGPU", stdout)

        # Stub log should be empty (dry-run skips InfluxDB entirely)
        log = self._read_log()
        self.assertEqual(log, "")

    def test_null_values_not_stringified(self):
        """Null nvtop values must not appear as string 'null' in output."""
        sample = json.dumps([
            {
                "device_name": "NullGPU",
                "gpu_clock": "500MHz",
                "gpu_util": None,
                "encode": None,
                "decode": None,
                "mem_util": "5%",
                "mem_total": "1000",
                "mem_used": "100",
                "mem_free": "900",
                "temp": "30C",
                "power_draw": "10W",
                "fan_speed": "off",
                "mem_clock": "100MHz",
                "processes": [
                    {
                        "pid": "1",
                        "cmdline": "test",
                        "kind": "graphic",
                        "user": "u",
                        "gpu_usage": None,
                        "gpu_mem_bytes_alloc": "100",
                        "gpu_mem_usage": "1%",
                        "encode": None,
                        "decode": None,
                    }
                ],
            }
        ])

        proc = subprocess.run(
            [
                sys.executable,
                os.path.join(os.path.dirname(__file__), "..", "src", "nvtop_influx_exporter.py"),
                "--stdin",
                "--once",
                "--dry-run",
                "--config",
                self.config_path,
            ],
            input=sample.encode(),
            capture_output=True,
            cwd=os.path.dirname(__file__),
        )
        self.assertEqual(proc.returncode, 0)

        output = proc.stdout.decode()
        # Null fields must NOT appear
        self.assertNotIn("gpu_util_percent", output)
        self.assertNotIn("encode_percent", output)
        self.assertNotIn("decode_percent", output)
        # Non-null fields must appear
        self.assertIn("gpu_clock_mhz=500", output)
        self.assertIn("temp_celsius=30", output)


class TestExporterWithRealNvtop(unittest.TestCase):
    """Integration tests using the actual nvtop binary on this machine."""

    def setUp(self):
        self.config_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.config_dir, "test.yaml")
        with open(self.config_path, "w") as f:
            f.write(
                'influxdb:\n'
                '  url: "http://stub:8086"\n'
                '  token: "stub-token"\n'
                '  org: "test-org"\n'
                '  bucket: "test-bucket"\n'
                "nvtop:\n"
                '  command: ["nvtop", "-s"]\n'
                "exporter:\n"
                "  hostname_tag: false\n"
                '  log_level: "ERROR"\n'
            )

    def tearDown(self):
        shutil.rmtree(self.config_dir, ignore_errors=True)

    @unittest.skipIf(
        shutil.which("nvtop") is None,
        "nvtop not installed on this machine",
    )
    def test_nvtop_snapshot_via_stdin(self):
        """Capture real nvtop -s output and pipe through the exporter."""
        # First, capture nvtop output
        nvtop_proc = subprocess.run(
            ["nvtop", "-s"],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(nvtop_proc.returncode, 0, "nvtop -s failed")
        nvtop_json = nvtop_proc.stdout

        # Parse it to verify structure
        data = json.loads(nvtop_json)
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0, "nvtop returned empty array")

        # Now pipe through exporter in dry-run
        proc = subprocess.run(
            [
                sys.executable,
                os.path.join(os.path.dirname(__file__), "..", "src", "nvtop_influx_exporter.py"),
                "--stdin",
                "--once",
                "--dry-run",
                "--config",
                self.config_path,
            ],
            input=nvtop_json,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, f"Exporter failed: {proc.stderr.decode()}")

        output = proc.stdout.decode()
        self.assertIn("gpu_stats", output)
        self.assertIn("device_name=", output)

    @unittest.skipIf(
        shutil.which("nvtop") is None,
        "nvtop not installed on this machine",
    )
    def test_nvtop_loop_via_stdin_multiple_batches(self):
        """Use nvtop -l (loop) for 2 seconds and verify multiple batches."""
        # We'll use a subprocess that runs nvtop -l and kills it after 2 seconds
        nvtop_proc = subprocess.Popen(
            ["nvtop", "-l", "-d", "10"],  # 10 = 1 second delay
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Let it run for ~2.5 seconds to capture 2-3 batches
        time.sleep(2.5)
        nvtop_proc.terminate()
        try:
            stdout, _ = nvtop_proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            nvtop_proc.kill()
            stdout, _ = nvtop_proc.communicate()

        # Count how many JSON arrays we got
        batch_count = 0
        for line in stdout.split("\n"):
            line = line.strip()
            if line.startswith("["):
                batch_count += 1

        self.assertGreaterEqual(batch_count, 1, "Expected at least 1 nvtop batch")

        # Pipe all output through exporter and count gpu_stats lines
        proc = subprocess.run(
            [
                sys.executable,
                os.path.join(os.path.dirname(__file__), "..", "src", "nvtop_influx_exporter.py"),
                "--stdin",
                "--dry-run",
                "--config",
                self.config_path,
            ],
            input=stdout.encode(),
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(proc.returncode, 0, f"Exporter failed: {proc.stderr.decode()}")

        output = proc.stdout.decode()
        gpu_stats_count = output.count("gpu_stats")
        self.assertGreaterEqual(
            gpu_stats_count, batch_count,
            f"Expected >= {batch_count} gpu_stats points, got {gpu_stats_count}",
        )

    @unittest.skipIf(
        shutil.which("nvtop") is None,
        "nvtop not installed on this machine",
    )
    def test_subprocess_mode(self):
        """Test the exporter spawning nvtop itself (subprocess mode)."""
        with open(TEST_LOG, "w") as f:
            f.write("")

        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                f"""
import sys
sys.path.insert(0, {repr(os.path.join(os.path.dirname(__file__), "..", "src"))})
from nvtop_influx_exporter import run_exporter
from unittest.mock import patch

TEST_LOG = {repr(TEST_LOG)}

class InfluxDBStub:
    def __init__(self, **kw):
        with open(TEST_LOG, "w") as f: f.write("")
    def ping(self): pass
    def write_api(self, **kw): return StubWriteApi()
    def close(self): pass

class StubWriteApi:
    def write(self, bucket, org, record):
        with open(TEST_LOG, "a") as f:
            for p in (record if isinstance(record, list) else [record]):
                f.write(str(p) + "\\n")
    def flush(self): pass

config = {{
    "influxdb": {{"url": "http://stub:8086", "token": "t", "org": "o", "bucket": "b"}},
    "nvtop": {{"command": ["nvtop", "-s"]}},
    "exporter": {{"hostname_tag": False, "log_level": "ERROR"}},
}}
with patch("nvtop_influx_exporter.InfluxDBClient", return_value=InfluxDBStub()):
    run_exporter(config=config, use_stdin=False, once=True, dry_run=False)
""",
            ],
            capture_output=True,
            timeout=15,
            cwd=os.path.dirname(__file__),
        )
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr.decode()}")

        with open(TEST_LOG) as f:
            log = f.read()
        self.assertIn("gpu_stats", log)


class TestEdgeCases(unittest.TestCase):
    """Test edge cases: multiple GPUs, empty processes, etc."""

    def test_multiple_gpus_in_one_batch(self):
        sample = json.dumps([
            {
                "device_name": "GPU-A",
                "gpu_clock": "1000MHz",
                "temp": "40C",
                "power_draw": "30W",
                "mem_util": "10%",
                "mem_total": "8000000000",
                "mem_used": "800000000",
                "mem_free": "7200000000",
                "gpu_util": None,
                "encode": None,
                "decode": None,
                "fan_speed": "on",
                "mem_clock": "200MHz",
                "processes": [],
            },
            {
                "device_name": "GPU-B",
                "gpu_clock": "1200MHz",
                "temp": "50C",
                "power_draw": "45W",
                "mem_util": "30%",
                "mem_total": "16000000000",
                "mem_used": "4800000000",
                "mem_free": "11200000000",
                "gpu_util": "80%",
                "encode": None,
                "decode": None,
                "fan_speed": "fast",
                "mem_clock": "300MHz",
                "processes": [],
            },
        ])

        proc = subprocess.run(
            [
                sys.executable,
                os.path.join(os.path.dirname(__file__), "..", "src", "nvtop_influx_exporter.py"),
                "--stdin",
                "--once",
                "--dry-run",
                "--config",
                "/dev/null",  # config not needed for dry-run parsing
            ],
            input=sample.encode(),
            capture_output=True,
        )
        # --config is required, so use a minimal one
        config_dir = tempfile.mkdtemp()
        config_path = os.path.join(config_dir, "test.yaml")
        with open(config_path, "w") as f:
            f.write(
                'influxdb:\n'
                '  url: "http://stub:8086"\n'
                '  token: "t"\n'
                '  org: "o"\n'
                '  bucket: "b"\n'
                "nvtop:\n"
                '  command: ["nvtop", "-s"]\n'
                "exporter:\n"
                "  hostname_tag: false\n"
                '  log_level: "ERROR"\n'
            )

        proc = subprocess.run(
            [
                sys.executable,
                os.path.join(os.path.dirname(__file__), "..", "src", "nvtop_influx_exporter.py"),
                "--stdin",
                "--once",
                "--dry-run",
                "--config",
                config_path,
            ],
            input=sample.encode(),
            capture_output=True,
        )
        shutil.rmtree(config_dir, ignore_errors=True)

        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr.decode()}")
        output = proc.stdout.decode()
        self.assertIn("GPU-A", output)
        self.assertIn("GPU-B", output)
        # Two gpu_stats points for two GPUs
        self.assertEqual(output.count("gpu_stats"), 2)

    def test_empty_processes_list(self):
        sample = json.dumps([
            {
                "device_name": "EmptyGPU",
                "gpu_clock": "100MHz",
                "temp": "25C",
                "power_draw": "5W",
                "mem_util": "0%",
                "mem_total": "1000",
                "mem_used": "0",
                "mem_free": "1000",
                "gpu_util": None,
                "encode": None,
                "decode": None,
                "fan_speed": "off",
                "mem_clock": "50MHz",
                "processes": [],
            }
        ])

        config_dir = tempfile.mkdtemp()
        config_path = os.path.join(config_dir, "test.yaml")
        with open(config_path, "w") as f:
            f.write(
                'influxdb:\n'
                '  url: "http://stub:8086"\n'
                '  token: "t"\n'
                '  org: "o"\n'
                '  bucket: "b"\n'
                "nvtop:\n"
                '  command: ["nvtop", "-s"]\n'
                "exporter:\n"
                "  hostname_tag: false\n"
                '  log_level: "ERROR"\n'
            )

        proc = subprocess.run(
            [
                sys.executable,
                os.path.join(os.path.dirname(__file__), "..", "src", "nvtop_influx_exporter.py"),
                "--stdin",
                "--once",
                "--dry-run",
                "--config",
                config_path,
            ],
            input=sample.encode(),
            capture_output=True,
        )
        shutil.rmtree(config_dir, ignore_errors=True)

        self.assertEqual(proc.returncode, 0)
        output = proc.stdout.decode()
        self.assertIn("gpu_stats", output)
        self.assertNotIn("gpu_process_stats", output)


if __name__ == "__main__":
    unittest.main()
