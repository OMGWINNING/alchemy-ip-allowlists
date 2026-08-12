# Alchemy Public IPs

This folder contains IP allowlists grouped by provider/region under a shared
`alchemyIPs` map. These files are intended to be passed as Helm values so that
charts can build allowlists (for example, Istio AuthorizationPolicies or
VirtualServices).

## How it is used (ArgoCD)

In ArgoCD, include the desired files from this folder as additional Helm values
for the target chart. Example (conceptual):

- Chart: `istio-gateway`
- Values: `istio-gateway/values.yaml`, `istio-gateway/values-observability-aws-ops-use1-0.yaml`
- Extra values: all files in `alchemy-public-ips/`

The `istio-gateway` chart consumes these via the allowlist template:

- `istio-gateway/templates/authorization-policy-allowlist.yaml`

## Local development (helm template)

Use this command to render locally (example):

```
helm template istio-gateway ./istio-gateway \
  -f istio-gateway/values.yaml \
  -f istio-gateway/values-observability-aws-ops-use1-0.yaml \
  -f alchemy-public-ips/alchemy-rollups-prod-eu-central-2.yaml \
  -f alchemy-public-ips/alchemy-rollups-prod-us-east-1.yaml \
  -f alchemy-public-ips/alchemy-rollups-prod-us-west-2.yaml \
  -f alchemy-public-ips/ap-southeast-sgp.yaml \
  -f alchemy-public-ips/eu-west-lim.yaml \
  -f alchemy-public-ips/route-53-ips.yaml \
  -f alchemy-public-ips/us-east-vin.yaml \
  -f alchemy-public-ips/us-west-hil.yaml
```

## VirtualService example

These IPs can also be used for `VirtualService` source IP matching. Example
template snippet:

```
{{- if .Values.alchemyIPs }}
{{- $allowed := list -}}
{{- $seen := dict -}}
{{- range $groupKey, $groupVal := .Values.alchemyIPs }}
  {{- if kindIs "map" $groupVal }}
    {{- range $subKey, $subVal := $groupVal }}
      {{- if kindIs "slice" $subVal }}
        {{- range $ip := $subVal }}
          {{- if and $ip (not (hasKey $seen $ip)) }}
            {{- $_ := set $seen $ip true }}
            {{- $allowed = append $allowed $ip }}
          {{- end }}
        {{- end }}
      {{- end }}
    {{- end }}
  {{- else if kindIs "slice" $groupVal }}
    {{- range $ip := $groupVal }}
      {{- if and $ip (not (hasKey $seen $ip)) }}
        {{- $_ := set $seen $ip true }}
        {{- $allowed = append $allowed $ip }}
      {{- end }}
    {{- end }}
  {{- end }}
{{- end }}

apiVersion: networking.istio.io/v1
kind: VirtualService
metadata:
  name: example-allowlist
  namespace: istio-system
spec:
  hosts:
    - example.internal
  gateways:
    - some-gateway
  http:
    - match:
        - sourceIp:
{{- range $allowed }}
            - {{ . | quote }}
{{- end }}
      route:
        - destination:
            host: example.internal
{{- end }}
```
