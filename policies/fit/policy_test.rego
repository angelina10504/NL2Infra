package fit

import rego.v1

# Tests for the 'fit' policy. Run with: conftest verify --policy policies
# Each rule has a passing and a failing case. The declared project type is injected with
# `with data.workload_type as "..."`, exactly as conftest --data does at runtime.

probe := {"httpGet": {"path": "/", "port": 8080}}

web_container(extra) := object.union({"name": "web", "image": "nginx:1.27-alpine"}, extra)

bare_container := web_container({})

ready_live_container := web_container({"readinessProbe": probe, "livenessProbe": probe})

ready_only_container := web_container({"readinessProbe": probe})

deployment(container) := {
	"kind": "Deployment",
	"metadata": {"name": "web", "namespace": "dev"},
	"spec": {"template": {"spec": {"containers": [container]}}},
}

job := {
	"kind": "Job",
	"metadata": {"name": "migrate", "namespace": "dev"},
	"spec": {"template": {"spec": {"containers": [bare_container]}}},
}

statefulset_with_volume := {
	"kind": "StatefulSet",
	"metadata": {"name": "db", "namespace": "dev"},
	"spec": {
		"volumeClaimTemplates": [{"metadata": {"name": "data"}}],
		"template": {"spec": {"containers": [bare_container]}},
	},
}

statefulset_no_volume := {
	"kind": "StatefulSet",
	"metadata": {"name": "db", "namespace": "dev"},
	"spec": {"template": {"spec": {"containers": [bare_container]}}},
}

cron_template := {"spec": {"template": {"spec": {"containers": [bare_container]}}}}

cronjob_with_schedule := {
	"kind": "CronJob",
	"metadata": {"name": "nightly", "namespace": "dev"},
	"spec": {"schedule": "0 2 * * *", "jobTemplate": cron_template},
}

cronjob_no_schedule := {
	"kind": "CronJob",
	"metadata": {"name": "nightly", "namespace": "dev"},
	"spec": {"jobTemplate": cron_template},
}

denies(manifest, wt) := {msg | some msg in deny with input as manifest with data.workload_type as wt}

# --- database: must be a StatefulSet, with a volume
test_database_deployment_is_denied if {
	some m in denies(deployment(bare_container), "database")
	contains(m, "[FIT_DATABASE_STATEFULSET]")
}

test_database_statefulset_with_volume_passes if {
	count(denies(statefulset_with_volume, "database")) == 0
}

test_database_statefulset_without_volume_is_denied if {
	some m in denies(statefulset_no_volume, "database")
	contains(m, "[FIT_DATABASE_VOLUME]")
}

# --- cron_job: must be a CronJob with a schedule
test_cron_job_as_deployment_is_denied if {
	some m in denies(deployment(bare_container), "cron_job")
	contains(m, "[FIT_CRONJOB_KIND]")
}

test_cron_job_with_schedule_passes if {
	count(denies(cronjob_with_schedule, "cron_job")) == 0
}

test_cron_job_without_schedule_is_denied if {
	some m in denies(cronjob_no_schedule, "cron_job")
	contains(m, "[FIT_CRONJOB_SCHEDULE]")
}

# --- one_time_job: must be a Job
test_one_time_job_as_deployment_is_denied if {
	some m in denies(deployment(bare_container), "one_time_job")
	contains(m, "[FIT_ONESHOT_KIND]")
}

test_one_time_job_as_job_passes if {
	count(denies(job, "one_time_job")) == 0
}

# --- queue_worker: not a CronJob/StatefulSet
test_queue_worker_as_cronjob_is_denied if {
	some m in denies(cronjob_no_schedule, "queue_worker")
	contains(m, "[FIT_WORKER_KIND]")
}

test_queue_worker_as_deployment_passes if {
	count(denies(deployment(bare_container), "queue_worker")) == 0
}

# --- web_app/api/ml_service: readiness + liveness probes
test_web_app_without_probes_is_denied if {
	some m in denies(deployment(bare_container), "web_app")
	contains(m, "[FIT_READINESS_PROBE]")
}

test_web_app_with_both_probes_passes if {
	count(denies(deployment(ready_live_container), "web_app")) == 0
}

test_api_missing_liveness_is_denied if {
	some m in denies(deployment(ready_only_container), "api")
	contains(m, "[FIT_LIVENESS_PROBE]")
}
