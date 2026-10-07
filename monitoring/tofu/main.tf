provider "grafana" {
  url  = var.grafana_url
  auth = var.grafana_token
}

data "grafana_data_source" "prometheus" {
  name = var.prometheus_data_source
}

data "grafana_data_source" "loki" {
  name = var.loki_data_source
}

resource "grafana_folder" "extractos" {
  title = "Extractos"
  uid   = "extractos-folder" # dashboards and folders share uids: the dashboard is "extractos"
}

locals {
  # ../extractos-dashboard.json is made by ../make_dashboard.py for manual import: it asks for
  # the data sources (${DS_PROMETHEUS}, ${DS_LOKI}). Here they're filled in, and the
  # import-only keys removed.
  dashboard_template = file("${path.module}/../extractos-dashboard.json")
  dashboard_json = replace(replace(local.dashboard_template,
    "$${DS_PROMETHEUS}", data.grafana_data_source.prometheus.uid),
  "$${DS_LOKI}", data.grafana_data_source.loki.uid)
  dashboard = { for k, v in jsondecode(local.dashboard_json) : k => v if !contains(["__inputs", "__requires"], k) }
}

resource "grafana_dashboard" "extractos" {
  folder      = grafana_folder.extractos.uid
  config_json = jsonencode(local.dashboard)
  # Replaces a dashboard with the same uid (e.g. one imported by hand before)
  overwrite = true
  message   = "OpenTofu (redo repo, monitoring/tofu)"
}

output "dashboard_url" {
  value = grafana_dashboard.extractos.url
}
