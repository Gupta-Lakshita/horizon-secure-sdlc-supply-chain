FROM aquasec/trivy:0.62.1 AS trivy

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NPM_CONFIG_CACHE=/tmp/npm-cache \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_CHROME_EXECUTABLE_PATH=/usr/bin/chromium \
    HOME=/home/horizon \
    JMETER_HOME=/opt/apache-jmeter \
    SONAR_SCANNER_HOME=/opt/sonar-scanner \
    PATH=/opt/apache-jmeter/bin:/opt/sonar-scanner/bin:$PATH

ARG KUBECTL_VERSION=v1.30.10
ARG HELM_VERSION=3.15.4
ARG JMETER_VERSION=5.6.3
ARG SONAR_SCANNER_VERSION=6.2.1.4610
ARG GITLEAKS_VERSION=8.24.3
ARG CONFTEST_VERSION=0.56.0

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        jq \
        maven \
        nodejs \
        npm \
        chromium \
        default-jdk-headless \
        tar \
        unzip \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    curl_common="curl --fail --location --show-error --connect-timeout 20 --max-time 300 --retry 3 --retry-delay 5 --retry-all-errors"; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) aws_arch="x86_64"; kubectl_arch="amd64"; tool_arch="x86_64"; conftest_arch="x86_64"; helm_arch="amd64" ;; \
      arm64) aws_arch="aarch64"; kubectl_arch="arm64"; tool_arch="arm64"; conftest_arch="arm64"; helm_arch="arm64" ;; \
      *) echo "Unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    ${curl_common} "https://awscli.amazonaws.com/awscli-exe-linux-${aws_arch}.zip" -o /tmp/awscliv2.zip; \
    unzip -q /tmp/awscliv2.zip -d /tmp; \
    /tmp/aws/install; \
    rm -rf /tmp/aws /tmp/awscliv2.zip; \
    ${curl_common} "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${kubectl_arch}/kubectl" -o /usr/local/bin/kubectl; \
    chmod +x /usr/local/bin/kubectl; \
    ${curl_common} "https://get.helm.sh/helm-v${HELM_VERSION}-linux-${helm_arch}.tar.gz" -o /tmp/helm.tgz; \
    tar -xzf /tmp/helm.tgz -C /tmp; \
    mv "/tmp/linux-${helm_arch}/helm" /usr/local/bin/helm; \
    chmod +x /usr/local/bin/helm; \
    rm -rf /tmp/helm.tgz "/tmp/linux-${helm_arch}"; \
    ${curl_common} "https://archive.apache.org/dist/jmeter/binaries/apache-jmeter-${JMETER_VERSION}.tgz" -o /tmp/jmeter.tgz; \
    mkdir -p /opt; \
    tar -xzf /tmp/jmeter.tgz -C /opt; \
    mv "/opt/apache-jmeter-${JMETER_VERSION}" /opt/apache-jmeter; \
    ln -sf /opt/apache-jmeter/bin/jmeter /usr/local/bin/jmeter; \
    rm -f /tmp/jmeter.tgz; \
    ${curl_common} "https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-${SONAR_SCANNER_VERSION}-linux-x64.zip" -o /tmp/sonar-scanner.zip; \
    unzip -q /tmp/sonar-scanner.zip -d /opt; \
    mv "/opt/sonar-scanner-${SONAR_SCANNER_VERSION}-linux-x64" /opt/sonar-scanner; \
    ln -sf /opt/sonar-scanner/bin/sonar-scanner /usr/local/bin/sonar-scanner; \
    rm -f /tmp/sonar-scanner.zip; \
    ${curl_common} "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${tool_arch}.tar.gz" -o /tmp/gitleaks.tgz; \
    tar -xzf /tmp/gitleaks.tgz -C /tmp gitleaks; \
    mv /tmp/gitleaks /usr/local/bin/gitleaks; \
    chmod +x /usr/local/bin/gitleaks; \
    rm -f /tmp/gitleaks.tgz; \
    ${curl_common} "https://github.com/open-policy-agent/conftest/releases/download/v${CONFTEST_VERSION}/conftest_${CONFTEST_VERSION}_Linux_${conftest_arch}.tar.gz" -o /tmp/conftest.tgz; \
    tar -xzf /tmp/conftest.tgz -C /tmp conftest; \
    mv /tmp/conftest /usr/local/bin/conftest; \
    chmod +x /usr/local/bin/conftest; \
    rm -f /tmp/conftest.tgz; \
    aws --version; \
    kubectl version --client=true; \
    helm version --short; \
    jmeter --version; \
    sonar-scanner --version; \
    gitleaks version; \
    conftest --version

RUN npm install -g newman newman-reporter-htmlextra @playwright/test@1.44.1

COPY --from=trivy /usr/local/bin/trivy /usr/local/bin/trivy

RUN useradd --create-home --uid 10001 horizon \
    && mkdir -p /var/lib/horizon-runner /tmp/npm-cache /ms-playwright \
    && chown -R 10001:10001 /var/lib/horizon-runner /tmp/npm-cache /ms-playwright /home/horizon

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt semgrep

COPY app /app/app
RUN chown -R 10001:10001 /app

USER 10001
EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
