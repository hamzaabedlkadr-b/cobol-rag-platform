# Build the upstream Cobol-REKT CLI with its required JDK 21 toolchain.
# Build context must be the cobol-rekt repository including its submodules:
#   docker build -f ../cobol-rag-platform/docker/cobol-rekt.Dockerfile .
FROM maven:3.9-eclipse-temurin-21 AS build
WORKDIR /src
COPY . .
RUN mvn clean package -Dcheckstyle.skip=true -Dmaven.test.skip=true

FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=build /src /app
ENTRYPOINT ["java", "-jar", "/app/smojol-cli/target/smojol-cli.jar"]

