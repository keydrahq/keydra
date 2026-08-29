#!/usr/bin/env python3
"""Writes the Grafana dashboards Keydra ships with.

    python3 scripts/keydra-dashboards.py        # from the repository root

A generator rather than a folder of hand-written JSON. Grafana's dashboard format is mostly
scaffolding — a panel is thirty lines of layout around one expression — and eleven dashboards
of it is tens of thousands of lines in which the queries, the only part anybody reviews, are
invisible. Here the queries are the bottom two thirds of one file and the layout is four
functions.

The output goes to deploy/observability/grafana/dashboards, which the Grafana in
deploy/keydra-observability.yaml provisions from. Editing the JSON directly works and a
reload picks it up; the next run of this script overwrites it, which is the usual trade of a
generated file.
"""

import json
import os

PROM = {"type": "prometheus", "uid": "PROM"}
LOKI = {"type": "loki", "uid": "LOKI"}

# A dashboard is 24 columns wide. Two panels to a row is the widest a time series can be and
# still be read at a glance; one to a row is for the ones with a legend worth reading.
FULL, HALF, THIRD, QUARTER = 24, 12, 8, 6

# Tall enough that a line has somewhere to go. The first version of these was seven rows high
# and every chart looked like a hedge.
STAT_HEIGHT, CHART_HEIGHT, TALL_HEIGHT = 5, 10, 12


def target(expr, legend, ds=PROM, ref="A", instant=False):
    t = {"refId": ref, "expr": expr, "legendFormat": legend, "datasource": ds}
    if instant:
        t["instant"] = True
    return t


def recent(expr):
    """Only instances that reported in the last minute.

    Prometheus keeps a series alive for five minutes after its last sample, which is right for
    a scrape that missed and wrong for a process that stopped: a development machine restarts
    Keydra all day, and every one of those instances would go on claiming, for five minutes
    each, to be the one doing the chores.
    """
    return f"last_over_time({expr}[1m])"


class Layout:
    """Places panels left to right, wrapping when a row is full."""

    def __init__(self):
        self.panels = []
        self.x = 0
        self.y = 0
        self.row_height = 0

    def add(self, panel, width, height):
        if self.x + width > FULL:
            self.y += self.row_height
            self.x = 0
            self.row_height = 0
        panel["id"] = len(self.panels) + 1
        panel["gridPos"] = {"x": self.x, "y": self.y, "w": width, "h": height}
        self.panels.append(panel)
        self.x += width
        self.row_height = max(self.row_height, height)
        return panel


def _panel(title, kind, targets, unit=None, desc=None, opts=None, defaults=None):
    p = {
        "type": kind,
        "title": title,
        "datasource": targets[0]["datasource"],
        "targets": targets,
        "fieldConfig": {"defaults": dict(defaults or {}), "overrides": []},
    }
    if unit:
        p["fieldConfig"]["defaults"]["unit"] = unit
    if desc:
        p["description"] = desc
    if opts:
        p["options"] = opts
    return p


def chart(title, targets, unit=None, desc=None, kind="timeseries"):
    return _panel(title, kind, targets, unit=unit, desc=desc)


def stat(title, targets, unit=None, desc=None, mappings=None, colour="value"):
    opts = {
        "colorMode": colour,
        "graphMode": "area",
        "textMode": "auto",
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
    }
    return _panel(title, "stat", targets, unit=unit, desc=desc, opts=opts,
                  defaults={"mappings": mappings} if mappings else None)


def logs(title, expr, desc=None):
    return _panel(
        title, "logs", [target(expr, "", ds=LOKI)], desc=desc,
        opts={"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
              "enableLogDetails": True, "dedupStrategy": "none", "prettifyLogMessage": False},
    )


def table(title, targets, unit=None, desc=None):
    return _panel(title, "table", targets, unit=unit, desc=desc, opts={"showHeader": True})


