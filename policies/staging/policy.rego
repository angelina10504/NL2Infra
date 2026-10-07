package main

import rego.v1

# Staging pack: added for senior_dev and platform_admin. Uses the helpers in base/.

long_running_kinds := {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"}

# An image with no tag resolves to :latest.
uses_latest(image) if endswith(image, ":latest")

uses_latest(image) if {
	not contains(image, "@")
	parts := split(image, "/")
	not contains(parts[count(parts) - 1], ":")
}

# 1. Long-running containers need a readiness probe
deny contains result("OPA_REQUIRE_READINESS_PROBE", msg) if {
	input.kind in long_running_kinds
	some c in app_containers
	not c.readinessProbe
	msg := sprintf("Container '%s' in %s must define a readinessProbe", [c.name, resource_id])
}

# 2. No :latest (or untagged) images
deny contains result("OPA_DISALLOW_LATEST_TAG", msg) if {
	some c in containers
	uses_latest(c.image)
	msg := sprintf("Container '%s' in %s uses image '%s'; a fixed tag is required", [c.name, resource_id, c.image])
}
