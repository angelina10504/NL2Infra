package main

import rego.v1

# Shared test fixtures. Run with: conftest verify --policy policies

good_container := {
	"name": "web",
	"image": "nginxinc/nginx-unprivileged:1.27-alpine",
	"securityContext": {"runAsNonRoot": true, "privileged": false, "readOnlyRootFilesystem": true},
	"resources": {"limits": {"cpu": "200m", "memory": "128Mi"}},
	"readinessProbe": {"httpGet": {"path": "/", "port": 8080}},
	"env": [
		{"name": "LOG_LEVEL", "value": "info"},
		{"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "db", "key": "password"}}},
	],
}

deployment_with(container, namespace, replicas) := {
	"apiVersion": "apps/v1",
	"kind": "Deployment",
	"metadata": {"name": "web", "namespace": namespace},
	"spec": {
		"replicas": replicas,
		"template": {"spec": {"containers": [container]}},
	},
}

good_deployment := deployment_with(good_container, "dev", 2)

rule_ids(manifest) := {r.rule_id | some r in deny with input as manifest}

without(container, key) := object.remove(container, [key])

test_good_deployment_has_no_violations if {
	count(deny) == 0 with input as good_deployment
}

test_result_shape if {
	some r in deny with input as deployment_with(without(good_container, "resources"), "dev", 2)
	r.rule_id == "OPA_REQUIRE_CPU_LIMIT"
	r.resource == "Deployment/web"
	startswith(r.msg, "[OPA_REQUIRE_CPU_LIMIT] ")
}

# OPA_NO_ROOT
test_no_root_pass if {
	not "OPA_NO_ROOT" in rule_ids(good_deployment)
}

test_no_root_pass_inherited_from_pod if {
	manifest := {
		"kind": "Pod",
		"metadata": {"name": "p", "namespace": "dev"},
		"spec": {
			"securityContext": {"runAsNonRoot": true},
			"containers": [without(good_container, "securityContext")],
		},
	}
	not "OPA_NO_ROOT" in rule_ids(manifest)
}

test_no_root_fail if {
	c := object.union(good_container, {"securityContext": {"runAsNonRoot": false}})
	"OPA_NO_ROOT" in rule_ids(deployment_with(c, "dev", 2))
}

test_no_root_fail_missing if {
	"OPA_NO_ROOT" in rule_ids(deployment_with(without(good_container, "securityContext"), "dev", 2))
}

test_no_root_fail_statefulset_init_container if {
	manifest := {
		"kind": "StatefulSet",
		"metadata": {"name": "db", "namespace": "dev"},
		"spec": {"template": {"spec": {
			"containers": [good_container],
			"initContainers": [without(good_container, "securityContext")],
		}}},
	}
	"OPA_NO_ROOT" in rule_ids(manifest)
}

test_no_root_fail_cronjob if {
	manifest := {
		"kind": "CronJob",
		"metadata": {"name": "nightly", "namespace": "dev"},
		"spec": {"jobTemplate": {"spec": {"template": {"spec": {"containers": [without(good_container, "securityContext")]}}}}},
	}
	"OPA_NO_ROOT" in rule_ids(manifest)
}

# OPA_NO_PRIVILEGED
test_no_privileged_pass if {
	not "OPA_NO_PRIVILEGED" in rule_ids(good_deployment)
}

test_no_privileged_fail if {
	c := object.union(good_container, {"securityContext": {"runAsNonRoot": true, "privileged": true}})
	"OPA_NO_PRIVILEGED" in rule_ids(deployment_with(c, "dev", 2))
}

# OPA_NO_PLAINTEXT_SECRET
test_no_plaintext_secret_pass if {
	not "OPA_NO_PLAINTEXT_SECRET" in rule_ids(good_deployment)
}

test_no_plaintext_secret_fail if {
	c := object.union(good_container, {"env": [{"name": "DB_PASSWORD", "value": "hunter2"}]})
	"OPA_NO_PLAINTEXT_SECRET" in rule_ids(deployment_with(c, "dev", 2))
}

# OPA_REQUIRE_CPU_LIMIT
test_cpu_limit_pass if {
	not "OPA_REQUIRE_CPU_LIMIT" in rule_ids(good_deployment)
}

test_cpu_limit_fail if {
	c := object.union(without(good_container, "resources"), {"resources": {"limits": {"memory": "128Mi"}}})
	ids := rule_ids(deployment_with(c, "dev", 2))
	"OPA_REQUIRE_CPU_LIMIT" in ids
	not "OPA_REQUIRE_MEM_LIMIT" in ids
}

# OPA_REQUIRE_MEM_LIMIT
test_mem_limit_pass if {
	not "OPA_REQUIRE_MEM_LIMIT" in rule_ids(good_deployment)
}

test_mem_limit_fail if {
	"OPA_REQUIRE_MEM_LIMIT" in rule_ids(deployment_with(without(good_container, "resources"), "dev", 2))
}