def build(uid, title, description, sections, refresh="10s", window="now-1h"):
    """A dashboard from a list of (panel, width, height)."""
    layout = Layout()
    for panel, width, height in sections:
        layout.add(panel, width, height)
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": ["keydra"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "refresh": refresh,
        "time": {"from": window, "to": "now"},
        "templating": {"list": []},
        "panels": layout.panels,
        "editable": True,
    }


HERE_OR_ELSEWHERE = [{"type": "value", "options": {
    "0": {"text": "elsewhere", "color": "text", "index": 1},
    "1": {"text": "here", "color": "green", "index": 0}}}]

BOARDS = []


def board(uid, title, description, sections, **kwargs):
    BOARDS.append((uid, build(uid, title, description, sections, **kwargs)))


# --- Overview ---------------------------------------------------------------

board(
    "keydra-overview", "Keydra — overview",
    "Where to start: whether this instance is doing the shared work, what it is reaching, and "
    "what it is being asked for.",
    [
        (stat("Chores", [target(recent("keydra_chores"), "{{keydra_instance}}")],
              desc="Whether this instance is the one doing the work that happens once. Where a "
                   "schedule did not run, this is the first question.",
              mappings=HERE_OR_ELSEWHERE, colour="background"), QUARTER, STAT_HEIGHT),
        (stat("Targets up", [target(recent('keydra_targets{state="up"}'), "{{keydra_instance}}")]),
         QUARTER, STAT_HEIGHT),
        (stat("Targets down", [target(recent('keydra_targets{state="down"}'), "{{keydra_instance}}")],
              desc="Targets Keydra last failed to reach. Not the same as targets nobody is "
                   "watching."), QUARTER, STAT_HEIGHT),
        (stat("Requests / s", [target("sum(rate(http_server_requests_seconds_count[5m]))", "served")],
              unit="reqps"), QUARTER, STAT_HEIGHT),
        (chart("Requests by endpoint",
               [target("sum by (uri) (rate(http_server_requests_seconds_count[5m]))", "{{uri}}")],
               unit="reqps",
               desc="Templated paths, not the ones that were typed: a series per key name would "
                    "be a series per key."), HALF, CHART_HEIGHT),
        (chart("Time to answer",
               [target("sum(rate(http_server_requests_seconds_sum[5m])) / sum(rate(http_server_requests_seconds_count[5m]))", "mean"),
                target("histogram_quantile(0.95, sum by (le) (rate(http_server_requests_seconds_bucket[5m])))", "95th", ref="B")],
               unit="s"), HALF, CHART_HEIGHT),
        (chart("What the log is saying",
               [target('sum by (severity_text) (count_over_time({service_name="keydra"} [$__auto]))',
                       "{{severity_text}}", ds=LOKI)],
               desc="Volume by level. A step in the error line is the thing to look at; the rest "
                    "is how busy it was."), HALF, CHART_HEIGHT),
        (chart("Heap",
               [target('sum(jvm_memory_used_bytes{area="heap"})', "used"),
                target('sum(jvm_memory_committed_bytes{area="heap"})', "committed", ref="B")],
               unit="bytes"), HALF, CHART_HEIGHT),
        (logs("Errors and warnings", '{service_name="keydra"} | severity_text =~ "WARN|ERROR"',
              desc="Straight from Loki, newest first."), FULL, TALL_HEIGHT),
    ],
)

# --- Targets ----------------------------------------------------------------

