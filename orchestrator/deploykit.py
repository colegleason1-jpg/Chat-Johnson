"""Deploy Kit: deterministic deployment artefacts, validated offline, never applied by the app.

Two live chats promised CI/CD, containers, Helm, Terraform, serverless, observability, rollback,
and runbooks. This module produces those files for a chosen target from templates (zero provider
calls), checks their syntax with what is available offline, and packages them. Running them is
the target repository's CI job once the operator adds secrets; this app never executes Docker,
Terraform, Helm, or cloud CLIs.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Sequence

TARGETS: Dict[str, str] = {
    "streamlit-cloud": "Streamlit Community Cloud (deploys main; CI + post-deploy smoke + runbook)",
    "docker-kubernetes": "Docker image + Kubernetes via Helm (CI/CD, chart, Terraform, observability, rollback)",
    "serverless": "Serverless container (AWS SAM, Google Cloud Run, or Azure Container Apps)",
}
CLOUDS = ("aws", "gcp", "azure", "none")
LANGUAGES = ("python", "node")


@dataclass
class KitSpec:
    app_name: str = "chat-johnson"
    target: str = "docker-kubernetes"
    language: str = "python"
    runtime_version: str = "3.12"
    port: int = 8501
    registry: str = "ghcr.io/OWNER/REPO"
    cloud: str = "aws"
    health_path: str = "/_stcore/health"  # Streamlit answers "ok" here; ?health=1 is the human/JSON view
    lint_command: str = "ruff check ."
    test_command: str = "python -m pytest -q"
    entrypoint: str = "python -m streamlit run app.py --server.port 8501 --server.address 0.0.0.0"
    deploy_url: str = "https://example.streamlit.app"

    def normalized(self) -> "KitSpec":
        spec = KitSpec(**asdict(self))
        spec.app_name = slug(self.app_name) or "app"
        spec.target = self.target if self.target in TARGETS else "docker-kubernetes"
        spec.language = self.language if self.language in LANGUAGES else "python"
        spec.cloud = self.cloud if self.cloud in CLOUDS else "none"
        spec.port = int(self.port) if 1 <= int(self.port) <= 65535 else 8501
        spec.registry = (self.registry or "ghcr.io/OWNER/REPO").strip().rstrip("/")
        spec.health_path = self.health_path.strip() or "/"
        spec.deploy_url = (self.deploy_url or "").strip().rstrip("/")
        return spec


@dataclass
class KitFile:
    path: str
    body: str
    language: str
    purpose: str


@dataclass
class Finding:
    path: str
    level: str  # ok | warn | error | skipped
    detail: str


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", (value or "").strip().lower()).strip("-")[:63]


def fill(template: str, spec: KitSpec) -> str:
    """[[field]] placeholders; nothing else is touched, so ``${{ }}``, ``$VAR`` and ``{{ .Values }}`` survive."""
    text = template
    for key, value in asdict(spec).items():
        text = text.replace(f"[[{key}]]", str(value))
    text = text.replace("[[image]]", f"{spec.registry}/{spec.app_name}")
    text = text.replace("[[health_url_path]]", spec.health_path)
    return text


# --------------------------------------------------------------------------- templates

_DOCKERFILE_PY = """# syntax=docker/dockerfile:1
FROM python:[[runtime_version]]-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN groupadd --system app && useradd --system --gid app --create-home app
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY . .
RUN chown -R app:app /app
USER app
EXPOSE [[port]]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \\
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:[[port]][[health_url_path]]', timeout=4).status < 400 else 1)"
CMD [[entrypoint_json]]
"""

_DOCKERFILE_NODE = """# syntax=docker/dockerfile:1
FROM node:[[runtime_version]]-alpine AS deps
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev

FROM node:[[runtime_version]]-alpine
ENV NODE_ENV=production
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
USER node
EXPOSE [[port]]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \\
  CMD wget -qO- http://127.0.0.1:[[port]][[health_url_path]] || exit 1
CMD [[entrypoint_json]]
"""

_DOCKERIGNORE = """.git
.github
.venv
venv
__pycache__
*.pyc
*.db
.pytest_cache
node_modules
.env
.env.*
*.log
dist
build
"""

_CI_HEADER = """name: ci-cd
on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:
permissions:
  contents: read
  packages: write
  id-token: write
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
jobs:
  lint-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
[[setup_steps]]
      - name: Lint
        run: [[lint_command]]
      - name: Test
        run: [[test_command]]
"""

_SETUP_PY = """      - uses: actions/setup-python@v5
        with:
          python-version: "[[runtime_version]]"
          cache: pip
      - run: pip install -r requirements.txt
"""

_SETUP_NODE = """      - uses: actions/setup-node@v4
        with:
          node-version: "[[runtime_version]]"
          cache: npm
      - run: npm ci
