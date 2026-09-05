"""Pure, source-separated model performance report exporters.

The report is a reference ledger, not a routing decision.  Each observation
keeps its source, model identity, effort (when present), benchmark/category,
version, capture timestamp, and source snapshot ID.  No source is averaged
with another source and no model-family fallback is applied.

Public contract
---------------
``build_model_performance_report`` accepts the existing Workbench catalog,
benchmark baseline, Radar/AI Frontier status mappings, and an optional local
performance snapshot.  It returns a JSON-compatible mapping with:

* ``models``: the catalog plus any source-only model identities;
* ``observations``: one flat, source-specific row per benchmark, Radar
  model/effort, AI Frontier model/category, local runtime bucket, or
  catalog-only model;
* ``source_snapshots`` and ``captured_at``: provenance separated from the
  report's ``generated_at`` timestamp; and
* ``counts`` and ``missing_data``: bounded, machine-readable coverage notes.

``report_to_csv`` emits one observation per row.  ``report_to_html`` emits a
self-contained Chinese reference artifact with safe text escaping and native
keyboard-accessible filters.  Both functions use only Python's standard
library and never perform model, filesystem, or network I/O.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
import csv
from html import escape
import io
import json
import math
import re
from urllib.parse import urlsplit, urlunsplit
from typing import Any


_REPORT_SCHEMA_VERSION = 1
_PROVIDER_ALIASES = {
    "openai": "codex",
    "codex": "codex",
    "anthropic": "claude",
    "claude": "claude",
}


def build_model_performance_report(
    *,
    catalog: Mapping[str, Any],
    baseline: Mapping[str, Any],
    radar_status: Mapping[str, Any] | None = None,
    ai_frontier_status: Mapping[str, Any] | None = None,
    performance_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a bounded, source-separated model performance ledger.

    The function deliberately consumes the supplied mappings as snapshots.  A
    status mapping may expose a provider snapshot under either ``snapshot``
    or ``active`` (the latter is the portable Radar export shape).  Missing or
    malformed optional source sections produce explicit missing-data notes;
    they do not cause data from another source to be copied or averaged.
    """

    if not isinstance(catalog, Mapping):
        raise TypeError("catalog must be a mapping")
    if not isinstance(baseline, Mapping):
        raise TypeError("baseline must be a mapping")
    if radar_status is not None and not isinstance(radar_status, Mapping):
        raise TypeError("radar_status must be a mapping or None")
    if ai_frontier_status is not None and not isinstance(ai_frontier_status, Mapping):
        raise TypeError("ai_frontier_status must be a mapping or None")
    if performance_snapshot is not None and not isinstance(performance_snapshot, Mapping):
        raise TypeError("performance_snapshot must be a mapping or None")

    generated_at = _now_iso()
    catalog_models = _catalog_models(catalog)
    catalog_index = {(item["provider"], item["model_id"]): item for item in catalog_models}
    observations: list[dict[str, Any]] = []
    source_snapshots: dict[str, dict[str, Any]] = {}

    baseline_doc = _baseline_document(baseline)
    baseline_rows = _baseline_observations(
        baseline_doc,
        generated_at=generated_at,
        catalog_index=catalog_index,
    )
    observations.extend(baseline_rows)
    source_snapshots["baseline"] = _baseline_source(baseline_doc, baseline_rows)

    radar_snapshot = _status_snapshot(radar_status)
    radar_rows = _radar_observations(
        radar_status,
        radar_snapshot,
        generated_at=generated_at,
        catalog_index=catalog_index,
    )
    observations.extend(radar_rows)
    source_snapshots["radar"] = _radar_source(radar_status, radar_snapshot, radar_rows)

    frontier_snapshot = _status_snapshot(ai_frontier_status)
    frontier_rows = _ai_frontier_observations(
        ai_frontier_status,
        frontier_snapshot,
        generated_at=generated_at,
        catalog_index=catalog_index,
    )
    observations.extend(frontier_rows)
    source_snapshots["ai_frontier"] = _ai_frontier_source(
        ai_frontier_status,
        frontier_snapshot,
        frontier_rows,
    )

    runtime_rows = _runtime_observations(
        performance_snapshot,
        generated_at=generated_at,
        catalog_index=catalog_index,
    )
    observations.extend(runtime_rows)
    source_snapshots["local_runtime"] = _runtime_source(
        performance_snapshot,
        runtime_rows,
    )

    observed_keys = {
        (str(row.get("provider") or "unknown"), str(row.get("model_id")))
        for row in observations
        if row.get("observation_type") != "catalog_only" and row.get("model_id")
    }
    catalog_only_rows: list[dict[str, Any]] = []
    for item in catalog_models:
        key = (item["provider"], item["model_id"])
        if key in observed_keys:
            continue
        catalog_only_rows.append(
            _catalog_only_observation(item, generated_at=generated_at)
        )
    observations.extend(catalog_only_rows)

    # The catalog remains the authoritative model index.  Source-only rows are
    # retained as separate identities so unknown Radar/Frontier models are not
    # silently discarded merely because they cannot be routed locally.
    model_index: dict[tuple[str, str], dict[str, Any]] = {
        (item["provider"], item["model_id"]): dict(item)
        for item in catalog_models
    }
    for row in observations:
        model_id = _text(row.get("model_id"))
        if model_id is None:
            continue
        provider = _canonical_provider(row.get("provider")) or "unknown"
        key = (provider, model_id)
        entry = model_index.setdefault(
            key,
            {
                "provider": provider,
                "model_id": model_id,
                "model_family": _text(row.get("model_family")),
                "routable": None,
                "supported_efforts": [],
                "agent_version": None,
                "catalog_present": False,
            },
        )
        if row.get("observation_type") != "catalog_only":
            entry.setdefault("observed_sources", set()).add(str(row["source"]))

    models: list[dict[str, Any]] = []
    for key in sorted(model_index):
        item = dict(model_index[key])
        observed_sources = item.pop("observed_sources", set())
        item["observed_sources"] = sorted(observed_sources)
        item["observation_count"] = sum(
            1
            for row in observations
            if _canonical_provider(row.get("provider")) == item["provider"]
            and row.get("model_id") == item["model_id"]
            and row.get("observation_type") != "catalog_only"
        )
        item["performance_available"] = item["observation_count"] > 0
        if not item["performance_available"] and item.get("catalog_present") is True:
            item["missing_data"] = ["no_performance_observation"]
        else:
            item["missing_data"] = []
        models.append(item)

    observations.sort(key=_observation_sort_key)
    for index, row in enumerate(observations, start=1):
        row["row_number"] = index
        row["observation_id"] = _observation_id(row, index)

    captured_at = {
        name: source.get("captured_at") for name, source in source_snapshots.items()
    }
    latest_captured_at = _latest_timestamp(captured_at.values())
    missing_data = [
        {
            "source": name,
            "missing": list(source.get("missing_data", [])),
        }
        for name, source in source_snapshots.items()
        if source.get("missing_data")
    ]
    source_counts = Counter(str(row.get("source")) for row in observations)
    return {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "report_type": "model-performance-reference-ledger",
        "generated_at": generated_at,
        "captured_at": captured_at,
        "actual_captured_at": dict(captured_at),
        "latest_captured_at": latest_captured_at,
        "catalog": {
            "catalog_id": _text(catalog.get("catalog_id")),
            "digest": _text(catalog.get("digest")),
            "observed_at": _iso_timestamp(
                _first(catalog, "observed_at", "captured_at", "fetched_at")
            ),
        },
        "models": models,
        "source_snapshots": source_snapshots,
        # ``sources`` is a convenient ordered view for consumers that prefer
        # an array; ``source_snapshots`` remains the canonical keyed mapping.
        "sources": [source_snapshots[name] for name in sorted(source_snapshots)],
        "observations": observations,
        "counts": {
            "catalog_models": len(catalog_models),
            "catalog_only_models": len(catalog_only_rows),
            "total_models": len(models),
            "total_observations": len(observations),
            "by_source": dict(sorted(source_counts.items())),
        },
        "missing_data": missing_data,
        "unit_definitions": {
            "quality_fraction": "fraction in [0, 1]",
            "consistency_fraction": "fraction in [0, 1]",
            "consistency_std_fraction": "fraction in [0, 1]",
            "publisher_relative_cost": "publisher-relative value; source unit not published",
            "iq": "Radar IQ points",
            "pass_rate": "fraction in [0, 1]",
            "cost_usd": "USD per task (Radar only)",
            "latency_seconds": "seconds per task (Radar only)",
            "duration_seconds": "seconds",
            "sample_count": "observations",
        },
    }


