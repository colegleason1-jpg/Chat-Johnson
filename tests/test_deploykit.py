"""Deploy Kit: deterministic generation, offline validation, packaging."""
import io
import json
import os
import re
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import deploykit as dk


@pytest.mark.parametrize("target", list(dk.TARGETS))
@pytest.mark.parametrize("cloud", ["aws", "gcp", "azure", "none"])
def test_every_target_generates_a_valid_kit(target, cloud):
    files = dk.generate_kit(dk.KitSpec(app_name="Demo App!", target=target, cloud=cloud))
    paths = {item.path for item in files}
    assert "docs/RUNBOOK.md" in paths and "scripts/smoke_test.sh" in paths
    findings = dk.validate_kit(files)
    errors = [(f.path, f.detail) for f in findings if f.level == "error"]
    assert errors == []
    assert not any(re.search(r"\[\[[a-z_]+\]\]", item.body) for item in files), "unfilled placeholder"
    runbook = next(item for item in files if item.path == "docs/RUNBOOK.md").body
    assert "demo-app" in runbook and "/?health=1" in next(item for item in files if item.path == "scripts/smoke_test.sh").body


def test_kubernetes_kit_has_the_whole_pipeline():
    files = {item.path: item for item in dk.generate_kit(dk.KitSpec(target="docker-kubernetes", cloud="aws"))}
    assert "Dockerfile" in files and "USER app" in files["Dockerfile"].body and "HEALTHCHECK" in files["Dockerfile"].body
    workflow = files[".github/workflows/ci-cd.yml"].body
    for job in ("lint-test:", "build-scan-push:", "deploy:", "post-deploy-smoke:"):
        assert job in workflow
    assert "${{ secrets.REGISTRY_TOKEN }}" in workflow  # GitHub expressions survive templating
    assert "helm upgrade --install chat-johnson" in workflow and "--atomic" in workflow
    assert "helm/chat-johnson/templates/deployment.yaml" in files and "readinessProbe" in files["helm/chat-johnson/templates/deployment.yaml"].body
    assert "terraform/main.tf" in files and "aws_ecr_repository" in files["terraform/main.tf"].body
    assert "observability/prometheus-rules.yaml" in files and "HighErrorRate" in files["observability/prometheus-rules.yaml"].body
    assert 'helm -n "${NAMESPACE}" rollback "${RELEASE}"' in files["scripts/rollback.sh"].body
    runbook = files["docs/RUNBOOK.md"].body
    assert "KUBECONFIG_B64" in runbook and "## Roll back" in runbook


def test_streamlit_cloud_kit_is_ci_plus_smoke_plus_runbook():
    files = {item.path: item for item in dk.generate_kit(dk.KitSpec(target="streamlit-cloud", cloud="none"))}
    assert set(files) == {".github/workflows/ci.yml", ".github/workflows/post-deploy-smoke.yml", "scripts/smoke_test.sh", "docs/RUNBOOK.md"}
    assert "git revert" in files["docs/RUNBOOK.md"].body


def test_serverless_flavours_follow_the_cloud():
    aws = {i.path for i in dk.generate_kit(dk.KitSpec(target="serverless", cloud="aws"))}
    gcp = {i.path for i in dk.generate_kit(dk.KitSpec(target="serverless", cloud="gcp"))}
    azure = {i.path for i in dk.generate_kit(dk.KitSpec(target="serverless", cloud="azure"))}
    assert "serverless/template.yaml" in aws and "serverless/service.yaml" in gcp and "serverless/containerapp.yaml" in azure


def test_validator_catches_real_mistakes():
    bad = [
        dk.KitFile("Dockerfile", "RUN echo hi\nFROM python:latest\nCMD [\"x\"]\n", "dockerfile", ""),
        dk.KitFile(".github/workflows/x.yml", "name: x\non: push\njobs:\n  a:\n    needs: ghost\n    steps: []\n", "yaml", ""),
        dk.KitFile("observability/d.json", "{not json", "json", ""),
        dk.KitFile("helm/x/templates/a.yaml", "{{ include x", "yaml", ""),
        dk.KitFile("terraform/main.tf", 'resource "a" "b" {\n', "hcl", ""),
    ]
    levels = {(f.path, f.level) for f in dk.validate_kit(bad)}
    assert ("Dockerfile", "error") in levels and ("Dockerfile", "warn") in levels
    assert (".github/workflows/x.yml", "error") in levels
    assert ("observability/d.json", "error") in levels
    assert ("helm/x/templates/a.yaml", "error") in levels
    assert ("terraform/main.tf", "error") in levels
    shell = dk.validate_kit([dk.KitFile("scripts/s.sh", "if [ 1 ]; then echo\n", "bash", "")])[0]
    assert shell.level in {"error", "skipped"}


def test_cloudformation_tags_and_zip_round_trip():
    files = dk.generate_kit(dk.KitSpec(target="serverless", cloud="aws"))
    template = next(f for f in files if f.path == "serverless/template.yaml")
    assert "!Ref ImageUri" in template.body
    assert [f.level for f in dk.validate_kit([template])] == ["ok"]
    archive = zipfile.ZipFile(io.BytesIO(dk.kit_zip(files)))
    assert set(archive.namelist()) == {f.path for f in files}
    info = archive.getinfo("scripts/smoke_test.sh")
    assert (info.external_attr >> 16) & 0o111  # executable bit kept


def test_fill_and_slug():
    spec = dk.KitSpec(app_name="My App", registry="ghcr.io/me/repo/")
    assert dk.slug("My App!!") == "my-app"
    assert dk.fill("[[app_name]] ${{ x }} {{ .Values.y }} $VAR [[image]]", spec.normalized()) == "my-app ${{ x }} {{ .Values.y }} $VAR ghcr.io/me/repo/my-app"
    assert json.loads(dk._entrypoint_json(spec))[0] == "python"