"""

_CI_BUILD_PUSH = """  build-scan-push:
    needs: lint-test
    runs-on: ubuntu-latest
    if: github.event_name != 'pull_request'
    outputs:
      image: ${{ steps.meta.outputs.image }}
    steps:
      - uses: actions/checkout@v4
      - name: Image tag
        id: meta
        run: echo "image=[[image]]:${GITHUB_SHA::12}" >> "$GITHUB_OUTPUT"
      - uses: docker/setup-buildx-action@v3
      - name: Registry login
        uses: docker/login-action@v3
        with:
          registry: [[registry_host]]
          username: ${{ github.actor }}
          password: ${{ secrets.REGISTRY_TOKEN }}
      - name: Build (local) for scanning
        uses: docker/build-push-action@v6
        with:
          context: .
          load: true
          tags: ${{ steps.meta.outputs.image }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
      - name: Vulnerability scan (fails on HIGH/CRITICAL)
        uses: aquasecurity/trivy-action@0.28.0
        with:
          image-ref: ${{ steps.meta.outputs.image }}
          format: table
          exit-code: "1"
          severity: HIGH,CRITICAL
          ignore-unfixed: true
      - name: Push
        uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.image }},[[image]]:latest
          cache-from: type=gha
"""

_CI_DEPLOY_HELM = """  deploy:
    needs: build-scan-push
    runs-on: ubuntu-latest
    environment: production
    steps:
      - uses: actions/checkout@v4
      - uses: azure/setup-helm@v4
      - name: Kubeconfig from secret
        run: |
          mkdir -p ~/.kube
          echo "${{ secrets.KUBECONFIG_B64 }}" | base64 -d > ~/.kube/config
          chmod 600 ~/.kube/config
      - name: Helm upgrade (atomic; rolls back on failure)
        run: |
          helm upgrade --install [[app_name]] ./helm/[[app_name]] \\
            --namespace [[app_name]] --create-namespace \\
            --set image.repository=[[image]] \\
            --set image.tag=${GITHUB_SHA::12} \\
            --atomic --timeout 5m --wait
      - name: Rollout status
        run: kubectl -n [[app_name]] rollout status deployment/[[app_name]] --timeout=300s
"""

_CI_DEPLOY_SAM = """  deploy:
    needs: build-scan-push
    runs-on: ubuntu-latest
    environment: production
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}
          aws-region: ${{ vars.AWS_REGION }}
      - uses: aws-actions/setup-sam@v2
      - name: SAM deploy (container image)
        run: |
          sam deploy --template-file serverless/template.yaml --stack-name [[app_name]] \\
            --image-repository ${{ vars.ECR_REPOSITORY_URI }} \\
            --parameter-overrides ImageUri=${{ needs.build-scan-push.outputs.image }} \\
            --capabilities CAPABILITY_IAM --no-confirm-changeset --no-fail-on-empty-changeset
"""

_CI_DEPLOY_CLOUDRUN = """  deploy:
    needs: build-scan-push
    runs-on: ubuntu-latest
    environment: production
    steps:
      - uses: actions/checkout@v4
      - uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: ${{ secrets.GCP_WORKLOAD_IDENTITY_PROVIDER }}
          service_account: ${{ secrets.GCP_DEPLOY_SERVICE_ACCOUNT }}
      - uses: google-github-actions/deploy-cloudrun@v2
        with:
          service: [[app_name]]
          region: ${{ vars.GCP_REGION }}
          image: ${{ needs.build-scan-push.outputs.image }}
          flags: --port=[[port]] --allow-unauthenticated
"""

_CI_DEPLOY_ACA = """  deploy:
    needs: build-scan-push
    runs-on: ubuntu-latest
    environment: production
    steps:
      - uses: actions/checkout@v4
      - uses: azure/login@v2
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
      - name: Container Apps update
        run: |
          az containerapp update --name [[app_name]] --resource-group ${{ vars.AZURE_RESOURCE_GROUP }} \\
            --image ${{ needs.build-scan-push.outputs.image }}
"""

_CI_SMOKE = """  post-deploy-smoke:
    needs: [[smoke_needs]]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Health and version check
        env:
          DEPLOY_URL: ${{ vars.DEPLOY_URL }}
          EXPECTED_SHA: ${{ github.sha }}
        run: bash scripts/smoke_test.sh
"""

_CI_STREAMLIT = """name: ci
on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:
permissions:
  contents: read
jobs:
  lint-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
[[setup_steps]]
      - name: Lint
        run: [[lint_command]]
      - name: Test
        run: [[test_command]]
"""

_SMOKE_WORKFLOW = """name: post-deploy-smoke
on:
  workflow_dispatch:
  schedule:
    - cron: "17 */6 * * *"
permissions:
  contents: read
jobs:
  smoke:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Health and version check against the live deployment
        env:
          DEPLOY_URL: ${{ vars.DEPLOY_URL }}
          EXPECTED_SHA: ${{ github.sha }}
        run: bash scripts/smoke_test.sh
"""

_SMOKE_SH = """#!/usr/bin/env bash
# Post-deploy smoke test: the health path answers, and (when the app exposes it) the build
# matches the commit that was deployed. Exit non-zero to fail the workflow.
set -euo pipefail
: "${DEPLOY_URL:?set DEPLOY_URL (repository variable)}"
HEALTH_PATH="[[health_url_path]]"
EXPECTED_SHA="${EXPECTED_SHA:-}"
url="${DEPLOY_URL%/}${HEALTH_PATH}"
echo "GET ${url}"
for attempt in 1 2 3 4 5 6; do
  if body=$(curl -fsS --max-time 20 "${url}"); then
    echo "healthy on attempt ${attempt}"
    if [[ -n "${EXPECTED_SHA}" ]] && echo "${body}" | grep -q "build"; then
      if echo "${body}" | grep -q "${EXPECTED_SHA:0:7}"; then
        echo "build marker matches ${EXPECTED_SHA:0:7}"
      else
        echo "WARNING: build marker does not mention ${EXPECTED_SHA:0:7} (deploy may still be rolling)"
      fi
    fi
    exit 0
  fi
  echo "not ready (attempt ${attempt}); waiting 20s"
  sleep 20
