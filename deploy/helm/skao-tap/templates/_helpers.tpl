{{- define "skao-tap.fullname" -}}
{{- printf "%s" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "skao-tap.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "skao-tap.imageTag" -}}
{{- .Values.image.tag | default .Chart.AppVersion -}}
{{- end -}}

{{- define "skao-tap.databaseUrl" -}}
{{- if .Values.postgresql.enabled -}}
{{- printf "postgresql://%s:%s@%s-postgres:5432/%s" .Values.postgresql.username .Values.postgresql.password (include "skao-tap.fullname" .) .Values.postgresql.database -}}
{{- else -}}
{{- required "externalDatabase.url is required when postgresql.enabled=false" .Values.externalDatabase.url -}}
{{- end -}}
{{- end -}}

{{- define "skao-tap.baseUrl" -}}
{{- if .Values.tapApi.baseUrl -}}
{{- .Values.tapApi.baseUrl -}}
{{- else if .Values.ingress.enabled -}}
{{- printf "http://%s/tap" .Values.ingress.host -}}
{{- else -}}
{{- printf "http://%s-tap-api:%d/tap" (include "skao-tap.fullname" .) (int .Values.tapApi.port) -}}
{{- end -}}
{{- end -}}

{{- /*
Scheduling constraints spreading a component's replicas across nodes and
zones (roadmap package 5). Preferred/ScheduleAnyway semantics, so single-node
clusters (kind, minikube) schedule exactly as before. Explicit per-component
affinity/topologySpreadConstraints values override the defaults wholesale.
Usage: include "skao-tap.scheduling" (dict "ctx" $ "component" "tap-api" "values" .Values.tapApi)
*/}}
{{- define "skao-tap.scheduling" -}}
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
