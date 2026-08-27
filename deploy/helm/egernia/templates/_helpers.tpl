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
Every name this deployment is served under: the canonical ingress host, plus
any extras. One list, because the ingress rules and the trusted-host list are
two answers to the same question — a name the ingress routes but the app does
not trust gets a 200 carrying URLs pointing somewhere else, and a name the app
trusts but the ingress does not route never arrives at all.
*/}}
{{- define "egernia.serviceHosts" -}}
{{- $hosts := .Values.ingress.extraHosts | default list -}}
{{- if .Values.ingress.host -}}
{{- $hosts = prepend $hosts .Values.ingress.host -}}
{{- end -}}
{{- join "," (uniq $hosts) -}}
{{- end -}}

{{- /*
Hosts allowed to decide the URLs the service prints into its own job
documents. The names above because they are the deployment's own, and loopback
because a tunnel or a `kubectl port-forward` is how an operator and a demo
audience reach it — a client arriving by any of them must be handed URLs it
can resolve, not the canonical host it may have no DNS for.
*/}}
{{- define "egernia.trustedHosts" -}}
{{- if .Values.tapApi.trustedHosts -}}
{{- .Values.tapApi.trustedHosts -}}
{{- else -}}
{{- $hosts := list -}}
{{- if .Values.ingress.enabled -}}
{{- $hosts = include "egernia.serviceHosts" . | splitList "," -}}
{{- end -}}
{{- join "," (uniq (concat $hosts (list "localhost" "127.0.0.1"))) -}}
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
