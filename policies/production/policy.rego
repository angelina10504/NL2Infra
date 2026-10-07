package main

import rego.v1

# Production pack: added for platform_admin. Uses the helpers in base/.

replicated_kinds := {"Deployment", "StatefulSet"}

# 1. Workloads in the production namespace need at least 2 replicas.
# Scoped to the namespace: the replica count is fixed by the approved plan, so
# applying this to an admin's dev request could never be fixed.
deny contains result("OPA_PRODUCTION_REPLICAS", msg) if {
	input.kind in replicated_kinds
	input.metadata.namespace == "production"
	replicas := object.get(input.spec, "replicas", 1)
	replicas < 2
	msg := sprintf("%s in production must have at least 2 replicas (found %d)", [resource_id, replicas])
}

# 2. Nothing goes into the default namespace
deny contains result("OPA_NO_DEFAULT_NAMESPACE", msg) if {
	not input.kind in cluster_scoped_kinds
	object.get(input.metadata, "namespace", "default") == "default"
	msg := sprintf("%s must not be placed in the 'default' namespace", [resource_id])
}

# 3. Read-only root filesystem
deny contains result("OPA_READONLY_ROOTFS", msg) if {
	some c in containers
	not c.securityContext.readOnlyRootFilesystem == true
	msg := sprintf("Container '%s' in %s must set securityContext.readOnlyRootFilesystem to true", [c.name, resource_id])
}