board(
    "keydra-targets", "Keydra — targets",
    "The servers Keydra watches, as Keydra experiences them: how long a reading takes, how "
    "often one fails, and which target is the slow one.",
    [
        (stat("Up", [target(recent('keydra_targets{state="up"}'), "{{keydra_instance}}")]), THIRD, STAT_HEIGHT),
        (stat("Down", [target(recent('keydra_targets{state="down"}'), "{{keydra_instance}}")]), THIRD, STAT_HEIGHT),
        (stat("Readings / minute",
              [target("sum(rate(keydra_target_sample_seconds_count[5m])) * 60", "taken")],
              desc="Across every target being sampled, whether a dashboard asked or a rule did."),
         THIRD, STAT_HEIGHT),
        (chart("Targets by state", [target(recent("keydra_targets"), "{{state}} · {{keydra_instance}}")],
               desc="What the health sweep last found. A target nobody is watching is in neither "
                    "line."), HALF, CHART_HEIGHT),
        (chart("Time to read a target",
               [target("rate(keydra_target_sample_seconds_sum[5m]) / rate(keydra_target_sample_seconds_count[5m])",
                       "mean · connection {{connection}}"),
                target("keydra_target_sample_seconds_max", "slowest · connection {{connection}}", ref="B")],
               unit="s",
               desc="Dialling, waiting and parsing included — what a reading actually costs."),
         HALF, CHART_HEIGHT),
        (chart("Readings taken",
               [target("rate(keydra_target_sample_seconds_count[5m]) * 60", "per minute · connection {{connection}}"),
                target("rate(keydra_target_sample_failures_total[5m]) * 60", "failed · connection {{connection}}", ref="B")],
               desc="A target that stops answering shows up here before it shows up anywhere "
                    "else."), HALF, CHART_HEIGHT),
        (chart("Failures",
               [target("sum by (connection) (increase(keydra_target_sample_failures_total[15m]))",
                       "connection {{connection}}")], kind="barchart",
               desc="Readings that did not come back, in fifteen-minute buckets."), HALF, CHART_HEIGHT),
        (table("Slowest to read",
               [target("topk(10, rate(keydra_target_sample_seconds_sum[5m]) / rate(keydra_target_sample_seconds_count[5m]))",
                       "connection {{connection}}", instant=True)], unit="s",
               desc="By connection id. Names are deliberately not in the metrics; the catalog "
                    "resolves them."), HALF, CHART_HEIGHT),
        (logs("What the watching said",
              '{service_name="keydra"} | severity_text =~ "WARN|ERROR" |~ "(?i)(connection|target|sampl)"'),
         HALF, CHART_HEIGHT),
    ],
)

# --- Scheduled work ---------------------------------------------------------

board(
    "keydra-schedules", "Keydra — scheduled work",
    "What the clock asked for: which jobs ran, what became of them, and whether anybody had to "
    "press anything.",
    [
        (stat("Runs in this window",
              [target("sum(increase(keydra_schedule_runs_total[$__range])) or vector(0)", "runs")],
              desc="Everything the clock and everybody asked for, together."), THIRD, STAT_HEIGHT),
        (stat("Failed",
              [target('sum(increase(keydra_schedule_runs_total{outcome="FAILED"}[$__range])) or vector(0)', "failed")],
              desc="A job that failed is a job whose reason is in the log below."), THIRD, STAT_HEIGHT),
        (stat("Refused",
              [target('sum(increase(keydra_schedule_runs_total{outcome="REFUSED"}[$__range])) or vector(0)', "refused")],
              desc="Somebody's rights were taken away and the schedule they left behind noticed. "
                   "Not an error — the check working."), THIRD, STAT_HEIGHT),
        (chart("Runs by outcome",
               [target("sum by (outcome) (increase(keydra_schedule_runs_total[15m]))", "{{outcome}}")],
               kind="barchart"), HALF, CHART_HEIGHT),
        (chart("Asked for by the clock, or by a person",
               [target("sum by (manual) (increase(keydra_schedule_runs_total[15m]))", "pressed {{manual}}")],
               kind="barchart",
               desc="A schedule that only ever ran because a person pressed it has not run."),
         HALF, CHART_HEIGHT),
        (chart("Runs over time",
               [target("sum by (outcome) (rate(keydra_schedule_runs_total[5m]) * 3600)", "{{outcome}} / hour")]),
         HALF, CHART_HEIGHT),
        (stat("Chores", [target(recent("keydra_chores"), "{{keydra_instance}}")],
              mappings=HERE_OR_ELSEWHERE, colour="background",
              desc="All of this happens on exactly one instance. This is which."), HALF, STAT_HEIGHT),
        (logs("What the schedules said",
              '{service_name="keydra"} |~ "(?i)scheduled job|schedule"',
              desc="Including the ones that failed, with what they said about it."), FULL, TALL_HEIGHT),
    ],
)

