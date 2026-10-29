package fit

import rego.v1

# Project-Aware Generation: the 'fit' policy. The declared project type arrives as data
# (conftest --data: data.workload_type); each rule checks the Kubernetes object fits that type.
# Evaluated in its own namespace ("fit"), so it never mixes with the base/staging/production
# packs. Messages are tagged [RULE_ID] so the Gauntlet can record a stable id.

workload_type := data.workload_type

workload_kinds := {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob", "Pod"}

pod_containers contains c if {
	input.kind in {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"}
	some c in input.spec.template.spec.containers
}

needs_probes := {"web_app", "api", "ml_service"}

# A database must be a StatefulSet, never a plain Deployment.
deny contains msg if {
	workload_type == "database"
	input.kind == "Deployment"
	msg := sprintf("[FIT_DATABASE_STATEFULSET] database must be a StatefulSet, not Deployment/%s", [input.metadata.name])
}

# A database StatefulSet must declare volumeClaimTemplates so data persists.
deny contains msg if {
	workload_type == "database"
	input.kind == "StatefulSet"
	not input.spec.volumeClaimTemplates
	msg := sprintf("[FIT_DATABASE_VOLUME] database StatefulSet %s must declare spec.volumeClaimTemplates", [input.metadata.name])
}

# A cron_job must be a CronJob.
deny contains msg if {
	workload_type == "cron_job"
	input.kind in (workload_kinds - {"CronJob"})
	msg := sprintf("[FIT_CRONJOB_KIND] cron_job must be a CronJob, not %s/%s", [input.kind, input.metadata.name])
}

# A cron_job CronJob must set a schedule.
deny contains msg if {
	workload_type == "cron_job"
	input.kind == "CronJob"
	not input.spec.schedule
	msg := sprintf("[FIT_CRONJOB_SCHEDULE] cron_job CronJob %s must set spec.schedule", [input.metadata.name])
}

# A one_time_job must be a Job (not a long-running or scheduled workload).
deny contains msg if {
	workload_type == "one_time_job"
	input.kind in {"Deployment", "StatefulSet", "CronJob", "DaemonSet"}
	msg := sprintf("[FIT_ONESHOT_KIND] one_time_job must be a Job, not %s/%s", [input.kind, input.metadata.name])
}

# A queue_worker has no inbound traffic: it should be a Deployment, not a CronJob or StatefulSet.
deny contains msg if {
	workload_type == "queue_worker"
	input.kind in {"CronJob", "StatefulSet"}
	msg := sprintf("[FIT_WORKER_KIND] queue_worker should be a Deployment, not %s/%s", [input.kind, input.metadata.name])
}

# web_app / api / ml_service must have a readinessProbe on every container.
deny contains msg if {
	workload_type in needs_probes
	some c in pod_containers
	not c.readinessProbe
	msg := sprintf("[FIT_READINESS_PROBE] %s container '%s' must define a readinessProbe", [workload_type, c.name])
}

# web_app / api / ml_service must have a livenessProbe on every container.
deny contains msg if {
	workload_type in needs_probes
	some c in pod_containers
	not c.livenessProbe
	msg := sprintf("[FIT_LIVENESS_PROBE] %s container '%s' must define a livenessProbe", [workload_type, c.name])
}
