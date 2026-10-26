# syntax=docker/dockerfile:1
# Optional: build everything (tools, step-wise Spark runner) into one image.
#
#   docker build -t biopipe-v5 v5                    # local files, Spark local mode
#   docker build --target hdfs -t biopipe-v5:hdfs v5  # adds the Hadoop client for hdfs://
FROM mambaorg/micromamba:2.9.0 AS base
COPY --chown=$MAMBA_USER:$MAMBA_USER environment.yml /tmp/environment.yml
RUN micromamba install --yes --name base --file /tmp/environment.yml && micromamba clean --all --yes
ARG MAMBA_DOCKERFILE_ACTIVATE=1
ARG PYSPARK_VERSION=3.5.3
RUN pip install --no-cache-dir "pyspark==${PYSPARK_VERSION}"
RUN command -v trimmomatic && command -v ngm && command -v samtools && command -v kraken2 && command -v cutadapt
COPY --chown=$MAMBA_USER:$MAMBA_USER biopipe.py spark_runner.py /app/
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 JAVA_HOME=/opt/conda
# Mount a config at /config, data at /data, and a writable /scratch (and /cache).
ENTRYPOINT ["/usr/local/bin/_entrypoint.sh", "python", "/app/spark_runner.py"]
CMD ["--help"]

FROM ghcr.io/apache/hadoop:3.5.0 AS apache-hadoop

FROM base AS hdfs
COPY --from=apache-hadoop /opt/hadoop /opt/hadoop
ENV HADOOP_HOME=/opt/hadoop HADOOP_CONF_DIR=/opt/hadoop/etc/hadoop PATH=/opt/hadoop/bin:$PATH
# Mount the site's core-site.xml/hdfs-site.xml over HADOOP_CONF_DIR at run time.
RUN /opt/hadoop/bin/hadoop version

# Default build target (last stage): no Hadoop client.
FROM base AS local