# --- Alerts -----------------------------------------------------------------

board(
    "keydra-alerts", "Keydra — alerts",
    "The conditions somebody asked to hear about: when they started, when they stopped, and "
    "whether the message got out.",
    [
        (stat("Fired",
              [target('sum(increase(keydra_alert_events_total{kind="FIRED"}[$__range])) or vector(0)', "fired")]),
         QUARTER, STAT_HEIGHT),
        (stat("Cleared",
              [target('sum(increase(keydra_alert_events_total{kind="CLEARED"}[$__range])) or vector(0)', "cleared")]),
         QUARTER, STAT_HEIGHT),
        (stat("Delivered",
              [target('sum(increase(keydra_alert_deliveries_total{outcome="SENT"}[$__range])) or vector(0)', "sent")]),
         QUARTER, STAT_HEIGHT),
        (stat("Delivery failed",
              [target('sum(increase(keydra_alert_deliveries_total{outcome="FAILED"}[$__range])) or vector(0)', "failed")],
              desc="Somebody who was not told. The alert still happened and is still in the "
                   "history."), QUARTER, STAT_HEIGHT),
        (chart("Alerts",
               [target("sum by (kind) (increase(keydra_alert_events_total[15m]))", "{{kind}}")],
               kind="barchart",
               desc="Only the transitions: a rule firing all night is one bar, not a wall of "
                    "them."), HALF, CHART_HEIGHT),
        (chart("Deliveries",
               [target("sum by (outcome) (increase(keydra_alert_deliveries_total[15m]))", "{{outcome}}")],
               kind="barchart",
               desc="NONE is a rule with nowhere to send: it is in the history and on the page, "
                    "and nobody was messaged."), HALF, CHART_HEIGHT),
        (logs("What the rules said", '{service_name="keydra"} |~ "(?i)alert|rule"'), FULL, TALL_HEIGHT),
    ],
)

# --- Backups and migrations -------------------------------------------------

board(
    "keydra-backups", "Keydra — backups and migrations",
    "The two long jobs: copies leaving the machine, and keys moving between targets.",
    [
        (stat("Backups taken",
              [target('sum(increase(keydra_backups_total{outcome="done"}[$__range])) or vector(0)', "done")]),
         THIRD, STAT_HEIGHT),
        (stat("Backups failed",
              [target('sum(increase(keydra_backups_total{outcome="failed"}[$__range])) or vector(0)', "failed")],
              desc="A backup that failed is the one failure a backup exists to prevent."),
         THIRD, STAT_HEIGHT),
        (stat("Migrations in flight",
              [target(recent("keydra_migrations_running"), "{{keydra_instance}}")],
              desc="Walks in progress on this instance. A restart marks its own as interrupted."),
         THIRD, STAT_HEIGHT),
        (chart("Backups",
               [target("sum by (outcome) (increase(keydra_backups_total[1h]))", "{{outcome}}")],
               kind="barchart"), HALF, CHART_HEIGHT),
        (chart("Migrations over time",
               [target(recent("keydra_migrations_running"), "{{keydra_instance}}")]), HALF, CHART_HEIGHT),
        (logs("What the copying said",
              '{service_name="keydra"} |~ "(?i)backup|migration|destination"'), FULL, TALL_HEIGHT),
    ],
)

# --- Requests ---------------------------------------------------------------

