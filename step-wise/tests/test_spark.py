import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import biopipe  # noqa: E402

try:
    import pyspark  # noqa: F401
    import spark_runner
except ImportError:
    spark_runner = None


@unittest.skipIf(spark_runner is None, "pyspark is not installed")
class SparkStepsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        fake = Path(__file__).resolve().parent / "fake_tool.py"
        tools = {}
        for name in ("trimmomatic", "ngm", "samtools", "kraken2", "java", "cutadapt"):
            wrapper = bin_dir / name
            wrapper.write_text('#!/bin/sh\nexec "%s" "%s" %s "$@"\n' % (sys.executable, fake, name))
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
            tools[name] = str(wrapper)
        for folder in ("inputs", "resources/human", "resources/kraken", "scratch"):
            (root / folder).mkdir(parents=True)
        (root / "resources/pair.fa").write_text("pair")
        (root / "resources/single.fa").write_text("single")
        (root / "resources/human/GRCh38.fa").write_text(">chr1\nACGT\n")
        (root / "resources/kraken/hash.k2d").write_bytes(b"db")
        samples = []
        for name in ("s1", "s2", "s3"):
            for mate in ("R1", "R2"):
                (root / "inputs" / (name + "_" + mate + ".fastq.gz")).write_bytes(b"reads")
            samples.append({"id": name, "revision": "v1",
                            "r1": str(root / "inputs" / (name + "_R1.fastq.gz")),
                            "r2": str(root / "inputs" / (name + "_R2.fastq.gz"))})
        self.root = root
        self.config = {
            "storage": "local", "output_root": str(root / "out"), "scratch_root": str(root / "scratch"),
            "resources_version": "ref-v1", "threads": 1, "samples": samples,
            "resources": {"trimmomatic_jar": None, "adapter_pair": str(root / "resources/pair.fa"),
                          "adapter_single": str(root / "resources/single.fa"),
                          "human_reference_dir": str(root / "resources/human"), "human_fasta_name": "GRCh38.fa",
                          "kraken_db": str(root / "resources/kraken")},
            "tools": tools,
            "trim": {"phred": "phred33", "leading": 3, "trailing": 3, "sliding_window": "4:15", "min_len": 36, "cutadapt_r2_bases": 0},
            "mapping": {"min_identity": 0.95, "min_residues": 0.75, "very_fast": False},
            "kraken": {"memory_mapping": True},
            "publish": {"fastq": False, "kraken_output": False},
        }
        biopipe.validate_config(self.config)

    def test_each_step_is_its_own_spark_job_and_rerun_is_skipped(self):
        outcome = spark_runner.run_spark(self.config, biopipe.STEPS, "local[2]")
        self.assertEqual([item["step"] for item in outcome["summary"]], list(biopipe.STEPS))
        for item in outcome["summary"]:
            self.assertEqual((item["samples"], item["completed"]), (3, 3))
            self.assertGreater(item["seconds"], 0)
        reports = [r for r in outcome["results"] if r["step"] == "classify"]
        self.assertEqual(len(reports), 3)
        for result in reports:
            self.assertTrue(Path(result["outputs"]["report"]).is_file())
        again = spark_runner.run_spark(self.config, biopipe.STEPS, "local[2]")
        self.assertTrue(all(item.get("skipped") == 3 for item in again["summary"]))

    def test_steps_can_be_run_in_separate_invocations(self):
        first = spark_runner.run_spark(self.config, ("trim", "map"), "local[2]")
        self.assertEqual([item["step"] for item in first["summary"]], ["trim", "map"])
        blocked = spark_runner.run_spark(self.config, ("fastq",), "local[2]")
        self.assertEqual(blocked["summary"][0].get("blocked"), 3)
        rest = spark_runner.run_spark(self.config, biopipe.STEPS[2:], "local[2]")
        self.assertEqual([item.get("completed") for item in rest["summary"]], [3, 3, 3])

    def test_failed_sample_is_dropped_from_later_steps_only(self):
        self.config["samples"][1]["r1"] = str(self.root / "inputs" / "missing.fastq.gz")
        outcome = spark_runner.run_spark(self.config, biopipe.STEPS, "local[2]")
        by_step = {item["step"]: item for item in outcome["summary"]}
        self.assertEqual(by_step["trim"].get("failed"), 1)
        self.assertEqual(by_step["trim"].get("completed"), 2)
        self.assertEqual(by_step["classify"]["samples"], 2)
        self.assertEqual(by_step["classify"].get("completed"), 2)

    def test_task_cpus_must_match_threads(self):
        self.config["threads"] = 2
        from pyspark.sql import SparkSession
        session = SparkSession.builder.master("local[2]").config("spark.task.cpus", "1").getOrCreate()
        try:
            with self.assertRaisesRegex(Exception, "spark.task.cpus"):
                spark_runner.run_spark(self.config, biopipe.STEPS, "local[2]")
        finally:
            session.stop()


if __name__ == "__main__":
    unittest.main()
