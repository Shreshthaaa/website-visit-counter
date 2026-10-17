{{- define "visit-counter.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "visit-counter.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "visit-counter.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "visit-counter.selectorLabels" -}}
app.kubernetes.io/name: {{ include "visit-counter.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "visit-counter.labels" -}}
helm.sh/chart: {{ include "visit-counter.chart" . }}
{{ include "visit-counter.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
The Redis pods share the release's selector labels, so anything that should
only match the app pods has to select on the component as well.
*/}}
{{- define "visit-counter.apiSelectorLabels" -}}
{{ include "visit-counter.selectorLabels" . }}
app.kubernetes.io/component: api
{{- end }}

{{- define "visit-counter.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "visit-counter.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Shared by a shard's StatefulSet and headless Service, so its pod resolves
as <name>-0.<name>.
*/}}
{{- define "visit-counter.shardName" -}}
{{- printf "%s-redis-%s" (include "visit-counter.fullname" .root) .shard.name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
REDIS_NODES for the app: one pod address per shard, in values order.
*/}}
{{- define "visit-counter.redisNodes" -}}
{{- $root := . -}}
{{- $nodes := list -}}
{{- range .Values.redis.shards -}}
{{- $svc := include "visit-counter.shardName" (dict "root" $root "shard" .) -}}
{{- $nodes = append $nodes (printf "redis://%s-0.%s:%v" $svc $svc .port) -}}
{{- end -}}
{{- join "," $nodes -}}
{{- end }}

{{- define "visit-counter.redisSecretName" -}}
{{- if .Values.redis.auth.existingSecret }}
{{- .Values.redis.auth.existingSecret }}
{{- else }}
{{- printf "%s-redis" (include "visit-counter.fullname" .) }}
{{- end }}
{{- end }}
