"""Morning-report renderer: [[placeholder]] keys only — copy is Fable's."""

from night_watch.report import MorningReport


def render_morning_report(report: MorningReport) -> str:
    lines = ["[[morning.header]]", "[[morning.window]]"]
    for node in sorted(report.per_node):
        lines.append("[[morning.node.summary]]")
        if any(v.dimension in ("max_wakes", "idle_burn")
               for v in report.violations) and report.per_node[node].idle_burns:
            lines.append("[[morning.node.violations]]")
        if report.per_node[node].paused:
            lines.append("[[morning.node.paused]]")
    for _loop in report.loops:
        lines.append("[[morning.loop.finding]]")
    if report.healthy_silence_nodes:
        lines.append("[[morning.healthy.silence]]")
    lines.append("[[morning.footer]]")
    return "\n".join(lines)
