{{- define "quay.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "quay.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "quay.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "quay.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
{{ include "quay.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "quay.selectorLabels" -}}
app.kubernetes.io/name: {{ include "quay.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "quay.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag -}}
{{- end -}}
{{- end }}