def report_to_csv(report: Mapping[str, Any]) -> str:
    """Serialize report observations as one explicitly-unit-labelled CSV row."""

    observations = report.get("observations") if isinstance(report, Mapping) else None
    if not isinstance(observations, list):
        raise TypeError("report observations must be a list")
    generated_at = _text(report.get("generated_at")) if isinstance(report, Mapping) else None
    fields = (
        "row_number",
        "observation_id",
        "source",
        "observation_type",
        "provider",
        "model_id",
        "model_family",
        "reasoning_effort",
        "agent_version",
        "task_type",
        "complexity",
        "benchmark",
        "benchmark_version",
        "category",
        "source_snapshot_id",
        "source_url",
        "captured_at",
        "generated_at",
        "catalog_routable",
        "routing_eligible",
        "quality_fraction",
        "consistency_fraction",
        "consistency_std_fraction",
        "publisher_relative_cost",
        "publisher_relative_cost_unit",
        "publisher_relative_cost_quoted",
        "publisher_relative_cost_surprise",
        "score",
        "score_unit",
        "score_kind",
        "iq",
        "iq_unit",
        "pass_rate",
        "pass_rate_unit",
        "sample_count",
        "sample_count_unit",
        "cost_usd",
        "cost_usd_unit",
        "latency_seconds",
        "latency_unit",
        "effective_sample_strength",
        "first_pass_rate",
        "final_acceptance_rate",
        "runtime_rate_unit",
        "duration_mean_seconds",
        "duration_p50_seconds",
        "duration_sample_count",
        "duration_unit",
        "runtime_attempt_count",
        "runtime_quality_sample_count",
        "runtime_success_count",
        "runtime_failure_count",
        "runtime_unresolved_count",
        "missing_data",
        "data_quality_flags",
    )
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in observations:
        if not isinstance(row, Mapping):
            continue
        metrics = row.get("metrics") if isinstance(row.get("metrics"), Mapping) else row
        units = row.get("units") if isinstance(row.get("units"), Mapping) else {}
        flattened = {
            field: _csv_value(_csv_field_value(field, row, metrics, units, generated_at))
            for field in fields
        }
        writer.writerow(flattened)
    return output.getvalue()


def report_to_html(report: Mapping[str, Any]) -> str:
    """Render a self-contained Chinese, searchable reference ledger."""

    if not isinstance(report, Mapping):
        raise TypeError("report must be a mapping")
    observations = [item for item in report.get("observations", ()) if isinstance(item, Mapping)]
    generated_at = _text(report.get("generated_at")) or "N/A"
    latest_captured_at = _text(report.get("latest_captured_at")) or "N/A"
    counts = report.get("counts") if isinstance(report.get("counts"), Mapping) else {}
    source_snapshots = (
        report.get("source_snapshots")
        if isinstance(report.get("source_snapshots"), Mapping)
        else {}
    )

    source_options = sorted({str(row.get("source") or "") for row in observations})
    model_options = sorted({str(row.get("model_id") or "") for row in observations if row.get("model_id")})
    option_html = ["<option value=\"\">全部来源</option>"]
    option_html.extend(
        f'<option value="{_html(value)}">{_html(value)}</option>' for value in source_options if value
    )
    model_option_html = ["<option value=\"\">全部模型</option>"]
    model_option_html.extend(
        f'<option value="{_html(value)}">{_html(value)}</option>' for value in model_options
    )

    table_rows: list[str] = []
    for row in observations:
        metrics = row.get("metrics") if isinstance(row.get("metrics"), Mapping) else row
        source = str(row.get("source") or "")
        model_id = str(row.get("model_id") or "")
        table_rows.append(
            "<tr data-source=\"{source_attr}\" data-model=\"{model_attr}\">"
            "<td>{row_number}</td><td>{source}</td><td>{observation_type}</td>"
            "<td>{model}</td><td>{effort}</td><td>{benchmark}</td><td>{version}</td>"
            "<td>{category}</td><td>{quality}</td><td>{consistency}</td>"
            "<td>{relative_cost}</td><td>{iq}</td><td>{pass_rate}</td>"
            "<td>{sample_count}</td><td>{cost_usd}</td><td>{latency}</td>"
            "<td>{first_pass}</td><td>{final_acceptance}</td>"
            "<td>{duration_mean}</td><td>{duration_p50}</td><td>{captured_at}</td>"
            "<td>{missing}</td></tr>".format(
                source_attr=_html(source.lower()),
                model_attr=_html(model_id.lower()),
                row_number=_html(row.get("row_number")),
                source=_html(source),
                observation_type=_html(row.get("observation_type")),
                model=_html(model_id),
                effort=_html(row.get("reasoning_effort")),
                benchmark=_html(row.get("benchmark")),
                version=_html(row.get("benchmark_version")),
                category=_html(row.get("category")),
                quality=_html(metrics.get("quality_fraction")),
                consistency=_html(metrics.get("consistency_fraction")),
                relative_cost=_html(metrics.get("publisher_relative_cost")),
                iq=_html(metrics.get("iq")),
                pass_rate=_html(metrics.get("pass_rate")),
                sample_count=_html(metrics.get("sample_count")),
                cost_usd=_html(metrics.get("cost_usd")),
                latency=_html(metrics.get("latency_seconds")),
                first_pass=_html(metrics.get("first_pass_rate")),
                final_acceptance=_html(metrics.get("final_acceptance_rate")),
                duration_mean=_html(metrics.get("duration_mean_seconds")),
                duration_p50=_html(metrics.get("duration_p50_seconds")),
                captured_at=_html(row.get("captured_at")),
                missing=_html(", ".join(str(item) for item in row.get("missing_data", ()))),
            )
        )

    source_rows: list[str] = []
    for name in sorted(source_snapshots):
        item = source_snapshots[name]
        source_rows.append(
            "<tr><th scope=\"row\">{name}</th><td>{snapshot}</td><td>{captured}</td>"
            "<td>{updated}</td><td>{url}</td><td>{missing}</td></tr>".format(
                name=_html(name),
                snapshot=_html(item.get("snapshot_id")),
                captured=_html(item.get("captured_at")),
                updated=_html(item.get("source_updated_at")),
                url=_html(item.get("source_url")),
                missing=_html(", ".join(str(value) for value in item.get("missing_data", ()))),
            )
        )

    template = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>分来源性能证据账本（非统一排行榜）</title>
