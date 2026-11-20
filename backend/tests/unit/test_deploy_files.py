"""Static checks of the optional Docker / Kubernetes deployment files (no Docker needed).

CI additionally builds the image and starts the compose stack (``docker`` job)."""

import re
from pathlib import Path
from typing import Any

import yaml

from app.api.health import router as health_router
from app.core.config import Settings

REPO = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO / "Dockerfile"
COMPOSE = REPO / "compose.yaml"
K8S = REPO / "deploy" / "kubernetes"
API_PREFIX = "/api/v1"
SETTING_NAMES = {name.upper() for name in Settings.model_fields}
HEALTH_PATHS = {
    f"{API_PREFIX}{route.path}"  # type: ignore[attr-defined]
    for route in health_router.routes
}


def load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def dockerfile_text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def dockerfile_instructions() -> list[str]:
    """Instructions with line continuations joined and comments dropped."""
    joined = re.sub(r"\\\n", " ", dockerfile_text())
    return [
        line.strip()
        for line in joined.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def k8s_deployment() -> dict[str, Any]:
    return load(K8S / "deployment.yaml")


def k8s_pod_spec() -> dict[str, Any]:
    spec = k8s_deployment()["spec"]["template"]["spec"]
    assert isinstance(spec, dict)
    return spec


def k8s_containers() -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in k8s_pod_spec()["containers"]}


# --- Dockerfile ------------------------------------------------------------------------------


def test_dockerfile_is_a_python_312_multi_stage_uv_build() -> None:
    lines = dockerfile_instructions()
    assert any(line.startswith("ARG PYTHON_IMAGE=python:3.12-slim-bookworm") for line in lines)
    assert sum(1 for line in lines if line.startswith("FROM ")) >= 2
    assert any("uv sync --frozen --no-dev --no-install-project" in line for line in lines)
    assert any(line.endswith("uv sync --frozen --no-dev $flags") for line in lines)
    assert re.search(
        r"COPY --from=ghcr\.io/astral-sh/uv:(latest|\d+\.\d+(\.\d+)?) ", dockerfile_text()
    )


def test_dockerfile_runs_as_a_non_root_user_with_a_data_volume() -> None:
    lines = dockerfile_instructions()
    users = [line for line in lines if line.startswith("USER ")]
    assert users
    assert users[-1].split()[1].split(":")[0] not in {"root", "0"}
    assert "VOLUME /data" in lines
    assert any("10001" in line and "useradd" in line for line in lines)


def test_dockerfile_healthcheck_uses_an_existing_route() -> None:
    match = re.search(r"HEALTHCHECK .*?(/api/v1/health/\w+)", dockerfile_text(), re.DOTALL)
    assert match is not None
    assert match.group(1) in HEALTH_PATHS


def test_dockerfile_cmd_is_the_serve_command() -> None:
    assert 'CMD ["python", "-m", "app.cli", "serve"]' in dockerfile_text()
    cli = (REPO / "backend" / "app" / "cli.py").read_text(encoding="utf-8")
    assert '"serve"' in cli
    assert (REPO / "backend" / "app" / "worker.py").is_file()


def test_dockerfile_env_names_are_real_settings() -> None:
    names: set[str] = set()
    for line in dockerfile_instructions():
        if line.startswith("ENV "):
            names.update(re.findall(r"\b([A-Z][A-Z_]+)=", line))
    container_settings = {"APP_HOST", "APP_PORT", "DATA_DIR", "DATABASE_URL", "CHECKPOINT_DB_PATH"}
    assert container_settings <= names
    for name in container_settings:
        assert name in SETTING_NAMES


# --- .dockerignore ---------------------------------------------------------------------------


