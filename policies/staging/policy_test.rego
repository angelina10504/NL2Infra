package main

import rego.v1

# OPA_REQUIRE_READINESS_PROBE
test_readiness_probe_pass if {
	not "OPA_REQUIRE_READINESS_PROBE" in rule_ids(good_deployment)
}

test_readiness_probe_pass_job_exempt if {
	manifest := {
		"kind": "Job",
		"metadata": {"name": "migrate", "namespace": "dev"},
		"spec": {"template": {"spec": {"containers": [without(good_container, "readinessProbe")]}}},
	}
	not "OPA_REQUIRE_READINESS_PROBE" in rule_ids(manifest)
}

test_readiness_probe_fail if {
	"OPA_REQUIRE_READINESS_PROBE" in rule_ids(deployment_with(without(good_container, "readinessProbe"), "dev", 2))
}

# OPA_DISALLOW_LATEST_TAG
test_latest_tag_pass if {
	not "OPA_DISALLOW_LATEST_TAG" in rule_ids(good_deployment)
}

test_latest_tag_pass_registry_port_and_digest if {
	tagged := object.union(good_container, {"image": "registry.local:5000/team/app:1.2.3"})
	not "OPA_DISALLOW_LATEST_TAG" in rule_ids(deployment_with(tagged, "dev", 2))
	digest := object.union(good_container, {"image": "redis@sha256:abc"})
	not "OPA_DISALLOW_LATEST_TAG" in rule_ids(deployment_with(digest, "dev", 2))
}

test_latest_tag_fail if {
	c := object.union(good_container, {"image": "nginx:latest"})
	"OPA_DISALLOW_LATEST_TAG" in rule_ids(deployment_with(c, "dev", 2))
}

test_latest_tag_fail_untagged if {
	c := object.union(good_container, {"image": "registry.local:5000/nginx"})
	"OPA_DISALLOW_LATEST_TAG" in rule_ids(deployment_with(c, "dev", 2))
}
