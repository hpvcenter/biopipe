import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import biopipe  # noqa: E402


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        inputs = root / "inputs"
        inputs.mkdir()
        (inputs / "R1.fastq.gz").write_bytes(b"read1")
        (inputs / "R2.fastq.gz").write_bytes(b"read2")
        resources = root / "resources"
        resources.mkdir()
        (resources / "pair.fa").write_text("pair")
        (resources / "single.fa").write_text("single")
        human = resources / "human"
        human.mkdir()
        (human / "GRCh38.fa").write_text(">chr1\nACGT\n")
        kraken = resources / "kraken"
        kraken.mkdir()
        (kraken / "hash.k2d").write_bytes(b"db")
        self.root = root
        self.config = {
            "storage": "local",
            "output_root": str(root / "out"),
            "scratch_root": str(root / "scratch"),
            "resources_version": "ref-v1",
            "threads": 1,
            "samples": [{"id": "sample01", "revision": "reads-v1", "r1": str(inputs / "R1.fastq.gz"), "r2": str(inputs / "R2.fastq.gz")}],
            "resources": {
                "trimmomatic_jar": None,
                "adapter_pair": str(resources / "pair.fa"),
                "adapter_single": str(resources / "single.fa"),
                "human_reference_dir": str(human),
                "human_fasta_name": "GRCh38.fa",
                "kraken_db": str(kraken),
            },
            "tools": {"trimmomatic": "trimmomatic", "java": "java", "ngm": "ngm", "samtools": "samtools", "kraken2": "kraken2", "cutadapt": "cutadapt"},
            "trim": {"phred": "phred33", "leading": 3, "trailing": 3, "sliding_window": "4:15", "min_len": 36, "cutadapt_r2_bases": 0},
            "mapping": {"min_identity": 0.95, "min_residues": 0.75, "very_fast": False},
            "kraken": {"memory_mapping": True},
            "publish": {"fastq": False, "kraken_output": False},
        }
        (root / "scratch").mkdir()

    @staticmethod
    def fake_tool(command, log):
        with log.open("a") as output:
            output.write(" ".join(command) + "\n")
        if command[0] == "trimmomatic":
            if "PE" in command:
                start = command.index("-threads") + 4
                outputs = command[start : start + 4]
            else:
                outputs = [command[command.index("-threads") + 3]]
        elif command[0] == "ngm":
            outputs = [command[command.index("-o") + 1]]
        elif command[0] == "samtools":
            outputs = (
                [command[command.index(flag) + 1] for flag in ("-1", "-2") if flag in command]
                if command[1] == "fastq" else [command[command.index("-o") + 1]]
            )
            if command[1] == "fastq" and "-1" not in command:
                outputs = [command[command.index("-0") + 1]]
        elif command[0] == "kraken2":
            outputs = [command[command.index("--report") + 1]]
        else:
            raise AssertionError(command)
        for item in outputs:
            Path(item).write_bytes(b"tool output")


    def test_steps_run_separately_and_rerun_is_skipped(self):
        biopipe.validate_config(self.config)
        sample = self.config["samples"][0]
        with patch.object(biopipe, "run_command", side_effect=self.fake_tool) as tool:
            results = [biopipe.run_step(sample, self.config, step) for step in biopipe.STEPS]
            again = biopipe.process_sample(sample, self.config)
        self.assertEqual([r["status"] for r in results], ["completed"] * 5)
        self.assertEqual([r["status"] for r in again], ["skipped"] * 5)
        self.assertEqual(tool.call_count, 6)
        # Every step moves its data through shared storage: reads its inputs, writes its outputs.
        for result in results:
            self.assertGreater(result["metrics"]["output_bytes"], 0)
            self.assertTrue(Path(result["outputs"]["log"]).is_file())
        self.assertEqual(results[0]["metrics"]["input_bytes"], 5 + 5 + 4)  # reads + adapter
        self.assertTrue(Path(results[-1]["outputs"]["report"]).is_file())
        self.assertEqual([len(r["metrics"]["commands"]) for r in results], [1, 1, 1, 2, 1])

    def test_step_is_blocked_until_upstream_is_done(self):
        sample = self.config["samples"][0]
        with patch.object(biopipe, "run_command", side_effect=self.fake_tool) as tool:
            blocked = biopipe.run_step(sample, self.config, "classify")
            chain = biopipe.process_sample(sample, self.config, ("map",))
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual([r["status"] for r in chain], ["blocked"])
        self.assertEqual(tool.call_count, 0)

    def test_failed_step_resumes_without_repeating_earlier_steps(self):
        sample = self.config["samples"][0]
        calls = []
        failed = []

        def fail_once_at_kraken(command, log):
            calls.append(command[0])
            if command[0] == "kraken2" and not failed:
                failed.append(True)
                raise RuntimeError("late failure")
            self.fake_tool(command, log)

        with patch.object(biopipe, "run_command", side_effect=fail_once_at_kraken):
            with self.assertRaisesRegex(RuntimeError, "late failure"):
                biopipe.process_sample(sample, self.config)
            first_calls = list(calls)
            calls.clear()
            results = biopipe.process_sample(sample, self.config)
        self.assertEqual(first_calls, ["trimmomatic", "ngm", "samtools", "samtools", "samtools", "kraken2"])
        self.assertEqual(calls, ["kraken2"])
        self.assertEqual([r["status"] for r in results], ["skipped"] * 4 + ["completed"])
        failures = list((self.root / "out" / "sample01").rglob("failure.json"))
        self.assertEqual(json.loads(failures[0].read_text())["step"], "classify")

    def test_missing_upstream_output_is_an_error_not_silent_reuse(self):
        sample = self.config["samples"][0]
        with patch.object(biopipe, "run_command", side_effect=self.fake_tool):
            done = biopipe.run_step(sample, self.config, "trim")
        Path(done["outputs"]["trim_r1"]).unlink()
        with self.assertRaisesRegex(RuntimeError, "missing outputs"):
            biopipe.run_step(sample, self.config, "map")

    def test_duplicate_task_uses_first_valid_marker(self):
        class RacingStorage(biopipe.LocalStorage):
            def put_once(self, source, target):
                if target.endswith(".done.json"):
                    super().put_once(source, target)
                    return False
                return super().put_once(source, target)

        sample = self.config["samples"][0]
        with patch.object(biopipe, "run_command", side_effect=self.fake_tool):
            result = biopipe.run_step(sample, self.config, "trim", RacingStorage())
        self.assertEqual(result["status"], "duplicate")
        self.assertTrue(Path(result["outputs"]["trim_r1"]).exists())

    def test_reference_cache_is_shared_between_samples_and_steps(self):
        self.config["reference_cache"] = str(self.root / "cache")
        second = dict(self.config["samples"][0], id="sample02")
        self.config["samples"].append(second)
        biopipe.validate_config(self.config)
        with patch.object(biopipe, "run_command", side_effect=self.fake_tool):
            first = biopipe.process_sample(self.config["samples"][0], self.config)
            other = biopipe.process_sample(second, self.config)
        hits = lambda results: [t["cache_hit"] for r in results for t in r["metrics"]["transfers"] if "cache_hit" in t]
        self.assertEqual(hits(first), [False, False, False])  # adapter, human reference, Kraken DB
        self.assertEqual(hits(other), [True, True, True])
        self.assertFalse(list((self.root / "cache").glob("*.partial")))

    def test_parse_steps_keeps_pipeline_order(self):
        self.assertEqual(biopipe.parse_steps(None), biopipe.STEPS)
        self.assertEqual(biopipe.parse_steps("map,unmapped"), ("map", "unmapped"))
        with self.assertRaises(ValueError):
            biopipe.parse_steps("unmapped,map")
        with self.assertRaises(ValueError):
            biopipe.parse_steps("align")

    def test_changed_revision_gets_new_marker(self):
        sample = self.config["samples"][0]
        first = biopipe.step_marker(self.config, sample, "trim")
        sample["revision"] = "reads-v2"
        self.assertNotEqual(first, biopipe.step_marker(self.config, sample, "trim"))

    def test_rejects_missing_revision_and_unsafe_id(self):
        sample = self.config["samples"][0]
        sample.pop("revision")
        with self.assertRaisesRegex(ValueError, "sample revision"):
            biopipe.validate_config(self.config)
        sample["revision"] = "v1"
        sample["id"] = "../escape"
        with self.assertRaisesRegex(ValueError, "safe path"):
            biopipe.validate_config(self.config)

    def test_hdfs_backend_through_filesystem_shell(self):
        hdfs_root = self.root / "fake-hdfs"
        hdfs_root.mkdir()
        config = self.config
        config["storage"] = "hdfs"
        config["output_root"] = "hdfs://namenode:8020/out"

        def map_uri(uri):
            parts = urlsplit(uri)
            self.assertEqual(parts.scheme, "hdfs")
            return hdfs_root / parts.netloc / parts.path.lstrip("/")

        for key in ("r1", "r2"):
            source = Path(config["samples"][0][key])
            config["samples"][0][key] = "hdfs://namenode:8020/inputs/" + source.name
            target = map_uri(config["samples"][0][key])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        for key, source_name in list(config["resources"].items()):
            if source_name is None or key == "human_fasta_name":
                continue
            source = Path(source_name)
            config["resources"][key] = "hdfs://namenode:8020/resources/" + key + "/" + source.name
            target = map_uri(config["resources"][key])
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)

        def fake_hadoop(command, check, capture_output, text):
            self.assertEqual(command[:2], ["hadoop", "fs"])
            op, *args = command[2:]
            code, output = 0, ""
            if op == "-get":
                source, target = map_uri(args[0]), Path(args[1])
                if source.is_dir():
                    shutil.copytree(source, target)
                else:
                    shutil.copy2(source, target)
            elif op == "-mkdir":
                map_uri(args[1]).mkdir(parents=True, exist_ok=True)
            elif op == "-put":
                shutil.copy2(args[0], map_uri(args[1]))
            elif op == "-mv":
                source, target = map_uri(args[0]), map_uri(args[1])
                if target.exists():
                    code = 1
                else:
                    source.rename(target)
            elif op == "-test":
                code = 0 if map_uri(args[1]).exists() else 1
            elif op == "-cat":
                output = map_uri(args[0]).read_text()
            elif op == "-rm":
                map_uri(args[1]).unlink(missing_ok=True)
            else:
                raise AssertionError(command)
            result = subprocess.CompletedProcess(command, code, output, "")
            if check and code:
                raise subprocess.CalledProcessError(code, command)
            return result

        biopipe.validate_config(config)
        with patch.object(biopipe.shutil, "which", return_value="/usr/bin/hadoop"), patch.object(biopipe.subprocess, "run", side_effect=fake_hadoop), patch.object(biopipe, "run_command", side_effect=self.fake_tool):
            first = biopipe.process_sample(config["samples"][0], config)
            second = biopipe.process_sample(config["samples"][0], config)
        self.assertEqual([r["status"] for r in first], ["completed"] * 5)
        self.assertEqual([r["status"] for r in second], ["skipped"] * 5)
        self.assertTrue(map_uri(biopipe.step_marker(config, config["samples"][0], "classify")).is_file())

    def test_hdfs_permission_error_is_not_a_missing_marker(self):
        with patch.object(biopipe.shutil, "which", return_value="/usr/bin/hadoop"), patch.object(
            biopipe.HdfsStorage, "_run",
            return_value=subprocess.CompletedProcess(["hadoop", "fs", "-test"], 1, "", "permission denied"),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                biopipe.HdfsStorage().exists("hdfs://namenode/out/marker.json")

    def test_hdfs_missing_marker_tolerates_warning_noise(self):
        noisy = subprocess.CompletedProcess(["hadoop", "fs", "-test"], 1, "", "WARN util.NativeCodeLoader: Unable to load native-hadoop library\n")
        with patch.object(biopipe.shutil, "which", return_value="/usr/bin/hadoop"), patch.object(biopipe.HdfsStorage, "_run", return_value=noisy):
            self.assertFalse(biopipe.HdfsStorage().exists("hdfs://namenode/out/marker.json"))
        failed = subprocess.CompletedProcess(["hadoop", "fs", "-test"], 255, "", "test: Permission denied")
        with patch.object(biopipe.shutil, "which", return_value="/usr/bin/hadoop"), patch.object(biopipe.HdfsStorage, "_run", return_value=failed):
            with self.assertRaises(subprocess.CalledProcessError):
                biopipe.HdfsStorage().exists("hdfs://namenode/out/marker.json")

    def test_threads_do_not_change_fingerprint_but_science_does(self):
        sample = self.config["samples"][0]
        first = biopipe.fingerprint(self.config, sample)
        self.config["threads"] = 8
        self.assertEqual(first, biopipe.fingerprint(self.config, sample))
        self.config["trim"]["min_len"] = 50
        self.assertNotEqual(first, biopipe.fingerprint(self.config, sample))
        self.config["threads"] = True
        with self.assertRaisesRegex(ValueError, "threads"):
            biopipe.validate_config(self.config)


if __name__ == "__main__":
    unittest.main()
