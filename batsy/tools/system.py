"""Live system monitoring: CPU, memory, temperatures, battery, disks, network, processes."""
import platform
import time

import psutil


def _gb(n: float) -> str:
    return f"{n / 1e9:.1f} GB"


def _top_processes(by: str, limit: int) -> list[str]:
    procs = list(psutil.process_iter(["pid", "name", "username", "memory_info"]))
    for p in procs:
        try:
            p.cpu_percent(None)
        except psutil.Error:
            pass
    time.sleep(0.5)
    rows = []
    for p in procs:
        try:
            rows.append((p.cpu_percent(None), p.info["memory_info"].rss if p.info["memory_info"] else 0, p))
        except psutil.Error:
            continue
    rows.sort(key=lambda r: r[0] if by == "cpu" else r[1], reverse=True)
    return [f"pid {p.pid:>7}  cpu {cpu:5.1f}%  mem {rss / 1e6:7.0f} MB  {p.info['name']}" for cpu, rss, p in rows[:limit]]


def snapshot(sections: list[str], top: int = 8) -> str:
    out = []
    if "overview" in sections:
        boot = time.time() - psutil.boot_time()
        out.append(f"host {platform.node()}, kernel {platform.release()}, up {boot / 3600:.1f} h")
        out.append(f"load avg {', '.join(f'{x:.2f}' for x in psutil.getloadavg())} on {psutil.cpu_count()} threads")
    if "cpu" in sections:
        per = psutil.cpu_percent(interval=0.5, percpu=True)
        freq = psutil.cpu_freq()
        out.append(f"CPU {sum(per) / len(per):.0f}% (per core: {', '.join(f'{x:.0f}' for x in per)})"
                   + (f", {freq.current:.0f} MHz" if freq else ""))
    if "memory" in sections:
        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        out.append(f"RAM {_gb(vm.used)} / {_gb(vm.total)} ({vm.percent}%), swap {_gb(sw.used)} / {_gb(sw.total)}")
    if "temps" in sections and hasattr(psutil, "sensors_temperatures"):
        for chip, entries in (psutil.sensors_temperatures() or {}).items():
            vals = ", ".join(f"{e.label or 'temp'} {e.current:.0f}C" for e in entries[:6])
            out.append(f"temp {chip}: {vals}")
        fans = psutil.sensors_fans() if hasattr(psutil, "sensors_fans") else {}
        for chip, entries in (fans or {}).items():
            out.append(f"fan {chip}: " + ", ".join(f"{e.current} rpm" for e in entries))
    if "battery" in sections:
        b = psutil.sensors_battery()
        if b:
            left = "" if b.power_plugged or b.secsleft < 0 else f", ~{b.secsleft / 3600:.1f} h left"
            out.append(f"battery {b.percent:.0f}% {'charging/plugged in' if b.power_plugged else 'on battery'}{left}")
        else:
            out.append("battery: none detected")
    if "disks" in sections:
        for part in psutil.disk_partitions():
            if part.fstype in ("squashfs", "tmpfs", "overlay") or part.mountpoint.startswith("/var/lib/snapd"):
                continue
            try:
                u = psutil.disk_usage(part.mountpoint)
            except OSError:
                continue
            out.append(f"disk {part.mountpoint} ({part.fstype}) {_gb(u.used)} / {_gb(u.total)}, {_gb(u.free)} free")
    if "network" in sections:
        a = psutil.net_io_counters()
        time.sleep(0.5)
        b = psutil.net_io_counters()
        out.append(f"net down {(b.bytes_recv - a.bytes_recv) * 2 / 1e3:.0f} KB/s, up {(b.bytes_sent - a.bytes_sent) * 2 / 1e3:.0f} KB/s")
        for name, addrs in psutil.net_if_addrs().items():
            ips = [x.address for x in addrs if x.family.name == "AF_INET" and not x.address.startswith("127.")]
            if ips:
                out.append(f"  {name}: {', '.join(ips)}")
    if "processes" in sections:
        out.append("top by CPU:")
        out.extend("  " + r for r in _top_processes("cpu", top))
        out.append("top by memory:")
        out.extend("  " + r for r in _top_processes("mem", top))
    return "\n".join(out)


ALL = ["overview", "cpu", "memory", "temps", "battery", "disks", "network", "processes"]


def register(reg):
    @reg.tool(
        "system_status",
        "Live readings of the laptop: overview, cpu, memory, temps (and fans), battery, disks, network, processes.",
        {
            "sections": {"type": "array", "items": {"type": "string", "enum": ALL},
                         "description": "which sections; default all"},
            "top": {"type": "integer", "description": "how many top processes to list (default 8)"},
        },
    )
    def system_status(ctx, sections=None, top=8):
        return snapshot(sections or ALL, top)
