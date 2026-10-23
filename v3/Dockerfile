# syntax=docker/dockerfile:1
FROM mambaorg/micromamba:2.9.0 AS biopipe-base
COPY --chown=$MAMBA_USER:$MAMBA_USER environment.yml /tmp/environment.yml
RUN micromamba install --yes --name base --file /tmp/environment.yml && micromamba clean --all --yes
ARG MAMBA_DOCKERFILE_ACTIVATE=1
RUN command -v trimmomatic && command -v ngm && command -v samtools && command -v kraken2 && command -v cutadapt
COPY --chown=$MAMBA_USER:$MAMBA_USER biopipe.py /app/biopipe.py
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1

# Local/POSIX image: docker build --target local -t biopipe-v3:local v3
FROM biopipe-base AS local
ENTRYPOINT ["/usr/local/bin/_entrypoint.sh", "python", "/app/biopipe.py"]

# Use the official Apache image as the source of prebuilt Hadoop client files.
FROM ghcr.io/apache/hadoop:3.5.0 AS apache-hadoop

# Default image also supports HDFS. Site-specific Hadoop config is mounted at runtime.
FROM biopipe-base AS hdfs
COPY --from=apache-hadoop /opt/hadoop /opt/hadoop
ENV HADOOP_HOME=/opt/hadoop
ENV JAVA_HOME=/opt/conda
ENV PATH=/opt/hadoop/bin:$PATH
ENV HADOOP_CONF_DIR=/opt/hadoop/etc/hadoop
RUN /opt/hadoop/bin/hadoop version
ENTRYPOINT ["/usr/local/bin/_entrypoint.sh", "python", "/app/biopipe.py"]
