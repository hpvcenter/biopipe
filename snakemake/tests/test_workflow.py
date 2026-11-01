import importlib.metadata
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("standalone_biopipe", SOURCE / "scripts/biopipe.py")
biopipe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = biopipe
spec.loader.exec_module(biopipe)
try:
    importlib.metadata.version("snakemake")
    HAS_SNAKEMAKE = True
except importlib.metadata.PackageNotFoundError:
    HAS_SNAKEMAKE = False


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="biopipe snakemake ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / "working directory"
        self.work.mkdir()
        self.events = self.root / "events.jsonl"
        self.failure = self.root / "failure.txt"
        self.env = dict(os.environ, BIOPIPE_TEST_EVENTS=str(self.events),
                        BIOPIPE_TEST_FAILURE=str(self.failure), PYTHONDONTWRITEBYTECODE="1",
                        XDG_CACHE_HOME=str(self.root / "cache"))
        tools = {}
        for name in ("trimmomatic", "ngm", "samtools", "kraken2", "cutadapt", "java"):
            wrapper = self.root / "bin" / name
            wrapper.parent.mkdir(exist_ok=True)
            wrapper.write_text("#!/bin/sh\nexec " + " ".join(shlex.quote(value) for value in
                               (sys.executable, str(SOURCE / "tests/fake_tool.py"), name)) + ' "$@"\n')
            wrapper.chmod(0o755)
            tools[name] = str(wrapper)
        for directory in ("reads", "references/human", "references/kraken"):
            (self.root / directory).mkdir(parents=True)
        for name in ("paired_R1", "paired_R2", "single_R1"):
            (self.root / "reads" / (name + ".fastq.gz")).write_bytes(b"input reads\n")
        (self.root / "references/pair.fa").write_text(">adapter\nACGT\n")
        (self.root / "references/single.fa").write_text(">adapter\nACGT\n")
        (self.root / "references/human/GRCh38.fa").write_text(">human\nACGT\n")
        for name in ("hash.k2d", "opts.k2d", "taxo.k2d"):
            (self.root / "references/kraken" / name).write_bytes(b"reference data")
        self.config = json.loads((SOURCE / "example.local.json").read_text())
        self.config.update(output_root=str(self.root / "results"), scratch_root=str(self.root / "scratch"),
                           tools=tools, threads=1)
        self.config["resources"].update(
            adapter_pair=str(self.root / "references/pair.fa"),
            adapter_single=str(self.root / "references/single.fa"),
            human_reference_dir=str(self.root / "references/human"),
            kraken_db=str(self.root / "references/kraken"))
        self.config["samples"] = [
            {"id": "paired", "revision": "reads-v1", "r1": str(self.root / "reads/paired_R1.fastq.gz"),
             "r2": str(self.root / "reads/paired_R2.fastq.gz")},
            {"id": "single", "revision": "reads-v1", "r1": str(self.root / "reads/single_R1.fastq.gz")},
        ]
        self.config_path = self.root / "config.json"
        self.save_config()

    def save_config(self):
        self.config_path.write_text(json.dumps(self.config))

    def recorded(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()] if self.events.exists() else []

    def result(self, sample="paired", filename="metrics.json"):
        return Path(self.config["output_root"]) / sample / filename


