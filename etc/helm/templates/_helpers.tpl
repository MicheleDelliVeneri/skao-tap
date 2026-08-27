{{- define "egernia.fullname" -}}
{{- printf "%s" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "egernia.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "egernia.imageTag" -}}
{{- .Values.image.tag | default .Chart.AppVersion -}}
{{- end -}}

{{- define "egernia.databaseUrl" -}}
{{- if .Values.postgresql.enabled -}}
{{- printf "postgresql://%s:%s@%s-postgres:5432/%s" .Values.postgresql.username .Values.postgresql.password (include "egernia.fullname" .) .Values.postgresql.database -}}
{{- else -}}
{{- required "externalDatabase.url is required when postgresql.enabled=false" .Values.externalDatabase.url -}}
{{- end -}}
{{- end -}}

{{- define "egernia.baseUrl" -}}
{{- if .Values.tapApi.baseUrl -}}
{{- .Values.tapApi.baseUrl -}}
{{- else if .Values.ingress.enabled -}}
{{- printf "http://%s/tap" .Values.ingress.host -}}
{{- else -}}
{{- printf "http://%s-tap-api:%d/tap" (include "egernia.fullname" .) (int .Values.tapApi.port) -}}
{{- end -}}
{{- end -}}

{{- /*
Hosts allowed to decide the URLs the service prints into its own job
documents: the ingress host, because that is the deployment's own name.
Anything else is answered with baseUrl, so an unvetted Host header cannot
choose the links this service hands out.
*/}}
{{- define "egernia.trustedHosts" -}}
{{- if .Values.tapApi.trustedHosts -}}
{{- .Values.tapApi.trustedHosts -}}
{{- else if and .Values.ingress.enabled .Values.ingress.host -}}
{{- .Values.ingress.host -}}
{{- end -}}
{{- end -}}

{{- /*
Scheduling constraints spreading a component's replicas across nodes and
zones (roadmap package 5). Preferred/ScheduleAnyway semantics, so single-node
clusters (kind, minikube) schedule exactly as before. Explicit per-component
affinity/topologySpreadConstraints values override the defaults wholesale.
Usage: include "egernia.scheduling" (dict "ctx" $ "component" "tap-api" "values" .Values.tapApi)
*/}}
{{- define "egernia.scheduling" -}}
{{- if .values.affinity }}
affinity:
  {{- toYaml .values.affinity | nindent 2 }}
{{- else if .ctx.Values.scheduling.spreadReplicas }}
affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          topologyKey: kubernetes.io/hostname
          labelSelector:
            matchLabels:
              app.kubernetes.io/instance: {{ .ctx.Release.Name }}
              app.kubernetes.io/component: {{ .component }}
{{- end }}
{{- if .values.topologySpreadConstraints }}
topologySpreadConstraints:
  {{- toYaml .values.topologySpreadConstraints | nindent 2 }}
{{- else if .ctx.Values.scheduling.spreadReplicas }}
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: ScheduleAnyway
    labelSelector:
      matchLabels:
        app.kubernetes.io/instance: {{ .ctx.Release.Name }}
        app.kubernetes.io/component: {{ .component }}
{{- end }}
{{- with .values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{- /*
Horizontal autoscaling (roadmap package 9).

The replica count a component actually starts at, and the one the
PodDisruptionBudget has to reason about: when an autoscaler owns the
Deployment, `replicas` is not what the cluster will run, `minReplicas` is the
floor. Usage: include "egernia.minReplicas" (dict "ctx" $ "component" "tap-api")
*/}}
{{- define "egernia.minReplicas" -}}
{{- $hpa := .ctx.Values.horizontalAutoscaling -}}
{{- if eq .component "tap-api" -}}
{{- if $hpa.tapApi.enabled -}}{{ int $hpa.tapApi.minReplicas }}{{- else -}}{{ int .ctx.Values.tapApi.replicas }}{{- end -}}
{{- else -}}
{{- if $hpa.tapExecutor.enabled -}}{{ int $hpa.tapExecutor.minReplicas }}{{- else -}}{{ int .ctx.Values.tapExecutor.replicas }}{{- end -}}
{{- end -}}
{{- end -}}

{{- /*
The Prometheus the executor's queue depth is read from. The chart deploys
none, so this is always the one the site already runs.
*/}}
{{- define "egernia.queueDepthPrometheus" -}}
{{- $spec := .Values.horizontalAutoscaling.tapExecutor -}}
{{- if $spec.prometheusAddress -}}
{{- $spec.prometheusAddress -}}
{{- else -}}
{{- fail "horizontalAutoscaling.tapExecutor.prometheusAddress is required: the queue depth is a Prometheus gauge, so an autoscaler needs to know which Prometheus has it. See docs/autoscaling.md." -}}
{{- end -}}
{{- end -}}

{{- /*
PromQL for the queue depth: the number of QUEUED jobs, not the oldest job's
age — depth grows with the work outstanding, where age saturates near one
job's service time as soon as the queue drains at all (measured: 1,713
queued, oldest 54 s). max() rather than sum(): every replica reports the
same figures for one shared queue, so sum() would scale on the replica
count and then feed on its own output. Namespace-scoped so a Prometheus
watching several namespaces does not autoscale this release on another one's
queue.
*/}}
{{- define "egernia.queueDepthQuery" -}}
{{- $spec := .Values.horizontalAutoscaling.tapExecutor -}}
{{- if $spec.query -}}
{{- $spec.query -}}
{{- else -}}
{{- printf "max(tap_jobs{phase=\"QUEUED\",namespace=\"%s\"})" .Release.Namespace -}}
{{- end -}}
{{- end -}}

{{- /* The routes, identical for every name this deployment answers to. */}}
{{- define "egernia.ingressPaths" -}}
- path: /tap
  pathType: Prefix
  backend:
    service:
      name: {{ include "egernia.fullname" . }}-tap-api
      port:
        name: http
# the JSON API: same service, own prefix — routed too, or a
# deployment behind an ingress serves VO clients and refuses every
# machine-to-machine caller
- path: /api/v1
  pathType: Prefix
  backend:
    service:
      name: {{ include "egernia.fullname" . }}-tap-api
      port:
        name: http
# the rest of the same service: OpenAPI, /docs, the health probes and
# the metrics exposition. Last, and a bare prefix, so the two above
# still win on longest-prefix match.
- path: /
  pathType: Prefix
  backend:
    service:
      name: {{ include "egernia.fullname" . }}-tap-api
      port:
        name: http
{{- end -}}
