package main

import rego.v1

# OPA_PRODUCTION_REPLICAS
test_production_replicas_pass if {
	not "OPA_PRODUCTION_REPLICAS" in rule_ids(deployment_with(good_container, "production", 2))
}

test_production_replicas_pass_outside_production if {
	not "OPA_PRODUCTION_REPLICAS" in rule_ids(deployment_with(good_container, "dev", 1))
}

test_production_replicas_fail if {
	"OPA_PRODUCTION_REPLICAS" in rule_ids(deployment_with(good_container, "production", 1))
}

test_production_replicas_fail_unset if {
	manifest := object.remove(deployment_with(good_container, "production", 1), ["spec"])
	"OPA_PRODUCTION_REPLICAS" in rule_ids(object.union(manifest, {"spec": {"template": {"spec": {"containers": [good_container]}}}}))
}

# OPA_NO_DEFAULT_NAMESPACE
test_no_default_namespace_pass if {
	not "OPA_NO_DEFAULT_NAMESPACE" in rule_ids(good_deployment)
}

test_no_default_namespace_pass_cluster_scoped if {
	not "OPA_NO_DEFAULT_NAMESPACE" in rule_ids({"kind": "Namespace", "metadata": {"name": "dev"}})
}

test_no_default_namespace_fail if {
	"OPA_NO_DEFAULT_NAMESPACE" in rule_ids(deployment_with(good_container, "default", 2))
}

test_no_default_namespace_fail_unset if {
	"OPA_NO_DEFAULT_NAMESPACE" in rule_ids({"kind": "Service", "metadata": {"name": "web"}})
}

# OPA_READONLY_ROOTFS
test_readonly_rootfs_pass if {
	not "OPA_READONLY_ROOTFS" in rule_ids(good_deployment)
}

test_readonly_rootfs_fail if {
	c := object.union(without(good_container, "securityContext"), {"securityContext": {"runAsNonRoot": true}})
	"OPA_READONLY_ROOTFS" in rule_ids(deployment_with(c, "dev", 2))
}