<style>
:root { color-scheme: light; --canvas: #f5f3ec; --surface: #fff; --ink: #252821; --forest: #355941; --amber: #9a6a16; --line: #d9d7ce; }
* { box-sizing: border-box; }
html { background: var(--canvas); color: var(--ink); }
body { margin: 0; font-family: system-ui, -apple-system, BlinkMacSystemFont, "PingFang SC", "Songti SC", sans-serif; line-height: 1.5; }
a { color: var(--forest); }
main { max-width: 1800px; margin: 0 auto; padding: 2rem clamp(1rem, 3vw, 3.5rem) 4rem; }
h1, h2, h3 { font-family: Georgia, "Songti SC", serif; color: var(--ink); font-weight: 600; }
h1 { margin: 0 0 .35rem; font-size: clamp(1.8rem, 3vw, 3rem); }
h2 { margin: 2.2rem 0 .7rem; font-size: 1.35rem; }
p { max-width: 72rem; }
.lede { color: #4d5148; }
.meta, .controls, .table-wrap { background: var(--surface); border-top: 3px solid var(--forest); padding: 1rem; }
.meta-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr)); gap: .35rem 1.25rem; }
.meta-grid dt { color: #5b6056; font-size: .83rem; }
.meta-grid dd { margin: 0; font-family: SFMono-Regular, Menlo, Consolas, monospace; overflow-wrap: anywhere; }
.controls { display: grid; grid-template-columns: minmax(18rem, 2fr) minmax(10rem, 1fr) minmax(14rem, 1fr) auto; gap: .75rem; align-items: end; }
label { display: grid; gap: .25rem; font-size: .88rem; color: #4d5148; }
input, select, button { min-height: 2.55rem; border: 1px solid #8b9085; background: #fff; color: var(--ink); padding: .45rem .65rem; font: inherit; }
button { border-color: var(--forest); color: var(--forest); cursor: pointer; font-weight: 600; }
input:focus-visible, select:focus-visible, button:focus-visible { outline: 3px solid #d6a83f; outline-offset: 2px; }
.status { min-height: 1.5rem; margin: .65rem 0 0; color: #4d5148; }
.missing { color: var(--amber); }
.table-wrap { overflow-x: auto; border-top-color: #8b9085; }
table { width: 100%; border-collapse: collapse; min-width: 125rem; font-size: .86rem; }
caption { text-align: left; padding: 0 0 .75rem; color: #4d5148; }
th, td { border-bottom: 1px solid var(--line); padding: .48rem .6rem; text-align: left; vertical-align: top; white-space: nowrap; }
thead th { position: sticky; top: 0; background: var(--surface); color: var(--forest); font-weight: 700; }
tbody tr[hidden] { display: none; }
code, .mono { font-family: SFMono-Regular, Menlo, Consolas, monospace; }
@media (max-width: 780px) { main { padding: 1.1rem .7rem 3rem; } .controls { grid-template-columns: 1fr; } }
@media print { html, body { background: #fff; } main { max-width: none; padding: 0; } .controls, button { display: none; } .table-wrap { overflow: visible; border-top: 1px solid #000; } table { min-width: 0; font-size: 7pt; } th, td { white-space: normal; } }
</style>
</head>
<body>
<a href="#ledger" class="skip">跳到证据表</a>
<main>
<header>
<h1>分来源性能证据账本（非统一排行榜）</h1>
<p class="lede">本页按来源分账，不形成统一排行榜；不同 benchmark、推理档位、harness 及成本单位不可直接横向比较；行数不等于独立质量样本数；N/A 代表未知或不适用，不是 0；采集/型号匹配不等于实测路由收益。</p>
</header>
<section class="meta" aria-labelledby="meta-title">
<h2 id="meta-title">报告时间与范围</h2>
<dl class="meta-grid">
<dt>生成时间 generated_at</dt><dd>{generated}</dd>
<dt>最新实际采集 latest captured_at</dt><dd>{captured}</dd>
<dt>观测行数</dt><dd>{total_observations}</dd>
<dt>目录模型数</dt><dd>{catalog_models}</dd>
<dt>仅目录模型数</dt><dd>{catalog_only}</dd>
</dl>
</section>
<section aria-labelledby="sources-title">
<h2 id="sources-title">来源快照</h2>
<div class="table-wrap">
<table><caption>每个来源保留自己的快照 ID、实际采集时间和缺失字段。</caption>
<thead><tr><th scope="col">来源</th><th scope="col">快照 ID</th><th scope="col">实际采集时间</th><th scope="col">来源更新时间</th><th scope="col">来源 URL</th><th scope="col">缺失数据</th></tr></thead>
<tbody>{source_rows}</tbody></table>
</div>
</section>
<section id="ledger" aria-labelledby="ledger-title">
<h2 id="ledger-title">证据观测表</h2>
<div class="controls" role="search" aria-label="筛选证据观测">
<label>搜索文本<input id="search" type="search" placeholder="模型、基准、版本……" autocomplete="off" aria-controls="ledger-table"></label>
<label>来源<select id="source-filter" aria-controls="ledger-table">{source_options}</select></label>
<label>模型<select id="model-filter" aria-controls="ledger-table">{model_options}</select></label>
<button id="reset" type="button">清除筛选</button>
</div>
<p id="filter-status" class="status" aria-live="polite"></p>
<div class="table-wrap">
<table id="ledger-table"><caption>一行一个来源观测；比例为 fraction，Radar 成本为 USD/任务，AI Frontier 成本为来源定义的相对值。</caption>
<thead><tr><th scope="col">行</th><th scope="col">来源</th><th scope="col">类型</th><th scope="col">模型</th><th scope="col">推理努力</th><th scope="col">基准</th><th scope="col">版本</th><th scope="col">类别</th><th scope="col">质量 fraction</th><th scope="col">一致性 fraction</th><th scope="col">相对成本</th><th scope="col">Radar IQ points</th><th scope="col">通过率 fraction</th><th scope="col">样本 observations</th><th scope="col">Radar 成本 USD</th><th scope="col">Radar 延迟 seconds</th><th scope="col">首轮率 fraction</th><th scope="col">最终接受率 fraction</th><th scope="col">时长均值 seconds</th><th scope="col">时长 P50 seconds</th><th scope="col">实际采集时间</th><th scope="col">缺失数据</th></tr></thead>
<tbody>{table_rows}</tbody></table>
</div>
</section>
<footer><p class="lede">键盘操作：Tab 聚焦搜索、下拉框和按钮；原生输入框支持方向键与 Enter。筛选只改变当前表格可见行，不改变证据内容。</p></footer>
</main>
<script>
(function () {
  const search = document.getElementById('search');
  const source = document.getElementById('source-filter');
  const model = document.getElementById('model-filter');
  const reset = document.getElementById('reset');
  const status = document.getElementById('filter-status');
  const rows = Array.from(document.querySelectorAll('#ledger-table tbody tr'));
  function apply() {
    const query = search.value.trim().toLocaleLowerCase();
    const selectedSource = source.value.toLocaleLowerCase();
    const selectedModel = model.value.toLocaleLowerCase();
    let visible = 0;
    rows.forEach(function (row) {
      const matchesQuery = !query || row.textContent.toLocaleLowerCase().includes(query);
      const matchesSource = !selectedSource || row.dataset.source === selectedSource;
      const matchesModel = !selectedModel || row.dataset.model === selectedModel;
      row.hidden = !(matchesQuery && matchesSource && matchesModel);
      if (!row.hidden) visible += 1;
    });
    status.textContent = '当前显示 ' + visible + ' / ' + rows.length + ' 行';
  }
  search.addEventListener('input', apply);
  source.addEventListener('change', apply);
  model.addEventListener('change', apply);
  reset.addEventListener('click', function () { search.value = ''; source.value = ''; model.value = ''; apply(); search.focus(); });
  apply();
}());
</script>
</body>
</html>"""
    # ``str.format`` would treat CSS/JavaScript braces as interpolation
    # fields.  Targeted replacement keeps the template readable and leaves
    # every dynamic value escaped before it enters the document.
    replacements = {
        "{generated}": _html(generated_at),
        "{captured}": _html(latest_captured_at),
        "{total_observations}": _html(counts.get("total_observations")),
        "{catalog_models}": _html(counts.get("catalog_models")),
        "{catalog_only}": _html(counts.get("catalog_only_models")),
        "{source_rows}": "".join(source_rows),
        "{source_options}": "".join(option_html),
        "{model_options}": "".join(model_option_html),
        "{table_rows}": "".join(table_rows),
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template


def _baseline_document(baseline: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = baseline.get("baseline")
    return nested if isinstance(nested, Mapping) else baseline


def _catalog_models(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    values = catalog.get("models")
    if not isinstance(values, list):
        return []
    models: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    agents = catalog.get("agents") if isinstance(catalog.get("agents"), Mapping) else {}
    for raw in values:
        if not isinstance(raw, Mapping):
            continue
        model_id = _text(_first(raw, "model_id", "model", "name"))
        if model_id is None:
            continue
        provider = _canonical_provider(_first(raw, "provider", "model_provider")) or "unknown"
        key = (provider, model_id)
        if key in seen:
            continue
        seen.add(key)
        reasoning = raw.get("reasoning") if isinstance(raw.get("reasoning"), Mapping) else {}
        supported = reasoning.get("supported_efforts")
        supported_efforts = sorted(
            {str(value).strip() for value in supported if _text(value)}
        ) if isinstance(supported, list) else []
        agent_version = _text(raw.get("agent_cli_version"))
        if agent_version is None and isinstance(agents, Mapping):
            agent = agents.get(provider)
            if isinstance(agent, Mapping):
                agent_version = _text(agent.get("cli_version"))
        models.append(
            {
                "provider": provider,
                "model_id": model_id,
                "model_family": _text(raw.get("model_family")),
                "routable": raw.get("routable") if isinstance(raw.get("routable"), bool) else None,
                "supported_efforts": supported_efforts,
                "agent_version": agent_version,
                "catalog_present": True,
            }
        )
    return sorted(models, key=lambda item: (item["provider"], item["model_id"]))


def _baseline_observations(
    baseline: Mapping[str, Any],
    *,
    generated_at: str,
    catalog_index: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    records = baseline.get("records")
    if not isinstance(records, list):
        return []
    snapshot_id = _text(_first(baseline, "snapshot_id", "baseline_id"))
    report: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        model_id = _text(record.get("model_id"))
        provider = _canonical_provider(record.get("provider")) or "unknown"
        catalog = catalog_index.get((provider, model_id or ""), {})
        score = _finite_number(record.get("score"))
        metrics = {
            "score": score,
            "score_fraction": _fraction(score),
            "sample_count": None,
            "effective_sample_strength": _finite_number(record.get("effective_sample_strength")),
            "quality_fraction": _fraction(score) if _score_is_quality(record) else None,
            "consistency_fraction": None,
            "consistency_std_fraction": None,
            "publisher_relative_cost": None,
            "publisher_relative_cost_quoted": None,
            "publisher_relative_cost_surprise": None,
            "iq": None,
            "pass_rate": None,
            "cost_usd": None,
            "latency_seconds": None,
            "first_pass_rate": None,
            "final_acceptance_rate": None,
            "duration_mean_seconds": None,
            "duration_p50_seconds": None,
            "duration_sample_count": None,
            "runtime_attempt_count": None,
            "runtime_quality_sample_count": None,
            "runtime_success_count": None,
            "runtime_failure_count": None,
            "runtime_unresolved_count": None,
        }
        missing = ["score"] if score is None else []
        if _score_is_quality(record) and score is None:
            missing.append("quality_fraction")
        row = _make_observation(
            source="baseline",
            observation_type="benchmark",
            provider=provider,
            model_id=model_id,
            model_family=_text(record.get("model_family")) or _text(catalog.get("model_family")),
            reasoning_effort=_text(record.get("reasoning_effort")),
            benchmark=_text(record.get("benchmark")),
            benchmark_version=_text(record.get("benchmark_version")),
            category=None,
            source_snapshot_id=snapshot_id,
            source_url=_safe_url(record.get("source_url")),
            captured_at=_iso_timestamp(
                _first(record, "captured_at", "observed_at", "fetched_at")
            ),
            generated_at=generated_at,
            catalog_routable=catalog.get("routable"),
            routing_eligible=record.get("routing_prior_eligible")
            if isinstance(record.get("routing_prior_eligible"), bool)
            else None,
            metrics=metrics,
            units={
                "score": "fraction [0, 1]" if score is not None else None,
                "score_fraction": "fraction [0, 1]",
                "effective_sample_strength": "weighted prior strength",
            },
            missing_data=missing,
            data_quality_flags=[],
            extras={
                "record_id": _text(record.get("record_id")),
                "domain": _text(record.get("domain")),
                "provenance": _text(record.get("provenance")),
                "score_kind": _text(record.get("score_kind")),
                "quality_evidence": _text(record.get("quality_evidence")),
            },
        )
        report.append(row)
    return report


def _radar_observations(
    status: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any] | None,
    *,
    generated_at: str,
    catalog_index: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(snapshot, Mapping):
        return []
    values = snapshot.get("models")
    if not isinstance(values, list):
        return []
    snapshot_id = _text(_first(status or {}, "snapshot_id")) or _text(snapshot.get("snapshot_id"))
    source_urls = snapshot.get("source_urls") if isinstance(snapshot.get("source_urls"), Mapping) else {}
    source_url = _primary_url(source_urls, "intelligence_efficiency", "current")
    benchmark_version = _text(snapshot.get("source_updated_at")) or _text(
        (snapshot.get("upstream") or {}).get("version")
        if isinstance(snapshot.get("upstream"), Mapping)
        else None
    )
    captured_at = _iso_timestamp(snapshot.get("fetched_at"))
    report: list[dict[str, Any]] = []
    for raw in values:
        if not isinstance(raw, Mapping):
            continue
        model_id = _text(_first(raw, "model", "model_id"))
        provider = _canonical_provider(raw.get("provider")) or _provider_from_model(model_id)
        effort = _text(_first(raw, "reasoning_effort", "effort"))
        catalog = catalog_index.get((provider or "unknown", model_id or ""), {})
        metrics = {
            "quality_fraction": None,
            "consistency_fraction": None,
            "consistency_std_fraction": None,
            "publisher_relative_cost": None,
            "publisher_relative_cost_quoted": None,
            "publisher_relative_cost_surprise": None,
            "score": None,
            "score_fraction": None,
            "effective_sample_strength": None,
            "iq": _finite_number(raw.get("iq")),
            "pass_rate": _fraction(_finite_number(raw.get("pass_rate"))),
            "sample_count": _nonnegative_int(raw.get("sample_count")),
            "cost_usd": _finite_number(
                _first(raw, "avg_cost_usd", "average_cost_usd", "cost_usd")
            ),
            "latency_seconds": _finite_number(
                _first(raw, "avg_runtime_seconds", "average_runtime_seconds", "latency_seconds")
            ),
            "first_pass_rate": None,
            "final_acceptance_rate": None,
            "duration_mean_seconds": None,
            "duration_p50_seconds": None,
            "duration_sample_count": None,
            "runtime_attempt_count": None,
            "runtime_quality_sample_count": None,
            "runtime_success_count": None,
            "runtime_failure_count": None,
            "runtime_unresolved_count": None,
        }
        missing = [
            field
            for field in ("iq", "pass_rate", "sample_count", "cost_usd", "latency_seconds")
            if metrics[field] is None
        ]
        row = _make_observation(
            source="radar",
            observation_type="radar_model_effort",
            provider=provider,
            model_id=model_id,
            model_family=_text(catalog.get("model_family")),
            reasoning_effort=effort,
            benchmark="Codex Radar community tasks",
            benchmark_version=benchmark_version,
            category=None,
            source_snapshot_id=snapshot_id,
            source_url=source_url,
            captured_at=captured_at,
            generated_at=generated_at,
            catalog_routable=catalog.get("routable"),
            routing_eligible=raw.get("routing_eligible")
            if isinstance(raw.get("routing_eligible"), bool)
            else None,
            metrics=metrics,
            units={
                "iq": "Radar IQ points",
                "pass_rate": "fraction [0, 1]",
                "sample_count": "observations",
                "cost_usd": "USD per task",
                "latency_seconds": "seconds per task",
            },
            missing_data=missing,
            data_quality_flags=[],
            extras={"metric_sources": _safe_mapping(raw.get("metric_sources"))},
        )
        report.append(row)
    return report


def _ai_frontier_observations(
    status: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any] | None,
    *,
    generated_at: str,
    catalog_index: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(snapshot, Mapping):
        return []
    models = snapshot.get("models")
    if not isinstance(models, list):
        return []
    snapshot_id = _text(_first(status or {}, "snapshot_id")) or _text(snapshot.get("snapshot_id"))
    captured_at = _iso_timestamp(snapshot.get("fetched_at"))
    source_urls = snapshot.get("source_urls") if isinstance(snapshot.get("source_urls"), Mapping) else {}
    version = _text(snapshot.get("source_updated_at")) or _text(snapshot.get("fetched_at"))
    model_by_source: dict[str, Mapping[str, Any]] = {}
    for raw in models:
        if isinstance(raw, Mapping):
            source_id = _text(raw.get("source_id"))
            if source_id:
                model_by_source[source_id] = raw
    report: list[dict[str, Any]] = []
    for raw in models:
        if not isinstance(raw, Mapping):
            continue
        source_model_id = _text(raw.get("source_id"))
        model_id = _text(raw.get("model_id")) or _model_from_source_id(source_model_id)
        provider = _canonical_provider(raw.get("provider")) or _provider_from_model(model_id)
        catalog = catalog_index.get((provider or "unknown", model_id or ""), {})
        metrics = _frontier_metrics(raw)
        missing = [
            field
            for field in (
                "quality_fraction",
                "consistency_fraction",
                "consistency_std_fraction",
                "publisher_relative_cost",
            )
            if metrics[field] is None
        ]
        report.append(
            _make_observation(
                source="ai_frontier",
                observation_type="ai_frontier_model",
                provider=provider,
                model_id=model_id,
                model_family=_text(catalog.get("model_family")),
                reasoning_effort=_text(_first(raw, "reasoning_effort", "effort")),
                benchmark="AI Frontier reliability leaderboard",
                benchmark_version=version,
                category="overall",
                source_snapshot_id=snapshot_id,
                source_url=_primary_url(
                    source_urls,
                    "reliability",
                    "reliability_leaderboard",
                    "leaderboard",
                    "homepage",
                ),
                captured_at=captured_at,
                generated_at=generated_at,
                catalog_routable=catalog.get("routable"),
                routing_eligible=False,
                metrics=metrics,
                units={
                    "quality_fraction": "fraction [0, 1]",
                    "consistency_fraction": "fraction [0, 1]",
                    "consistency_std_fraction": "fraction [0, 1]",
                    "publisher_relative_cost": "publisher-relative; source unit not published",
                },
                missing_data=missing,
                data_quality_flags=[],
                extras={"source_model_id": source_model_id},
            )
        )

    categories = snapshot.get("categories")
    if not isinstance(categories, list):
        categories = []
    for raw in categories:
        if not isinstance(raw, Mapping):
            continue
        source_model_id = _text(raw.get("source_id"))
        model = model_by_source.get(source_model_id or "")
        model_id = _text(raw.get("model_id"))
        if model_id is None and model is not None:
            model_id = _text(model.get("model_id"))
        model_id = model_id or _model_from_source_id(source_model_id)
        provider = _canonical_provider(raw.get("provider"))
        if provider is None and model is not None:
            provider = _canonical_provider(model.get("provider"))
        provider = provider or _provider_from_model(model_id)
        catalog = catalog_index.get((provider or "unknown", model_id or ""), {})
        category = _text(_first(raw, "category_key", "category", "benchmark"))
        metrics = _frontier_metrics(raw)
        missing = [
            field
            for field in (
                "quality_fraction",
                "consistency_fraction",
                "consistency_std_fraction",
                "publisher_relative_cost",
            )
            if metrics[field] is None
        ]
        report.append(
            _make_observation(
                source="ai_frontier",
                observation_type="ai_frontier_category",
                provider=provider,
                model_id=model_id,
                model_family=_text(catalog.get("model_family")),
                reasoning_effort=None,
                benchmark="AI Frontier category observation",
                benchmark_version=version,
                category=category,
                source_snapshot_id=snapshot_id,
                source_url=_ai_category_url(source_urls, category),
                captured_at=captured_at,
                generated_at=generated_at,
                catalog_routable=catalog.get("routable"),
                routing_eligible=False,
                metrics=metrics,
                units={
                    "quality_fraction": "fraction [0, 1]",
                    "consistency_fraction": "fraction [0, 1]",
                    "consistency_std_fraction": "fraction [0, 1]",
                    "publisher_relative_cost": "publisher-relative; source unit not published",
                },
                missing_data=missing,
                data_quality_flags=[],
                extras={"source_model_id": source_model_id},
            )
        )
    return report


def _frontier_metrics(raw: Mapping[str, Any]) -> dict[str, Any]:
    relative_cost = _finite_number(
        _first(raw, "real_cost", "cost", "observed_cost", "publisher_relative_cost")
    )
    quoted = _finite_number(_first(raw, "quoted_cost", "publisher_relative_cost_quoted"))
    surprise = _finite_number(
        _first(raw, "cost_surprise", "publisher_relative_cost_surprise")
    )
    quality = _fraction(
        _finite_number(_first(raw, "quality", "Quality", "accuracy", "score"))
    )
    consistency = _fraction(
        _finite_number(_first(raw, "consistency", "Consistency"))
    )
    consistency_std = _fraction(
        _finite_number(_first(raw, "consistency_std", "Consistency Std", "consistency_stddev"))
    )
    return {
        # Source-native aliases remain available for consumers; the explicit
        # ``*_fraction`` fields are the canonical unit-labelled columns.
        "quality": quality,
        "consistency": consistency,
        "consistency_std": consistency_std,
        "quality_fraction": quality,
        "consistency_fraction": consistency,
        "consistency_std_fraction": consistency_std,
        "publisher_relative_cost": relative_cost,
        "publisher_relative_cost_quoted": quoted,
        "publisher_relative_cost_surprise": surprise,
        "score": None,
        "score_fraction": None,
        "score_kind": None,
        "effective_sample_strength": None,
        "iq": None,
        "pass_rate": None,
        "sample_count": None,
        "cost_usd": None,
        "latency_seconds": None,
        "first_pass_rate": None,
        "final_acceptance_rate": None,
        "duration_mean_seconds": None,
        "duration_p50_seconds": None,
        "duration_sample_count": None,
        "runtime_attempt_count": None,
        "runtime_quality_sample_count": None,
        "runtime_success_count": None,
        "runtime_failure_count": None,
        "runtime_unresolved_count": None,
    }


def _runtime_observations(
    snapshot: Mapping[str, Any] | None,
    *,
    generated_at: str,
    catalog_index: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(snapshot, Mapping):
        return []
    values = snapshot.get("metrics")
    if not isinstance(values, list):
        return []
    snapshot_id = _text(snapshot.get("snapshot_id"))
    captured_at = _runtime_captured_at(snapshot)
    report: list[dict[str, Any]] = []
    for raw in values:
        if not isinstance(raw, Mapping):
            continue
        key = raw.get("key") if isinstance(raw.get("key"), Mapping) else {}
        runtime = raw.get("runtime") if isinstance(raw.get("runtime"), Mapping) else {}
        first_pass = runtime.get("first_pass") if isinstance(runtime.get("first_pass"), Mapping) else {}
        final_acceptance = runtime.get("final_acceptance") if isinstance(runtime.get("final_acceptance"), Mapping) else {}
        duration = runtime.get("duration_seconds") if isinstance(runtime.get("duration_seconds"), Mapping) else {}
        quality = runtime.get("quality_calibration") if isinstance(runtime.get("quality_calibration"), Mapping) else {}
        provider = _canonical_provider(key.get("provider")) or "unknown"
        model_id = _text(key.get("model_id"))
        catalog = catalog_index.get((provider, model_id or ""), {})
        agent_version = _text(key.get("agent_version"))
        quality_sample_count = _nonnegative_int(quality.get("sample_count"))
        attempt_count = _nonnegative_int(runtime.get("attempt_count"))
        flags: list[str] = []
        if agent_version in {None, "unattested", "unknown"}:
            flags.append("agent_version_unattested")
        if quality_sample_count == 0 and (attempt_count or 0) > 0:
            flags.append("quality_denominator_zero")
        same_model_other_provider = any(
            item_key[1] == model_id and item_key[0] != provider
            for item_key in catalog_index
        )
        if same_model_other_provider:
            flags.append("provider_model_mismatch")
        quality_status = "observed" if (quality_sample_count or 0) > 0 and not flags else "unavailable"
        metrics = {
            "quality_fraction": None,
            "consistency_fraction": None,
            "consistency_std_fraction": None,
            "publisher_relative_cost": None,
            "publisher_relative_cost_quoted": None,
            "publisher_relative_cost_surprise": None,
            "score": None,
            "score_fraction": None,
            "effective_sample_strength": None,
            "iq": None,
            "pass_rate": None,
            "sample_count": quality_sample_count,
            "cost_usd": None,
            "latency_seconds": None,
            "first_pass_rate": _fraction(_finite_number(first_pass.get("rate"))),
            "final_acceptance_rate": _fraction(_finite_number(final_acceptance.get("rate"))),
            "duration_mean_seconds": _finite_number(duration.get("mean")),
            "duration_p50_seconds": _finite_number(duration.get("p50")),
            "duration_sample_count": _nonnegative_int(duration.get("sample_count")),
            "runtime_attempt_count": attempt_count,
            "runtime_quality_sample_count": quality_sample_count,
            "runtime_success_count": _nonnegative_int(quality.get("successes")),
            "runtime_failure_count": _nonnegative_int(quality.get("failures")),
            "runtime_unresolved_count": _nonnegative_int(quality.get("unresolved")),
        }
        missing = [
            field
            for field in (
                "first_pass_rate",
                "final_acceptance_rate",
                "duration_mean_seconds",
                "duration_p50_seconds",
            )
            if metrics[field] is None
        ]
        if quality_status == "unavailable":
            missing.append("quality_acceptance")
        report.append(
            _make_observation(
                source="local_runtime",
                observation_type="runtime_bucket",
                provider=provider,
                model_id=model_id,
                model_family=_text(catalog.get("model_family")),
                reasoning_effort=_text(key.get("reasoning_effort")),
                benchmark="Workbench runtime ledger",
                benchmark_version=None,
                category=None,
                source_snapshot_id=snapshot_id,
                source_url=None,
                captured_at=captured_at,
                generated_at=generated_at,
                catalog_routable=catalog.get("routable"),
                routing_eligible=None,
                metrics=metrics,
                units={
                    "sample_count": "quality calibration observations",
                    "first_pass_rate": "fraction [0, 1]",
                    "final_acceptance_rate": "fraction [0, 1]",
                    "duration_mean_seconds": "seconds",
                    "duration_p50_seconds": "seconds",
                    "duration_sample_count": "duration observations",
                    "runtime_attempt_count": "attempts",
                },
                missing_data=missing,
                data_quality_flags=flags,
                extras={
                    "agent_name": _text(key.get("agent_name")),
                    "agent_version": agent_version,
                    "task_type": _text(key.get("task_type")),
                    "complexity": _text(key.get("complexity")),
                    "quality_status": quality_status,
                    "quality_note": (
                        "metadata-unattested; acceptance denominator is zero"
                        if "agent_version_unattested" in flags
                        else None
                    ),
                },
            )
        )
    return report


def _make_observation(
    *,
    source: str,
    observation_type: str,
    provider: str | None,
    model_id: str | None,
    model_family: str | None,
    reasoning_effort: str | None,
    benchmark: str | None,
    benchmark_version: str | None,
    category: str | None,
    source_snapshot_id: str | None,
    source_url: str | None,
    captured_at: str | None,
    generated_at: str,
    catalog_routable: bool | None,
    routing_eligible: bool | None,
    metrics: Mapping[str, Any],
    units: Mapping[str, Any],
    missing_data: Iterable[str],
    data_quality_flags: Iterable[str],
    extras: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    clean_metrics = {str(key): _json_value(value) for key, value in metrics.items()}
    row: dict[str, Any] = {
        "source": source,
        "observation_type": observation_type,
        "provider": provider or "unknown",
        "model_id": model_id,
        "model_family": model_family,
        "reasoning_effort": reasoning_effort,
        "benchmark": benchmark,
        "benchmark_version": benchmark_version,
        "category": category,
        "source_snapshot_id": source_snapshot_id,
        "source_url": source_url,
        "captured_at": captured_at,
        "generated_at": generated_at,
        "catalog_routable": catalog_routable,
        "routing_eligible": routing_eligible,
        "metrics": clean_metrics,
        "units": {str(key): _json_value(value) for key, value in units.items()},
        "missing_data": sorted({str(value) for value in missing_data if str(value)}),
        "data_quality_flags": sorted({str(value) for value in data_quality_flags if str(value)}),
    }
    # Flat aliases make the ledger easy to consume from DSH and keep CSV/HTML
    # adapters simple while ``metrics`` remains the canonical grouped view.
    row.update(clean_metrics)
    if extras:
        for key, value in extras.items():
            if key in row or key == "metrics":
                continue
            row[str(key)] = _json_value(value)
    return row


def _catalog_only_observation(item: Mapping[str, Any], *, generated_at: str) -> dict[str, Any]:
    metrics = {
        "quality_fraction": None,
        "consistency_fraction": None,
        "consistency_std_fraction": None,
        "publisher_relative_cost": None,
        "publisher_relative_cost_quoted": None,
        "publisher_relative_cost_surprise": None,
        "score": None,
        "score_fraction": None,
        "effective_sample_strength": None,
        "iq": None,
        "pass_rate": None,
        "sample_count": None,
        "cost_usd": None,
        "latency_seconds": None,
        "first_pass_rate": None,
        "final_acceptance_rate": None,
        "duration_mean_seconds": None,
        "duration_p50_seconds": None,
        "duration_sample_count": None,
        "runtime_attempt_count": None,
        "runtime_quality_sample_count": None,
        "runtime_success_count": None,
        "runtime_failure_count": None,
        "runtime_unresolved_count": None,
    }
    return _make_observation(
        source="catalog",
        observation_type="catalog_only",
        provider=_text(item.get("provider")) or "unknown",
        model_id=_text(item.get("model_id")),
        model_family=_text(item.get("model_family")),
        reasoning_effort=None,
        benchmark=None,
        benchmark_version=None,
        category=None,
        source_snapshot_id=None,
        source_url=None,
        captured_at=None,
        generated_at=generated_at,
        catalog_routable=item.get("routable") if isinstance(item.get("routable"), bool) else None,
        routing_eligible=None,
        metrics=metrics,
        units={},
        missing_data=["no_performance_observation"],
        data_quality_flags=[],
        extras={"catalog_present": True},
    )


def _baseline_source(baseline: Mapping[str, Any], rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    captured = _iso_timestamp(
        _first(baseline, "captured_at", "observed_at", "fetched_at", "updated_at")
    )
    urls = [_safe_url(row.get("source_url")) for row in rows if row.get("source_url")]
    urls = [value for value in urls if value]
    missing: list[str] = []
    if not rows:
        missing.append("benchmark_records")
    if captured is None:
        missing.append("captured_at")
    return {
        "source": "baseline",
        "snapshot_id": _text(_first(baseline, "snapshot_id", "baseline_id")),
        "digest": None,
        "captured_at": captured,
        "source_updated_at": None,
        "source_url": urls[0] if urls else None,
        "source_urls": sorted(set(urls)),
        "missing_data": missing,
    }


def _radar_source(
    status: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any] | None,
    rows: list[Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        return {
            "source": "radar",
            "snapshot_id": _text(_first(status or {}, "snapshot_id")),
            "digest": None,
            "captured_at": _iso_timestamp(_first(status or {}, "fetched_at")),
            "source_updated_at": _iso_timestamp(_first(status or {}, "source_updated_at")),
            "source_url": None,
            "source_urls": [],
            "missing_data": ["snapshot"],
        }
    urls = _safe_urls(snapshot.get("source_urls"))
    captured = _iso_timestamp(snapshot.get("fetched_at"))
    missing = []
    if not rows:
        missing.append("models")
    if captured is None:
        missing.append("captured_at")
    if not urls:
        missing.append("source_url")
    return {
        "source": "radar",
        "snapshot_id": _text(_first(status or {}, "snapshot_id")) or _text(snapshot.get("snapshot_id")),
        "digest": _text(snapshot.get("digest")),
        "captured_at": captured,
        "source_updated_at": _iso_timestamp(snapshot.get("source_updated_at")),
        "source_url": _primary_url(snapshot.get("source_urls"), "intelligence_efficiency", "current"),
        "source_urls": urls,
        "state": _text((status or {}).get("state")),
        "missing_data": missing,
    }


def _ai_frontier_source(
    status: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any] | None,
    rows: list[Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        return {
            "source": "ai_frontier",
            "snapshot_id": _text(_first(status or {}, "snapshot_id")),
            "digest": None,
            "captured_at": _iso_timestamp(_first(status or {}, "fetched_at")),
            "source_updated_at": None,
            "source_url": None,
            "source_urls": [],
            "missing_data": ["snapshot"],
        }
    urls = _safe_urls(snapshot.get("source_urls"))
    captured = _iso_timestamp(snapshot.get("fetched_at"))
    missing = []
    if not rows:
        missing.append("models_or_categories")
    if captured is None:
        missing.append("captured_at")
    if not urls:
        missing.append("source_url")
    return {
        "source": "ai_frontier",
        "snapshot_id": _text(_first(status or {}, "snapshot_id")) or _text(snapshot.get("snapshot_id")),
        "digest": _text(snapshot.get("digest")),
        "captured_at": captured,
        "source_updated_at": _iso_timestamp(snapshot.get("source_updated_at")),
        "source_url": _primary_url(
            snapshot.get("source_urls"),
            "reliability",
            "reliability_leaderboard",
            "leaderboard",
            "homepage",
        ),
        "source_urls": urls,
        "state": _text((status or {}).get("state")),
        "missing_data": missing,
    }


def _runtime_source(snapshot: Mapping[str, Any] | None, rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        return {
            "source": "local_runtime",
            "snapshot_id": None,
            "digest": None,
            "captured_at": None,
            "source_updated_at": None,
            "source_url": None,
            "source_urls": [],
            "missing_data": ["snapshot"],
        }
    captured = _runtime_captured_at(snapshot)
    missing = []
    if not rows:
        missing.append("metrics")
    if captured is None:
        missing.append("captured_at")
    return {
        "source": "local_runtime",
        "snapshot_id": _text(snapshot.get("snapshot_id")),
        "digest": _text(snapshot.get("digest")),
        "captured_at": captured,
        "source_updated_at": None,
        "source_url": None,
        "source_urls": [],
        "missing_data": missing,
    }


def _status_snapshot(status: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(status, Mapping):
        return None
    for key in ("snapshot", "active"):
        value = status.get(key)
        if isinstance(value, Mapping):
            return value
    return status if isinstance(status.get("models"), list) else None


def _runtime_captured_at(snapshot: Mapping[str, Any]) -> str | None:
    # Do not use scan_progress.claude_observed_at: that timestamp belongs to
    # account quota metadata, which is intentionally not a performance metric.
    return _iso_timestamp(_first(snapshot, "captured_at", "observed_at", "created_at", "updated_at"))


def _score_is_quality(record: Mapping[str, Any]) -> bool:
    score_kind = (_text(record.get("score_kind")) or "").lower()
    return score_kind in {"accuracy", "resolved_rate", "success_rate", "pass_rate"}


def _observation_sort_key(row: Mapping[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        str(row.get("source") or ""),
        str(row.get("provider") or ""),
        str(row.get("model_id") or ""),
        str(row.get("reasoning_effort") or ""),
        str(row.get("benchmark") or ""),
        str(row.get("category") or ""),
    )


def _observation_id(row: Mapping[str, Any], index: int) -> str:
    identity = "-".join(
        _slug(value)
        for value in (
            row.get("source"),
            row.get("model_id"),
            row.get("reasoning_effort"),
            row.get("benchmark"),
            row.get("category"),
        )
        if _text(value)
    )
    return f"observation-{index:04d}-{identity or 'unidentified'}"


def _csv_field_value(
    field: str,
    row: Mapping[str, Any],
    metrics: Mapping[str, Any],
    units: Mapping[str, Any],
    generated_at: str | None,
) -> Any:
    if field == "generated_at":
        return row.get("generated_at") or generated_at
    if field in {"missing_data", "data_quality_flags"}:
        return json.dumps(row.get(field, []), ensure_ascii=False, separators=(",", ":"))
    if field in {"publisher_relative_cost_unit", "score_unit", "iq_unit", "pass_rate_unit", "sample_count_unit", "cost_usd_unit", "latency_unit", "runtime_rate_unit", "duration_unit"}:
        mapping = {
            "publisher_relative_cost_unit": "publisher_relative_cost",
            "score_unit": "score",
            "iq_unit": "iq",
            "pass_rate_unit": "pass_rate",
            "sample_count_unit": "sample_count",
            "cost_usd_unit": "cost_usd",
            "latency_unit": "latency_seconds",
            "runtime_rate_unit": "first_pass_rate",
            "duration_unit": "duration_mean_seconds",
        }
        defaults = {
            "publisher_relative_cost": "publisher-relative; source unit not published",
            "score": "fraction [0, 1]",
            "iq": "Radar IQ points",
            "pass_rate": "fraction [0, 1]",
            "sample_count": "observations",
            "cost_usd": "USD per task",
            "latency_seconds": "seconds per task",
            "first_pass_rate": "fraction [0, 1]",
            "duration_mean_seconds": "seconds",
        }
        return units.get(mapping[field], defaults[mapping[field]])
    if field in metrics:
        return metrics.get(field)
    return row.get(field)


def _csv_value(value: Any) -> Any:
    if value is None:
        return "N/A"
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _safe_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): _json_value(item)
        for key, item in value.items()
        if _text(key) is not None and isinstance(item, (str, int, float, bool, type(None)))
    }


def _safe_urls(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    return sorted({url for url in (_safe_url(item) for item in value.values()) if url})


def _primary_url(value: Any, *keys: str) -> str | None:
    if not isinstance(value, Mapping):
        return None
    for key in keys:
        url = _safe_url(value.get(key))
        if url:
            return url
    return None


def _ai_category_url(source_urls: Any, category: str | None) -> str | None:
    if category == "cost-comparison":
        return _primary_url(source_urls, "cost_comparison", "cost-comparison", "cost") or _primary_url(
            source_urls,
            "reliability",
            "reliability_leaderboard",
            "homepage",
        )
    return _primary_url(source_urls, "reliability", "reliability_leaderboard", "leaderboard", "homepage")


def _model_from_source_id(source_id: str | None) -> str | None:
    if not source_id:
        return None
    return source_id.split("/", 1)[1] if "/" in source_id else source_id


def _provider_from_model(model_id: str | None) -> str | None:
    if not model_id:
        return None
    lowered = model_id.lower()
    if lowered.startswith("claude-") or lowered in {"sonnet", "opus", "fable"}:
        return "claude"
    if lowered.startswith("gpt-") or "codex" in lowered:
        return "codex"
    return "unknown"


def _canonical_provider(value: Any) -> str | None:
    text = _text(value)
    return _PROVIDER_ALIASES.get(text.lower()) if text else None


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _finite_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result):
        return None
    if result.is_integer() and isinstance(value, int):
        return int(result)
    return result


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _fraction(value: Any) -> float | int | None:
    number = _finite_number(value)
    if number is None:
        return None
    result = float(number)
    if result > 1 and result <= 100:
        result /= 100
    return result if 0 <= result <= 1 else None


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return _text(value)


def _iso_timestamp(value: Any) -> str | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _latest_timestamp(values: Iterable[Any]) -> str | None:
    parsed: list[tuple[datetime, str]] = []
    for value in values:
        iso = _iso_timestamp(value)
        if iso is None:
            continue
        parsed.append((datetime.fromisoformat(iso.replace("Z", "+00:00")), iso))
    return max(parsed, key=lambda item: item[0])[1] if parsed else None


def _safe_url(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        hostname = parsed.hostname
        netloc = hostname
        if parsed.port is not None:
            netloc = f"{hostname}:{parsed.port}"
    except ValueError:
        return None
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _slug(value: Any) -> str:
    text = _text(value) or "item"
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", text).strip("-")[:80] or "item"


def _html(value: Any) -> str:
    if value is None or value == "":
        text = "N/A"
    elif isinstance(value, (Mapping, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    else:
        text = str(value)
    return escape(text, quote=True)
