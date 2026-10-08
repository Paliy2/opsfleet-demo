{{- define "opsfleet.base" -}}
{{- required "baseDomain is required" .Values.baseDomain -}}
{{- end -}}

{{- define "opsfleet.host" -}}
{{- printf "%s.%s" .prefix (include "opsfleet.base" .root) -}}
{{- end -}}

{{- define "opsfleet.labels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/part-of: opsfleet
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
{{- end -}}

{{- define "opsfleet.selector" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
{{- end -}}

{{- define "opsfleet.image" -}}
{{- printf "%s:%s" .Values.image.repository (required "image.tag is required" .Values.image.tag) -}}
{{- end -}}

{{- define "opsfleet.appEnv" -}}
- name: OPSFLEET_ENV
  value: production
- name: OPSFLEET_GCP_PROJECT
  value: {{ required "gcpProject is required" .Values.gcpProject | quote }}
- name: OPSFLEET_MODEL_PROFILE
  value: {{ .Values.app.modelProfile | quote }}
- name: OPSFLEET_PROMPT_LABEL
  value: {{ .Values.app.promptLabel | quote }}
- name: OPSFLEET_LLM_BASE_URL
  value: http://bifrost:8080/v1
- name: OPSFLEET_LANGFUSE_HOST
  value: http://langfuse-web:3000
- name: OPSFLEET_AUTH_ISSUER
  value: {{ printf "https://%s/realms/opsfleet" (include "opsfleet.host" (dict "prefix" "auth" "root" .)) | quote }}
- name: OPSFLEET_AUTH_JWKS_URL
  value: http://keycloak:8080/realms/opsfleet/protocol/openid-connect/certs
- name: OPSFLEET_REDIS_URL
  value: redis://redis-app:6379/0
- name: OPSFLEET_RATE_LIMIT_PER_MINUTE
  value: {{ .Values.app.rateLimitPerMinute | quote }}
- name: OPSFLEET_RATE_LIMIT_PER_DAY
  value: {{ .Values.app.rateLimitPerDay | quote }}
- name: OPSFLEET_TEMPORAL_ADDRESS
  value: {{ .Values.app.temporalAddress | quote }}
- name: OPSFLEET_TEMPORAL_NAMESPACE
  value: {{ .Values.app.temporalNamespace | quote }}
- name: OPSFLEET_CHART_BUCKET
  value: {{ required "artifactsBucket is required" .Values.artifactsBucket | quote }}
- name: OPSFLEET_METRICS_PORT
  value: "9464"
{{- end -}}

{{- define "opsfleet.appEnvFrom" -}}
- secretRef:
    name: opsfleet-app
{{- end -}}

{{- define "opsfleet.remoteRef" -}}
- secretKey: {{ .key }}
  remoteRef:
    key: {{ .secret }}
{{- end -}}

{{- define "opsfleet.zeroDowntime" -}}
minReadySeconds: {{ .Values.rollout.minReadySeconds }}
strategy:
  type: RollingUpdate
  rollingUpdate:
    maxSurge: 1
    maxUnavailable: 0
{{- end }}
