# syntax=docker/dockerfile:1
# One coherent Conda environment is used for the tool binaries. Copying only
# executables from separate bioinformatics images would omit their shared libs.
FROM mambaorg/micromamba:2.9.0

COPY --chown=$MAMBA_USER:$MAMBA_USER environment.yml /tmp/biopipe-environment.yml
RUN micromamba install --yes --name base --file /tmp/biopipe-environment.yml && \
    micromamba clean --all --yes

ARG MAMBA_DOCKERFILE_ACTIVATE=1
RUN python -m pip install --no-cache-dir 'hopsworks==5.2.1' && \
    python -m pip check && \
    command -v trimmomatic && \
    command -v ngm && \
    command -v samtools && \
    command -v kraken2 && \
    command -v cutadapt

COPY --chown=$MAMBA_USER:$MAMBA_USER biopipe_v2.py /app/biopipe_v2.py
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["/usr/local/bin/_entrypoint.sh", "python", "/app/biopipe_v2.py"]
