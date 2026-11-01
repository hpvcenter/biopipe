"""Offline tiny-data check; optionally compare with another sample executor.

Requires the tools in environment.yml. All generated data stays in --work-dir.
"""

import argparse
import copy
import gzip
import importlib.util
import json
import random
import subprocess
import sys
from pathlib import Path


def load_executor(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--compare-with", type=Path)
    args = parser.parse_args()
    root = args.work_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        parser.error("--work-dir must be empty to protect existing data")
    source = Path(__file__).resolve().parents[1]
    rng = random.Random(734)
    human, microbe = ("".join(rng.choices("ACGT", k=20000)) for _ in range(2))
    references = root / "references"
    (references / "human").mkdir(parents=True)
    (references / "human/GRCh38.fa").write_text(">human\n" + human + "\n")
    (references / "adapter.fa").write_text(">Adapter1\nAGATCGGAAGAGCACACGTCTGAACTCCAGTCA\n")
    microbial_fasta = references / "microbe.fa"
    microbial_fasta.write_text(">microbe|kraken:taxid|3\n" + microbe + "\n")
    database = references / "kraken"
    taxonomy = database / "taxonomy"
    taxonomy.mkdir(parents=True)
    nodes, names = [], []
    for taxid, parent, rank, name in ((1, 1, "no rank", "root"), (2, 1, "superkingdom", "Bacteria"), (3, 2, "species", "Synthetic microbe")):
        nodes.append("\t|\t".join(map(str, (taxid, parent, rank, "", 0, 0, 1, 0, 0, 0, 0, 0, ""))) + "\t|\n")
        names.append("\t|\t".join(map(str, (taxid, name, "", "scientific name"))) + "\t|\n")
    (taxonomy / "nodes.dmp").write_text("".join(nodes))
    (taxonomy / "names.dmp").write_text("".join(names))
    build_log = root / "database-build.log"
    with build_log.open("w") as log:
        for command in (["kraken2-build", "--add-to-library", str(microbial_fasta), "--db", str(database)],
                        ["kraken2-build", "--build", "--threads", "1", "--db", str(database)]):
            subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT)
    reads = root / "reads"
    reads.mkdir()
    complement = str.maketrans("ACGT", "TGCA")
    for mate in (1, 2):
        with gzip.open(reads / ("R%d.fastq.gz" % mate), "wt") as stream:
            for name, sequence in (("host", human), ("microbial", microbe)):
                read = sequence[500:650] if mate == 1 else sequence[850:1000].translate(complement)[::-1]
                stream.write("@%s/%d\n%s\n+\n%s\n" % (name, mate, read, "I" * len(read)))
    config = json.loads((source / "example.local.json").read_text())
    config.update(output_root=str(root / "standalone-results"), scratch_root=str(root / "scratch"), threads=1)
    config["resources"].update(adapter_pair=str(references / "adapter.fa"), adapter_single=str(references / "adapter.fa"),
                               human_reference_dir=str(references / "human"), kraken_db=str(database))
    config["samples"] = [
        {"id": "paired", "revision": "synthetic-v1", "r1": str(reads / "R1.fastq.gz"), "r2": str(reads / "R2.fastq.gz")},
        {"id": "single", "revision": "synthetic-v1", "r1": str(reads / "R1.fastq.gz")},
    ]
    config["publish"] = {"fastq": True, "kraken_output": True}
    Path(config["scratch_root"]).mkdir()
    standalone = load_executor(source / "scripts/biopipe.py", "standalone_real_tools")
    standalone.validate_config(config)
    (root / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    baseline = load_executor(args.compare_with.resolve(), "baseline_real_tools") if args.compare_with else None
    summary = []
    for sample in config["samples"]:
        result = standalone.process_sample(sample, config)
        classification = Path(result["outputs"]["kraken_output"]).read_text().splitlines()
        if len(classification) != 1 or not classification[0].startswith("C\tmicrobial") or classification[0].split("\t")[2] != "3":
            raise AssertionError("expected exactly the synthetic microbial read classified at taxid 3: " + repr(classification))
        if baseline:
            baseline_config = copy.deepcopy(config)
            baseline_config["output_root"] = str(root / "baseline-results")
            original = baseline.process_sample(sample, baseline.validate_config(baseline_config))
            for key in ("report", "kraken_output", "fastq_r1", "fastq_r2"):
                if key not in result["outputs"]:
                    continue
                first, second = Path(result["outputs"][key]), Path(original["outputs"][key])
                if key.startswith("fastq"):
                    with gzip.open(first, "rb") as left, gzip.open(second, "rb") as right:
                        equal = left.read() == right.read()
                else:
                    equal = first.read_bytes() == second.read_bytes()
                if not equal:
                    raise AssertionError("baseline mismatch: " + sample["id"] + ":" + key)
        summary.append({"sample": sample["id"], "classified_reads_or_pairs": len(classification), "baseline_matches": bool(baseline)})
    print(json.dumps({"work_dir": str(root), "results": summary}, sort_keys=True))


if __name__ == "__main__":
    main()
