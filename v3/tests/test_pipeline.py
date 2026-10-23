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
import biopipe as v3  # noqa: E402


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

    def test_local_run_metrics_and_idempotent_retry(self):
        v3.validate_config(self.config)
        with patch.object(v3, "run_command", side_effect=self.fake_tool) as tool:
            first = v3.process_sample(self.config["samples"][0], self.config)
            second = v3.process_sample(self.config["samples"][0], self.config)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(tool.call_count, 6)
        marker = Path(v3.complete_marker(self.config, self.config["samples"][0]))
        done = json.loads(marker.read_text())
        self.assertTrue(Path(done["outputs"]["report"]).is_file())
        self.assertEqual(done["metrics"]["input_bytes"], 5 + 5 + 4 + 11 + 2)
        self.assertGreater(done["metrics"]["output_bytes"], 0)
        self.assertEqual(len(done["metrics"]["commands"]), 6)
        self.assertEqual(len(done["metrics"]["stages"]), 5)
        self.assertEqual(len(done["metrics"]["transfers"]), 7)

    def test_late_failure_has_no_completion_marker(self):
        def fail_at_kraken(command, log):
            if command[0] == "kraken2":
                raise RuntimeError("late failure")
            self.fake_tool(command, log)

        with patch.object(v3, "run_command", side_effect=fail_at_kraken):
            with self.assertRaisesRegex(RuntimeError, "late failure"):
                v3.process_sample(self.config["samples"][0], self.config)
        marker = Path(v3.complete_marker(self.config, self.config["samples"][0]))
        self.assertFalse(marker.exists())
        failures = list((self.root / "out" / "sample01").rglob("failure.json"))
        self.assertEqual(len(failures), 1)
        self.assertEqual(json.loads(failures[0].read_text())["error"], "RuntimeError: late failure")

    def test_duplicate_pod_uses_first_valid_marker(self):
        class RacingStorage(v3.LocalStorage):
            def put_once(self, source, target):
                if target.endswith(".json") and "/done/" in target:
                    super().put_once(source, target)
                    return False
                return super().put_once(source, target)

        with patch.object(v3, "storage_for", return_value=RacingStorage()), patch.object(v3, "run_command", side_effect=self.fake_tool):
            result = v3.process_sample(self.config["samples"][0], self.config)
        self.assertEqual(result["status"], "duplicate")
        self.assertTrue(Path(result["outputs"]["report"]).exists())

    def test_changed_revision_gets_new_marker(self):
        sample = self.config["samples"][0]
        first = v3.complete_marker(self.config, sample)
        sample["revision"] = "reads-v2"
        self.assertNotEqual(first, v3.complete_marker(self.config, sample))

    def test_rejects_missing_revision_and_unsafe_id(self):
        sample = self.config["samples"][0]
        sample.pop("revision")
        with self.assertRaisesRegex(ValueError, "sample revision"):
            v3.validate_config(self.config)
        sample["revision"] = "v1"
        sample["id"] = "../escape"
        with self.assertRaisesRegex(ValueError, "safe path"):
            v3.validate_config(self.config)

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

        v3.validate_config(config)
        with patch.object(v3.shutil, "which", return_value="/usr/bin/hadoop"), patch.object(v3.subprocess, "run", side_effect=fake_hadoop), patch.object(v3, "run_command", side_effect=self.fake_tool):
            first = v3.process_sample(config["samples"][0], config)
            second = v3.process_sample(config["samples"][0], config)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "skipped")
        self.assertTrue(map_uri(v3.complete_marker(config, config["samples"][0])).is_file())

    def test_hdfs_permission_error_is_not_a_missing_marker(self):
        with patch.object(v3.shutil, "which", return_value="/usr/bin/hadoop"), patch.object(
            v3.HdfsStorage, "_run",
            return_value=subprocess.CompletedProcess(["hadoop", "fs", "-test"], 1, "", "permission denied"),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                v3.HdfsStorage().exists("hdfs://namenode/out/marker.json")


if __name__ == "__main__":
    unittest.main()
