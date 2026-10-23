import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import biopipe_v2 as v2  # noqa: E402


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
        for name in ("trim.jar", "pair.fa", "single.fa"):
            (resources / name).write_text(name)
        human = resources / "human"
        human.mkdir()
        (human / "GRCh38.fa").write_text(">chr1\nACGT\n")
        kraken = resources / "kraken"
        kraken.mkdir()
        (kraken / "hash.k2d").write_bytes(b"db")
        self.config = {
            "storage": "local",
            "output_root": str(root / "out"),
            "scratch_root": str(root),
            "threads": 1,
            "samples": [{"id": "sample01", "r1": str(inputs / "R1.fastq.gz"), "r2": str(inputs / "R2.fastq.gz")}],
            "resources": {
                "trimmomatic_jar": str(resources / "trim.jar"),
                "adapter_pair": str(resources / "pair.fa"),
                "adapter_single": str(resources / "single.fa"),
                "human_reference_dir": str(human),
                "human_fasta_name": "GRCh38.fa",
                "kraken_db": str(kraken),
            },
            "tools": {"java": "java", "ngm": "ngm", "samtools": "samtools", "kraken2": "kraken2", "cutadapt": "cutadapt"},
            "trim": {"phred": "phred33", "leading": 3, "trailing": 3, "sliding_window": "4:15", "min_len": 36, "cutadapt_r2_bases": 0},
            "mapping": {"min_identity": 0.95, "min_residues": 0.75, "very_fast": False},
            "kraken": {"memory_mapping": True},
            "publish": {"fastq": False, "kraken_output": False},
        }

    def fake_command(self, command, log):
        log.write_text(log.read_text() + " ".join(command) + "\n" if log.exists() else " ".join(command) + "\n")
        outputs = []
        if command[0] == "java":
            if "PE" in command:
                start = command.index("-threads") + 4
                outputs = command[start : start + 4]
            else:
                outputs = [command[command.index("-threads") + 3]]
        elif command[0] == "ngm":
            outputs = [command[command.index("-o") + 1]]
        elif command[0] == "samtools":
            if command[1] == "fastq":
                outputs = [command[command.index(flag) + 1] for flag in ("-1", "-2") if flag in command]
                if "-o" in command:
                    outputs.append(command[command.index("-o") + 1])
                if "-1" not in command and "-0" in command:
                    outputs.append(command[command.index("-0") + 1])
            else:
                outputs = [command[command.index("-o") + 1]]
        elif command[0] == "kraken2":
            outputs = [command[command.index("--report") + 1]]
        for output in outputs:
            Path(output).write_bytes(b"fake tool output")

    def test_pipeline_keeps_intermediates_local_and_skips_completed_sample(self):
        v2.validate_config(self.config)
        with patch.object(v2, "run_command", side_effect=self.fake_command) as runner:
            first = v2.process_sample(self.config["samples"][0], self.config)
            second = v2.process_sample(self.config["samples"][0], self.config)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(runner.call_count, 6)
        output_dir = Path(self.config["output_root"]) / "sample01"
        self.assertEqual({p.name for p in output_dir.iterdir()}, {"kraken_report.txt", "pipeline.log", "done.json"})
        done = json.loads((output_dir / "done.json").read_text())
        self.assertEqual(done["sample"], "sample01")
        self.assertEqual(done["fingerprint"], v2.fingerprint(self.config, self.config["samples"][0]))

    def test_failure_does_not_publish_done_marker(self):
        with patch.object(v2, "run_command", side_effect=RuntimeError("tool failed")):
            with self.assertRaisesRegex(RuntimeError, "tool failed"):
                v2.process_sample(self.config["samples"][0], self.config)
        self.assertFalse((Path(self.config["output_root"]) / "sample01" / "done.json").exists())

    def test_rejects_duplicate_or_unsafe_sample_ids(self):
        sample = self.config["samples"][0]
        self.config["samples"] = [sample, dict(sample)]
        with self.assertRaisesRegex(ValueError, "duplicate sample id"):
            v2.validate_config(self.config)
        self.config["samples"] = [dict(sample, id="../outside")]
        with self.assertRaisesRegex(ValueError, "safe path component"):
            v2.validate_config(self.config)

    def test_force_removes_old_marker_before_failed_attempt(self):
        output = Path(self.config["output_root"]) / "sample01"
        output.mkdir(parents=True)
        (output / "done.json").write_text("old")
        with patch.object(v2, "run_command", side_effect=RuntimeError("tool failed")):
            with self.assertRaisesRegex(RuntimeError, "tool failed"):
                v2.process_sample(self.config["samples"][0], self.config, force=True)
        self.assertFalse((output / "done.json").exists())

    def test_single_end_uses_unpaired_fastq_output(self):
        sample = dict(self.config["samples"][0])
        sample.pop("r2")
        self.config["samples"] = [sample]
        commands = []

        def record(command, log):
            commands.append(command)
            self.fake_command(command, log)

        with patch.object(v2, "run_command", side_effect=record):
            v2.process_sample(sample, self.config)
        fastq = next(command for command in commands if command[:2] == ["samtools", "fastq"])
        self.assertIn("-0", fastq)
        self.assertNotIn("-o", fastq)
        kraken = next(command for command in commands if command[0] == "kraken2")
        self.assertNotIn("--paired", kraken)

    def test_paired_ngm_uses_two_input_files_without_interleaved_flag(self):
        commands = []

        def record(command, log):
            commands.append(command)
            self.fake_command(command, log)

        with patch.object(v2, "run_command", side_effect=record):
            v2.process_sample(self.config["samples"][0], self.config)
        ngm = next(command for command in commands if command[0] == "ngm")
        self.assertIn("-1", ngm)
        self.assertIn("-2", ngm)
        self.assertNotIn("-p", ngm)

    def test_adding_another_sample_does_not_invalidate_first(self):
        sample = self.config["samples"][0]
        before = v2.fingerprint(self.config, sample)
        self.config["samples"].append(dict(sample, id="sample02"))
        self.assertEqual(before, v2.fingerprint(self.config, sample))

    def test_missing_published_file_reprocesses_sample(self):
        sample = self.config["samples"][0]
        with patch.object(v2, "run_command", side_effect=self.fake_command) as runner:
            v2.process_sample(sample, self.config)
            (Path(self.config["output_root"]) / "sample01" / "kraken_report.txt").unlink()
            result = v2.process_sample(sample, self.config)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(runner.call_count, 12)

    def test_installed_trimmomatic_avoids_jar_download(self):
        self.config["resources"]["trimmomatic_jar"] = None
        self.config["tools"]["trimmomatic"] = "trimmomatic"
        commands = []

        # The fake tool writes a Trimmomatic output using the same argument
        # positions after the executable as the Java launcher.
        def installed_tool(command, log):
            commands.append(command)
            if command[0] == "trimmomatic":
                command = ["java", "-jar", "installed.jar"] + command[1:]
            self.fake_command(command, log)

        with patch.object(v2, "run_command", side_effect=installed_tool):
            result = v2.process_sample(self.config["samples"][0], self.config)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(commands[0][0], "trimmomatic")

    def test_dataset_api_backend_downloads_reference_tree(self):
        root = Path(self.temp.name)
        backend = v2.HopsworksApiStorage.__new__(v2.HopsworksApiStorage)

        class FakeApi:
            def get(self, path):
                return {"attributes": {"dir": path == "Resources/human"}}

            def list(self, path, offset=0, limit=1000):
                return ["Resources/human/GRCh38.fa"] if offset == 0 else []

            def download(self, path, local_path):
                Path(local_path).write_text(">chr1\nACGT\n")
                return local_path

        backend.api = FakeApi()
        directory = backend.get("Resources/human", root / "human")
        self.assertEqual((directory / "GRCh38.fa").read_text(), ">chr1\nACGT\n")


if __name__ == "__main__":
    unittest.main()
