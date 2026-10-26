"""Stand-in for the bioinformatics tools: writes the output files they would."""
import sys
from pathlib import Path

name, command = sys.argv[1], sys.argv[1:]
if name == "trimmomatic":
    start = command.index("-threads") + 2
    outputs = command[start + 2 : start + 6] if "PE" in command else [command[start + 1]]
elif name == "ngm":
    outputs = [command[command.index("-o") + 1]]
elif name == "samtools":
    if command[1] == "fastq":
        outputs = [command[command.index(flag) + 1] for flag in ("-1", "-2", "-0") if flag in command and command[command.index(flag) + 1] != "/dev/null"]
    else:
        outputs = [command[command.index("-o") + 1]]
elif name == "kraken2":
    outputs = [command[command.index("--report") + 1]]
else:
    sys.exit("unknown tool " + name)
for item in outputs:
    Path(item).write_bytes(b"tool output")