class ExecutorTest(Fixture):
    def test_validation_rejects_unsafe_storage_and_output_overlap(self):
        for change in ({"storage": "hdfs"}, {"threads": True}, {"scheduling": {"mem_mb": 0}},
                       {"output_root": str(self.root / "references/human")}, {"publish": {"fastq": "false"}},
                       {"scratch_root": str(self.root / "references/human/scratch")},
                       {"reference_cache": str(self.root / "references/kraken/cache")}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                biopipe.validate_config(dict(self.config, **change))

    def test_validator_needs_no_input_data(self):
        result = subprocess.run([sys.executable, str(SOURCE / "scripts/biopipe.py"), "--config",
                                 str(SOURCE / "example.local.json")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["storage"], "local")

    def test_publication_failure_preserves_diagnostics_and_cleans_partial_files(self):
        from unittest.mock import patch

        original = biopipe.atomic_copy
        def fail_metrics(source, target):
            if target.name == "metrics.json":
                raise OSError("injected publication failure")
            return original(source, target)

        with patch.object(biopipe, "atomic_copy", side_effect=fail_metrics):
            with self.assertRaisesRegex(OSError, "publication"):
                biopipe.process_sample(self.config["samples"][0], self.config)
        self.assertFalse(self.result(filename="kraken_report.txt").exists())
        self.assertTrue(self.result(filename="failure.json").exists())
        self.assertIn("injected publication failure", self.result(filename="pipeline.log").read_text())
        self.assertEqual(list(Path(self.config["scratch_root"]).iterdir()), [])
        self.assertFalse(list(Path(self.config["output_root"]).rglob("*.partial")))


@unittest.skipUnless(HAS_SNAKEMAKE, "install snakemake to run workflow integration tests")
class WorkflowTest(Fixture):
    def run_workflow(self, *args, expected=0, snakefile=None, cores=2):
        result = subprocess.run(
            [sys.executable, "-m", "snakemake", "--snakefile", str(snakefile or SOURCE / "Snakefile"),
             "--configfile", str(self.config_path), "--cores", str(cores), "--rerun-incomplete", *args],
            cwd=self.work, env=self.env, capture_output=True, text=True, timeout=90)
        if expected == 0:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_mixed_samples_optional_products_and_parallelism(self):
        self.config["publish"] = {"fastq": True, "kraken_output": True}
        self.config["trim"]["cutadapt_r2_bases"] = 2
        self.save_config()
        self.env["BIOPIPE_TEST_DELAY"] = "0.5"
        self.run_workflow()
        for sample in self.config["samples"]:
            for path in biopipe.output_paths(self.config, sample).values():
                self.assertTrue(path.is_file(), path)
        self.assertFalse(self.result("single", "nonhuman_R2.fastq.gz").exists())
        intervals = {}
        for event in self.recorded():
            if event["tool"] == "trimmomatic":
                intervals.setdefault(event["sample"], {})[event["action"]] = event["time"]
        self.assertLess(max(item["start"] for item in intervals.values()), min(item["end"] for item in intervals.values()))
        cuts = [event for event in self.recorded() if event["tool"] == "cutadapt" and event["action"] == "start"]
        self.assertEqual([event["sample"] for event in cuts], ["paired"])
        self.assertEqual(list(Path(self.config["scratch_root"]).iterdir()), [])
        self.assertEqual((self.root / "reads/paired_R1.fastq.gz").read_bytes(), b"input reads\n")

    def test_noop_missing_output_revision_and_forced_rerun(self):
        self.run_workflow()
        first = self.recorded()
        self.run_workflow()
        self.assertEqual(self.recorded(), first)
        self.result(filename="kraken_report.txt").unlink()
        self.run_workflow()
        new = self.recorded()[len(first):]
        self.assertEqual({event["sample"] for event in new}, {"paired"})
        count = len(self.recorded())
        self.config["samples"][1]["revision"] = "reads-v2"
        self.save_config()
        self.run_workflow()
        self.assertEqual({event["sample"] for event in self.recorded()[count:]}, {"single"})
        count = len(self.recorded())
        self.run_workflow("--forceall")
        self.assertEqual({event["sample"] for event in self.recorded()[count:]}, {"paired", "single"})

    def test_input_settings_reference_and_optional_output_changes(self):
        self.run_workflow()
        count = len(self.recorded())
        read = Path(self.config["samples"][0]["r1"])
        read.write_bytes(b"updated reads, longer than before\n")
        self.run_workflow()
        self.assertEqual({event["sample"] for event in self.recorded()[count:]}, {"paired"})
        count = len(self.recorded())
        self.config["trim"]["min_len"] = 40
        self.save_config()
        self.run_workflow()
        self.assertEqual({event["sample"] for event in self.recorded()[count:]}, {"paired", "single"})
        count = len(self.recorded())
        database = Path(self.config["resources"]["kraken_db"]) / "hash.k2d"
        database.write_bytes(b"updated reference data with a different size")
        self.run_workflow()
        self.assertEqual({event["sample"] for event in self.recorded()[count:]}, {"paired", "single"})
        self.config["publish"]["kraken_output"] = True
        self.save_config()
        self.run_workflow()
        count = len(self.recorded())
        self.result("single", "kraken_output.txt").unlink()
        self.run_workflow()
        self.assertEqual({event["sample"] for event in self.recorded()[count:]}, {"single"})

    def test_failure_keeps_logs_other_sample_finishes_and_retry_is_scoped(self):
        self.failure.write_text("kraken2:paired")
        self.run_workflow("--keep-going", expected=1)
        self.assertTrue(self.result("single").is_file())
        self.assertFalse(self.result().exists())
        self.assertFalse(self.result(filename="kraken_report.txt").exists())
        self.assertIn("injected failure", self.result(filename="pipeline.log").read_text())
        self.assertTrue(self.result(filename="failure.json").is_file())
        self.assertEqual(list(Path(self.config["scratch_root"]).iterdir()), [])
        count = len(self.recorded())
        self.failure.unlink()
        self.run_workflow("--keep-going")
        self.assertEqual({event["sample"] for event in self.recorded()[count:]}, {"paired"})
        self.assertFalse(self.result(filename="failure.json").exists())

    def test_threads_are_scaled_and_operational_changes_do_not_rerun(self):
        self.config["threads"] = 8
        self.save_config()
        self.run_workflow(cores=1)
        for sample in ("paired", "single"):
            self.assertEqual(json.loads(self.result(sample).read_text())["threads"], 1)
        for event in self.recorded():
            if event["action"] == "start":
                flag = {"trimmomatic": "-threads", "ngm": "-t", "samtools": "-@", "kraken2": "--threads"}[event["tool"]]
                self.assertEqual(event["args"][event["args"].index(flag) + 1], "1")
        count = len(self.recorded())
        self.config["threads"] = 2
        self.config["scheduling"]["mem_mb"] = 32000
        self.config["scratch_root"] = str(self.root / "another scratch")
        self.save_config()
        self.run_workflow()
        self.assertEqual(len(self.recorded()), count)

    def test_copied_directory_dry_run_single_layouts_and_shared_cache(self):
        copied = self.root / "exported standalone version"
        shutil.copytree(SOURCE, copied, ignore=shutil.ignore_patterns("__pycache__", ".snakemake"))
        self.run_workflow("--dry-run", snakefile=copied / "Snakefile")
        self.assertFalse(self.events.exists())
        self.config["reference_cache"] = str(self.root / "reference cache")
        self.config["samples"] = self.config["samples"][:1]
        self.save_config()
        self.run_workflow(snakefile=copied / "Snakefile")
        self.config["samples"] = [{"id": "single", "revision": "v1", "r1": str(self.root / "reads/single_R1.fastq.gz")}]
        self.save_config()
        self.run_workflow(snakefile=copied / "Snakefile")
        transfers = json.loads(self.result("single").read_text())["metrics"]["transfers"]
        hits = [item["cache_hit"] for item in transfers if "cache_hit" in item]
        self.assertEqual(hits.count(True), 2)  # human reference and Kraken DB
        self.assertEqual(hits.count(False), 1)  # single-end adapter differs
        self.config["resources_version"] = "ref-v2"
        self.save_config()
        self.run_workflow(snakefile=copied / "Snakefile")
        transfers = json.loads(self.result("single").read_text())["metrics"]["transfers"]
        self.assertEqual([item["cache_hit"] for item in transfers if "cache_hit" in item], [False, False, False])


if __name__ == "__main__":
    unittest.main()
