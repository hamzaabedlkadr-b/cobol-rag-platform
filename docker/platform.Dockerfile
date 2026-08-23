FROM eclipse-temurin:21-jre AS java-runtime

FROM maven:3.9-eclipse-temurin-21 AS rekt-build
WORKDIR /src
COPY --from=cobol_rekt . .
RUN mvn clean package -Dcheckstyle.skip=true -Dmaven.test.skip=true

FROM python:3.12-slim

COPY --from=java-runtime /opt/java/openjdk /opt/java/openjdk
COPY --from=rekt-build /src /opt/cobol-rekt
ENV JAVA_HOME=/opt/java/openjdk
ENV PATH="/opt/java/openjdk/bin:${PATH}"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /opt/platform
COPY docker/requirements-rag.txt /tmp/requirements-rag.txt
RUN pip install --no-cache-dir -r /tmp/requirements-rag.txt
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .
COPY config/container.toml /opt/platform/config/container.toml

WORKDIR /workspace
ENV COBOL_PLATFORM_CONFIG=/opt/platform/config/container.toml
ENV COBOL_PLATFORM_PROGRAMS_DIR=/workspace/programs
ENV COBOL_PLATFORM_RUNS_DIR=/workspace/.runs
ENTRYPOINT ["python", "-m", "cobol_rag_platform"]