def test_dockerignore_excludes_secrets_and_data() -> None:
    lines = [
        line.strip()
        for line in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert {".env", ".env.*", "!.env.example", "data/", ".git"} <= set(lines)
    assert {"*.db", "*.sqlite", "**/.venv"} <= set(lines)
    # The negation must come after the pattern it re-includes, or it has no effect.
    # Docker applies the LAST matching rule, so no .env exclusion may follow the re-include
    # (the Dockerfile COPYs .env.example; a later `**/.env.*` broke the first CI build).
    env_rules = [i for i, line in enumerate(lines) if ".env" in line and line != "!.env.example"]
    assert lines.index("!.env.example") > max(env_rules)
    # COPY backend/ must not be able to pull a nested .env or credentials file into the image.
    assert {"**/.env", "**/credentials*", ".manual-demo"} <= set(lines)


# --- compose ---------------------------------------------------------------------------------


def test_compose_runs_web_and_worker_on_one_volume() -> None:
    compose = load(COMPOSE)
    services = compose["services"]
    assert {"web", "worker"} <= set(services)
    assert "coursepilot-data" in compose["volumes"]
    for name in ("web", "worker"):
        assert "coursepilot-data:/data" in services[name]["volumes"]
        assert services[name]["env_file"] == ".env"
        assert services[name]["restart"] == "unless-stopped"
    assert services["web"]["command"][-2:] == ["app.cli", "serve"]
    assert services["worker"]["command"][-1] == "app.worker"
    assert services["worker"]["depends_on"]["web"]["condition"] == "service_healthy"
    assert "ports" not in services["worker"]
    assert "deploy" not in services["web"]  # no replicas here: single instance only
    # The image HEALTHCHECK probes the web port; the worker serves none, so it must be disabled
    # there or `docker compose up --wait` never sees a healthy stack (failed the first CI run).
    assert services["worker"]["healthcheck"] == {"disable": True}


def test_compose_overrides_paths_and_checks_a_real_health_route() -> None:
    compose = load(COMPOSE)
    for name in ("web", "worker"):
        env = compose["services"][name]["environment"]
        assert env["DATA_DIR"] == "/data"
        assert env["DATABASE_URL"] == "sqlite+aiosqlite:////data/coursepilot.db"
        assert env["CHECKPOINT_DB_PATH"] == "/data/checkpoints.db"
        assert env["APP_HOST"] == "0.0.0.0"  # noqa: S104
        for key in env:
            assert key in SETTING_NAMES
    test = " ".join(compose["services"]["web"]["healthcheck"]["test"])
    assert any(path in test for path in HEALTH_PATHS)


def test_dockerfile_extras_are_an_empty_build_arg_for_both_syncs() -> None:
    """EXTRAS (e.g. local-embeddings) must reach BOTH syncs: the second one is exact and would
    otherwise remove what the first installed. The default image installs no extra."""
    lines = dockerfile_instructions()
    assert 'ARG EXTRAS=""' in lines
    syncs = [line for line in lines if "uv sync" in line]
    assert len(syncs) == 2
    assert all("--extra $extra" in line and "$EXTRAS" in line for line in syncs)


def test_compose_ollama_is_an_optional_profile_with_its_own_volume() -> None:
    compose = load(COMPOSE)
    ollama = compose["services"]["ollama"]
    assert ollama["profiles"] == ["local-ai"]  # off unless asked for
    assert ollama["image"] == "ollama/ollama:0.40.16"
    assert "ollama-models:/root/.ollama" in ollama["volumes"]
    assert "ollama-models" in compose["volumes"]
    assert "ports" not in ollama  # internal network only
    for name in ("web", "worker"):  # nothing in the default stack waits for it
        assert "ollama" not in compose["services"][name].get("depends_on", {})
        assert "profiles" not in compose["services"][name]
    assert compose["services"]["web"]["build"]["args"] == {"EXTRAS": "${EXTRAS:-}"}


# --- Kubernetes ------------------------------------------------------------------------------


def test_k8s_is_one_replica_recreate() -> None:
    spec = k8s_deployment()["spec"]
    assert spec["replicas"] == 1
    assert spec["strategy"]["type"] == "Recreate"


def test_k8s_web_and_worker_share_the_claim_at_data() -> None:
    containers = k8s_containers()
    assert set(containers) == {"web", "worker"}
    pod = k8s_pod_spec()
    claims = {
        v["name"]: v["persistentVolumeClaim"]["claimName"]
        for v in pod["volumes"]
        if "persistentVolumeClaim" in v
    }
    pvc = load(K8S / "pvc.yaml")
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    for container in containers.values():
        mounts = {m["mountPath"]: m["name"] for m in container["volumeMounts"]}
        assert claims[mounts["/data"]] == pvc["metadata"]["name"]
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
        assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
        for env in container["env"]:
            assert env["name"] in SETTING_NAMES
        assert container["envFrom"][0]["secretRef"]["name"] == "coursepilot-env"
    assert containers["web"]["command"][-2:] == ["app.cli", "serve"]
    assert containers["worker"]["command"][-1] == "app.worker"
    security = pod["securityContext"]
    assert security["runAsNonRoot"] is True
    assert security["runAsUser"] == security["fsGroup"] == 10001


def test_k8s_probes_use_existing_health_routes() -> None:
    web = k8s_containers()["web"]
    for probe in ("readinessProbe", "livenessProbe", "startupProbe"):
        assert web[probe]["httpGet"]["path"] in HEALTH_PATHS
    assert web["readinessProbe"]["httpGet"]["path"].endswith("/ready")
    assert web["livenessProbe"]["httpGet"]["path"].endswith("/live")


def test_k8s_service_and_ingress_point_at_the_pod() -> None:
    service = load(K8S / "service.yaml")
    assert service["spec"]["ports"][0]["targetPort"] == "http"
    assert k8s_containers()["web"]["ports"][0]["name"] == "http"
    ingress = load(K8S / "ingress.yaml")
    backend = ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    assert backend["name"] == service["metadata"]["name"]


def test_kustomization_lists_every_manifest() -> None:
    kustomization = load(K8S / "kustomization.yaml")
    manifests = {p.name for p in K8S.glob("*.yaml") if p.name != "kustomization.yaml"}
    assert set(kustomization["resources"]) == manifests
