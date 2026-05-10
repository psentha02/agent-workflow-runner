{{/*
  charts/agent-runner/templates/_helpers.tpl
  Named templates shared across all chart resources.
*/}}

{{/*
  Expand the chart name.
*/}}
{{- define "agent-runner.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
  Full release name — used as a prefix on all resource names.
  Truncated to 63 chars (K8s limit).
*/}}
{{- define "agent-runner.fullname" -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
  Common labels applied to every resource.
  These are what kubectl uses to group resources by release.
*/}}
{{- define "agent-runner.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "agent-runner.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
  Selector labels — used by Deployments and Services to find Pods.
  Deliberately minimal — only name and instance so selectors stay stable
  across chart upgrades even if other labels change.
*/}}
{{- define "agent-runner.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agent-runner.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
  Component-specific selector labels.
  Pass a dict with "component" key: {{ include "agent-runner.componentLabels" (dict "component" "fastapi" "context" .) }}
*/}}
{{- define "agent-runner.componentLabels" -}}
{{ include "agent-runner.selectorLabels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}