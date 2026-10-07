variable "grafana_url" {
  description = "Grafana Cloud stack"
  type        = string
  default     = "https://leandro.grafana.net"
}

variable "grafana_token" {
  description = "Service account token (Editor) of the stack. GitHub secret GRAFANA_SA_TOKEN"
  type        = string
  sensitive   = true
}

variable "state_passphrase" {
  description = "Encrypts grafana.tfstate (at least 16 characters). GitHub secret TOFU_STATE_PASSPHRASE"
  type        = string
  sensitive   = true
}

variable "prometheus_data_source" {
  description = "Name of the stack's Prometheus data source (Connections → Data sources)"
  type        = string
  default     = "grafanacloud-leandro-prom"
}

variable "loki_data_source" {
  description = "Name of the stack's Loki data source"
  type        = string
  default     = "grafanacloud-leandro-logs"
}