board(
    "keydra-requests", "Keydra — requests",
    "What is being asked of the API, how long it takes and how often it goes wrong.",
    [
        (stat("Requests / s", [target("sum(rate(http_server_requests_seconds_count[5m]))", "served")],
              unit="reqps"), THIRD, STAT_HEIGHT),
        (stat("Errors / s",
              [target('sum(rate(http_server_requests_seconds_count{status=~"5.."}[5m])) or vector(0)', "failed")],
              unit="reqps"), THIRD, STAT_HEIGHT),
        (stat("95th percentile",
              [target("histogram_quantile(0.95, sum by (le) (rate(http_server_requests_seconds_bucket[5m])))", "95th")],
              unit="s"), THIRD, STAT_HEIGHT),
        (chart("By endpoint",
               [target("sum by (uri) (rate(http_server_requests_seconds_count[5m]))", "{{uri}}")],
               unit="reqps"), HALF, CHART_HEIGHT),
        (chart("By outcome",
               [target("sum by (status) (rate(http_server_requests_seconds_count[5m]))", "{{status}}")],
               unit="reqps",
               desc="401 and 403 are the security model working, not a fault; 500 is not."),
         HALF, CHART_HEIGHT),
        (chart("How long, by percentile",
               [target("histogram_quantile(0.5, sum by (le) (rate(http_server_requests_seconds_bucket[5m])))", "median"),
                target("histogram_quantile(0.95, sum by (le) (rate(http_server_requests_seconds_bucket[5m])))", "95th", ref="B"),
                target("histogram_quantile(0.99, sum by (le) (rate(http_server_requests_seconds_bucket[5m])))", "99th", ref="C")],
               unit="s"), HALF, CHART_HEIGHT),
        (chart("Slowest endpoints",
               [target("topk(8, sum by (uri) (rate(http_server_requests_seconds_sum[5m]) / rate(http_server_requests_seconds_count[5m])))",
                       "{{uri}}")], unit="s"), HALF, CHART_HEIGHT),
        (table("Where the time goes",
               [target("topk(15, sum by (uri, method) (rate(http_server_requests_seconds_sum[5m])))",
                       "{{method}} {{uri}}", instant=True)], unit="s",
               desc="Seconds spent per second, which is the product of how slow and how often — "
                    "the endpoint worth looking at first."), FULL, CHART_HEIGHT),
    ],
)

# --- Runtime ----------------------------------------------------------------

board(
    "keydra-runtime", "Keydra — runtime",
    "The process itself. Read this when the question is about Keydra rather than about a "
    "target.",
    [
        (stat("Heap in use", [target('sum(jvm_memory_used_bytes{area="heap"})', "used")], unit="bytes"),
         QUARTER, STAT_HEIGHT),
        (stat("Threads", [target("jvm_threads_live_threads", "live")]), QUARTER, STAT_HEIGHT),
        (stat("Processor", [target("process_cpu_usage", "this process")], unit="percentunit"),
         QUARTER, STAT_HEIGHT),
        (stat("Uptime", [target("process_uptime_seconds", "up")], unit="s"), QUARTER, STAT_HEIGHT),
        (chart("Heap",
               [target('sum(jvm_memory_used_bytes{area="heap"})', "used"),
                target('sum(jvm_memory_committed_bytes{area="heap"})', "committed", ref="B"),
                target('sum(jvm_memory_max_bytes{area="heap"})', "max", ref="C")], unit="bytes"),
         HALF, CHART_HEIGHT),
        (chart("Off heap", [target('sum(jvm_memory_used_bytes{area="nonheap"})', "used")], unit="bytes"),
         HALF, CHART_HEIGHT),
        (chart("Garbage collection",
               [target("sum by (cause) (rate(jvm_gc_pause_seconds_sum[5m]))", "{{cause}}")], unit="s",
               desc="Seconds of pause per second. A number approaching one is a process that is "
                    "mostly collecting garbage."), HALF, CHART_HEIGHT),
        (chart("Threads",
               [target("jvm_threads_live_threads", "live"),
                target("jvm_threads_daemon_threads", "daemon", ref="B")]), HALF, CHART_HEIGHT),
        (chart("Database connections",
               [target("sum(agroal_active_count)", "in use"),
                target("sum(agroal_available_count)", "idle", ref="B"),
                target("sum(agroal_awaiting_count)", "waiting", ref="C")],
               desc="Waiting is the line that matters: a request waiting for a connection is a "
                    "request that has already been slow."), HALF, CHART_HEIGHT),
        (chart("Processor",
               [target("system_cpu_usage", "machine"),
                target("process_cpu_usage", "this process", ref="B")], unit="percentunit"),
         HALF, CHART_HEIGHT),
    ],
)

