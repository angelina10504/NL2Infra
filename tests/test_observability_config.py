"""Guards the provisioned observability config: the files parse and the Grafana dashboard
only queries metrics the app actually exports (so a typo fails a test, not a demo)."""
import json
import os
import re

import pytest

yaml = pytest.importorskip("yaml")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OBS = os.path.join(ROOT, "observability")

# The metric base names the app exports (without _bucket/_sum/_count/_total suffixes).
KNOWN_METRICS = {
    "nl2infra_requests_total", "nl2infra_blocked_requests_total", "nl2infra_fix_loop_rounds",
    "nl2infra_violations_total", "nl2infra_pipeline_duration_seconds", "nl2infra_llm_tokens_total",
    "nl2infra_llm_latency_seconds", "nl2infra_pending_approvals",
}


def test_compose_and_yaml_configs_parse():
    for rel in [
        "../compose.yaml",
        "prometheus/prometheus.yml",
        "prometheus/alert_rules.yml",
        "alertmanager/alertmanager.yml",
        "grafana/provisioning/datasources/datasource.yml",
        "grafana/provisioning/dashboards/dashboards.yml",
    ]:
        with open(os.path.join(OBS, rel), encoding="utf-8") as f:
            assert yaml.safe_load(f) is not None, rel


def test_compose_services_follow_our_container_policy():
    """compose.yaml must pass the rules our policies enforce on generated workloads."""
    with open(os.path.join(ROOT, "compose.yaml"), encoding="utf-8") as f:
        compose = yaml.safe_load(f)
    services = compose["services"]
    assert {"app", "prometheus", "alertmanager", "grafana"} <= set(services)
    for name, svc in services.items():
        image = svc["image"]
        assert ":latest" not in image and not image.endswith(":latest"), name
        if name != "app":  # the app is the local `make image` build
            assert re.search(r":[\w.\-]+@sha256:[0-9a-f]{64}$", image), f"{name}: pin by tag and digest"
        uid = int(str(svc["user"]).split(":")[0])
        assert uid >= 10000 or uid == 65534, f"{name}: user {uid}"
        assert svc.get("mem_limit") and svc.get("cpus"), f"{name}: memory and CPU limits"
        assert svc.get("cap_drop") == ["ALL"], name
        assert "no-new-privileges:true" in svc.get("security_opt", []), name
        assert svc.get("read_only") is True, name
        assert not svc.get("privileged") and svc.get("network_mode") != "host", name
        assert "healthcheck" in svc, name
        for port in svc.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name}: {port}"
    # Inside the compose network, services reach each other by name, never via the host.
    for rel in ["prometheus/prometheus.yml", "alertmanager/alertmanager.yml",
                "grafana/provisioning/datasources/datasource.yml"]:
        with open(os.path.join(OBS, rel), encoding="utf-8") as f:
            assert "host.docker.internal" not in f.read(), rel
    assert services["grafana"]["environment"]["GF_AUTH_ANONYMOUS_ORG_ROLE"] == "Viewer"


def test_alert_rules_cover_the_three_required_alerts():
    with open(os.path.join(OBS, "prometheus/alert_rules.yml"), encoding="utf-8") as f:
        rules = yaml.safe_load(f)
    names = {r["alert"] for group in rules["groups"] for r in group["rules"]}
    assert {"LowSuccessRate", "FixLoopExhaustedSpike", "PendingApprovalsStale"} <= names


def test_dashboard_is_valid_json_and_queries_known_metrics():
    with open(os.path.join(OBS, "grafana/dashboards/nl2infra_overview.json"), encoding="utf-8") as f:
        dash = json.load(f)
    assert dash["title"] == "NL2Infra Overview"
    panels = dash["panels"]
    assert len(panels) >= 8
    exprs = [t["expr"] for p in panels for t in p.get("targets", [])]
    assert exprs, "dashboard has no queries"
    referenced = set(re.findall(r"nl2infra_[a-z_]+", " ".join(exprs)))
    # Strip Prometheus suffixes before comparing to the known base names.
    for name in referenced:
        base = re.sub(r"_(bucket|sum|count)$", "", name)
        assert base in KNOWN_METRICS, f"dashboard references unknown metric: {name}"