done
echo "health check failed after 6 attempts" >&2
exit 1
"""

_ROLLBACK_SH = """#!/usr/bin/env bash
# Roll the release back one step. Helm keeps release history, so this is one command;
# the previous image tag is printed so the operator can pin it if needed.
set -euo pipefail
NAMESPACE="${NAMESPACE:-[[app_name]]}"
RELEASE="${RELEASE:-[[app_name]]}"
echo "Release history for ${RELEASE} in ${NAMESPACE}:"
helm -n "${NAMESPACE}" history "${RELEASE}" --max 5
current=$(kubectl -n "${NAMESPACE}" get deployment "${RELEASE}" -o jsonpath='{.spec.template.spec.containers[0].image}')
echo "Current image: ${current}"
helm -n "${NAMESPACE}" rollback "${RELEASE}" --wait --timeout 5m
kubectl -n "${NAMESPACE}" rollout status "deployment/${RELEASE}" --timeout=300s
echo "Rolled back. Previous image was ${current}; verify with scripts/smoke_test.sh"
"""

_CHART_YAML = """apiVersion: v2
name: [[app_name]]
description: Helm chart generated by Chat Johnson Deploy Kit
type: application
version: 0.1.0
appVersion: "0.1.0"
"""

_VALUES_YAML = """replicaCount: 2
image:
  repository: [[image]]
  tag: latest
  pullPolicy: IfNotPresent
service:
  type: ClusterIP
  port: 80
  targetPort: [[port]]
ingress:
  enabled: false
  className: nginx
  host: [[app_name]].example.com
  tls: false
resources:
  requests:
    cpu: 100m
    memory: 256Mi
  limits:
    cpu: 500m
    memory: 512Mi
autoscaling:
  enabled: true
  minReplicas: 2
  maxReplicas: 6
  targetCPUUtilizationPercentage: 70
probes:
  path: [[health_url_path]]