# --- Traces -----------------------------------------------------------------

board(
    "keydra-traces", "Keydra — traces",
    "Every span Tempo has seen, as numbers: one per request and one per statement underneath "
    "it. This is where a slow endpoint turns out to be a slow query.",
    [
        (stat("Spans / s",
              [target('sum(rate(traces_spanmetrics_calls_total{service="keydra"}[5m])) or vector(0)', "keydra")],
              unit="reqps"), HALF, STAT_HEIGHT),
        (stat("From the browser",
              [target('sum(rate(traces_spanmetrics_calls_total{service="keydra-web"}[5m])) or vector(0)', "keydra-web")],
              unit="reqps",
              desc="Spans the page produced. They carry the trace header on, so they are the "
                   "same traces as the ones above."), HALF, STAT_HEIGHT),
        (chart("Busiest operations",
               [target('topk(10, sum by (span_name) (rate(traces_spanmetrics_calls_total{service="keydra"}[5m])))',
                       "{{span_name}}")], unit="reqps"), HALF, CHART_HEIGHT),
        (chart("Slowest operations",
               [target('topk(10, sum by (span_name) (rate(traces_spanmetrics_latency_sum{service="keydra"}[5m]) '
                       '/ rate(traces_spanmetrics_latency_count{service="keydra"}[5m])))', "{{span_name}}")],
               unit="s"), HALF, CHART_HEIGHT),
        (chart("Spans that ended badly",
               [target('sum by (span_name) (rate(traces_spanmetrics_calls_total{service="keydra", status_code="STATUS_CODE_ERROR"}[5m]))',
                       "{{span_name}}")], unit="reqps",
               desc="An error here is a span the code marked as failed, which is not always a "
                    "request that failed."), HALF, CHART_HEIGHT),
        (chart("How long a span takes",
               [target('histogram_quantile(0.95, sum by (le) (rate(traces_spanmetrics_latency_bucket{service="keydra"}[5m])))', "95th"),
                target('histogram_quantile(0.5, sum by (le) (rate(traces_spanmetrics_latency_bucket{service="keydra"}[5m])))', "median", ref="B")],
               unit="s"), HALF, CHART_HEIGHT),
        (table("Every operation",
               [target('topk(20, sum by (span_name) (rate(traces_spanmetrics_latency_sum{service="keydra"}[5m]) '
                       '/ rate(traces_spanmetrics_latency_count{service="keydra"}[5m])))',
                       "{{span_name}}", instant=True)], unit="s",
               desc="Mean duration by name. The SQL statements in here are Hibernate's, seen "
                    "from inside the request that ran them."), FULL, CHART_HEIGHT),
    ],
)

# --- Logs -------------------------------------------------------------------

