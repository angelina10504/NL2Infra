package main

import rego.v1

# Base pack: applied to every role.
# Shared helpers live here; the staging and production packs are always loaded with it.

workload_kinds := {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob", "Pod"}

cluster_scoped_kinds := {
	"Namespace", "ClusterRole", "ClusterRoleBinding", "PersistentVolume", "StorageClass",
	"CustomResourceDefinition", "PriorityClass", "IngressClass",
}

pod_spec := input.spec if {
	input.kind == "Pod"
} else := input.spec.jobTemplate.spec.template.spec if {
	input.kind == "CronJob"
} else := input.spec.template.spec if {
	input.kind in workload_kinds
}

app_containers contains c if some c in pod_spec.containers

containers contains c if some c in app_containers

containers contains c if some c in pod_spec.initContainers

default resource_name := "<unnamed>"

resource_name := input.metadata.name

resource_id := sprintf("%s/%s", [input.kind, resource_name])

result(rule_id, msg) := {
	"msg": sprintf("[%s] %s", [rule_id, msg]),
	"rule_id": rule_id,
	"resource": resource_id,
}

# A container is non-root if it says so itself, or inherits it from the pod.
runs_as_non_root(c) if c.securityContext.runAsNonRoot == true

runs_as_non_root(c) if {
	pod_spec.securityContext.runAsNonRoot == true
	not c.securityContext.runAsNonRoot == false
}

secret_patterns := ["PASSWORD", "PASSWD", "SECRET", "TOKEN", "API_KEY", "APIKEY", "PRIVATE_KEY"]

# 1. Containers must not run as root
deny contains result("OPA_NO_ROOT", msg) if {
	some c in containers
	not runs_as_non_root(c)
	msg := sprintf("Container '%s' in %s must set securityContext.runAsNonRoot to true", [c.name, resource_id])
}

# 2. No privileged containers
deny contains result("OPA_NO_PRIVILEGED", msg) if {
	some c in containers
	c.securityContext.privileged == true
	msg := sprintf("Container '%s' in %s must not run in privileged mode", [c.name, resource_id])
}

# 3. No literal value in a password-like environment variable
deny contains result("OPA_NO_PLAINTEXT_SECRET", msg) if {
	some c in containers
	some env_var in c.env
	some pattern in secret_patterns
	contains(upper(env_var.name), pattern)
	env_var.value != ""
	msg := sprintf(
		"Environment variable '%s' in container '%s' of %s has a literal value. Keep the value in a Secret and mount it as a file",
		[env_var.name, c.name, resource_id],
	)
}

# 4. CPU limit required
deny contains result("OPA_REQUIRE_CPU_LIMIT", msg) if {
	some c in containers
	not c.resources.limits.cpu
	msg := sprintf("Container '%s' in %s must define resources.limits.cpu", [c.name, resource_id])
}

# 5. Memory limit required
deny contains result("OPA_REQUIRE_MEM_LIMIT", msg) if {
	some c in containers
	not c.resources.limits.memory
	msg := sprintf("Container '%s' in %s must define resources.limits.memory", [c.name, resource_id])
}