env: {}
existingSecret: ""
"""

_HELPERS_TPL = """{{- define "app.name" -}}
{{ .Chart.Name }}
{{- end -}}
{{- define "app.labels" -}}
app.kubernetes.io/name: {{ include "app.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Values.image.tag | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
"""

_DEPLOYMENT_YAML = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "app.name" . }}
  labels:
    {{- include "app.labels" . | nindent 4 }}
spec:
  {{- if not .Values.autoscaling.enabled }}
  replicas: {{ .Values.replicaCount }}
  {{- end }}
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  selector:
    matchLabels:
      app.kubernetes.io/name: {{ include "app.name" . }}
  template:
    metadata:
      labels:
        {{- include "app.labels" . | nindent 8 }}
    spec:
      securityContext:
        runAsNonRoot: true
      containers:
        - name: app
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          ports:
            - containerPort: {{ .Values.service.targetPort }}
          env:
            {{- range $key, $value := .Values.env }}
            - name: {{ $key }}
              value: {{ $value | quote }}
            {{- end }}
          {{- if .Values.existingSecret }}
          envFrom:
            - secretRef:
                name: {{ .Values.existingSecret }}
          {{- end }}
          readinessProbe:
            httpGet:
              path: {{ .Values.probes.path }}
              port: {{ .Values.service.targetPort }}
            initialDelaySeconds: 10
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: {{ .Values.probes.path }}
              port: {{ .Values.service.targetPort }}
            initialDelaySeconds: 30
            periodSeconds: 20
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
"""

_SERVICE_YAML = """apiVersion: v1
kind: Service
metadata:
  name: {{ include "app.name" . }}
  labels:
    {{- include "app.labels" . | nindent 4 }}
spec:
  type: {{ .Values.service.type }}
  ports:
    - port: {{ .Values.service.port }}
      targetPort: {{ .Values.service.targetPort }}
      protocol: TCP
  selector:
    app.kubernetes.io/name: {{ include "app.name" . }}
"""

_INGRESS_YAML = """{{- if .Values.ingress.enabled }}
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {{ include "app.name" . }}
spec:
  ingressClassName: {{ .Values.ingress.className }}
  {{- if .Values.ingress.tls }}
  tls:
    - hosts: [{{ .Values.ingress.host | quote }}]
      secretName: {{ include "app.name" . }}-tls
  {{- end }}
  rules:
    - host: {{ .Values.ingress.host | quote }}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {{ include "app.name" . }}
                port:
                  number: {{ .Values.service.port }}
{{- end }}
"""

_HPA_YAML = """{{- if .Values.autoscaling.enabled }}
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ include "app.name" . }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ include "app.name" . }}
  minReplicas: {{ .Values.autoscaling.minReplicas }}
  maxReplicas: {{ .Values.autoscaling.maxReplicas }}
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {{ .Values.autoscaling.targetCPUUtilizationPercentage }}
{{- end }}
"""

_TF_VERSIONS = """terraform {
  required_version = ">= 1.6"
  required_providers {
[[tf_providers]]
  }
  # backend "s3" {}  # configure remote state before the first apply
}
"""

_TF_MAIN_AWS = """provider "aws" {
  region = var.region
}

resource "aws_ecr_repository" "app" {
  name                 = var.app_name
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
}

# GitHub Actions deploys through OIDC; no long-lived cloud keys in CI secrets.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

data "aws_iam_policy_document" "github_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:*"]
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  name               = "${var.app_name}-github-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_assume.json
}

# module "eks" {
#   source          = "terraform-aws-modules/eks/aws"
#   version         = "~> 20.0"
#   cluster_name    = var.app_name
#   cluster_version = "1.30"
#   vpc_id          = var.vpc_id
#   subnet_ids      = var.subnet_ids
# }
"""

_TF_MAIN_GCP = """provider "google" {
  project = var.project
  region  = var.region
}

resource "google_artifact_registry_repository" "app" {
  location      = var.region
  repository_id = var.app_name
  format        = "DOCKER"
}

resource "google_service_account" "deploy" {
  account_id   = "${var.app_name}-deploy"
  display_name = "GitHub Actions deploy for ${var.app_name}"
}

# module "gke" {
#   source     = "terraform-google-modules/kubernetes-engine/google"
#   project_id = var.project
#   name       = var.app_name
#   region     = var.region
#   network    = var.network
#   subnetwork = var.subnetwork
# }
"""

_TF_MAIN_AZURE = """provider "azurerm" {
  features {}
}

resource "azurerm_resource_group" "app" {
  name     = "${var.app_name}-rg"
  location = var.region
}

resource "azurerm_container_registry" "app" {
  name                = replace(var.app_name, "-", "")
  resource_group_name = azurerm_resource_group.app.name
  location            = azurerm_resource_group.app.location
  sku                 = "Basic"
}

# resource "azurerm_kubernetes_cluster" "app" {
#   name                = var.app_name
#   location            = azurerm_resource_group.app.location
#   resource_group_name = azurerm_resource_group.app.name
#   dns_prefix          = var.app_name
#   default_node_pool {
#     name       = "default"
#     node_count = 2
#     vm_size    = "Standard_B2s"
#   }
#   identity { type = "SystemAssigned" }
# }
"""

_TF_MAIN_NONE = """# No cloud selected. Add a provider block and the registry/cluster resources for your platform.
"""

_TF_VARIABLES = """variable "app_name" {
  type    = string
  default = "[[app_name]]"
}

variable "region" {
  type    = string
  default = "[[tf_region]]"
}

variable "github_repository" {
  type        = string
  description = "owner/repo allowed to assume the deploy role"
  default     = "OWNER/REPO"
}
[[tf_extra_variables]]
"""

_TF_OUTPUTS = """output "app_name" {
  value = var.app_name
}
[[tf_extra_outputs]]
"""

_OTEL_COLLECTOR = """receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318
  prometheus:
    config:
      scrape_configs:
        - job_name: [[app_name]]
          scrape_interval: 30s
          static_configs:
            - targets: ["[[app_name]]:[[port]]"]
processors:
  batch: {}
  memory_limiter:
    check_interval: 5s
    limit_percentage: 80
exporters:
  prometheus:
    endpoint: 0.0.0.0:9464
  otlp:
    endpoint: ${OTLP_ENDPOINT}
    headers:
      authorization: ${OTLP_AUTH_HEADER}
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [otlp]
    metrics:
      receivers: [otlp, prometheus]
      processors: [memory_limiter, batch]
      exporters: [prometheus, otlp]
    logs:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [otlp]
"""

_PROM_RULES = """groups:
  - name: [[app_name]]-golden-signals
    rules:
      - alert: HighErrorRate
        expr: sum(rate(http_requests_total{app="[[app_name]]",status=~"5.."}[5m])) / sum(rate(http_requests_total{app="[[app_name]]"}[5m])) > 0.02
        for: 10m
        labels:
          severity: page
        annotations:
          summary: "5xx ratio above 2% for 10 minutes"
      - alert: HighLatencyP95
        expr: histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{app="[[app_name]]"}[5m])) by (le)) > 1.5
        for: 10m
        labels:
          severity: warn
        annotations:
          summary: "p95 latency above 1.5s for 10 minutes"
      - alert: PodRestarting
        expr: increase(kube_pod_container_status_restarts_total{container="app",namespace="[[app_name]]"}[30m]) > 3
        for: 5m
        labels:
          severity: page
        annotations:
          summary: "container restarted more than 3 times in 30 minutes"
      - alert: HighMemory
        expr: container_memory_working_set_bytes{container="app",namespace="[[app_name]]"} / container_spec_memory_limit_bytes{container="app",namespace="[[app_name]]"} > 0.9
        for: 15m
        labels:
          severity: warn
        annotations:
          summary: "memory above 90% of limit for 15 minutes"
"""

_GRAFANA_JSON = {
    "title": "[[app_name]] golden signals",
    "schemaVersion": 39,
    "panels": [
        {"type": "timeseries", "title": "Request rate", "targets": [{"expr": 'sum(rate(http_requests_total{app="[[app_name]]"}[5m]))'}]},
        {"type": "timeseries", "title": "5xx ratio", "targets": [{"expr": 'sum(rate(http_requests_total{app="[[app_name]]",status=~"5.."}[5m])) / sum(rate(http_requests_total{app="[[app_name]]"}[5m]))'}]},
        {"type": "timeseries", "title": "p95 latency", "targets": [{"expr": 'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{app="[[app_name]]"}[5m])) by (le))'}]},
        {"type": "timeseries", "title": "CPU / memory", "targets": [{"expr": 'sum(rate(container_cpu_usage_seconds_total{container="app",namespace="[[app_name]]"}[5m]))'}, {"expr": 'sum(container_memory_working_set_bytes{container="app",namespace="[[app_name]]"})'}]},
    ],
}

_SAM_TEMPLATE = """AWSTemplateFormatVersion: "2010-09-09"
Transform: AWS::Serverless-2016-10-31
Description: [[app_name]] as a Lambda container behind an HTTP API
Parameters:
  ImageUri:
    Type: String
Resources:
  AppFunction:
    Type: AWS::Serverless::Function
    Properties:
      PackageType: Image
      ImageUri: !Ref ImageUri
      MemorySize: 1024
      Timeout: 30
      Environment:
        Variables:
          PORT: "[[port]]"
      Events:
        Http:
          Type: HttpApi
          Properties:
            Path: /{proxy+}
            Method: ANY
Outputs:
  ApiUrl:
    Value: !Sub "https://${ServerlessHttpApi}.execute-api.${AWS::Region}.amazonaws.com/"
"""

_CLOUDRUN_SERVICE = """apiVersion: serving.knative.dev/v1
kind: Service
metadata:
  name: [[app_name]]
spec:
  template:
    metadata:
      annotations:
        autoscaling.knative.dev/minScale: "0"
        autoscaling.knative.dev/maxScale: "5"
    spec:
      containerConcurrency: 40
      timeoutSeconds: 60
      containers:
        - image: [[image]]:latest
          ports:
            - containerPort: [[port]]
          resources:
            limits:
              cpu: "1"
              memory: 512Mi
          startupProbe:
            httpGet:
              path: [[health_url_path]]
              port: [[port]]
            periodSeconds: 5
            failureThreshold: 12
"""

_ACA_YAML = """properties:
  configuration:
    ingress:
      external: true
      targetPort: [[port]]
    secrets: []
  template:
    containers:
      - name: [[app_name]]
        image: [[image]]:latest
        resources:
          cpu: 0.5
          memory: 1Gi
        probes:
          - type: Readiness
            httpGet:
              path: [[health_url_path]]
              port: [[port]]
            periodSeconds: 10
    scale:
      minReplicas: 0
      maxReplicas: 5
"""

_RUNBOOK = """# Runbook · [[app_name]] ([[target]])

Generated by Chat Johnson Deploy Kit. Fill the bracketed owners and URLs; keep this file next to the code.

## Deploy
[[deploy_steps]]

## Verify after every deploy
1. `DEPLOY_URL=[[deploy_url]] bash scripts/smoke_test.sh` (or let the `post-deploy-smoke` job do it); the health path is `[[health_url_path]]`.
2. Watch the golden signals for 15 minutes: error ratio < 2%, p95 latency < 1.5s, no restart loop.
3. Confirm the build marker on the live app matches the deployed commit.

## Roll back
[[rollback_steps]]

## Secrets and variables the pipeline needs
[[secrets_table]]

## Key rotation
1. Create the new key at the vendor.
2. Update the secret (repository settings → Secrets, or the platform's secret store).
3. Redeploy or restart; confirm with the smoke test; revoke the old key.

## Ownership
- Service owner: [name]
- On-call: [rotation link]
- Escalation: [channel]
"""


# --------------------------------------------------------------------------- generation

def _entrypoint_json(spec: KitSpec) -> str:
    return json.dumps(spec.entrypoint.split())


def _registry_host(spec: KitSpec) -> str:
    return spec.registry.split("/", 1)[0]


def _tf_bits(spec: KitSpec) -> Dict[str, str]:
    if spec.cloud == "aws":
        return {
            "tf_providers": '    aws = {\n      source  = "hashicorp/aws"\n      version = "~> 5.0"\n    }',
            "tf_region": "us-east-1",
            "tf_extra_variables": 'variable "vpc_id" {\n  type    = string\n  default = ""\n}\n\nvariable "subnet_ids" {\n  type    = list(string)\n  default = []\n}\n',
            "tf_extra_outputs": 'output "ecr_repository_url" {\n  value = aws_ecr_repository.app.repository_url\n}\n\noutput "github_deploy_role_arn" {\n  value = aws_iam_role.github_deploy.arn\n}\n',
            "tf_main": _TF_MAIN_AWS,
        }
    if spec.cloud == "gcp":
        return {
            "tf_providers": '    google = {\n      source  = "hashicorp/google"\n      version = "~> 5.0"\n    }',
            "tf_region": "us-central1",
            "tf_extra_variables": 'variable "project" {\n  type = string\n}\n\nvariable "network" {\n  type    = string\n  default = "default"\n}\n\nvariable "subnetwork" {\n  type    = string\n  default = "default"\n}\n',
            "tf_extra_outputs": 'output "artifact_registry" {\n  value = google_artifact_registry_repository.app.name\n}\n',
            "tf_main": _TF_MAIN_GCP,
        }
    if spec.cloud == "azure":
        return {
            "tf_providers": '    azurerm = {\n      source  = "hashicorp/azurerm"\n      version = "~> 3.0"\n    }',
            "tf_region": "eastus",
            "tf_extra_variables": "",
            "tf_extra_outputs": 'output "acr_login_server" {\n  value = azurerm_container_registry.app.login_server\n}\n',
            "tf_main": _TF_MAIN_AZURE,
        }
    return {"tf_providers": "", "tf_region": "local", "tf_extra_variables": "", "tf_extra_outputs": "", "tf_main": _TF_MAIN_NONE}


def _render(template: str, spec: KitSpec, extra: Dict[str, str]) -> str:
    text = fill(template, spec)
    text = text.replace("[[entrypoint_json]]", _entrypoint_json(spec)).replace("[[registry_host]]", _registry_host(spec))
    for key, value in extra.items():
        text = text.replace(f"[[{key}]]", value)
    return text


def _runbook(spec: KitSpec, secrets: Sequence[Sequence[str]]) -> str:
    if spec.target == "streamlit-cloud":
        deploy = ("1. Merge the pull request into `main`; Streamlit Community Cloud redeploys automatically.\n"
                  "2. Open the app's *Manage app* panel and reboot if the build marker did not change.")
        rollback = ("1. `git revert <bad merge commit>` on `main` and push; the platform redeploys the previous state.\n"
                    "2. Reboot from *Manage app* if the old build lingers.")
    elif spec.target == "docker-kubernetes":
        deploy = ("1. Merge into `main`; `ci-cd.yml` lints, tests, builds, scans, pushes the image, and runs `helm upgrade --atomic` "
                  "in the `production` environment (approve it there).\n"
                  "2. `kubectl -n [[app_name]] rollout status deployment/[[app_name]]` if watching by hand.")
        rollback = ("1. `bash scripts/rollback.sh` (Helm rollback to the previous revision, waits for the rollout).\n"
                    "2. Or pin an image: `helm -n [[app_name]] upgrade [[app_name]] ./helm/[[app_name]] --set image.tag=<previous>`.")
    else:
        deploy = ("1. Merge into `main`; `ci-cd.yml` builds, scans, pushes the image, then deploys it to the serverless platform "
                  "in the `production` environment.\n"
                  "2. Check the platform console for the new revision serving 100% of traffic.")
        rollback = ("1. Re-run the deploy job with the previous image tag (workflow_dispatch), or shift traffic to the previous revision "
                    "in the platform console (Cloud Run revisions, Lambda alias, Container Apps revision).")
    table = "| Name | Kind | Purpose |\n|---|---|---|\n" + "\n".join(f"| `{n}` | {k} | {p} |" for n, k, p in secrets)
    return _render(_RUNBOOK, spec, {"deploy_steps": fill(deploy, spec), "rollback_steps": fill(rollback, spec), "secrets_table": table})


def generate_kit(raw_spec: KitSpec) -> List[KitFile]:
    """Every file for the chosen target, from templates only. No provider call, no network."""
    spec = raw_spec.normalized()
    setup = fill(_SETUP_PY if spec.language == "python" else _SETUP_NODE, spec)
    files: List[KitFile] = []
    secrets: List[Sequence[str]] = [("DEPLOY_URL", "variable", "public URL for the post-deploy smoke test")]

    if spec.target == "streamlit-cloud":
        files.append(KitFile(".github/workflows/ci.yml", _render(_CI_STREAMLIT, spec, {"setup_steps": setup}), "yaml", "lint and test on every push"))
        files.append(KitFile(".github/workflows/post-deploy-smoke.yml", _render(_SMOKE_WORKFLOW, spec, {}), "yaml", "health + build check against the live app"))
        files.append(KitFile("scripts/smoke_test.sh", _render(_SMOKE_SH, spec, {}), "bash", "post-deploy smoke test"))
        files.append(KitFile("docs/RUNBOOK.md", _runbook(spec, secrets), "markdown", "deploy, verify, roll back, rotate keys"))
        return files

    files.append(KitFile("Dockerfile", _render(_DOCKERFILE_PY if spec.language == "python" else _DOCKERFILE_NODE, spec, {}), "dockerfile", "non-root image with a health check"))
    files.append(KitFile(".dockerignore", _DOCKERIGNORE, "text", "keeps secrets, caches, and git out of the image"))
    secrets.append(("REGISTRY_TOKEN", "secret", f"push access to {_registry_host(spec)}"))

    if spec.target == "docker-kubernetes":
        deploy_job = _CI_DEPLOY_HELM
        secrets.append(("KUBECONFIG_B64", "secret", "base64 kubeconfig for the deploy job (scoped service account)"))
        for path, template in (
            (f"helm/{spec.app_name}/Chart.yaml", _CHART_YAML),
            (f"helm/{spec.app_name}/values.yaml", _VALUES_YAML),
            (f"helm/{spec.app_name}/templates/_helpers.tpl", _HELPERS_TPL),
            (f"helm/{spec.app_name}/templates/deployment.yaml", _DEPLOYMENT_YAML),
            (f"helm/{spec.app_name}/templates/service.yaml", _SERVICE_YAML),
            (f"helm/{spec.app_name}/templates/ingress.yaml", _INGRESS_YAML),
            (f"helm/{spec.app_name}/templates/hpa.yaml", _HPA_YAML),
        ):
            files.append(KitFile(path, _render(template, spec, {}), "yaml", "Helm chart"))
        bits = _tf_bits(spec)
        files.append(KitFile("terraform/versions.tf", _render(_TF_VERSIONS, spec, bits), "hcl", "provider pins and remote state hook"))
        files.append(KitFile("terraform/main.tf", _render(bits["tf_main"], spec, bits), "hcl", "registry, deploy identity, cluster module hook"))
        files.append(KitFile("terraform/variables.tf", _render(_TF_VARIABLES, spec, bits), "hcl", "inputs"))
        files.append(KitFile("terraform/outputs.tf", _render(_TF_OUTPUTS, spec, bits), "hcl", "outputs the pipeline needs"))
        files.append(KitFile("observability/otel-collector.yaml", _render(_OTEL_COLLECTOR, spec, {}), "yaml", "traces, metrics, logs pipeline"))
        files.append(KitFile("observability/prometheus-rules.yaml", _render(_PROM_RULES, spec, {}), "yaml", "golden-signal alerts"))
        files.append(KitFile("observability/grafana-dashboard.json", json.dumps(json.loads(fill(json.dumps(_GRAFANA_JSON), spec)), indent=2), "json", "golden-signal dashboard"))
        files.append(KitFile("scripts/rollback.sh", _render(_ROLLBACK_SH, spec, {}), "bash", "one-step Helm rollback"))
    else:
        if spec.cloud == "gcp":
            deploy_job = _CI_DEPLOY_CLOUDRUN
            secrets += [("GCP_WORKLOAD_IDENTITY_PROVIDER", "secret", "OIDC provider resource name"), ("GCP_DEPLOY_SERVICE_ACCOUNT", "secret", "deploy service account email"), ("GCP_REGION", "variable", "Cloud Run region")]
            files.append(KitFile("serverless/service.yaml", _render(_CLOUDRUN_SERVICE, spec, {}), "yaml", "Cloud Run service"))
        elif spec.cloud == "azure":
            deploy_job = _CI_DEPLOY_ACA
            secrets += [("AZURE_CLIENT_ID", "secret", "federated credential client id"), ("AZURE_TENANT_ID", "secret", "tenant"), ("AZURE_SUBSCRIPTION_ID", "secret", "subscription"), ("AZURE_RESOURCE_GROUP", "variable", "resource group of the Container App")]
            files.append(KitFile("serverless/containerapp.yaml", _render(_ACA_YAML, spec, {}), "yaml", "Azure Container Apps revision"))
        else:
            deploy_job = _CI_DEPLOY_SAM
            secrets += [("AWS_DEPLOY_ROLE_ARN", "secret", "OIDC role for the deploy job"), ("AWS_REGION", "variable", "region"), ("ECR_REPOSITORY_URI", "variable", "image repository for SAM")]
            files.append(KitFile("serverless/template.yaml", _render(_SAM_TEMPLATE, spec, {}), "yaml", "AWS SAM template (Lambda container + HTTP API)"))

    workflow = _render(_CI_HEADER, spec, {"setup_steps": setup}) + _render(_CI_BUILD_PUSH, spec, {}) + _render(deploy_job, spec, {}) + _render(_CI_SMOKE, spec, {"smoke_needs": "deploy"})
    files.append(KitFile(".github/workflows/ci-cd.yml", workflow, "yaml", "lint, test, build, scan, push, deploy, smoke"))
    files.append(KitFile("scripts/smoke_test.sh", _render(_SMOKE_SH, spec, {}), "bash", "post-deploy smoke test"))
    files.append(KitFile("docs/RUNBOOK.md", _runbook(spec, secrets), "markdown", "deploy, verify, roll back, rotate keys"))
    return files


# --------------------------------------------------------------------------- validation

def _yaml_module():
    try:
        import yaml  # type: ignore
    except ImportError:  # pragma: no cover - depends on the environment
        return None
    return yaml


def _yaml_documents(yaml, body: str) -> List[Any]:
    """Parse with CloudFormation/SAM intrinsic tags (!Ref, !Sub, !GetAtt …) accepted as plain values."""

    class Loader(yaml.SafeLoader):
        pass

    def any_tag(loader, tag_suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    Loader.add_multi_constructor("!", any_tag)
    return list(yaml.load_all(body, Loader=Loader))


def _balanced(text: str, pairs: Sequence[Sequence[str]]) -> str:
    for opener, closer in pairs:
        if text.count(opener) != text.count(closer):
            return f"unbalanced {opener} {closer}: {text.count(opener)} vs {text.count(closer)}"
    return ""


def _check_dockerfile(body: str) -> List[Finding]:
    lines = [line.strip() for line in body.splitlines() if line.strip() and not line.strip().startswith("#")]
    findings: List[Finding] = []
    if not lines or not lines[0].upper().startswith("FROM"):
        findings.append(Finding("Dockerfile", "error", "first instruction must be FROM"))
    if not any(line.upper().startswith("USER ") for line in lines):
        findings.append(Finding("Dockerfile", "error", "no USER instruction: the container would run as root"))
    if any(re.search(r"^FROM\s+\S+:latest\b", line, re.I) for line in lines):
        findings.append(Finding("Dockerfile", "warn", "base image pinned to :latest; pin a version"))
    if not any(line.upper().startswith("HEALTHCHECK") for line in lines):
        findings.append(Finding("Dockerfile", "warn", "no HEALTHCHECK"))
    if not any(line.upper().startswith(("CMD", "ENTRYPOINT")) for line in lines):
        findings.append(Finding("Dockerfile", "error", "no CMD or ENTRYPOINT"))
    return findings


def _check_workflow(path: str, data: Any) -> List[Finding]:
    findings: List[Finding] = []
    if not isinstance(data, dict):
        return [Finding(path, "error", "workflow is not a mapping")]
    if "on" not in data and True not in data:  # PyYAML parses a bare `on:` key as boolean True
        findings.append(Finding(path, "error", "missing 'on' trigger"))
    jobs = data.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        return findings + [Finding(path, "error", "no jobs")]
    for name, job in jobs.items():
        if not isinstance(job, dict) or "runs-on" not in job:
            findings.append(Finding(path, "error", f"job {name}: missing runs-on"))
        elif not job.get("steps"):
            findings.append(Finding(path, "error", f"job {name}: no steps"))
        needs = job.get("needs") if isinstance(job, dict) else None
        for dep in ([needs] if isinstance(needs, str) else (needs or [])):
            if dep not in jobs:
                findings.append(Finding(path, "error", f"job {name}: needs unknown job {dep}"))
    return findings


def validate_kit(files: Sequence[KitFile]) -> List[Finding]:
    """Offline checks only: parsers and structural rules, no tool that needs a cloud or a daemon."""
    yaml = _yaml_module()
    bash = shutil.which("bash")
    findings: List[Finding] = []
    for item in files:
        path, body = item.path, item.body
        try:
            if path.endswith("Dockerfile"):
                found = _check_dockerfile(body)
                findings.extend(Finding(path, f.level, f.detail) for f in found)
                if not found:
                    findings.append(Finding(path, "ok", "FROM first, non-root USER, HEALTHCHECK, CMD"))
            elif "/templates/" in path and "helm/" in path:
                problem = _balanced(body, (("{{", "}}"),))
                findings.append(Finding(path, "error" if problem else "ok", problem or "Go template braces balanced"))
            elif path.endswith((".yml", ".yaml")):
                if yaml is None:
                    findings.append(Finding(path, "skipped", "pyyaml not installed"))
                    continue
                documents = _yaml_documents(yaml, body)
                if "/workflows/" in path:
                    found = _check_workflow(path, documents[0] if documents else None)
                    findings.extend(found)
                    if not found:
                        findings.append(Finding(path, "ok", f"workflow parses; jobs: {', '.join(documents[0]['jobs'])}"))
                else:
                    findings.append(Finding(path, "ok", f"YAML parses ({len(documents)} document(s))"))
            elif path.endswith(".json"):
                json.loads(body)
                findings.append(Finding(path, "ok", "JSON parses"))
            elif path.endswith(".sh"):
                if not bash:
                    findings.append(Finding(path, "skipped", "bash not available for -n"))
                    continue
                result = subprocess.run([bash, "-n"], input=body, capture_output=True, text=True, timeout=10)
                findings.append(Finding(path, "ok" if result.returncode == 0 else "error", "bash -n clean" if result.returncode == 0 else result.stderr.strip()[:200]))
            elif path.endswith(".tf"):
                problem = _balanced(body, (("{", "}"), ("[", "]"), ("(", ")")))
                if not problem and body.count('"') % 2:
                    problem = "unbalanced double quotes"
                findings.append(Finding(path, "error" if problem else "ok", problem or "HCL braces, brackets, and quotes balanced"))
            else:
                findings.append(Finding(path, "ok", "no parser applies"))
        except Exception as exc:  # a parser error is a finding, never a crash
            findings.append(Finding(path, "error", f"{type(exc).__name__}: {str(exc)[:200]}"))
    return findings


def kit_zip(files: Sequence[KitFile]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in files:
            info = zipfile.ZipInfo(item.path)
            info.external_attr = (0o755 if item.path.endswith(".sh") else 0o644) << 16
            archive.writestr(info, item.body)
    return buffer.getvalue()


def summarize(findings: Sequence[Finding]) -> Dict[str, int]:
    counts = {"ok": 0, "warn": 0, "error": 0, "skipped": 0}
    for finding in findings:
        counts[finding.level] = counts.get(finding.level, 0) + 1
    return counts


__all__ = ["KitSpec", "KitFile", "Finding", "TARGETS", "CLOUDS", "LANGUAGES", "generate_kit", "validate_kit", "kit_zip", "summarize", "slug", "fill"]
_ = (os, field)  # keep imports that templates may grow into without a lint churn