board(
    "keydra-logs", "Keydra — logs",
    "What Keydra said, and how much of it. A log record written inside a request carries the "
    "trace it belongs to, which is what makes a span's logs findable.",
    [
        (stat("Errors",
              [target('sum(count_over_time({service_name="keydra"} | severity_text = "ERROR" [$__range])) or vector(0)',
                      "errors", ds=LOKI)]), THIRD, STAT_HEIGHT),
        (stat("Warnings",
              [target('sum(count_over_time({service_name="keydra"} | severity_text = "WARN" [$__range])) or vector(0)',
                      "warnings", ds=LOKI)]), THIRD, STAT_HEIGHT),
        (stat("Lines",
              [target('sum(count_over_time({service_name="keydra"} [$__range])) or vector(0)', "lines", ds=LOKI)]),
         THIRD, STAT_HEIGHT),
        (chart("By level",
               [target('sum by (severity_text) (count_over_time({service_name="keydra"} [$__auto]))',
                       "{{severity_text}}", ds=LOKI)]), HALF, CHART_HEIGHT),
        (chart("Who is talking",
               [target('topk(10, sum by (bridge_name) (count_over_time({service_name="keydra"} [$__auto])))',
                       "{{bridge_name}}", ds=LOKI)],
               desc="By logger. A class that suddenly has a lot to say is usually the first sign "
                    "of something."), HALF, CHART_HEIGHT),
        (logs("Errors", '{service_name="keydra"} | severity_text = "ERROR"'), HALF, TALL_HEIGHT),
        (logs("Warnings", '{service_name="keydra"} | severity_text = "WARN"'), HALF, TALL_HEIGHT),
        (logs("Everything", '{service_name="keydra"}',
              desc="The whole stream, for when the question is not yet a filter."), FULL, TALL_HEIGHT),
    ],
)

# --- The browser ------------------------------------------------------------

board(
    "keydra-browser", "Keydra — the browser",
    "The half that runs in somebody's tab: what it measured, what it threw, and how long it "
    "waited for the other half.",
    [
        (stat("Sessions",
              [target('count(count by (session_id) (count_over_time({service_name="keydra-web"} | logfmt [$__range]))) or vector(0)',
                      "sessions", ds=LOKI)],
              desc="Distinct browser sessions in the window. A session lasts as long as a tab "
                   "does; nothing here recognises anybody tomorrow."), QUARTER, STAT_HEIGHT),
        (stat("Errors",
              [target('sum(count_over_time({service_name="keydra-web", kind="exception"} [$__range])) or vector(0)',
                      "thrown", ds=LOKI)],
              desc="Exceptions the page reported. Zero is the number this should be, and it is "
                   "written as zero rather than as nothing."), QUARTER, STAT_HEIGHT),
        (stat("Page loads",
              [target('sum(count_over_time({service_name="keydra-web"} |= "faro.performance.navigation" [$__range])) or vector(0)',
                      "loads", ds=LOKI)],
              desc="Whole-page loads. Moving between views inside the application is not one, "
                   "which is the point of the application being one page."), QUARTER, STAT_HEIGHT),
        (stat("Requests the page made",
              [target('sum(rate({service_name="keydra-web"} |= "faro.tracing.fetch" [5m])) * 60 or vector(0)',
                      "per minute", ds=LOKI)],
              desc="Every one carries the trace header on to the backend, which is what puts a "
                   "slow page and a slow query in the same trace."), QUARTER, STAT_HEIGHT),
        (chart("What the browser is doing",
               [target('sum by (kind) (count_over_time({service_name="keydra-web"} [$__auto]))',
                       "{{kind}}", ds=LOKI)],
               desc="Logs, events, measurements and exceptions, by kind."), HALF, CHART_HEIGHT),
        (chart("Web vitals",
               [target('avg_over_time({service_name="keydra-web", kind="measurement"} | logfmt | lcp != "" | unwrap lcp [$__auto]) by (service_name)',
                       "largest contentful paint", ds=LOKI),
                target('avg_over_time({service_name="keydra-web", kind="measurement"} | logfmt | fcp != "" | unwrap fcp [$__auto]) by (service_name)',
                       "first contentful paint", ds=LOKI, ref="B"),
                target('avg_over_time({service_name="keydra-web", kind="measurement"} | logfmt | inp != "" | unwrap inp [$__auto]) by (service_name)',
                       "interaction to next paint", ds=LOKI, ref="C")],
               unit="ms",
               desc="What the browser measured about itself, in milliseconds."), HALF, CHART_HEIGHT),
        (chart("Time the browser waited",
               # Grouped by the service and by nothing else. Parsing a line turns every field in
               # it into a label, so without this there is one series per distinct request — a
               # legend of two hundred identical names.
               [target('quantile_over_time(0.95, {service_name="keydra-web"} |= "faro.tracing.fetch" | logfmt | unwrap event_data_duration_ns [$__auto]) by (service_name) / 1000000',
                       "95th", ds=LOKI),
                target('avg_over_time({service_name="keydra-web"} |= "faro.tracing.fetch" | logfmt | unwrap event_data_duration_ns [$__auto]) by (service_name) / 1000000',
                       "mean", ds=LOKI, ref="B")],
               unit="ms",
               desc="How long a request took as the browser saw it — which includes the network "
                    "and the queue the server has not got to yet, and is therefore a different "
                    "number from the one the server reports."), HALF, CHART_HEIGHT),
        (chart("What it loaded",
               [target('sum by (event_data_cacheHitStatus) (count_over_time({service_name="keydra-web"} |= "faro.performance.resource" | logfmt [$__auto]))',
                       "{{event_data_cacheHitStatus}}", ds=LOKI)],
               desc="Resources the page fetched, by whether the cache answered."), HALF, CHART_HEIGHT),
        (logs("Exceptions", '{service_name="keydra-web", kind="exception"}',
              desc="With the stack the browser had. URLs are redacted before they leave the "
                   "page: a key name is the contents of a target."), FULL, TALL_HEIGHT),
    ],
)

# --- Instances --------------------------------------------------------------

board(
    "keydra-instances", "Keydra — instances",
    "More than one Keydra against one database: which of them is doing the work that happens "
    "once, and how each of them is bearing up.",
    [
        (stat("Doing the chores",
              [target(recent("keydra_chores"), "{{keydra_instance}}")],
              mappings=HERE_OR_ELSEWHERE, colour="background",
              desc="Exactly one of these should say here. Two would mean the lease is not doing "
                   "its job; none means nobody has claimed it yet."), HALF, STAT_HEIGHT),
        (stat("Instances answering",
              [target(f'count({recent("keydra_chores")}) or vector(0)', "instances")],
              desc="How many processes reported in the last minute."), HALF, STAT_HEIGHT),
        (chart("Who holds the chores",
               [target(recent("keydra_chores"), "{{keydra_instance}}")],
               desc="A handover is the step from one line to another. Where they overlap, one "
                    "instance had not noticed yet — which is what the lease's own expiry is "
                    "for."), FULL, CHART_HEIGHT),
        (chart("Requests by instance",
               [target("sum by (keydra_instance) (rate(http_server_requests_seconds_count[5m]))",
                       "{{keydra_instance}}")], unit="reqps",
               desc="Whether the load balancer is actually balancing."), HALF, CHART_HEIGHT),
        (chart("Heap by instance",
               [target('sum by (keydra_instance) (jvm_memory_used_bytes{area="heap"})', "{{keydra_instance}}")],
               unit="bytes"), HALF, CHART_HEIGHT),
        (chart("Targets watched by instance",
               [target(recent("keydra_targets"), "{{keydra_instance}} · {{state}}")],
               desc="Sampling on a rule's behalf happens only on the instance holding the "
                    "chores; a dashboard somebody has open is a different reason and can be "
                    "anywhere."), HALF, CHART_HEIGHT),
        (chart("Sampling cost by instance",
               [target("sum by (keydra_instance) (rate(keydra_target_sample_seconds_count[5m])) * 60",
                       "{{keydra_instance}} · readings / minute")]), HALF, CHART_HEIGHT),
        (logs("Handovers", '{service_name="keydra"} |~ "chores"',
              desc="Every line an instance wrote about taking the work on or giving it up."),
         FULL, TALL_HEIGHT),
    ],
)


def main():
    out = os.path.join("deploy", "observability", "grafana", "dashboards")
    for uid, doc in BOARDS:
        with open(os.path.join(out, uid + ".json"), "w") as handle:
            handle.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        print(f"{uid}: {len(doc['panels'])} panels")


if __name__ == "__main__":
    main()
